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

"""Durable source-dependency eligibility without rewriting historical evidence."""

from __future__ import annotations

from graphlib import CycleError, TopologicalSorter
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import insert, select, update

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryCitation
from powercontext.builtin.persistence.artifact_governance import ArtifactGovernanceRepository, ArtifactLifecycleState
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_TABLE
from powercontext.builtin.records import BaseValueNotFoundError
from powercontext.builtin.runtime.models import GetMemoryEntryRequest
from powercontext.builtin.sources import validate_scope_id
from powercontext.errors import PowerContextError
from powercontext.sources import SourceMaterialization, SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime


class SourceLifecycleError(ValueError):
    """A binding, dependency, event identity, or intended use is invalid."""

    def __init__(self, issue: str) -> None:
        messages = {
            "text": "Lifecycle identifiers and reasons must be non-blank strings",
            "dag": "Lifecycle dependencies must be a complete DAG",
            "configuration": "Source lifecycle persistence is not configured",
            "source": "Source registration requires a SourceRef and positive integer revision",
            "memory": "Memory registration requires a MemoryCitation",
            "topic": "Topic registration requires a Topic Memory ArtifactRef",
            "parents": "Derived nodes require parents and an all/any policy",
            "duplicate-parent": "Lifecycle parents must be distinct and cannot include the node itself",
            "binding": "Lifecycle node is already bound to different evidence or dependencies",
            "missing-parent": "Lifecycle parents must be registered first",
            "captured": "Registered Source must exist and retain captured evidence",
            "reference": "Lifecycle evidence must exist and be readable in this scope",
            "event": "Lifecycle application requires a validated event",
            "target": "Event must target a registered Source at its exact lifecycle revision",
            "sequence": "Event sequence is stale or conflicts with an accepted event",
            "purpose": "Unknown lifecycle reference purpose",
            "unknown": "Unknown lifecycle node",
            "unavailable": "Unavailable lifecycle evidence cannot be used for generation",
            "body": "Registered Memory body version is no longer current",
        }
        super().__init__(messages.get(issue, issue))


def _require_text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceLifecycleError("text")
    return value


class SourceLifecycleEvent(BaseModel):
    """An exact, replayable Source availability decision from a caller."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    node_id: str
    revision: Annotated[int, Field(gt=0)]
    sequence: Annotated[int, Field(gt=0)]
    available: bool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def require_non_blank(cls, value: str) -> str:
        return _require_text(value)


_Reference = SourceRef | MemoryCitation | ArtifactRef
_Policy = Literal["all", "any"]


class _Node(BaseModel):
    kind: Literal["source", "memory", "topic"]
    reference: _Reference
    parents: tuple[str, ...] = ()
    policy: _Policy = "all"
    revision: int = 1
    event: SourceLifecycleEvent | None = None
    applied: bool = True


class _Graph(BaseModel):
    nodes: dict[str, _Node] = Field(default_factory=dict)


def _availability(graph: _Graph) -> dict[str, bool]:
    """Evaluate the immutable DAG independently of registration/serialization order."""
    states: dict[str, bool] = {}
    try:
        order = TopologicalSorter({key: node.parents for key, node in graph.nodes.items()}).static_order()
        for key in order:
            node = graph.nodes[key]
            if node.kind == "source":
                states[key] = node.event is None or node.event.available
            else:
                support = (states[parent] for parent in node.parents)
                states[key] = all(support) if node.policy == "all" else any(support)
    except (CycleError, KeyError) as error:
        raise SourceLifecycleError("dag") from error
    return states


class SourceLifecycleApplication:
    """Bind per-scope dependency services to the Runtime's native database."""

    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase | None) -> None:
        self._runtime = runtime
        self._database = database

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycle:
        if self._database is None:
            raise SourceLifecycleError("configuration")
        return ScopedSourceLifecycle(self._runtime, self._database, validate_scope_id(scope_id))


class ScopedSourceLifecycle:
    """Single-writer, scope-local dependency decisions and recoverable propagation."""

    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase, scope_id: str) -> None:
        self._runtime = runtime
        self._database = database
        self.scope_id = scope_id

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int) -> None:
        """Bind captured evidence to one immutable lifecycle identity and revision."""
        if type(revision) is not int or revision < 1 or not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("source")
        await self._register(node_id, _Node(kind="source", reference=source_ref, revision=revision))

    async def register_memory(
        self, node_id: str, citation: MemoryCitation, *, parents: tuple[str, ...], policy: _Policy
    ) -> None:
        """Bind an exact Memory body and immediately apply its dependency policy."""
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("memory")
        await self._register_derived(node_id, "memory", citation, parents, policy)

    async def register_topic(
        self, node_id: str, artifact_ref: ArtifactRef, *, parents: tuple[str, ...], policy: _Policy
    ) -> None:
        """Bind a historical Topic revision while controlling current eligibility."""
        if not isinstance(artifact_ref, ArtifactRef) or artifact_ref.family != "topic-memory":
            raise SourceLifecycleError("topic")
        await self._register_derived(node_id, "topic", artifact_ref, parents, policy)

    async def _register_derived(
        self,
        node_id: str,
        kind: Literal["memory", "topic"],
        reference: MemoryCitation | ArtifactRef,
        parents: tuple[str, ...],
        policy: _Policy,
    ) -> None:
        if not isinstance(parents, tuple) or not parents or policy not in {"all", "any"}:
            raise SourceLifecycleError("parents")
        for parent in parents:
            _require_text(parent)
        if len(set(parents)) != len(parents) or node_id in parents:
            raise SourceLifecycleError("duplicate-parent")
        await self._register(
            node_id, _Node(kind=kind, reference=reference, parents=tuple(sorted(parents)), policy=policy)
        )

    async def _register(self, node_id: str, node: _Node) -> None:
        _require_text(node_id)
        async with self._runtime._scoped_operation(self.scope_id):
            graph = await self._load()
            existing = graph.nodes.get(node_id)
            if existing is not None and existing.model_dump(exclude={"event", "applied"}) != node.model_dump(
                exclude={"event", "applied"}
            ):
                raise SourceLifecycleError("binding")
            if any(parent not in graph.nodes for parent in node.parents):
                raise SourceLifecycleError("missing-parent")
            await self._validate_reference(node)
            if existing is None:
                graph.nodes[node_id] = node
                _availability(graph)
                # Retain the binding before native writes so an interrupted registration can retry.
                await self._save(graph)
            await self._reconcile(graph, "Lifecycle registration")

    async def _validate_reference(self, node: _Node) -> None:
        try:
            if isinstance(node.reference, SourceRef):
                async with self._runtime._context(self.scope_id) as context:
                    entries = await context.sources.journal.entries()
                    source = next((item.source for item in entries if item.source_ref == node.reference), None)
                    if source is None or source.materialization is not SourceMaterialization.CAPTURED:
                        raise SourceLifecycleError("captured")
            elif isinstance(node.reference, MemoryCitation):
                await self._runtime.memory.for_scope(self.scope_id).get(GetMemoryEntryRequest(citation=node.reference))
            else:
                await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                    node.reference.family, node.reference.artifact_id, node.reference.revision
                )
        except (PowerContextError, BaseValueNotFoundError) as error:
            raise SourceLifecycleError("reference") from error

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        """Persist intent first; an identical retry reconciles any interrupted native writes."""
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("event")
        async with self._runtime._scoped_operation(self.scope_id):
            graph = await self._load()
            node = graph.nodes.get(event.node_id)
            if node is None or node.kind != "source" or node.revision != event.revision:
                raise SourceLifecycleError("target")
            if node.event is not None:
                if event.sequence < node.event.sequence or (
                    event.sequence == node.event.sequence and event != node.event
                ):
                    raise SourceLifecycleError("sequence")
                if event == node.event and node.applied:
                    return sorted(key for key, available in _availability(graph).items() if available)
            node.event = event
            node.applied = False
            await self._save(graph)
            states = await self._reconcile(graph, event.reason)
            return sorted(key for key, available in states.items() if available)

    async def resolve(self, node_id: str, *, purpose: Literal["generation", "audit"]) -> _Reference:
        _require_text(node_id)
        if purpose not in {"generation", "audit"}:
            raise SourceLifecycleError("purpose")
        async with self._runtime._scoped_operation(self.scope_id):
            graph = await self._load()
            node = graph.nodes.get(node_id)
            if node is None:
                raise SourceLifecycleError("unknown")
            if purpose == "generation" and not _availability(graph)[node_id]:
                raise SourceLifecycleError("unavailable")
            return node.reference

    async def _load(self) -> _Graph:
        async with self._database.transaction() as connection:
            payload = await connection.scalar(
                select(SOURCE_LIFECYCLE_TABLE.c.payload).where(SOURCE_LIFECYCLE_TABLE.c.scope_id == self.scope_id)
            )
        return _Graph() if payload is None else _Graph.model_validate_json(payload)

    async def _save(self, graph: _Graph) -> None:
        payload = graph.model_dump_json().encode("utf-8")
        async with self._database.transaction() as connection:
            result = await connection.execute(
                update(SOURCE_LIFECYCLE_TABLE)
                .where(SOURCE_LIFECYCLE_TABLE.c.scope_id == self.scope_id)
                .values(payload=payload)
            )
            if result.rowcount == 0:
                await connection.execute(insert(SOURCE_LIFECYCLE_TABLE).values(scope_id=self.scope_id, payload=payload))

    async def _reconcile(self, graph: _Graph, reason: str) -> dict[str, bool]:
        states = _availability(graph)
        async with self._runtime._context(self.scope_id) as context, self._runtime._locked(self.scope_id):
            for node_id, node in graph.nodes.items():
                if isinstance(node.reference, MemoryCitation):
                    service = context.artifacts.memory
                    current, entries = await service.head_entries(context.artifacts.memory_artifact_id)
                    entry = next((item for item in entries if item.entry_id == node.reference.entry_id), None)
                    if entry is None or entry.entry_version_id != node.reference.entry_version_id:
                        raise SourceLifecycleError("body")
                    target = "active" if states[node_id] else "inactive"
                    if (
                        next(item.state for item in current.content.manifest.entries if item.entry_id == entry.entry_id)
                        == target
                    ):
                        continue
                    # Native transitions are idempotent and retain the original entry body version.
                    change = service.reactivate if states[node_id] else service.forget
                    await change(current, entries=(entry,), reason=reason)
                elif isinstance(node.reference, ArtifactRef):
                    async with self._database.transaction() as connection:
                        governance = ArtifactGovernanceRepository()
                        current_state = await governance.get(
                            connection, self.scope_id, node.reference.family, node.reference.artifact_id
                        )
                        requested = (
                            ArtifactLifecycleState.ACTIVE if states[node_id] else ArtifactLifecycleState.DEPRECATED
                        )
                        if current_state.lifecycle_state != requested:
                            await governance.transition(
                                connection,
                                self.scope_id,
                                node.reference.family,
                                node.reference.artifact_id,
                                current_state.governance_generation,
                                requested,
                                None,
                            )
        # A crash after the final native commit still leaves an accepted, retryable event.
        for node in graph.nodes.values():
            node.applied = True
        await self._save(graph)
        return states


__all__ = ["SourceLifecycleError", "SourceLifecycleEvent"]
