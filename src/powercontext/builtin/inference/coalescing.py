# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Cross-call embedding request coalescing with a bounded LRU vector cache."""

from __future__ import annotations

import asyncio
from collections import OrderedDict

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage


class _Batch:
    """One dispatched underlying request and the waiters it serves."""

    __slots__ = ("futures", "texts", "usage")

    def __init__(self, texts: tuple[str, ...], futures: dict[str, asyncio.Future]) -> None:
        self.texts = texts
        self.futures = futures
        self.usage = InferenceUsage(requests=0)


class CoalescingEmbeddingModel:
    """Deduplicate texts across concurrent calls, bounding each provider request.

    Identical texts shared within one call, across the coalescing window, or
    with an in-flight request resolve from a single underlying batch. Successful
    vectors are cached with LRU eviction; failures are never cached and remain
    retryable, and cancelling one waiter leaves shared work and other waiters
    intact.
    """

    def __init__(self, model, *, max_batch_size=64, coalesce_ms=5.0, cache_size=1024):
        if type(max_batch_size) is not int or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if coalesce_ms < 0:
            raise ValueError("coalesce_ms must be non-negative")  # noqa: TRY003
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")  # noqa: TRY003
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = coalesce_ms / 1000.0
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._waiting: dict[str, asyncio.Future] = {}
        self._queue: dict[str, asyncio.Future] = {}
        self._tasks: set[asyncio.Task] = set()
        self._flush_handle: asyncio.TimerHandle | None = None

    @property
    def profile(self):
        return self._model.profile

    async def embed(self, texts):
        items = tuple(texts)
        if not items:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        sources: dict[str, EmbeddingVector | asyncio.Future] = {}
        for text in items:
            if text in sources:
                continue
            sources[text] = self._acquire(text)
        resolved: dict[str, EmbeddingVector] = {}
        waited: set[_Batch] = set()
        for text, source in sources.items():
            if asyncio.isfuture(source):
                # Shield so cancelling one waiter cannot cancel the shared batch.
                vector, batch = await asyncio.shield(source)
                resolved[text] = vector
                waited.add(batch)
            else:
                resolved[text] = source
        return EmbeddingResult(vectors=tuple(resolved[text] for text in items), usage=_merge_usages(waited))

    def _acquire(self, text: str) -> EmbeddingVector | asyncio.Future:
        if self._cache_size > 0 and text in self._cache:
            self._cache.move_to_end(text)
            return self._cache[text]
        waiting = self._waiting.get(text)
        if waiting is not None:
            return waiting
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._waiting[text] = future
        self._queue[text] = future
        if self._flush_handle is None:
            self._flush_handle = loop.call_later(self._coalesce_seconds, self._flush)
        return future

    def _flush(self) -> None:
        self._flush_handle = None
        if not self._queue:
            return
        queued = tuple(self._queue.items())
        self._queue.clear()
        loop = asyncio.get_running_loop()
        for start in range(0, len(queued), self._max_batch_size):
            group = queued[start : start + self._max_batch_size]
            batch = _Batch(tuple(text for text, _ in group), dict(group))
            task = loop.create_task(self._run_batch(batch))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run_batch(self, batch: _Batch) -> None:
        try:
            result = await self._model.embed(batch.texts)
        except Exception as exc:
            self._fail_batch(batch, exc)
            return
        vectors = result.vectors
        if len(vectors) != len(batch.texts):
            self._fail_batch(batch, ValueError("embedding returned an unexpected number of vectors"))
            return
        batch.usage = result.usage
        for text, vector in zip(batch.texts, vectors, strict=True):
            self._cache_vector(text, vector)
            future = batch.futures[text]
            if not future.done():
                future.set_result((vector, batch))
        for text in batch.texts:
            self._waiting.pop(text, None)

    def _fail_batch(self, batch: _Batch, exc: BaseException) -> None:
        for text, future in batch.futures.items():
            if not future.done():
                future.set_exception(exc)
            self._waiting.pop(text, None)

    def _cache_vector(self, text: str, vector: EmbeddingVector) -> None:
        if self._cache_size <= 0:
            return
        self._cache[text] = vector
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


def _merge_usages(batches: set[_Batch]) -> InferenceUsage:
    requests = 0
    input_tokens = None
    output_tokens = None
    for batch in batches:
        usage = batch.usage
        requests += usage.requests
        if usage.input_tokens is not None:
            input_tokens = (input_tokens or 0) + usage.input_tokens
        if usage.output_tokens is not None:
            output_tokens = (output_tokens or 0) + usage.output_tokens
    return InferenceUsage(requests=requests, input_tokens=input_tokens, output_tokens=output_tokens)


__all__ = ["CoalescingEmbeddingModel"]
