"""Persistent literature notes backed by a real embedded pyseekdb collection."""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pyseekdb import Client

from .seed_data import SEED_NOTES

COLLECTION_NAME = "literature_notes"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RepositoryError(RuntimeError):
    """An operation on the SeekDB repository failed."""


def _flat(value: Any) -> list[Any]:
    """Accept both query-shaped and flat results across pyseekdb releases."""
    if value is None:
        return []
    values = list(value)
    if values and isinstance(values[0], (list, tuple)):
        return list(values[0])
    return values


class LiteratureRepository:
    def __init__(self, path: str | Path | None = None, database: str | None = None):
        configured_path = path or os.getenv("SEEKDB_PATH") or "backend/data/seekdb"
        db_path = Path(configured_path).expanduser()
        if not db_path.is_absolute():
            db_path = PROJECT_ROOT / db_path
        self.path = str(db_path.resolve())
        self.database = database or os.getenv("SEEKDB_DATABASE", "test")
        self._client = Client(path=self.path, database=self.database)
        self._collection: Any = None
        self._lock = threading.RLock()

    def initialize(self) -> None:
        with self._lock:
            if self._collection is not None:
                return
            Path(self.path).mkdir(parents=True, exist_ok=True)
            try:
                if self._client.has_collection(COLLECTION_NAME):
                    self._collection = self._client.get_collection(COLLECTION_NAME)
                else:
                    self._collection = self._client.create_collection(COLLECTION_NAME)
            except Exception as exc:
                raise RepositoryError(f"cannot open SeekDB collection: {exc}") from exc

    def seed(self) -> int:
        """Insert only missing built-in IDs; returns the number imported now."""
        with self._lock:
            self.initialize()
            try:
                seed_ids = [note["id"] for note in SEED_NOTES]
                existing = set(_flat(self._collection.get(ids=seed_ids, include=[]).get("ids", [])))
                missing = [note for note in SEED_NOTES if note["id"] not in existing]
                if missing:
                    self._collection.add(
                        ids=[note["id"] for note in missing],
                        documents=[note["text"] for note in missing],
                        metadatas=[note["metadata"] for note in missing],
                    )
                return len(missing)
            except Exception as exc:
                raise RepositoryError(f"cannot seed literature notes: {exc}") from exc

    def stats(self) -> dict[str, Any]:
        with self._lock:
            self.initialize()
            try:
                rows = self._collection.get(include=["metadatas"])
                paper_ids = {
                    metadata.get("paper_id")
                    for metadata in rows.get("metadatas", [])
                    if metadata and metadata.get("paper_id")
                }
                return {
                    "engine": "seekdb",
                    "collection": COLLECTION_NAME,
                    "document_count": self._collection.count(),
                    "paper_count": len(paper_ids),
                    "builtin_note_count": len(SEED_NOTES),
                }
            except Exception as exc:
                raise RepositoryError(f"cannot read SeekDB stats: {exc}") from exc

    def list_papers(self) -> dict[str, Any]:
        with self._lock:
            self.initialize()
            try:
                rows = self._collection.get(include=["documents", "metadatas"])
            except Exception as exc:
                raise RepositoryError(f"cannot list literature notes: {exc}") from exc

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        paper_meta: dict[str, dict[str, Any]] = {}
        ids = _flat(rows.get("ids"))
        documents = _flat(rows.get("documents"))
        metadatas = _flat(rows.get("metadatas"))
        for index, note_id in enumerate(ids):
            metadata = metadatas[index] if index < len(metadatas) and metadatas[index] else {}
            paper_id = str(metadata.get("paper_id") or note_id)
            note = {
                "id": str(note_id),
                "text": documents[index] if index < len(documents) else "",
                "metadata": metadata,
            }
            grouped[paper_id].append(note)
            paper_meta[paper_id] = metadata

        papers = []
        for paper_id, notes in grouped.items():
            metadata = paper_meta[paper_id]
            notes.sort(key=lambda note: (note["metadata"].get("chunk_index", 0), note["id"]))
            papers.append(
                {
                    "paper_id": paper_id,
                    "title": metadata.get("title", paper_id),
                    "authors": str(metadata.get("authors", "")).split("; "),
                    "year": metadata.get("year"),
                    "source_url": metadata.get("source_url"),
                    "note_count": len(notes),
                    "notes": notes,
                }
            )
        papers.sort(key=lambda paper: (paper["year"] or 0, paper["paper_id"]))
        return {"papers": papers, "paper_count": len(papers)}

    def add_document(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.initialize()
            paper_id = f"user-{uuid.uuid4().hex}"
            note_id = f"{paper_id}-note-01"
            metadata = {
                "paper_id": paper_id,
                "title": data["title"],
                "authors": "; ".join(data["authors"]),
                "year": data["year"],
                "section": data["section"],
                "page": data["page"],
                "chunk_index": 1,
                "source": "user",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                self._collection.add(ids=[note_id], documents=[data["body"]], metadatas=[metadata])
            except Exception as exc:
                raise RepositoryError(f"cannot add literature note: {exc}") from exc
            return {"id": note_id, "text": data["body"], "metadata": metadata, "distance": None}

    def search(self, query: str, mode: str, keyword: str | None, n_results: int) -> dict[str, Any]:
        with self._lock:
            self.initialize()
            full_text: dict[str, Any] = {}
            if mode == "hybrid" and keyword:
                full_text = {"where_document": {"$contains": keyword}}
            started = time.perf_counter()
            try:
                raw = self._collection.hybrid_search(
                    query=full_text,
                    knn={"query_texts": [query]},
                    rank={"rrf": {}},
                    n_results=self._candidate_count(n_results),
                    include=["documents", "metadatas", "distances"],
                )
            except Exception as exc:
                raise RepositoryError(f"SeekDB hybrid_search failed: {exc}") from exc
            duration_ms = round((time.perf_counter() - started) * 1000, 3)

        ids = _flat(raw.get("ids"))
        documents = _flat(raw.get("documents"))
        metadatas = _flat(raw.get("metadatas"))
        distances = _flat(raw.get("distances"))
        results = []
        for index, note_id in enumerate(ids):
            distance = distances[index] if index < len(distances) else None
            results.append(
                {
                    "id": str(note_id),
                    "text": documents[index] if index < len(documents) else "",
                    "metadata": metadatas[index] if index < len(metadatas) and metadatas[index] else {},
                    "distance": float(distance) if distance is not None else None,
                }
            )
        if mode == "hybrid" and keyword:
            results = self._apply_full_text_filter(results, keyword)
        results = results[:n_results]
        return {
            "trace_id": uuid.uuid4().hex,
            "duration_ms": duration_ms,
            "engine": "seekdb",
            "mode": mode,
            "query": query,
            "keyword": keyword if mode == "hybrid" else None,
            "results": results,
        }

    @staticmethod
    def _candidate_count(n_results: int) -> int:
        """Fetch a wider candidate pool so keyword filtering still has material to work with."""
        return min(max(n_results * 5, n_results), 50)

    @staticmethod
    def _apply_full_text_filter(results: list[dict[str, Any]], keyword: str) -> list[dict[str, Any]]:
        """Enforce the keyword constraint in the service layer.

        pyseekdb 1.4.0 accepts ``where_document`` in hybrid_search but does not apply it,
        so the filter is verified here instead of being reported as working.
        """
        needle = keyword.strip().lower()
        if not needle:
            return results
        return [result for result in results if needle in str(result.get("text", "")).lower()]
