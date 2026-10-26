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

from __future__ import annotations

import json

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory import MemoryHit
from powercontext.builtin.runtime import PrepareContextRequest
from powercontext.builtin.runtime.prepared_context import (
    PreparedContextBuilder,
    PreparedMemoryCandidates,
)
from powercontext.builtin.runtime.prepared_evidence import (
    MemoryEntryAddress,
)

_MEMORY_REF = ArtifactRef(family="memory", artifact_id="mem", revision=1)
_BUILDER = PreparedContextBuilder()


def _hits(texts: list[str], scores: list[float] | None = None) -> tuple[MemoryHit, ...]:
    return tuple(
        MemoryHit(
            memory_ref=_MEMORY_REF,
            entry_id=f"entry-{index}",
            entry_version_id=f"version-{index}",
            text=text,
            score=1.0 if scores is None else scores[index],
            matched_by=(),
        )
        for index, text in enumerate(texts)
    )


def _select(
    query: str,
    texts: list[str],
    max_bytes: int = 2000,
    *,
    scope_id: str = "scope",
    scores: list[float] | None = None,
):
    request = PrepareContextRequest(query=query, max_bytes=max_bytes, evidence_selection=True)
    return _BUILDER.build_scopes_result(
        request=request,
        current_scope_id=scope_id,
        memory_candidates=(
            PreparedMemoryCandidates(scope_id=scope_id, memory_ref=_MEMORY_REF, hits=_hits(texts, scores)),
        ),
    )


def _envelope_items(result) -> list[dict]:
    assert result.context is not None
    return json.loads(result.context.content.splitlines()[-2])["items"]


def test_fragments_are_exact_slices_and_envelope_matches():
    texts = [
        "Background filler material. " * 12 + "The cobalt sensor is calibrated at 37 degrees. ",
        "Background filler material. " * 12 + "The amber valve opens at 19 degrees. ",
    ]
    result = _select("cobalt sensor calibrated", texts, max_bytes=1500)

    assert result.fragments
    for fragment in result.fragments:
        assert fragment.address.memory.artifact == _MEMORY_REF
        assert type(fragment.start) is int and type(fragment.end) is int
        assert 0 <= fragment.start < fragment.end
        assert (
            fragment.text == texts[int(fragment.address.entry_id.removeprefix("entry-"))][fragment.start : fragment.end]
        )
    assert any("37" in fragment.text for fragment in result.fragments)

    items = _envelope_items(result)
    assert len(items) == len(result.fragments) == len(result.origins)
    for item, fragment, origin in zip(items, result.fragments, result.origins, strict=True):
        assert isinstance(origin, MemoryCitation)
        assert origin.memory_ref == _MEMORY_REF
        assert origin.entry_id == fragment.address.entry_id
        assert origin.entry_version_id == fragment.address.entry_version_id
        assert item["content"] == fragment.text
        assert item["start"] == fragment.start and item["end"] == fragment.end
        assert item["citation"] == {
            "scope_id": "scope",
            "memory_ref": _MEMORY_REF.model_dump(mode="json"),
            "entry_id": fragment.address.entry_id,
            "entry_version_id": fragment.address.entry_version_id,
        }


def test_cross_scope_origins_use_entry_addresses():
    texts = [
        "Filler notes about nothing. " * 10 + "The sensor reads 37 degrees. ",
    ]
    result = _select("sensor reads", texts, scope_id="current-scope")

    assert result.fragments
    assert all(isinstance(origin, MemoryCitation) for origin in result.origins)
    assert result.origins[0].memory_ref == _MEMORY_REF

    # The same candidates arriving from another Scope must cite entry addresses.
    request = PrepareContextRequest(query="sensor reads", max_bytes=2000, evidence_selection=True)
    foreign = _BUILDER.build_scopes_result(
        request=request,
        current_scope_id="current-scope",
        memory_candidates=(
            PreparedMemoryCandidates(scope_id="other-scope", memory_ref=_MEMORY_REF, hits=_hits(texts)),
        ),
    )
    assert foreign.fragments
    assert all(isinstance(origin, MemoryEntryAddress) for origin in foreign.origins)
    for origin in foreign.origins:
        assert origin.memory.scope_id == "other-scope"
        assert origin.memory.artifact == _MEMORY_REF


def test_default_prepare_request_is_unchanged():
    texts = ["A station records temperature."]
    request = PrepareContextRequest(query="temperature", max_bytes=1200)
    result = _BUILDER.build_scopes_result(
        request=request,
        current_scope_id="scope",
        memory_candidates=(PreparedMemoryCandidates(scope_id="scope", memory_ref=_MEMORY_REF, hits=_hits(texts)),),
    )
    assert result.fragments == ()
    items = _envelope_items(result)
    assert items[0]["content"] == "A station records temperature."


def test_adjacent_sentences_merge_into_one_item():
    texts = [
        "Irrelevant preamble. " * 8
        + "Vitamin D deficiency is common. "
        + "Supplementation reduces fracture risk. "
        + "Trials confirm the effect. "
        + "Closing remarks. " * 4,
    ]
    result = _select("vitamin D deficiency supplementation fractures", texts, max_bytes=1200)

    items = _envelope_items(result)
    assert items, "selection should produce evidence items"
    contents = [item["content"] for item in items]
    assert any("Supplementation reduces fracture risk." in content for content in contents)
    # Adjacent selected sentences of one entry must be merged: the item count
    # stays far below the sentence count.
    assert len(items) < 3
    for item in items:
        entry_id = item["citation"]["entry_id"]
        text = texts[int(entry_id.removeprefix("entry-"))]
        assert item["content"] == text[item["start"] : item["end"]]


def test_multibyte_text_respects_byte_budget_without_broken_chars():
    text = "海洋观测 station records temperatures. " * 40
    result = _select("海洋观测 station", [text], max_bytes=1200)

    assert result.context is not None
    assert result.context.content_bytes == len(result.context.content.encode("utf-8")) <= 1200
    for fragment in result.fragments:
        entry_text = text
        assert fragment.text == entry_text[fragment.start : fragment.end]
        fragment.text.encode("utf-8")  # decodable, no broken UTF-8 sequences
    assert result.fragments


def test_empty_or_irrelevant_candidates_yield_empty_selection():
    request = PrepareContextRequest(query="anything", max_bytes=2000, evidence_selection=True)
    empty = _BUILDER.build_scopes_result(
        request=request,
        current_scope_id="scope",
        memory_candidates=(PreparedMemoryCandidates(scope_id="scope", memory_ref=_MEMORY_REF, hits=()),),
    )
    assert empty.fragments == ()
    assert empty.context is not None and empty.context.status == "empty"

    # No lexical overlap at all: the budget is still filled with original
    # evidence, never left empty.
    no_match = _select("zzzqqq unrelated", ["Alpha notes. " * 10], max_bytes=900)
    assert no_match.fragments
    assert all(fragment.text in "Alpha notes. " * 10 for fragment in no_match.fragments)


def test_higher_retrieval_score_steers_selection():
    winner = "Filler. " * 10 + "Cobalt sensors calibrate at 37 degrees. "
    loser = "Filler. " * 10 + "Cobalt appears in many alloys. "
    result = _select("cobalt sensor calibrated", [loser, winner], max_bytes=900, scores=[0.2, 1.0])
    assert result.fragments
    assert any("37 degrees" in fragment.text for fragment in result.fragments)


def test_content_bytes_matches_exact_utf8_size():
    texts = ["Measured evidence sentence with numbers 12345. " * 6]
    result = _select("measured evidence", texts, max_bytes=1024)
    assert result.context is not None
    assert result.context.content_bytes == len(result.context.content.encode("utf-8"))
    assert result.context.content_bytes <= 1024
