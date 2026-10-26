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

"""Lifecycle identity, strict events, and durable native-write recovery."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryEntryInput, MemoryService
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import (
    BuiltinConfig,
    BuiltinRuntime,
    CaptureSource,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    SearchMemoryRequest,
    SearchTopicMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleError, SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceRef


@pytest.mark.parametrize(
    "change",
    [
        {"node_id": " "},
        {"node_id": 1},
        {"reason": "\t"},
        {"reason": None},
        {"revision": True},
        {"revision": 1.0},
        {"revision": "1"},
        {"revision": 0},
        {"sequence": False},
        {"sequence": 1.0},
        {"sequence": "1"},
        {"sequence": -1},
        {"available": 0},
        {"available": "false"},
        {"extra": "forbidden"},
    ],
)
def test_event_rejects_coercion_blank_fields_and_extras(change: dict[str, object]) -> None:
    payload = {"node_id": "root", "revision": 3, "sequence": 1, "available": False, "reason": "withdrawn"}
    with pytest.raises(ValidationError):
        SourceLifecycleEvent.model_validate(payload | change)


def _event(sequence: int, available: bool) -> SourceLifecycleEvent:
    return SourceLifecycleEvent(node_id="root", revision=3, sequence=sequence, available=available, reason="controlled")


async def _register(runtime: BuiltinRuntime, key: str):
    assert runtime.scopes is not None
    scope = await runtime.scopes.create(ScopeDraft(title=key, summary="Lifecycle", idempotency_key=key))
    service = runtime.lifecycle.for_scope(scope.scope_id)
    captured = await runtime.sources.for_scope(scope.scope_id).capture(
        CaptureSource(source_id="root", content="Original evidence", metadata={})
    )
    await service.register_source("root", captured.source_ref, revision=3)
    citations = {}
    for node in ("one", "two"):
        result = await runtime.memory.for_scope(scope.scope_id).remember(
            RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text=f"{node} lifecycle evidence"),))
        )
        assert result.entry is not None
        citations[node] = result.entry.citation
        await service.register_memory(node, result.entry.citation, parents=("root",), policy="all")
    topic = await runtime.records.for_scope(scope.scope_id).create_artifact(
        "topic-memory",
        ArtifactWrite(content={"title": "topicevidence", "summary": "topicevidence", "detail": "detail"}),
    )
    reference = ArtifactRef(family="topic-memory", artifact_id=topic.artifact_id, revision=topic.revision)
    await service.register_topic("topic", reference, parents=("one", "two"), policy="all")
    return scope.scope_id, service, captured.source_ref, citations, reference


@pytest.mark.parametrize("restoring", [False, True])
@pytest.mark.parametrize("after_commit", [False, True])
async def test_retry_after_interrupted_native_transition_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restoring: bool, after_commit: bool
) -> None:
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'retry.db'}"))
    async with open_builtin_runtime(config) as runtime:
        scope, service, _, citations, topic = await _register(runtime, "retry")
        if restoring:
            await service.apply(_event(1, False))
        baseline = (await runtime.memory.for_scope(scope).list(include_inactive=True)).memory_ref
        assert baseline is not None
        method = "reactivate" if restoring else "forget"
        original = getattr(MemoryService, method)

        async def fail_once(self, *args, **kwargs):
            if after_commit:
                await original(self, *args, **kwargs)
            raise OSError("Simulated interrupted native transition")  # noqa: TRY003

        with monkeypatch.context() as patch:
            patch.setattr(MemoryService, method, fail_once)
            with pytest.raises(OSError):
                await service.apply(_event(2 if restoring else 1, restoring))

    async with open_builtin_runtime(config) as runtime:
        service = runtime.lifecycle.for_scope(scope)
        expected = ["one", "root", "topic", "two"] if restoring else []
        event = _event(2 if restoring else 1, restoring)
        assert await service.apply(event) == expected
        memory = runtime.memory.for_scope(scope)
        page = await memory.list(include_inactive=True)
        assert page.memory_ref is not None
        assert page.memory_ref.revision == baseline.revision + 2
        assert all((item.state == "active") == restoring for item in page.entries)
        assert {item.entry.entry_version_id for item in page.entries} == {
            citation.entry_version_id for citation in citations.values()
        }
        assert await service.apply(event) == expected
        assert (await memory.list(include_inactive=True)).memory_ref == page.memory_ref
        for node, citation in citations.items():
            assert await service.resolve(node, purpose="audit") == citation
            assert (
                await memory.get(GetMemoryEntryRequest(citation=citation))
            ).entry.text == f"{node} lifecycle evidence"
            hits = await memory.search(SearchMemoryRequest(query=node, mode="fts", limit=10))
            assert bool(hits.hits) == restoring
        topics = await runtime.topic_memory.for_scope(scope).search(SearchTopicMemoryRequest(query="topicevidence"))
        assert bool(topics.hits) == restoring
        assert bool(await runtime.topic_memory.for_scope(scope).browse(limit=10)) == restoring
        assert await service.resolve("topic", purpose="audit") == topic


async def test_bindings_references_sequences_and_scope_isolation(tmp_path: Path) -> None:
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'scopes.db'}"))
    async with open_builtin_runtime(config) as runtime:
        scope, service, source, citations, topic = await _register(runtime, "first")
        other_scope, other, _, _, _ = await _register(runtime, "second")
        head = (await runtime.memory.for_scope(scope).list()).memory_ref
        await service.register_source("root", source_ref=source, revision=3)
        await service.register_memory("one", citation=citations["one"], parents=("root",), policy="all")
        await service.register_topic("topic", artifact_ref=topic, parents=("two", "one"), policy="all")
        assert (await runtime.memory.for_scope(scope).list()).memory_ref == head
        with pytest.raises(SourceLifecycleError):
            await service.register_source("root", source, revision=4)
        with pytest.raises(SourceLifecycleError):
            await service.register_source("missing", SourceRef(source_type="content", source_id="absent"), revision=1)
        with pytest.raises(SourceLifecycleError):
            await other.register_memory("foreign", citations["one"], parents=("root",), policy="all")
        with pytest.raises(SourceLifecycleError):
            await other.register_topic("foreign-topic", topic, parents=("root",), policy="all")
        with pytest.raises(SourceLifecycleError):
            await service.register_memory("missing-parent", citations["one"], parents=("absent",), policy="all")
        with pytest.raises(SourceLifecycleError):
            await service.register_memory("duplicate", citations["one"], parents=("root", "root"), policy="any")
        with pytest.raises(SourceLifecycleError):
            await service.resolve("one", purpose="unsupported")
        with pytest.raises(SourceLifecycleError):
            await service.resolve("absent", purpose="audit")
        await service.apply(_event(1, False))
        await service.register_source("root", source, revision=3)
        assert await other.apply(_event(1, True)) == ["one", "root", "topic", "two"]
        assert len((await runtime.memory.for_scope(other_scope).list()).entries) == 2
        with pytest.raises(SourceLifecycleError):
            await service.resolve("one", purpose="generation")

    async with open_builtin_runtime(config) as runtime:
        service = runtime.lifecycle.for_scope(scope)
        other = runtime.lifecycle.for_scope(other_scope)
        assert await service.resolve("root", purpose="audit") == source
        with pytest.raises(SourceLifecycleError):
            await service.resolve("root", purpose="generation")
        assert await other.resolve("root", purpose="generation") == source
        await service.apply(_event(2, True))
        for node, citation in citations.items():
            assert await service.resolve(node, purpose="generation") == citation
