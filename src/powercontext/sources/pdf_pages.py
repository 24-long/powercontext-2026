# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Financial report PDF full-text parsing and evidence-page retrieval.

Each physical page of a PDF becomes one independently addressable CAPTURED
Source. Page identity is derived from ``(document_id, document_sha256,
page_number)`` so a revised document yields new identities while earlier
references keep pointing at their original revision.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import unicodedata
from collections import Counter
from typing import TYPE_CHECKING

from pydantic import BaseModel
from pypdf import PasswordType, PdfReader

from powercontext.artifacts import MemoryCitation
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory import MemoryHit
    from powercontext.builtin.runtime.application import ScopedMemoryApplication

PDF_PAGE_SOURCE_NAME = "pdf-page"

_TOKEN_PATTERN = re.compile(r"[a-z]+|\d+")

# Bound work per query without truncating ordinary report collections.
_MAX_CANDIDATES_PER_QUERY = 4096
_PREFIX_LENGTH = 5
_PREFIX_WEIGHT = 0.7

# Heading evidence: reports repeat the running "Table of Contents" header and
# "Item N." section markers, so a page's own section title is recoverable from
# its text. Only terms rare across the candidate pool earn heading credit;
# company names and years sit in every heading and would drown the signal.
_HEADING_SEARCH_TOKENS = 24
_HEADING_SPAN_TOKENS = 12
_ITEM_HEADING_SPAN_TOKENS = 7
_RARE_TERM_RATIO = 0.2
_HEADING_WEIGHT = 2.0

# Metric questions ("gross margin", "largest liability", "which geographies")
# are answered by the corresponding statements or segment sections even when
# the question shares no literal word with that page. The mapping below is
# standard report structure knowledge, not per-question data.
_METRIC_WEIGHT = 8.0
_METRIC_STATEMENTS = {
    "margin": ("income",),
    "margins": ("income",),
    "ebitda": ("income", "cash"),
    "revenue": ("income",),
    "revenues": ("income",),
    "profit": ("income",),
    "tax": ("income",),
    "gross": ("income",),
    "liability": ("balance",),
    "liabilities": ("balance",),
    "asset": ("balance",),
    "assets": ("balance",),
    "debt": ("balance",),
    "liquidity": ("balance", "cash"),
    "capex": ("cash",),
    "depreciation": ("cash",),
    "geographies": ("geographic",),
    "geography": ("geographic",),
    "regions": ("geographic",),
    "region": ("geographic",),
    "segment": ("geographic",),
    "segments": ("geographic",),
}
_STATEMENT_KEYS = {
    "income": ("income", "operations", "earnings"),
    "balance": ("balance",),
    "cash": ("cash", "flows", "flow"),
    "geographic": ("geographic", "geographies", "segment", "segments", "operations"),
}

# Function words excluded from retrieval scoring so the evidence terms in a
# FinanceBench question dominate the page ranking.
_STOPWORDS = frozenset(
    (  # noqa: SIM905
        "a an and are as at be been being by can could did do does for from had has have having he her his "
        "how i if in is it its just may might must no nor not of on only or our please shall she should so "
        "such than that the their them then there these they this those to was we were what when where which "
        "who why will with would you your"
    ).split()
)


class PDFDocumentInput(BaseModel):
    """A complete original PDF submitted for full physical-page parsing."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical page captured in document order, numbered from 1."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


def _validate_document_id(document_id: str) -> None:
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must not be empty or whitespace only")  # noqa: TRY003


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Return the stable, bounded Source identity for one physical page."""

    digest = hashlib.sha256(f"{document_id}\x00{document_sha256}\x00{page_number}".encode()).hexdigest()
    identity = f"{PDF_PAGE_SOURCE_NAME}:{digest}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError("PDF page source identity exceeds the source ID length limit")  # noqa: TRY003
    return identity


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of a complete PDF, failing atomically.

    A PDF with an empty user-password permission marker is readable without
    credentials. Empty files, non-PDF content, and any document that cannot be
    fully parsed raise ``ValueError`` without returning partial pages.
    """
    _validate_document_id(value.document_id)
    content = value.content
    document_sha256 = hashlib.sha256(content).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted and reader.decrypt("") == PasswordType.NOT_DECRYPTED:
            raise ValueError("PDF requires a password that was not provided")  # noqa: TRY003, TRY301
        pages: list[PDFPageInput] = []
        for index, page in enumerate(reader.pages, start=1):
            # Extraction can skip the content stream when resources are absent;
            # resolve it first so a broken later page cannot become a blank page.
            page.get_contents()
            text = unicodedata.normalize("NFC", page.extract_text() or "")
            pages.append(
                PDFPageInput(
                    document_id=value.document_id,
                    document_sha256=document_sha256,
                    page_number=index,
                    text=text,
                )
            )
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("PDF could not be fully parsed") from error  # noqa: TRY003
    return tuple(pages)


class PDFPageSource(Source):
    """CAPTURED Source carrying one physical page's extracted text."""

    materialization: SourceMaterialization = SourceMaterialization.CAPTURED
    document_id: str
    document_sha256: str
    page_number: int
    text: str


class _PDFPageAdapter:
    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        identity = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=identity,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class _PDFPageTextEvidence:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    _PDFPageAdapter(),
    version="1",
    projections=(_PDFPageTextEvidence(),),
)


def _question_terms(question: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFC", question).casefold()
    return tuple(term for term in _TOKEN_PATTERN.findall(normalized) if term not in _STOPWORDS)


def _expansion_queries(terms: tuple[str, ...]) -> tuple[str, ...]:
    """Cover every term using two-term FTS queries with a one-match floor.

    Longer queries require at least two matches. Short financial terms such
    as EPS and tax are evidence too; filtering by word length loses pages.
    """
    unique = tuple(dict.fromkeys(terms))
    return tuple(" ".join(unique[index : index + 2]) for index in range(0, len(unique), 2))


def _document_score(text: str, question: str) -> float:
    """Prefer the named report over incidental company mentions in peer reports."""
    document_id = text.partition("\n")[0].casefold()
    components = re.split(r"[_\s-]+", document_id)
    names = [part for part in components if any(character.isalpha() for character in part)]
    names = [part for part in names if re.fullmatch(r"\d+[kq]", part) is None]
    query = "".join(_TOKEN_PATTERN.findall(question.casefold()))
    name_length = sum(len(part) for part in names)
    matched = sum(len(part) for part in names if part in query)
    years = re.findall(r"(?:19|20)\d{2}", document_id)
    return 4.0 * matched / max(name_length, 1) + float(any(year in query for year in years))


def _heading_tokens(tokens: list[str]) -> tuple[str, ...]:
    """Return the page-top heading: the span after the running ToC marker."""

    head = tokens[:_HEADING_SEARCH_TOKENS]
    for index in range(min(8, len(head) - 2)):
        if head[index : index + 3] == ["table", "of", "contents"]:
            return tuple(head[index + 3 : index + 3 + _HEADING_SPAN_TOKENS])
    return tuple(head[:_HEADING_SPAN_TOKENS])


def _item_heading_tokens(tokens: list[str]) -> set[str]:
    """Collect the spans following every ``item <digit>`` marker in the page."""

    out: set[str] = set()
    for index, token in enumerate(tokens):
        if token == "item" and index + 1 < len(tokens) and tokens[index + 1].isdigit():
            out.update(tokens[index + 2 : index + 2 + _ITEM_HEADING_SPAN_TOKENS])
    return out


def _prefix_key(token: str) -> str | None:
    return token[:_PREFIX_LENGTH] if len(token) >= _PREFIX_LENGTH else None


def _appears_in(term: str, heading: tuple[str, ...] | set[str]) -> bool:
    if term in heading:
        return True
    key = _prefix_key(term)
    return key is not None and any(_prefix_key(token) == key for token in heading)


def _statement_credit(heading_terms: set[str], kinds: set[str]) -> float:
    """Credit metric kinds whose statement or section heading is on the page."""

    credit = 0.0
    has_statement = bool(heading_terms & {"statement", "statements"})
    for kind in kinds:
        if kind == "balance":
            if "balance" in heading_terms and heading_terms & {"sheet", "sheets", "statement", "statements"}:
                credit += 1.0
        elif kind == "income":
            if "summary" in heading_terms or (has_statement and heading_terms & set(_STATEMENT_KEYS[kind])):
                credit += 1.0
        elif kind == "geographic":
            if heading_terms & set(_STATEMENT_KEYS[kind]):
                credit += 1.0
        elif has_statement and heading_terms & set(_STATEMENT_KEYS[kind]):
            credit += 1.0
    return credit


def _pool_idf(total: int, frequency: int) -> float:
    return math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5))


def _lexical_scores(texts: list[str], query_terms: tuple[str, ...]) -> list[float]:
    """Score pool pages by distinct query-term coverage weighted by pool IDF.

    Term frequency is deliberately ignored: report boilerplate repeats the
    company name and years on every page, and repeated tokens would inflate
    generic pages above the ones that actually carry the evidence terms. Terms
    missing from the whole pool fall back to a shared-prefix match (plural and
    derived forms such as "geographies" versus "geographic").

    Two structural signals are added on top. Rare terms appearing in the
    page's own heading (section title or an ``Item N.`` marker) earn heading
    credit because evidence pages are usually section or statement starts.
    Metric terms additionally credit pages holding the matching financial
    statement or segment section, since a question like "gross margin" is
    answered by the statement of operations even without sharing a word.
    """

    total = len(texts)
    term_sets: list[set[str]] = []
    prefix_sets: list[set[str]] = []
    headings: list[tuple[str, ...]] = []
    item_headings: list[set[str]] = []
    term_df: Counter[str] = Counter()
    prefix_df: Counter[str] = Counter()
    for text in texts:
        tokens = _TOKEN_PATTERN.findall(unicodedata.normalize("NFC", text).casefold())
        terms = set(tokens)
        prefixes = {token[:_PREFIX_LENGTH] for token in tokens if len(token) >= _PREFIX_LENGTH}
        term_sets.append(terms)
        prefix_sets.append(prefixes)
        headings.append(_heading_tokens(tokens))
        item_headings.append(_item_heading_tokens(tokens))
        term_df.update(terms)
        prefix_df.update(prefixes)

    unique_terms = tuple(dict.fromkeys(query_terms))
    metric_terms = [term for term in unique_terms if term in _METRIC_STATEMENTS]

    scores: list[float] = []
    for index in range(total):
        score = 0.0
        for term in unique_terms:
            frequency = term_df.get(term, 0)
            weight = 1.0
            if term not in term_sets[index]:
                prefix = term[:_PREFIX_LENGTH]
                if frequency != 0 or len(term) < _PREFIX_LENGTH or prefix not in prefix_sets[index]:
                    continue
                frequency = prefix_df.get(prefix, 0)
                weight = _PREFIX_WEIGHT
            idf = _pool_idf(total, frequency)
            score += idf * weight
            if frequency and frequency / total < _RARE_TERM_RATIO:
                if _appears_in(term, headings[index]) or _appears_in(term, item_headings[index]):
                    score += _HEADING_WEIGHT * idf
        heading_terms = set(headings[index])
        for term in metric_terms:
            score += _METRIC_WEIGHT * _statement_credit(heading_terms, set(_METRIC_STATEMENTS[term]))
        scores.append(score)
    return scores


async def _search_candidates(memory: ScopedMemoryApplication, queries: tuple[str, ...], limit: int) -> list[MemoryHit]:
    """Widen saturated native FTS results before ranking across reports."""
    from powercontext.builtin.runtime.models import SearchMemoryRequest

    pool_size = min(max(limit * 10, 100), _MAX_CANDIDATES_PER_QUERY)
    hits: list[MemoryHit] = []
    seen: set[str] = set()
    for query in queries:
        query_limit = pool_size
        while True:
            page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=query_limit))
            if len(page.hits) < query_limit or query_limit >= _MAX_CANDIDATES_PER_QUERY:
                break
            query_limit = min(query_limit * 2, _MAX_CANDIDATES_PER_QUERY)
        for hit in page.hits:
            if hit.entry_id not in seen:
                seen.add(hit.entry_id)
                hits.append(hit)
    return hits


async def search_pdf_pages(
    memory: ScopedMemoryApplication, question: str, limit: int = 10
) -> tuple[MemoryCitation, ...]:
    """Search scoped Memory and return distinct exact physical-page citations.

    Expand all question terms, widen saturated native FTS results, and rank
    by report identity and lexical evidence without model or network calls.
    """
    from powercontext.builtin.runtime.models import GetMemoryEntryRequest

    if limit <= 0 or not question.strip():
        return ()
    query_terms = _question_terms(question)
    hits = await _search_candidates(memory, (question, *_expansion_queries(query_terms)), limit)
    if not hits:
        return ()

    texts = [hit.text for hit in hits]
    scores = _lexical_scores(texts, query_terms) if query_terms else [0.0] * len(hits)

    citations: list[MemoryCitation] = []
    ranked = sorted(
        range(len(hits)),
        key=lambda index: (_document_score(texts[index], question), scores[index]),
        reverse=True,
    )
    seen_pages: set[str] = set()
    for index in ranked:
        hit = hits[index]
        citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
        page_refs = {source.source_id for source in entry.sources if source.source_type == PDF_PAGE_SOURCE_NAME}
        if len(page_refs) != 1 or seen_pages.intersection(page_refs):
            continue
        seen_pages.update(page_refs)
        citations.append(citation)
        if len(citations) >= limit:
            break
    return tuple(citations)


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
