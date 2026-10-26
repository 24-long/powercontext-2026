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

import pytest

from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryEntryInput
from powercontext.builtin.runtime import BuiltinConfig, CaptureSource, PrepareContextRequest, open_builtin_runtime
from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.builtin.sources import ContentSource


class _CandidatePipeline:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        return tuple(
            MemoryEntryInput(kind="conversation-turn", text=source.content, sources=(source,))
            for source in request.sources
            if isinstance(source, ContentSource)
        )


async def _flush(memory) -> None:
    while (await memory.flush()).processed:
        pass


async def test_prepare_incremental_rebuilds_prepare_content_across_rounds() -> None:
    async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_CandidatePipeline()) as runtime:
        scope = await runtime.scopes.create(
            ScopeDraft(
                title="Incremental e2e",
                summary="Context pack runtime integration",
                idempotency_key="context-02-e2e",
            )
        )
        sources = runtime.sources.for_scope(scope.scope_id)
        memory = runtime.memory.for_scope(scope.scope_id)
        context = runtime.context.for_scope(scope.scope_id)
        turns = [
            "Project Manager: what are your experiences with remote controls ?",
            "Engineer: the buttons are too small and hard to find in the dark",
            "Project Manager: noted . Anything else about the setup ?",
        ]
        for index, text in enumerate(turns[:2]):
            await sources.capture(CaptureSource(source_id=f"turn-{index}", content=text, metadata={}))
        await _flush(memory)

        request = PrepareContextRequest(query="remote", max_bytes=4096)
        first = await context.prepare_incremental(request)
        assert isinstance(first, ContextPack)
        assert b"".join(first.items).decode("utf-8") == (await context.prepare(request)).content

        await sources.capture(CaptureSource(source_id="turn-2", content=turns[2], metadata={}))
        await _flush(memory)
        second = await context.prepare_incremental(request, base_pack=first)
        assert isinstance(second, ContextPackDelta)
        expected = await context.prepare(request)
        assert expected.content is not None

    rebuilt = apply_context_pack_delta(
        ContextPack.from_bytes(first.to_bytes()), ContextPackDelta.from_bytes(second.to_bytes())
    )
    assert b"".join(rebuilt.items).decode("utf-8") == expected.content
    assert first.byte_count == len(first.to_bytes())
    assert second.byte_count == len(second.to_bytes())


async def test_prepare_incremental_empty_result_and_wrong_base() -> None:
    async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_CandidatePipeline()) as runtime:
        scope = await runtime.scopes.create(
            ScopeDraft(title="Incremental empty", summary="Empty pack handling", idempotency_key="context-02-empty")
        )
        context = runtime.context.for_scope(scope.scope_id)
        request = PrepareContextRequest(query="remote", max_bytes=4096)
        first = await context.prepare_incremental(request)
        assert isinstance(first, ContextPack)
        assert first.items == ()
        assert b"".join(first.items) == b""

        sources = runtime.sources.for_scope(scope.scope_id)
        memory = runtime.memory.for_scope(scope.scope_id)
        await sources.capture(CaptureSource(source_id="turn-0", content="the remote works", metadata={}))
        await _flush(memory)
        second = await context.prepare_incremental(request, base_pack=first)
        assert isinstance(second, ContextPackDelta)
        assert apply_context_pack_delta(first, second).items != ()

        with pytest.raises(ContextPackError):
            await context.prepare_incremental(request, base_pack="not-a-pack")  # type: ignore[arg-type]
