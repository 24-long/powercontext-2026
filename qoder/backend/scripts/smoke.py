"""End-to-end smoke test; uses only the standard library plus installed runtime deps."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.repository import LiteratureRepository  # noqa: E402
from app.seed_data import SEED_NOTES  # noqa: E402


def open_client(path: str) -> tuple[TestClient, object]:
    app = create_app(LiteratureRepository(path=path, database="test"))
    return TestClient(app), app


def check_seed(client: TestClient) -> None:
    health = client.get("/api/health")
    assert health.status_code == 200 and health.json()["engine"] == "seekdb", health.text
    response = client.get("/api/papers")
    assert response.status_code == 200, response.text
    papers = response.json()["papers"]
    builtin = [paper for paper in papers if not paper["paper_id"].startswith("user-")]
    assert len(builtin) == 3, f"expected 3 built-in papers, got {len(builtin)}"
    assert all(paper["note_count"] >= 3 for paper in builtin), "each paper needs >=3 notes"
    all_ids = [note["id"] for paper in papers for note in paper["notes"]]
    for note in SEED_NOTES:
        assert all_ids.count(note["id"]) == 1, f"seed ID is missing or duplicated: {note['id']}"


def verify_existing(path: str, expected_count: int) -> None:
    client, app = open_client(path)
    with client:
        assert app.state.imported_notes == 0, "restart attempted to import built-in notes again"
        stats = client.get("/api/stats")
        assert stats.status_code == 200, stats.text
        assert stats.json()["document_count"] == expected_count, stats.text
        check_seed(client)
        response = client.post(
            "/api/search",
            json={"query": "retrieval augmented generation evidence", "mode": "semantic", "n_results": 3},
        )
        assert response.status_code == 200 and response.json()["results"], response.text
    print(f"PASS persistence: reopened in a second Python process; count={expected_count}, seed reimport=0")


def run_first_process(path: str, keep: bool, is_temporary: bool) -> None:
    client, app = open_client(path)
    with client:
        assert app.state.imported_notes == len(SEED_NOTES), "first startup did not import every seed note"
        check_seed(client)
        stats = client.get("/api/stats").json()
        assert stats["document_count"] == len(SEED_NOTES), stats
        print(f"PASS import: 3 papers, {stats['document_count']} notes")

        semantic = client.post(
            "/api/search",
            json={
                "query": "How does self-attention avoid sequential computation?",
                "mode": "semantic",
                "n_results": 4,
            },
        )
        assert semantic.status_code == 200, semantic.text
        semantic_data = semantic.json()
        assert semantic_data["engine"] == "seekdb" and semantic_data["mode"] == "semantic"
        assert semantic_data["trace_id"] and semantic_data["duration_ms"] >= 0
        assert semantic_data["results"], "semantic search returned no results"
        assert all(item["id"] and item["text"] and item["distance"] is not None for item in semantic_data["results"])
        print(
            "PASS semantic hybrid_search: "
            f"ids={[item['id'] for item in semantic_data['results']]}, "
            f"duration_ms={semantic_data['duration_ms']}"
        )

        hybrid = client.post(
            "/api/search",
            json={
                "query": "怎样并行建立序列中任意位置的联系？",
                "keyword": "自注意力",
                "mode": "hybrid",
                "n_results": 4,
            },
        )
        assert hybrid.status_code == 200, hybrid.text
        hybrid_data = hybrid.json()
        hybrid_ids = [item["id"] for item in hybrid_data["results"]]
        assert hybrid_data["mode"] == "hybrid" and hybrid_data["keyword"] == "自注意力"
        assert "attention-note-01" in hybrid_ids, f"full-text evidence absent: {hybrid_ids}"
        assert hybrid_ids, "hybrid search returned no results"
        assert all("自注意力" in item["text"] for item in hybrid_data["results"]), (
            f"keyword constraint not enforced: {hybrid_ids}"
        )
        print(f"PASS keyword hybrid_search: ids={hybrid_ids}")

        unmatched = client.post(
            "/api/search",
            json={
                "query": "怎样并行建立序列中任意位置的联系？",
                "keyword": "不存在的关键词xyz",
                "mode": "hybrid",
                "n_results": 4,
            },
        )
        assert unmatched.status_code == 200, unmatched.text
        assert unmatched.json()["results"] == [], "keyword filter did not exclude unmatched notes"
        print("PASS keyword exclusion: unmatched keyword returns no evidence")

        evidence = semantic_data["results"][:2]
        evidence_ids = [item["id"] for item in evidence]
        cards = client.post(
            "/api/cards",
            json={
                "trace_id": semantic_data["trace_id"],
                "question": "自注意力为什么能减少顺序计算？",
                "evidence_ids": evidence_ids,
            },
        )
        assert cards.status_code == 200, cards.text
        card_data = cards.json()
        assert card_data["generation"] == "extractive"
        assert len(card_data["cards"]) == len(evidence)
        for card, source in zip(card_data["cards"], evidence, strict=True):
            assert card["answer"] == source["text"], "card answer is not verbatim search evidence"
            assert card["evidence_id"] == source["id"]
            assert card["citations"][0]["result_id"] == source["id"]
        rejected = client.post(
            "/api/cards",
            json={
                "trace_id": semantic_data["trace_id"],
                "question": "不能引用未检索证据",
                "evidence_ids": ["not-in-search"],
            },
        )
        assert rejected.status_code == 400, rejected.text
        print(f"PASS cards: citations={evidence_ids}; foreign evidence rejected")

        invalid = client.post(
            "/api/documents",
            json={"title": "", "authors": [], "year": 0, "section": "", "page": 0, "body": ""},
        )
        assert invalid.status_code == 422, invalid.text
        created = client.post(
            "/api/documents",
            json={
                "title": "Smoke Test Note",
                "authors": ["LitSeek Smoke"],
                "year": 2026,
                "section": "Persistence",
                "page": 1,
                "body": "A user-authored note persisted in SeekDB for the restart test.",
            },
        )
        assert created.status_code == 201, created.text
        expected_count = client.get("/api/stats").json()["document_count"]
        assert expected_count == len(SEED_NOTES) + 1

    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--path",
            path,
            "--verify-existing",
            "--expect-count",
            str(expected_count),
        ],
        check=True,
    )
    retained = keep or not is_temporary
    print(
        json.dumps(
            {
                "status": "PASS",
                "database_path": path,
                "document_count": expected_count,
                "database_retained": retained,
            },
            ensure_ascii=False,
        )
    )
    if not retained:
        shutil.rmtree(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", help="SeekDB directory; defaults to a new temporary directory")
    parser.add_argument("--keep", action="store_true", help="retain an automatically created temporary database")
    parser.add_argument("--verify-existing", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--expect-count", type=int, default=10, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.verify_existing:
        if not args.path:
            parser.error("--verify-existing requires --path")
        verify_existing(str(Path(args.path).resolve()), args.expect_count)
        return
    is_temporary = not args.path
    path = str(Path(args.path).resolve()) if args.path else tempfile.mkdtemp(prefix="litseek-smoke-")
    run_first_process(path, args.keep, is_temporary)


if __name__ == "__main__":
    main()
