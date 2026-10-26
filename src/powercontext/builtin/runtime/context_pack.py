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

"""Context Pack: chunked, digest-checked context payloads with prefix-reuse deltas.

A :class:`ContextPack` is the complete wire representation of one prepared context:
ordered UTF-8 chunks plus a digest and the exact serialized byte count. A
:class:`ContextPackDelta` reuses a prefix of an already-delivered base pack and
carries only the appended suffix, so consecutive requests transmit far fewer bytes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

_PACK_SCHEMA = "powercontext.context-pack.v1"
_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
_DEFAULT_CHUNK_BYTES = 512
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ContextPackError(ValueError):
    """A Context Pack protocol or chunking validation failure."""


def _canonical_json(payload: dict[str, Any]) -> bytes:
    """UTF-8 JSON with sorted keys and no optional whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(value: Any) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError("invalid-items-array")
    decoded: list[bytes] = []
    for item in value:
        if not isinstance(item, str):
            raise ContextPackError("invalid-item-type")
        try:
            decoded.append(base64.b64decode(item, validate=True))
        except (binascii.Error, ValueError) as error:
            raise ContextPackError("invalid-base64") from error
    return tuple(decoded)


def _decode_json_object(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ContextPackError("invalid-json") from error
    if not isinstance(payload, dict):
        raise ContextPackError("invalid-json-object")
    return payload


def _validate_digest_field(value: Any) -> str:
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        raise ContextPackError("invalid-digest")
    return value


def _pack_digest(items: tuple[bytes, ...]) -> str:
    """SHA-256 over the canonical JSON of the pack's schema and items only."""
    unsigned = {"schema": _PACK_SCHEMA, "items": _encode_items(items)}
    return "sha256:" + hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _utf8_boundary(data: bytes, end: int) -> int:
    """Move ``end`` back to the start of the character it points into."""
    while end > 0 and (data[end] & 0xC0) == 0x80:
        end -= 1
    return end


def _append_utf8_chunks(chunks: list[bytes], piece: bytes, chunk_bytes: int) -> None:
    """Append ``piece`` as valid-UTF-8 chunks no larger than ``chunk_bytes``."""
    start = 0
    while start < len(piece):
        end = start + chunk_bytes
        if end < len(piece):
            boundary = _utf8_boundary(piece, end)
            if boundary == start:
                raise ContextPackError("unboundable-chunk")
            end = boundary
        chunks.append(piece[start:end])
        start = end


def _chunk_utf8(data: bytes, chunk_bytes: int) -> tuple[bytes, ...]:
    """Split UTF-8 bytes greedily at line and character boundaries.

    The greedy pass only looks at bytes before each boundary, so any content
    prefix produces the same chunk prefix: appending or extending later lines
    never reshuffles earlier chunks, which is what makes delta prefixes large.
    """
    chunks: list[bytes] = []
    for line in data.splitlines(keepends=True):
        _append_utf8_chunks(chunks, line, chunk_bytes)
    return tuple(chunks)


@dataclass(frozen=True)
class ContextPack:
    """A complete context payload: ordered chunks, wire digest, and exact size."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        """Build a complete pack from raw chunk bytes."""
        normalized = tuple(items)
        for item in normalized:
            if not isinstance(item, bytes):
                raise ContextPackError("invalid-pack-items")
        digest = _pack_digest(normalized)
        payload: dict[str, Any] = {
            "schema": _PACK_SCHEMA,
            "items": _encode_items(normalized),
            "digest": digest,
            "byte_count": 0,
        }
        # ``byte_count`` must equal the length of the payload that contains it,
        # so resolve the fixed point instead of guessing the digit count.
        byte_count = 0
        while True:
            payload["byte_count"] = byte_count
            data = _canonical_json(payload)
            if len(data) == byte_count:
                break
            byte_count = len(data)
        return cls(items=normalized, digest=digest, byte_count=byte_count)

    @classmethod
    def from_content(cls, content: str | bytes, *, chunk_bytes: int = _DEFAULT_CHUNK_BYTES) -> ContextPack:
        """Chunk UTF-8 text deterministically, preserving the original bytes.

        Every chunk is valid UTF-8 and no larger than ``chunk_bytes``.
        """
        if type(chunk_bytes) is not int or chunk_bytes < 1:
            raise ContextPackError("invalid-chunk-limit")
        if isinstance(content, str):
            data = content.encode("utf-8")
        elif isinstance(content, (bytes, bytearray)):
            data = bytes(content)
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ContextPackError("invalid-content-encoding") from error
        else:
            raise ContextPackError("invalid-content-type")
        return cls.build(_chunk_utf8(data, chunk_bytes))

    def to_bytes(self) -> bytes:
        """Serialize the pack with the canonical wire encoding."""
        payload: dict[str, Any] = {
            "schema": _PACK_SCHEMA,
            "items": _encode_items(self.items),
            "digest": self.digest,
            "byte_count": self.byte_count,
        }
        data = _canonical_json(payload)
        if len(data) != self.byte_count:
            # Rebuild the fixed point for directly constructed packs so the
            # serialized form always satisfies byte_count == len(to_bytes()).
            rebuilt = self.build(self.items)
            data = rebuilt.to_bytes()
        return data

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        """Decode and validate a complete pack from its wire encoding."""
        payload = _decode_json_object(data)
        if set(payload) != {"schema", "items", "digest", "byte_count"}:
            raise ContextPackError("invalid-pack-fields")
        if payload["schema"] != _PACK_SCHEMA:
            raise ContextPackError("unsupported-pack-schema")
        items = _decode_items(payload["items"])
        digest = _validate_digest_field(payload["digest"])
        if digest != _pack_digest(items):
            raise ContextPackError("pack-digest-mismatch")
        byte_count = payload["byte_count"]
        if type(byte_count) is not int or byte_count != len(data):
            raise ContextPackError("pack-byte-count-mismatch")
        return cls(items=items, digest=digest, byte_count=byte_count)


@dataclass(frozen=True)
class ContextPackDelta:
    """Prefix-reuse delta: keep ``prefix_items`` base chunks and append the rest."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        """Build a delta that rebuilds ``target`` from ``base`` with maximal prefix reuse."""
        prefix = 0
        for base_item, target_item in zip(base.items, target.items, strict=False):
            if base_item != target_item:
                break
            prefix += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=target.items[prefix:],
        )

    @property
    def byte_count(self) -> int:
        """Exact wire size of this delta; never serialized as a wire field."""
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        """Serialize the delta with the canonical wire encoding."""
        payload: dict[str, Any] = {
            "schema": _DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encode_items(tuple(self.appended)),
        }
        return _canonical_json(payload)

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        """Decode and validate a delta from its wire encoding."""
        payload = _decode_json_object(data)
        if set(payload) != {"schema", "base_digest", "target_digest", "prefix_items", "appended"}:
            raise ContextPackError("invalid-delta-fields")
        if payload["schema"] != _DELTA_SCHEMA:
            raise ContextPackError("unsupported-delta-schema")
        base_digest = _validate_digest_field(payload["base_digest"])
        target_digest = _validate_digest_field(payload["target_digest"])
        prefix = payload["prefix_items"]
        if type(prefix) is not int or prefix < 0:
            raise ContextPackError("invalid-delta-prefix")
        appended = _decode_items(payload["appended"])
        return cls(base_digest=base_digest, target_digest=target_digest, prefix_items=prefix, appended=appended)


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild the target pack from ``base`` and ``delta``, verifying every digest."""
    if not isinstance(base, ContextPack):
        raise ContextPackError("invalid-base-type")
    if not isinstance(delta, ContextPackDelta):
        raise ContextPackError("invalid-delta-type")
    if base.digest != delta.base_digest:
        raise ContextPackError("base-digest-mismatch")
    prefix = delta.prefix_items
    if type(prefix) is not int or prefix < 0 or prefix > len(base.items):
        raise ContextPackError("delta-prefix-bounds")
    target = ContextPack.build(base.items[:prefix] + tuple(delta.appended))
    if target.digest != delta.target_digest:
        raise ContextPackError("target-digest-mismatch")
    return target
