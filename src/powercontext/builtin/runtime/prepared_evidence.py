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

"""Query-driven evidence fragment selection inside the prepared-context byte budget.

Selection is fully offline and lexical: every candidate entry is split into
sentences, each sentence is scored against the query tokens (exact, shared
prefix, and adjacent bigram matches weighted by corpus IDF), and the best
sentences are packed into the envelope until the complete rendered UTF-8
output would exceed ``max_bytes``. Every returned fragment is an exact
``entry.text[start:end]`` slice, so citations always resolve to the original
entry.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from powercontext.artifacts import ArtifactAddress, ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

_BEGIN_MARKER = "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1"
_END_MARKER = "END_POWERCONTEXT_PREPARED_CONTEXT_V1"
_TOKEN_PATTERN = re.compile(r"[a-z]+|\d+")
_SENTENCE_BREAK_PATTERN = re.compile(r"(?<=[.!?])\s+")
_PREFIX_MATCH_LENGTH = 5
_PREFIX_MATCH_WEIGHT = 0.6
_CONTAINMENT_MATCH_LENGTH = 4
_CONTAINMENT_MATCH_WEIGHT = 0.7
_BIGRAM_MATCH_WEIGHT = 0.25
_ENTRY_PRIOR_WEIGHT = 4.0
_ADJACENT_BONUS = 0.3
_ADJACENT_PASSES = 2
_MERGE_GAP_CHARS = 400
_CHARGRAM_WEIGHT = 4.0
_MIN_TRIMMED_CONTENT_BYTES = 32
_DUPLICATE_JACCARD = 0.85


@dataclass(frozen=True)
class MemoryEntryAddress:
    """Identify one exact Memory entry version across Scope boundaries."""

    memory: ArtifactAddress
    entry_id: str
    entry_version_id: str


@dataclass(frozen=True)
class EvidenceFragment:
    """One exact original-text span selected for the prepared context."""

    address: MemoryEntryAddress
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class EvidenceSelection:
    """Complete output of one evidence-selection pass."""

    fragments: tuple[EvidenceFragment, ...]
    origins: tuple[MemoryCitation | MemoryEntryAddress, ...]
    content: str | None


class MemoryCandidatesView(Protocol):
    """Structural view of ``PreparedMemoryCandidates`` (avoids an import cycle)."""

    scope_id: str
    memory_ref: ArtifactRef | None
    hits: tuple[MemoryHit, ...]


@dataclass(frozen=True)
class _CandidateGroup:
    scope_id: str
    memory_ref: ArtifactRef | None
    hits: tuple[MemoryHit, ...]


@dataclass(frozen=True)
class _Sentence:
    start: int
    end: int
    text: str
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class _RankedSentence:
    group_index: int
    hit_index: int
    sentence_index: int
    sentence: _Sentence
    score: float


def _tokenize(text: str) -> tuple[str, ...]:
    """Lowercase and extract words and numbers, mirroring the scoring tokenizer."""
    return tuple(_TOKEN_PATTERN.findall(text.lower()))


def _char_ngrams(text: str, n: int) -> set[str]:
    """Character n-grams of lowercased text, for paraphrase-tolerant overlap."""
    lower = text.lower()
    return {lower[index : index + n] for index in range(len(lower) - n + 1)}


def _split_sentences(text: str) -> tuple[_Sentence, ...]:
    """Split text into sentences with char offsets, keeping the final punctuation."""
    sentences: list[_Sentence] = []
    start = 0
    for match in _SENTENCE_BREAK_PATTERN.finditer(text):
        end = match.start()
        piece = text[start:end]
        if piece.strip():
            sentences.append(_Sentence(start=start, end=end, text=piece, tokens=_tokenize(piece)))
        start = match.end()
    tail = text[start:]
    if tail.strip():
        sentences.append(_Sentence(start=start, end=len(text), text=tail, tokens=_tokenize(tail)))
    return tuple(sentences)


def _collect_entries(
    memory_candidates: Sequence[MemoryCandidatesView],
) -> tuple[tuple[_CandidateGroup, ...], dict[tuple[int, int], tuple[ArtifactAddress, MemoryHit]]]:
    """Validate candidate groups and flatten them into (group, hit) entry records."""
    groups = tuple(
        _CandidateGroup(scope_id=candidates.scope_id, memory_ref=candidates.memory_ref, hits=candidates.hits)
        for candidates in memory_candidates
    )
    entries: dict[tuple[int, int], tuple[ArtifactAddress, MemoryHit]] = {}
    for group_index, group in enumerate(groups):
        if group.hits and group.memory_ref is None:
            raise PreparedContextInvariantError("memory-ref-missing")
        seen: set[tuple[str, str]] = set()
        for hit_index, hit in enumerate(group.hits):
            if hit.memory_ref != group.memory_ref:
                raise PreparedContextInvariantError("memory-ref-mismatch")
            key = (hit.entry_id, hit.entry_version_id)
            if key in seen:
                continue
            seen.add(key)
            if not hit.entry_id.strip() or not hit.entry_version_id.strip() or not hit.text.strip():
                continue
            address = ArtifactAddress(scope_id=group.scope_id, artifact=group.memory_ref)
            entries[(group_index, hit_index)] = (address, hit)
    return groups, entries


def _score_sentence(
    sentence: _Sentence,
    query_set: set[str],
    query_bigrams: set[tuple[str, str]],
    idf: dict[str, float],
    query_char3: set[str],
    query_char4: set[str],
) -> float:
    """Score one sentence by lexical overlap with the query."""
    tokens = sentence.tokens
    unique = set(tokens)
    score = sum(idf[token] for token in unique if token in query_set)
    for token in unique:
        if token in query_set:
            continue
        if len(token) >= _PREFIX_MATCH_LENGTH and any(
            len(query_token) >= _PREFIX_MATCH_LENGTH
            and query_token[:_PREFIX_MATCH_LENGTH] == token[:_PREFIX_MATCH_LENGTH]
            for query_token in query_set
        ):
            score += _PREFIX_MATCH_WEIGHT * idf[token]
            continue
        if len(token) >= _CONTAINMENT_MATCH_LENGTH and any(
            len(query_token) >= _CONTAINMENT_MATCH_LENGTH and (token in query_token or query_token in token)
            for query_token in query_set
        ):
            score += _CONTAINMENT_MATCH_WEIGHT * idf[token]
    for index in range(len(tokens) - 1):
        pair = (tokens[index], tokens[index + 1])
        if pair in query_bigrams:
            score += _BIGRAM_MATCH_WEIGHT * (idf[pair[0]] + idf[pair[1]])
    if _CHARGRAM_WEIGHT:
        char3 = _char_ngrams(sentence.text, 3)
        if char3 and query_char3:
            score += _CHARGRAM_WEIGHT * (2.0 * len(query_char3 & char3) / (len(query_char3) + len(char3)))
        char4 = _char_ngrams(sentence.text, 4)
        if char4 and query_char4:
            score += _CHARGRAM_WEIGHT * (2.0 * len(query_char4 & char4) / (len(query_char4) + len(char4)))
    return score


def _render_items(items: list[dict[str, Any]]) -> str:
    envelope = {"trust": "untrusted_history", "items": items}
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return "\n\n".join((TRUST_POLICY, f"{_BEGIN_MARKER}\n{encoded}\n{_END_MARKER}"))


def _rendered_bytes(items: list[dict[str, Any]]) -> int:
    return len(_render_items(items).encode("utf-8"))


def _citation_item(address: ArtifactAddress, hit: MemoryHit, start: int, end: int, content: str) -> dict[str, Any]:
    citation = {
        "scope_id": address.scope_id,
        "memory_ref": address.artifact.model_dump(mode="json"),
        "entry_id": hit.entry_id,
        "entry_version_id": hit.entry_version_id,
    }
    return {"citation": citation, "content": content, "start": start, "end": end}


def _try_append(
    items: list[dict[str, Any]],
    item: dict[str, Any],
    *,
    max_bytes: int,
) -> dict[str, Any] | None:
    if _rendered_bytes([*items, item]) <= max_bytes:
        items.append(item)
        return item
    return None


def _trim_content(text: str, byte_budget: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_budget:
        return text
    return encoded[:byte_budget].decode("utf-8", errors="ignore")


def _append_trimmed(
    items: list[dict[str, Any]],
    *,
    address: ArtifactAddress,
    hit: MemoryHit,
    sentence: _Sentence,
    max_bytes: int,
) -> dict[str, Any] | None:
    """Append the longest prefix of ``sentence`` that fits the remaining budget."""
    full = _citation_item(address, hit, sentence.start, sentence.end, sentence.text)
    current = _rendered_bytes(items)
    overhead = _rendered_bytes([*items, full]) - current - len(sentence.text.encode("utf-8"))
    content_budget = max_bytes - current - overhead
    if content_budget < _MIN_TRIMMED_CONTENT_BYTES:
        return None
    content = _trim_content(sentence.text, content_budget)
    if not content.strip():
        return None
    item = _citation_item(address, hit, sentence.start, sentence.start + len(content), content)
    items.append(item)
    return item


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


def _rank_entries(
    entries: dict[tuple[int, int], tuple[ArtifactAddress, MemoryHit]],
    sentences_by_entry: dict[tuple[int, int], tuple[_Sentence, ...]],
    *,
    query_set: set[str],
    query_bigrams: set[tuple[str, str]],
    query_char3: set[str],
    query_char4: set[str],
    idf: dict[str, float],
) -> dict[tuple[int, int], list[_RankedSentence]]:
    """Score every sentence and propagate scores to adjacent sentences.

    The per-entry prior is derived from each entry's strongest lexical
    sentence rather than the retriever score: a document whose best sentence
    concentrates the query vocabulary is the likely evidence source, whereas
    whole-document retrieval overlap rewards distractors that sprinkle query
    words across many sentences. Entries with no lexical overlap get no boost.
    """
    lexical_by_entry: dict[tuple[int, int], list[float]] = {
        key: [_score_sentence(sentence, query_set, query_bigrams, idf, query_char3, query_char4) for sentence in sentences]
        for key, sentences in sentences_by_entry.items()
    }
    max_lexical = {key: max(scores, default=0.0) for key, scores in lexical_by_entry.items()}
    overall_max = max(max_lexical.values(), default=0.0)
    entry_prior = {key: (max_lexical[key] / overall_max if overall_max > 0.0 else 1.0) for key in entries}
    ranked_by_entry: dict[tuple[int, int], list[_RankedSentence]] = {}
    for key, _entry in entries.items():
        entry_ranked = []
        for sentence_index, sentence in enumerate(sentences_by_entry[key]):
            lexical = lexical_by_entry[key][sentence_index]
            score = lexical + (_ENTRY_PRIOR_WEIGHT * entry_prior[key] if lexical > 0.0 else 0.0)
            entry_ranked.append(
                _RankedSentence(
                    group_index=key[0],
                    hit_index=key[1],
                    sentence_index=sentence_index,
                    sentence=sentence,
                    score=score,
                )
            )
        # Adjacency propagation: sentences next to a query-relevant one inherit
        # part of its score, so evidence regions with no lexical overlap (e.g.
        # paraphrased claims) still enter the positive queue.
        for _ in range(_ADJACENT_PASSES):
            entry_ranked = [
                replace(
                    item,
                    score=item.score
                    + _ADJACENT_BONUS
                    * (
                        (entry_ranked[index - 1].score if index > 0 else 0.0)
                        + (entry_ranked[index + 1].score if index + 1 < len(entry_ranked) else 0.0)
                    ),
                )
                for index, item in enumerate(entry_ranked)
            ]
        ranked_by_entry[key] = entry_ranked
    return ranked_by_entry


def _build_queue(
    ranked_by_entry: dict[tuple[int, int], list[_RankedSentence]],
    entries: dict[tuple[int, int], tuple[ArtifactAddress, MemoryHit]],
) -> list[_RankedSentence]:
    """Order candidates: strongest sentences first, zero-scored fill last."""
    positive_by_entry: dict[tuple[int, int], list[_RankedSentence]] = {}
    zeros_by_entry: dict[tuple[int, int], list[_RankedSentence]] = {}
    for key, entry_ranked in ranked_by_entry.items():
        positive = [item for item in entry_ranked if item.score > 0.0]
        positive.sort(key=lambda item: (-item.score, item.sentence_index))
        positive_by_entry[key] = positive
        zeros_by_entry[key] = [item for item in entry_ranked if item.score <= 0.0]
    # Rank the strongest sentences first regardless of entry, so a single
    # document with exact query matches wins the budget instead of being
    # diluted by round-robin interleaving across distractors.
    queue: list[_RankedSentence] = []
    for group in positive_by_entry.values():
        queue.extend(group)
    queue.sort(key=lambda item: (-item.score, item.group_index, item.hit_index, item.sentence_index))
    # Zero-scored sentences follow so the budget is filled with original
    # evidence instead of being left unused, interleaved across entries so no
    # single document monopolizes the remaining space.
    entry_mass = {key: sum(item.score for item in group) for key, group in positive_by_entry.items()}
    ordered_keys = sorted(entries.keys(), key=lambda key: -entry_mass.get(key, 0.0))
    max_zero = max((len(zero_group) for zero_group in zeros_by_entry.values()), default=0)
    for index in range(max_zero):
        for key in ordered_keys:
            zero_group = zeros_by_entry[key]
            if index < len(zero_group):
                queue.append(zero_group[index])
    return queue


def _pack_fragments(  # noqa: C901 - greedy packing branches: extend-or-append, trim, and fallback.
    queue: list[_RankedSentence],
    entries: dict[tuple[int, int], tuple[ArtifactAddress, MemoryHit]],
    groups: tuple[_CandidateGroup, ...],
    sentences_by_entry: dict[tuple[int, int], tuple[_Sentence, ...]],
    *,
    current_scope_id: str | None,
    max_bytes: int,
) -> tuple[
    list[dict[str, Any]],
    list[EvidenceFragment],
    list[MemoryCitation | MemoryEntryAddress],
]:
    """Pack the ordered candidates into budget-fitting envelope items."""
    items: list[dict[str, Any]] = []
    fragments: list[EvidenceFragment] = []
    origins: list[MemoryCitation | MemoryEntryAddress] = []
    chosen_texts: set[str] = set()
    chosen_token_sets: list[set[str]] = []
    item_indices_by_entry: dict[tuple[int, int], list[int]] = {}

    def try_extend(candidate: _RankedSentence, start: int, end: int) -> bool:
        key = (candidate.group_index, candidate.hit_index)
        address, hit = entries[key]
        near: list[tuple[int, int]] = []
        for index in item_indices_by_entry.get(key, ()):
            previous = items[index]
            if start - previous["end"] <= _MERGE_GAP_CHARS:
                near.append((index, start - previous["end"]))
        if not near:
            return False
        # Out-of-order picks coalesce into the nearest existing item so each
        # entry grows one contiguous block instead of scattered fragments.
        near.sort(key=lambda pair: pair[1])
        merged = near[0][0]
        previous = items[merged]
        new_start = min(previous["start"], start)
        new_end = max(previous["end"], end)
        content = hit.text[new_start:new_end]
        extended = _citation_item(address, hit, new_start, new_end, content)
        if _rendered_bytes([*items[:merged], extended, *items[merged + 1 :]]) > max_bytes:
            return False
        items[merged] = extended
        fragments[merged] = EvidenceFragment(
            address=fragments[merged].address,
            start=new_start,
            end=new_end,
            text=content,
        )
        chosen_texts.discard(previous["content"])
        chosen_texts.add(content)
        chosen_token_sets[merged] = set(_tokenize(content))
        return True

    def append_candidate(candidate: _RankedSentence, start: int, end: int) -> None:
        key = (candidate.group_index, candidate.hit_index)
        if try_extend(candidate, start, end):
            return
        address, hit = entries[key]
        group = groups[candidate.group_index]
        text = hit.text[start:end]
        rendered = _citation_item(address, hit, start, end, text)
        accepted = _try_append(items, rendered, max_bytes=max_bytes)
        if accepted is None:
            accepted = _append_trimmed(
                items, address=address, hit=hit, sentence=candidate.sentence, max_bytes=max_bytes
            )
        if accepted is None:
            return
        content = accepted["content"]
        start = accepted["start"]
        fragment = EvidenceFragment(
            address=MemoryEntryAddress(
                memory=address,
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            ),
            start=start,
            end=accepted["end"],
            text=content,
        )
        fragments.append(fragment)
        if group.scope_id == current_scope_id:
            origins.append(
                MemoryCitation(
                    memory_ref=group.memory_ref,
                    entry_id=hit.entry_id,
                    entry_version_id=hit.entry_version_id,
                )
            )
        else:
            origins.append(fragment.address)
        chosen_texts.add(content)
        chosen_token_sets.append(set(_tokenize(content)))
        item_indices_by_entry.setdefault(key, []).append(len(items) - 1)

    for candidate in queue:
        sentence = candidate.sentence
        tokens = set(sentence.tokens)
        duplicate = sentence.text in chosen_texts or any(
            _jaccard(tokens, chosen) > _DUPLICATE_JACCARD for chosen in chosen_token_sets
        )
        if duplicate:
            continue
        append_candidate(candidate, sentence.start, sentence.end)

    if not fragments:
        # No lexical overlap with the query: fall back to spreading across the
        # candidate documents so the context still carries original evidence.
        max_sentences = max((len(sentences) for sentences in sentences_by_entry.values()), default=0)
        for sentence_index in range(max_sentences):
            for key, _entry in entries.items():
                sentences = sentences_by_entry[key]
                if sentence_index >= len(sentences):
                    continue
                sentence = sentences[sentence_index]
                append_candidate(
                    _RankedSentence(
                        group_index=key[0],
                        hit_index=key[1],
                        sentence_index=sentence_index,
                        sentence=sentence,
                        score=0.0,
                    ),
                    sentence.start,
                    sentence.end,
                )
                if _rendered_bytes(items) >= max_bytes:
                    break
            if _rendered_bytes(items) >= max_bytes:
                break

    return items, fragments, origins


def select_evidence(
    *,
    query: str,
    max_bytes: int,
    current_scope_id: str | None,
    memory_candidates: Sequence[MemoryCandidatesView],
) -> EvidenceSelection:
    """Select, dedupe, and render query-relevant fragments within ``max_bytes``."""
    groups, entries = _collect_entries(memory_candidates)
    sentences_by_entry: dict[tuple[int, int], tuple[_Sentence, ...]] = {}
    idf_counter: Counter[str] = Counter()
    for key, (_address, hit) in entries.items():
        sentences = _split_sentences(hit.text)
        sentences_by_entry[key] = sentences
        for sentence in sentences:
            idf_counter.update(set(sentence.tokens))
    total_sentences = sum(len(sentences) for sentences in sentences_by_entry.values())
    idf = {token: math.log((total_sentences + 1) / (count + 1)) + 1.0 for token, count in idf_counter.items()}
    query_tokens = _tokenize(query)
    query_set = set(query_tokens)
    query_bigrams = {(query_tokens[i], query_tokens[i + 1]) for i in range(len(query_tokens) - 1)}
    query_char3 = _char_ngrams(query, 3)
    query_char4 = _char_ngrams(query, 4)

    ranked_by_entry = _rank_entries(
        entries,
        sentences_by_entry,
        query_set=query_set,
        query_bigrams=query_bigrams,
        query_char3=query_char3,
        query_char4=query_char4,
        idf=idf,
    )
    queue = _build_queue(ranked_by_entry, entries)
    items, fragments, origins = _pack_fragments(
        queue,
        entries,
        groups,
        sentences_by_entry,
        current_scope_id=current_scope_id,
        max_bytes=max_bytes,
    )
    content = None if not items else _render_items(items)
    return EvidenceSelection(fragments=tuple(fragments), origins=tuple(origins), content=content)
