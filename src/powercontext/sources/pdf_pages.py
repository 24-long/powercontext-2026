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

from pydantic import BaseModel
from pypdf import PasswordType, PdfReader

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PDF_PAGE_SOURCE_NAME = "pdf-page"

_TOKEN_PATTERN = re.compile(r"[a-z]+|\d+")

# Retrieval tuning: broadened FTS pool size, heading boost, prefix fallback.
_EXPANSION_SINGLE_QUERIES = 14
_EXPANSION_PAIR_QUERIES = 10
_TITLE_TOKEN_COUNT = 16
_TITLE_BOOST = 3.0
_PREFIX_LENGTH = 5
_PREFIX_WEIGHT = 0.7

# Function words excluded from retrieval scoring so the evidence terms in a
# FinanceBench question dominate the page ranking.
_STOPWORDS = frozenset(
    (
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

    digest = hashlib.sha256(f"{document_id}\x00{document_sha256}\x00{page_number}".encode("utf-8")).hexdigest()
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
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            if reader.decrypt("") == PasswordType.NOT_DECRYPTED:
                raise ValueError("PDF requires a password that was not provided")  # noqa: TRY003
        pages: list[PDFPageInput] = []
        for index, page in enumerate(reader.pages, start=1):
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
    except Exception as error:  # noqa: BLE001 - atomic failure for any parse problem
        raise ValueError("PDF could not be fully parsed") from error
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
    """Short FTS queries that widen the candidate pool beyond the full question.

    Memory's FTS admission requires a page to cover a quarter of the query
    terms, so a long natural-language question admits only a handful of pages
    and can miss the evidence page entirely. Single-term and adjacent-pair
    queries have a one-term floor and recover those pages.
    """

    uniq = list(dict.fromkeys(terms))
    singles = sorted((term for term in uniq if len(term) >= 4), key=lambda term: (-len(term), term))
    pairs: list[tuple[int, str]] = []
    for left, right in zip(uniq, uniq[1:]):
        if len(left) >= 4 and len(right) >= 4:
            pairs.append((len(left) + len(right), f"{left} {right}"))
    pairs.sort(key=lambda item: (-item[0], item[1]))
    return tuple(singles[:_EXPANSION_SINGLE_QUERIES]) + tuple(text for _, text in pairs[:_EXPANSION_PAIR_QUERIES])


def _title_tokens(tokens: list[str]) -> set[str]:
    head = tokens[:_TITLE_TOKEN_COUNT]
    if head[:3] == ["table", "of", "contents"]:
        head = head[3:]
    return set(head)


def _pool_idf(total: int, frequency: int) -> float:
    return math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5))


def _lexical_scores(texts: list[str], query_terms: tuple[str, ...]) -> list[float]:
    """Score pool pages by distinct query-term coverage weighted by pool IDF.

    Term frequency is deliberately ignored: report boilerplate repeats the
    company name and years on every page, and repeated tokens would inflate
    generic pages above the ones that actually carry the evidence terms. Terms
    missing from the whole pool fall back to a shared-prefix match (plural and
    derived forms such as "geographies" versus "geographic"), and a term found
    in the page's leading heading tokens counts tripled because evidence pages
    are usually section or statement starts.
    """

    total = len(texts)
    term_sets: list[set[str]] = []
    prefix_sets: list[set[str]] = []
    titles: list[set[str]] = []
    term_df: Counter[str] = Counter()
    prefix_df: Counter[str] = Counter()
    for text in texts:
        tokens = _TOKEN_PATTERN.findall(unicodedata.normalize("NFC", text).casefold())
        terms = set(tokens)
        prefixes = {token[:_PREFIX_LENGTH] for token in tokens if len(token) >= _PREFIX_LENGTH}
        term_sets.append(terms)
        prefix_sets.append(prefixes)
        titles.append(_title_tokens(tokens))
        term_df.update(terms)
        prefix_df.update(prefixes)

    scores: list[float] = []
    for index in range(total):
        score = 0.0
        for term in dict.fromkeys(query_terms):
            frequency = term_df.get(term, 0)
            weight = 1.0
            if term not in term_sets[index]:
                prefix = term[:_PREFIX_LENGTH]
                if frequency != 0 or len(term) < _PREFIX_LENGTH or prefix not in prefix_sets[index]:
                    continue
                frequency = prefix_df.get(prefix, 0)
                weight = _PREFIX_WEIGHT
            value = _pool_idf(total, frequency) * weight
            if term in titles[index]:
                value *= _TITLE_BOOST
            score += value
        scores.append(score)
    return scores


async def search_pdf_pages(memory, question: str, limit: int = 10):
    """Search scoped Memory for pages evidencing one FinanceBench question.

    Uses the public FTS Memory API for an offline broad candidate pool, then
    re-ranks candidates with a lexical IDF score over question terms and
    returns at most ``limit`` distinct exact page citations.
    """
    from powercontext.artifacts import MemoryCitation
    from powercontext.builtin.runtime.models import SearchMemoryRequest

    if limit <= 0:
        return ()
    query_terms = _question_terms(question)
    pool_size = max(limit * 10, 100)
    hits: list = []
    seen: set[str] = set()
    for query in (question, *_expansion_queries(query_terms)):
        page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=pool_size))
        for hit in page.hits:
            if hit.entry_id not in seen:
                seen.add(hit.entry_id)
                hits.append(hit)
    if not hits:
        return ()

    texts = [hit.text for hit in hits]
    scores = _lexical_scores(texts, query_terms) if query_terms else [0.0] * len(hits)

    citations: list[MemoryCitation] = []
    ranked = sorted(range(len(hits)), key=lambda index: -scores[index])
    for index in ranked:
        hit = hits[index]
        citations.append(
            MemoryCitation(
                memory_ref=hit.memory_ref,
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
        )
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
