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

"""CSV capture, processing, and exact evidence reads across Runtime restarts."""

import asyncio
import csv
import io
import json
from pathlib import Path

from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryCitation, MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SearchMemoryRequest,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    SourceDefinitionRegistry,
    SourceObservation,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.csv_rows import (
    CSV_ROW_SOURCE_DEFINITION,
    CSVDocumentInput,
    CSVRowSource,
    csv_row_source_id,
    parse_csv_document,
)


class _RowPipeline:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        entries = []
        for source in request.sources:
            assert isinstance(source, SourceObservation)
            evidence = TextEvidence.model_validate(source.projection(TEXT_EVIDENCE_PROJECTION_KEY))
            entries.append(MemoryEntryInput(kind="csv-row", text=evidence.content, sources=(source,)))
        return tuple(entries)


def test_unicode_rows_and_citations_survive_multiple_restarts(tmp_path: Path) -> None:
    columns = (" id ", "z_name", "amount", "a_note", "empty")
    values = tuple(
        (f" key:{index:02}/% ", f"東京 recordtoken{index:02}", f"00{index}.00", 'first\r\n\r\nsecond "quoted"', "")
        for index in range(12)
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(columns)
    writer.writerows(reversed(values))
    rows = parse_csv_document(
        CSVDocumentInput(document_id="表:一/原始", primary_key=" id ", content=output.getvalue().encode())
    )
    expected_rows = {row.primary_key_value: row for row in rows}
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'unicode-rows.db'}"))

    async def scenario() -> None:
        async with open_builtin_runtime(config) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="CSV evidence", summary="Offline rows", idempotency_key="unicode-csv")
            )
            await runtime.ingestion.register(manifest_for_definition(CSV_ROW_SOURCE_DEFINITION))
            for row in rows:
                observation = project_source_for_transport(registry, await registry.resolve(row))
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )

        async with open_builtin_contexts(config) as contexts:
            persisted = await (await contexts.get(scope.scope_id)).sources.journal.entries()
            assert len(persisted) == len(rows)
            persisted_by_id = {item.source_ref.source_id: item.source for item in persisted}
            for item in persisted:
                assert isinstance(item.source, SourceObservation)
                source = CSVRowSource.model_validate(item.source.payload)
                assert await registry.read(source) == expected_rows[source.primary_key_value]
                assert item.source.projection(TEXT_EVIDENCE_PROJECTION_KEY) == registry.project(
                    source, TEXT_EVIDENCE_PROJECTION_KEY
                )

        citations: list[tuple[MemoryCitation, str]] = []
        async with open_builtin_runtime(config, candidate_pipeline=_RowPipeline()) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            processed = 0
            for _ in range(len(rows) + 1):
                result = await memory.flush()
                if not result.processed:
                    break
                processed += result.source_count
            assert processed == len(rows)
            for index, fields in enumerate(values):
                hits = await memory.search(SearchMemoryRequest(query=f"recordtoken{index:02}", mode="fts", limit=20))
                assert len(hits.hits) == 1
                hit = hits.hits[0]
                citation = MemoryCitation(
                    memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
                )
                citations.append((citation, fields[0]))

        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            for citation, key in citations:
                entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
                row = expected_rows[key]
                mapping = dict(zip(row.columns, row.values, strict=True))
                assert entry.text == json.dumps(mapping, ensure_ascii=False, separators=(",", ":"))
                assert tuple(json.loads(entry.text)) == columns
                assert len(entry.sources) == 1
                reference = entry.sources[0]
                assert reference.source_type == "csv-row"
                assert reference.source_id == csv_row_source_id(row.document_id, key)
                source = persisted_by_id[reference.source_id]
                assert isinstance(source, SourceObservation)
                assert source.name == reference.source_id
                assert TextEvidence.model_validate(source.projection(TEXT_EVIDENCE_PROJECTION_KEY)).metadata == {
                    "document_id": row.document_id,
                    "primary_key": row.primary_key,
                    "primary_key_value": key,
                    "columns": list(columns),
                    "row": mapping,
                }

    asyncio.run(scenario())
