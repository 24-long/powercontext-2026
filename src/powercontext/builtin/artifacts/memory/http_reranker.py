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

"""HTTP cross-encoder reranker adapter for Memory search."""

from __future__ import annotations

import json
import math

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import (
    MemoryRerankDecision,
    _validate_bounds,
)
from powercontext.builtin.inference.models import InferenceUsage


class HTTPMemoryReranker:
    """Score candidates via an external HTTP reranking service.

    Splits the candidate pool into batches of at most ``batch_size``,
    POSTs each batch to ``endpoint``, validates every response, and
    produces a single globally ordered selection from all collected scores.
    """

    policy_id: str

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Send batched scoring requests and return a globally sorted decision."""

        _validate_bounds(len(candidates), limit)

        global_scores: dict[int, float] = {}
        request_count = 0

        for batch_start in range(0, len(candidates), self._batch_size):
            batch_end = min(batch_start + self._batch_size, len(candidates))
            batch_ranks: set[int] = set()
            batch_payload: list[dict[str, object]] = []
            for idx in range(batch_start, batch_end):
                global_rank = idx + 1
                batch_ranks.add(global_rank)
                batch_payload.append({"rank": global_rank, "text": candidates[idx].text})

            response = await self._client.post(
                self._endpoint,
                json={"query": query, "candidates": batch_payload},
            )
            response.raise_for_status()
            request_count += 1

            batch_scores = _parse_batch_response(response, batch_ranks)
            global_scores.update(batch_scores)

        ranked = sorted(global_scores, key=lambda rank: (-global_scores[rank], rank))
        selected = tuple(ranked[:limit])

        return MemoryRerankDecision(
            selected_ranks=selected,
            usage=InferenceUsage(requests=request_count),
            discarded_rank_count=len(candidates) - len(selected),
        )


def _parse_batch_response(response: httpx.Response, expected_ranks: set[int]) -> dict[int, float]:
    """Validate one batch response and return the rank-to-score mapping."""

    try:
        payload = response.json()
    except Exception as error:
        raise ValueError("reranker response is not valid JSON") from error

    if not isinstance(payload, dict):
        raise ValueError("reranker response must be a JSON object")
    if set(payload.keys()) != {"scores"}:
        raise ValueError("reranker response must contain exactly the 'scores' field")

    scores_list = payload["scores"]
    if not isinstance(scores_list, list):
        raise ValueError("'scores' must be a JSON array")
    if len(scores_list) != len(expected_ranks):
        raise ValueError("response scores count does not match the batch size")

    result: dict[int, float] = {}
    for item in scores_list:
        if not isinstance(item, dict):
            raise ValueError("each score entry must be a JSON object")
        if set(item.keys()) != {"rank", "score"}:
            raise ValueError("each score entry must have exactly 'rank' and 'score'")

        rank = item["rank"]
        score = item["score"]

        if not isinstance(rank, int) or isinstance(rank, bool):
            raise ValueError("score rank must be an integer")
        if rank not in expected_ranks:
            raise ValueError(f"score rank {rank} is not in the current batch")
        if rank in result:
            raise ValueError(f"duplicate score rank {rank}")

        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("score value must be a number")
        if not math.isfinite(score):
            raise ValueError("score value must be finite")

        result[rank] = float(score)

    if set(result.keys()) != expected_ranks:
        raise ValueError("response scores do not cover all batch ranks")

    return result


__all__ = ["HTTPMemoryReranker"]
