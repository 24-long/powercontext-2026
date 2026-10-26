"""FastAPI entry point for 页间 LitSeek."""

from __future__ import annotations

import os
import threading
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .models import (
    CardRequest,
    CardResponse,
    Citation,
    DocumentCreate,
    ReviewCard,
    SearchRequest,
    SearchResponse,
    SearchResult,
)
from .repository import LiteratureRepository, RepositoryError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


class SearchEvidenceStore:
    """Bounded, server-side evidence snapshots keyed by search trace ID."""

    def __init__(self, capacity: int = 128):
        self._capacity = capacity
        self._items: OrderedDict[str, tuple[SearchResult, ...]] = OrderedDict()
        self._lock = threading.Lock()

    def remember(self, trace_id: str, results: list[SearchResult]) -> None:
        with self._lock:
            self._items[trace_id] = tuple(results)
            self._items.move_to_end(trace_id)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)

    def get(self, trace_id: str) -> tuple[SearchResult, ...] | None:
        with self._lock:
            return self._items.get(trace_id)


def create_app(repository: LiteratureRepository | None = None) -> FastAPI:
    evidence_store = SearchEvidenceStore()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.repository = repository or LiteratureRepository()
        app.state.repository.initialize()
        app.state.imported_notes = app.state.repository.seed()
        yield

    app = FastAPI(title="页间 LitSeek API", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    @app.exception_handler(RepositoryError)
    async def repository_error_handler(_request: Request, exc: RepositoryError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": str(exc)},
        )

    def repo(request: Request) -> LiteratureRepository:
        return request.app.state.repository

    @app.get("/api/health")
    def health(request: Request) -> dict[str, Any]:
        stats = repo(request).stats()
        return {
            "status": "ok",
            "service": "页间 LitSeek",
            "engine": "seekdb",
            "collection": stats["collection"],
        }

    @app.get("/api/stats")
    def stats(request: Request) -> dict[str, Any]:
        return repo(request).stats()

    @app.get("/api/papers")
    def papers(request: Request) -> dict[str, Any]:
        return repo(request).list_papers()

    @app.post("/api/search", response_model=SearchResponse)
    def search(payload: SearchRequest, request: Request) -> SearchResponse:
        response = SearchResponse(
            **repo(request).search(
                query=payload.query,
                mode=payload.mode,
                keyword=payload.keyword,
                n_results=payload.n_results,
            )
        )
        evidence_store.remember(response.trace_id, response.results)
        return response

    @app.post(
        "/api/documents",
        response_model=SearchResult,
        status_code=status.HTTP_201_CREATED,
    )
    def documents(payload: DocumentCreate, request: Request) -> SearchResult:
        return SearchResult(**repo(request).add_document(payload.model_dump()))

    @app.post("/api/cards", response_model=CardResponse)
    def cards(payload: CardRequest) -> CardResponse:
        evidence = evidence_store.get(payload.trace_id)
        if evidence is None:
            raise HTTPException(status_code=404, detail="unknown or expired search trace_id")
        if not evidence:
            raise HTTPException(status_code=400, detail="the search returned no evidence")

        by_id = {result.id: result for result in evidence}
        evidence_ids = payload.evidence_ids or list(by_id)
        unknown = [result_id for result_id in evidence_ids if result_id not in by_id]
        if unknown:
            raise HTTPException(status_code=400, detail=f"evidence IDs not in this search: {unknown}")
        if len(set(evidence_ids)) != len(evidence_ids):
            raise HTTPException(status_code=400, detail="evidence_ids must not contain duplicates")

        review_cards = []
        for result_id in evidence_ids:
            result = by_id[result_id]
            metadata = result.metadata
            citation = Citation(
                result_id=result.id,
                paper_id=metadata.get("paper_id"),
                title=str(metadata.get("title", "未命名文献")),
                section=str(metadata.get("section", "未标注章节")),
                page=metadata.get("page"),
                distance=result.distance,
            )
            review_cards.append(
                ReviewCard(
                    id=f"card-{uuid.uuid4().hex}",
                    question=payload.question,
                    answer=result.text,
                    evidence_id=result.id,
                    citations=[citation],
                )
            )
        return CardResponse(search_trace_id=payload.trace_id, cards=review_cards)

    serve_dist = os.getenv("SERVE_DIST", "true").strip().lower() not in {"0", "false", "no", "off"}
    dist = PROJECT_ROOT / "dist"
    if serve_dist and dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="dist")

    return app


app = create_app()
