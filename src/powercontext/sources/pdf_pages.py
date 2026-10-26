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
    if not isinstance(document_id, str) or not document_id.strip() or document_id != document_id.strip():
        raise ValueError("document_id must be non-empty without surrounding whitespace")  # noqa: TRY003


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


def _page_terms(text: str) -> Counter[str]:
    normalized = unicodedata.normalize("NFC", text).casefold()
    return Counter(_TOKEN_PATTERN.findall(normalized))


async def search_pdf_pages(memory, question: str, limit: int = 10):
    """Search scoped Memory for pages evidencing one FinanceBench question.

    Uses the public FTS Memory API for an offline broad candidate pool, then
    re-ranks candidates with a lexical BM25-style score over question terms and
    returns at most ``limit`` distinct exact page citations.
    """
    from powercontext.artifacts import MemoryCitation
    from powercontext.builtin.runtime.models import SearchMemoryRequest

    if limit <= 0:
        return ()
    query_terms = _question_terms(question)
    pool_size = max(limit * 10, 100)
    page = await memory.search(SearchMemoryRequest(query=question, mode="fts", limit=pool_size))
    hits = page.hits
    if not hits:
        return ()

    if query_terms:
        texts = [hit.text for hit in hits]
        term_counts = [_page_terms(text) for text in texts]
        num_docs = len(texts)
        average_length = sum(len(counts) for counts in term_counts) / num_docs if num_docs else 0.0
        document_frequency: dict[str, int] = {}
        for counts in term_counts:
            for term in counts:
                document_frequency[term] = document_frequency.get(term, 0) + 1

        def score(index: int) -> float:
            counts = term_counts[index]
            length = len(counts)
            total = 0.0
            for term in query_terms:
                df = document_frequency.get(term, 0)
                if df == 0:
                    continue
                term_frequency = counts.get(term, 0)
                if term_frequency == 0:
                    continue
                idf = math.log(1.0 + (num_docs - df + 0.5) / (df + 0.5))
                k1 = 1.5
                b = 0.75
                denominator = term_frequency + k1 * (1.0 - b + b * length / average_length)
                total += idf * term_frequency * (k1 + 1.0) / denominator
            return total

        ranked = sorted(range(len(hits)), key=score, reverse=True)
    else:
        ranked = list(range(len(hits)))

    citations: list[MemoryCitation] = []
    seen: set[str] = set()
    for index in ranked:
        hit = hits[index]
        if hit.entry_id in seen:
            continue
        seen.add(hit.entry_id)
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
