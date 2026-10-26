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

import base64
import hashlib
import json

import pytest

from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
)

_PACK_SCHEMA = "powercontext.context-pack.v1"
_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"


def _unsigned(items: tuple[bytes, ...]) -> bytes:
    payload = {"schema": _PACK_SCHEMA, "items": [base64.b64encode(item).decode("ascii") for item in items]}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def test_pack_wire_matches_independent_oracle() -> None:
    items = (b"alpha\n", "海洋".encode(), b"")
    pack = ContextPack.build(items)
    payload = json.loads(pack.to_bytes())
    assert payload == {
        "schema": _PACK_SCHEMA,
        "items": [base64.b64encode(item).decode("ascii") for item in items],
        "digest": "sha256:" + hashlib.sha256(_unsigned(items)).hexdigest(),
        "byte_count": len(pack.to_bytes()),
    }
    assert set(payload) == {"schema", "items", "digest", "byte_count"}
    assert pack.to_bytes() == json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert pack.digest == payload["digest"]
    assert pack.byte_count == len(pack.to_bytes())


def test_pack_byte_count_is_checked_independently() -> None:
    first = ContextPack.build((b"alpha",))
    payload = json.loads(first.to_bytes())
    payload["byte_count"] = 1
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def test_pack_wire_has_no_optional_whitespace_and_sorted_keys() -> None:
    pack = ContextPack.build((b"a", b"bb", b"ccc"))
    raw = pack.to_bytes()
    assert raw.decode("utf-8") == json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
    assert b" " not in raw and b"\t" not in raw and b"\n" not in raw


def test_pack_rejects_invalid_inputs() -> None:
    pack = ContextPack.build((b"alpha", b"beta"))
    payload = json.loads(pack.to_bytes())
    for field in payload:
        incomplete = {key: value for key, value in payload.items() if key != field}
        with pytest.raises(ContextPackError):
            ContextPack.from_bytes(json.dumps(incomplete).encode("utf-8"))
    for extra in ({"extra": 1}, {"items": "not-an-array"}):
        with pytest.raises(ContextPackError):
            ContextPack.from_bytes(json.dumps({**payload, **extra}).encode("utf-8"))
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps({**payload, "schema": "powercontext.context-pack.v2"}).encode("utf-8"))
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps({**payload, "items": ["a", "!!!", "b"]}).encode("utf-8"))
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps({**payload, "items": ["a", 5]}).encode("utf-8"))
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps({**payload, "digest": "not-a-digest"}).encode("utf-8"))
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps({**payload, "digest": "sha256:" + "f" * 63}).encode("utf-8"))
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(pack.to_bytes().replace(pack.digest.encode(), b"sha256:" + b"0" * 64))
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps({**payload, "byte_count": True}).encode("utf-8"))
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps({**payload, "byte_count": "123"}).encode("utf-8"))
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(b"\xff\xfe not json")
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(b"[1, 2]")


def test_pack_build_rejects_non_bytes_items() -> None:
    with pytest.raises(ContextPackError):
        ContextPack.build((b"ok", "text"))  # type: ignore[arg-type]


def test_from_content_chunks_are_bounded_valid_and_deterministic() -> None:
    for chunk_bytes in (1, 2):
        text = "line one\nline two"
        pack = ContextPack.from_content(text, chunk_bytes=chunk_bytes)
        assert b"".join(pack.items).decode("utf-8") == text
        assert all(len(item) <= chunk_bytes for item in pack.items)
    text = "line one\n海洋 ocean\nline three"
    for chunk_bytes in (3, 7, 64):
        pack = ContextPack.from_content(text, chunk_bytes=chunk_bytes)
        assert b"".join(pack.items).decode("utf-8") == text
        assert all(len(item) <= chunk_bytes for item in pack.items)
        assert all(item.decode("utf-8") for item in pack.items)
    assert ContextPack.from_content(text).to_bytes() == ContextPack.from_content(text).to_bytes()
    assert ContextPack.from_content(text.encode("utf-8")).to_bytes() == ContextPack.from_content(text).to_bytes()


def test_from_content_preserves_line_endings() -> None:
    text = "first\r\nsecond\rthird\n"
    pack = ContextPack.from_content(text, chunk_bytes=512)
    assert b"".join(pack.items).decode("utf-8") == text


def test_from_content_rejects_unboundable_and_invalid_inputs() -> None:
    with pytest.raises(ContextPackError):
        ContextPack.from_content("海", chunk_bytes=2)
    with pytest.raises(ContextPackError):
        ContextPack.from_content(b"\xff\xfe\xfd", chunk_bytes=512)
    with pytest.raises(ContextPackError):
        ContextPack.from_content("text", chunk_bytes=0)
    with pytest.raises(ContextPackError):
        ContextPack.from_content("text", chunk_bytes=-4)
    with pytest.raises(ContextPackError):
        ContextPack.from_content("text", chunk_bytes=True)  # type: ignore[arg-type]
    with pytest.raises(ContextPackError):
        ContextPack.from_content(12345)  # type: ignore[arg-type]


def test_empty_pack_round_trip_and_digest() -> None:
    empty = ContextPack.build(())
    assert ContextPack.from_bytes(empty.to_bytes()) == empty
    assert empty.items == ()
    assert empty.digest == "sha256:" + hashlib.sha256(_unsigned(())).hexdigest()
    assert ContextPack.from_content("").to_bytes() == empty.to_bytes()


def test_delta_wire_contract() -> None:
    base = ContextPack.build((b"alpha", b"beta"))
    target = ContextPack.build((b"alpha", b"beta", b"gamma"))
    delta = ContextPackDelta.between(base, target)
    payload = json.loads(delta.to_bytes())
    assert set(payload) == {"schema", "base_digest", "target_digest", "prefix_items", "appended"}
    assert payload["schema"] == _DELTA_SCHEMA
    assert payload["base_digest"] == base.digest
    assert payload["target_digest"] == target.digest
    assert type(payload["prefix_items"]) is int
    assert payload["prefix_items"] == 2
    assert tuple(base64.b64decode(value) for value in payload["appended"]) == (b"gamma",)
    assert delta.byte_count == len(delta.to_bytes())
    assert "byte_count" not in payload


def test_delta_between_uses_longest_common_prefix() -> None:
    base = ContextPack.build((b"a", b"b", b"c", b"d"))
    assert ContextPackDelta.between(base, ContextPack.build((b"a", b"b", b"x"))).prefix_items == 2
    assert ContextPackDelta.between(base, ContextPack.build((b"a", b"b", b"c", b"d"))).prefix_items == 4
    assert ContextPackDelta.between(base, ContextPack.build((b"a", b"b", b"c", b"d", b"e"))).prefix_items == 4
    assert ContextPackDelta.between(base, ContextPack.build((b"x",))).prefix_items == 0
    assert ContextPackDelta.between(base, ContextPack.build(())).prefix_items == 0
    assert ContextPackDelta.between(base, base).appended == ()


def test_apply_delta_rebuilds_after_full_serialization() -> None:
    base = ContextPack.build((b"a", b"b", b"old"))
    for target_items in ((b"a", b"b", b"new", b"tail"), (b"new", b"b", b"old"), (b"a", b"new", b"old"), ()):
        target = ContextPack.build(target_items)
        delta = ContextPackDelta.between(base, target)
        rebuilt = apply_context_pack_delta(
            ContextPack.from_bytes(base.to_bytes()), ContextPackDelta.from_bytes(delta.to_bytes())
        )
        assert rebuilt.to_bytes() == target.to_bytes()


def test_apply_delta_accepts_any_valid_prefix() -> None:
    base = ContextPack.build((b"a", b"b", b"old"))
    target = ContextPack.build((b"a", b"b", b"new"))
    full = ContextPackDelta.between(base, target)
    payload = json.loads(full.to_bytes())
    for prefix in (0, 1, 2):
        payload["prefix_items"] = prefix
        payload["appended"] = [base64.b64encode(item).decode("ascii") for item in target.items[prefix:]]
        delta = ContextPackDelta.from_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        assert apply_context_pack_delta(base, delta).to_bytes() == target.to_bytes()


def test_delta_rejects_non_integer_prefix_on_wire() -> None:
    base = ContextPack.build((b"alpha",))
    delta = ContextPackDelta.between(base, ContextPack.build((b"alpha", b"beta")))
    payload = json.loads(delta.to_bytes())
    for prefix in (True, 1.5, -1, "1", None):
        payload["prefix_items"] = prefix
        with pytest.raises(ContextPackError):
            ContextPackDelta.from_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def test_delta_wire_validation() -> None:
    base = ContextPack.build((b"alpha",))
    target = ContextPack.build((b"alpha", b"beta"))
    delta = ContextPackDelta.between(base, target)
    payload = json.loads(delta.to_bytes())
    for field in payload:
        incomplete = {key: value for key, value in payload.items() if key != field}
        with pytest.raises(ContextPackError):
            ContextPackDelta.from_bytes(json.dumps(incomplete).encode("utf-8"))
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(json.dumps({**payload, "extra": 1}).encode("utf-8"))
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(json.dumps({**payload, "schema": "powercontext.context-pack.v1"}).encode("utf-8"))
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(json.dumps({**payload, "appended": ["!!!"]}).encode("utf-8"))
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(json.dumps({**payload, "base_digest": "sha256:" + "e" * 63}).encode("utf-8"))
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(json.dumps({**payload, "target_digest": 7}).encode("utf-8"))


def test_apply_delta_rejects_wrong_base_and_bounds() -> None:
    base = ContextPack.build((b"one",))
    delta = ContextPackDelta.between(base, ContextPack.build((b"one", b"two")))
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(ContextPack.build((b"other",)), delta)
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(ContextPack.build(()), delta)
    empty = ContextPack.build(())
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(empty, ContextPackDelta(empty.digest, "sha256:" + "0" * 64, 1, ()))
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(empty, ContextPackDelta(empty.digest, "sha256:" + "0" * 64, 0, (b"x",)))
    with pytest.raises(ContextPackError):
        apply_context_pack_delta("not-a-pack", delta)  # type: ignore[arg-type]
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, "not-a-delta")  # type: ignore[arg-type]
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, ContextPackDelta(base.digest, "sha256:" + "0" * 64, 0, ()))


def test_chunking_is_prefix_stable_for_appends() -> None:
    head = "meeting note\nsecond line\n" + "x" * 900 + "\n"
    base = ContextPack.from_content(head)
    for suffix in ("appended tail\n", "more\nlines\nhere"):
        target = ContextPack.from_content(head + suffix)
        delta = ContextPackDelta.between(base, target)
        assert delta.prefix_items == len(base.items)
        assert apply_context_pack_delta(base, delta).to_bytes() == target.to_bytes()
