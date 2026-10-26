"""API request and response models."""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class DocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    authors: list[str] = Field(min_length=1, max_length=50)
    year: int = Field(ge=1000, le=2100)
    section: str = Field(min_length=1, max_length=300)
    page: int = Field(ge=1, le=1_000_000)
    body: str = Field(min_length=1, max_length=30_000)

    @field_validator("title", "section", "body")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("authors")
    @classmethod
    def clean_authors(cls, value: list[str]) -> list[str]:
        authors = [author.strip() for author in value]
        if any(not author or len(author) > 200 for author in authors):
            raise ValueError("each author must contain 1-200 characters")
        return authors


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2_000)
    mode: Literal["hybrid", "semantic"] = "hybrid"
    keyword: str | None = Field(default=None, max_length=200)
    n_results: int = Field(default=6, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def clean_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value

    @field_validator("keyword")
    @classmethod
    def clean_keyword(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class SearchResult(BaseModel):
    id: str
    text: str
    metadata: dict[str, Any]
    distance: float | None


class SearchResponse(BaseModel):
    trace_id: str
    duration_ms: float
    engine: Literal["seekdb"] = "seekdb"
    mode: Literal["hybrid", "semantic"]
    query: str
    keyword: str | None
    results: list[SearchResult]


class CardRequest(BaseModel):
    trace_id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=2_000)
    evidence_ids: list[str] | None = Field(default=None, min_length=1, max_length=20)

    @field_validator("question")
    @classmethod
    def clean_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question must not be blank")
        return value


class Citation(BaseModel):
    result_id: str
    paper_id: str | None = None
    title: str
    section: str
    page: int | None = None
    distance: float | None = None


class ReviewCard(BaseModel):
    id: str
    card_type: Literal["question_evidence"] = "question_evidence"
    question: str
    answer: str
    evidence_id: str
    citations: list[Citation]


class CardResponse(BaseModel):
    search_trace_id: str
    generation: Literal["extractive"] = "extractive"
    cards: list[ReviewCard]
