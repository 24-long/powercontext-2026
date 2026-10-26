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

"""Pi session branch capture: raw-event parsing, text projection, incremental ingestion.

A Pi session file is a UTF-8 JSONL document. The first record is the session
header; every later record is a graph node identified by ``id`` and linked
through ``parentId``. The selected leaf and its ancestors form one history
path. Only that path's visible events become text evidence; control nodes keep
their place in the graph but produce no evidence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
)
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_SESSION_CONNECTOR_VERSION = "1"

_SESSION_EVENT_TYPE = "session"
_SESSION_VERSION = 3


class PiSessionInput(BaseModel):
    """Raw Pi session content and the optional selected leaf node."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event on the selected branch, with its complete raw record."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


class PiEventSource(Source):
    """CAPTURED Source carrying one immutable Pi event."""

    materialization: SourceMaterialization = SourceMaterialization.CAPTURED
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Return the stable, bounded Source identity for one session event."""

    digest = hashlib.sha256(f"{session_id}\x00{event_id}".encode("utf-8")).hexdigest()
    return f"{PI_EVENT_SOURCE_NAME}:{digest}"


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    return value


def _record_hash(record: dict[str, Any]) -> str:
    encoded = json.dumps(_canonicalize(record), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _join_text_blocks(content: Any) -> str:
    """Extract visible text from a string or a list of content blocks."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _render_tool_call(block: dict[str, Any]) -> str:
    return f"[tool_call {block.get('name', '')} {block.get('id', '')}] {_compact_json(block.get('arguments'))}"


def _message_body(content: Any) -> str:
    """Render visible message content: string, text blocks, or toolCall blocks."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif block_type == "toolCall":
                parts.append(_render_tool_call(block))
        return "\n".join(parts)
    return ""


def _message_body_for_role(message: dict[str, Any]) -> str:
    role = message.get("role")
    if role == "toolResult":
        tool_name = message.get("toolName", "")
        tool_call_id = message.get("toolCallId", "")
        header = f"[tool_result {tool_name} {tool_call_id}]"
        text = _join_text_blocks(message.get("content"))
        return f"{header} {text}" if text else header
    if role == "bashExecution":
        command = message.get("command", "")
        output = message.get("output", "")
        return f"$ {command}\n{output}"
    return _message_body(message.get("content"))


def _event_for_record(record: dict[str, Any], session_id: str) -> PiEventInput | None:
    event_type = record.get("type")
    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        body = _message_body_for_role(message)
    elif event_type in {"compaction", "branch_summary"}:
        role = event_type
        summary = record.get("summary")
        body = summary if isinstance(summary, str) else ""
    elif event_type == "custom_message":
        role = "custom"
        body = _join_text_blocks(record.get("content"))
    else:
        return None

    if not body:
        return None
    return PiEventInput(
        session_id=session_id,
        event_id=record["id"],
        parent_id=record.get("parentId"),
        event_type=event_type,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _validate_header(record: Any) -> None:
    if not isinstance(record, dict):
        raise ValueError("Pi session must begin with a session header object")
    if record.get("type") != _SESSION_EVENT_TYPE:
        raise ValueError("Pi session must begin with a session header record")
    if record.get("version") != _SESSION_VERSION:
        raise ValueError("Pi session header must declare version 3")
    session_id = record.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Pi session header must declare a non-empty id")


def _validate_node(record: Any, seen: set[str]) -> None:
    if not isinstance(record, dict):
        raise ValueError("Pi session event must be an object")
    node_id = record.get("id")
    if not isinstance(node_id, str) or not node_id:
        raise ValueError("Pi session event must declare a non-empty id")
    if node_id in seen:
        raise ValueError("Pi session event ids must be unique")
    if "parentId" not in record:
        raise ValueError("Pi session event must declare a parentId")
    parent = record.get("parentId")
    if parent is not None and (not isinstance(parent, str) or parent not in seen):
        raise ValueError("Pi session event parent must reference an earlier node")


def parse_pi_session(session: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Parse a Pi session file and return the selected branch's visible events."""

    try:
        text = session.content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session is not valid UTF-8") from error

    records: list[Any] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except (json.JSONDecodeError, ValueError) as error:
            raise ValueError("Pi session contains invalid JSON") from error

    if not records:
        raise ValueError("Pi session is empty")

    _validate_header(records[0])
    session_id = records[0]["id"]

    nodes = records[1:]
    if not nodes:
        raise ValueError("Pi session has no events")

    by_id: dict[str, Any] = {}
    seen: set[str] = set()
    for record in nodes:
        _validate_node(record, seen)
        by_id[record["id"]] = record
        seen.add(record["id"])

    if session.leaf_id is None:
        leaf = nodes[-1]
    else:
        leaf = by_id.get(session.leaf_id)
        if leaf is None:
            raise ValueError("Pi session leaf was not found")

    chain: list[Any] = []
    current: Any = leaf
    while current is not None:
        chain.append(current)
        parent = current.get("parentId")
        current = by_id.get(parent) if parent is not None else None
    chain.reverse()

    events = []
    for record in chain:
        event = _event_for_record(record, session_id)
        if event is not None:
            events.append(event)
    return tuple(events)


class _PiEventAdapter:
    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        return PiEventSource(
            name=pi_event_source_id(value.session_id, value.event_id),
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class _PiEventTextEvidence:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class = TextEvidence

    def project(self, source: PiEventSource, /) -> dict[str, Any]:
        return {
            "source_type": PI_EVENT_SOURCE_NAME,
            "source_id": source.name,
            "content": source.content,
            "metadata": {
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        }


PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    adapter=_PiEventAdapter(),
    version="1",
    projections=(_PiEventTextEvidence(),),
)


class PiSessionConnector:
    """Incrementally import the selected branch of one Pi session file."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_SESSION_CONNECTOR_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        events = parse_pi_session(PiSessionInput(content=self._path.read_bytes(), leaf_id=self._leaf_id))
        imported = _decode_checkpoint(session.checkpoint)

        for event in events:
            recorded = imported.get(event.event_id)
            if recorded is not None and recorded != _record_hash(event.record):
                raise ValueError("Pi session event was rewritten after import")

        for event in events:
            if event.event_id not in imported:
                await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)
                imported[event.event_id] = _record_hash(event.record)

        return ConnectorRunCompletion(status=ConnectorRunStatus.COMPLETE, checkpoint=_encode_checkpoint(imported))


def _encode_checkpoint(imported: dict[str, str]) -> dict[str, Any]:
    return {
        "version": 1,
        "events": [{"event_id": event_id, "record_sha256": digest} for event_id, digest in sorted(imported.items())],
    }


def _decode_checkpoint(checkpoint: Any) -> dict[str, str]:
    if checkpoint is None:
        return {}
    if not isinstance(checkpoint, dict) or checkpoint.get("version") != 1:
        raise ValueError("Pi session checkpoint is not a supported format")
    imported: dict[str, str] = {}
    for entry in checkpoint.get("events", []):
        if not isinstance(entry, dict):
            raise ValueError("Pi session checkpoint is malformed")
        imported[entry["event_id"]] = entry["record_sha256"]
    return imported


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PiEventInput",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
