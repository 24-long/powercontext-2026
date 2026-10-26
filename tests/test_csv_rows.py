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

"""Lossless CSV capture boundaries beyond the published airport workload."""

import asyncio
import csv
import io
import json

import pytest

from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    SourceDefinitionRegistry,
    SourceMaterialization,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.csv_rows import (
    CSV_ROW_SOURCE_DEFINITION,
    CSVDocumentInput,
    CSVRowInput,
    CSVRowSource,
    csv_row_source_id,
    parse_csv_document,
)


@pytest.mark.parametrize(
    "content",
    [
        b"\xef\xbb\xbf",
        b"\n",
        b'""\n',
        b"id, \t\n",
        b"id,name,id\n",
        b'id,name\n1,valid\n2,"closed"junk\n',
        b'id,name\n1,valid\n2,"unclosed\n',
        b"id,name\n1,valid\n2,extra,field\n",
        b"id,name\n1,valid\n2\n",
        b"id,name\n1,valid\n \t,blank-key\n",
        b"id,name\n1,valid\n1,duplicate\n",
        b"id,name\n1,valid\n\xc0\xaf,invalid\n",
        b"id,name\n1,valid\n\xed\xa0\x80,invalid\n",
        b'id\n1\n""\n',
    ],
)
def test_invalid_documents_fail_atomically(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content=content))


@pytest.mark.parametrize("document_id", ["", " ", " doc", "doc\n", "\u3000doc"])
def test_invalid_document_identity_is_rejected_even_without_rows(document_id: str) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id=document_id, primary_key="id", content=b"id\n"))


@pytest.mark.parametrize("primary_key", ["", "id", " id", "id "])
def test_primary_key_must_exactly_match_a_spaced_column(primary_key: str) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="doc", primary_key=primary_key, content=b" id ,name\n"))


def test_unicode_values_newlines_column_order_and_json_round_trip() -> None:
    columns = (" id ", "id", "name", "number", "enabled", "empty", "note")
    values = (" key ", "different", "東京", "0001.00", "false", "", 'first\r\n\r\nsecond\rthird\n"last"')
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(columns)
    writer.writerow(values)
    rows = parse_csv_document(
        CSVDocumentInput(document_id="表:一", primary_key=" id ", content=b"\xef\xbb\xbf" + buffer.getvalue().encode())
    )
    assert len(rows) == 1
    assert rows[0].columns == columns
    assert rows[0].values == values
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(rows[0]))
    assert isinstance(source, CSVRowSource)
    assert source.materialization is SourceMaterialization.CAPTURED
    restored = CSVRowSource.model_validate_json(source.model_dump_json())
    assert asyncio.run(registry.read(restored)) == rows[0]
    evidence = TextEvidence.model_validate(registry.project(restored, TEXT_EVIDENCE_PROJECTION_KEY))
    expected = dict(zip(columns, values, strict=True))
    assert evidence.content == json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
    projected_row = evidence.metadata["row"]
    assert isinstance(projected_row, dict)
    assert list(projected_row) == list(columns)
    assert evidence.metadata == {
        "document_id": "表:一",
        "primary_key": " id ",
        "primary_key_value": " key ",
        "columns": list(columns),
        "row": expected,
    }
    observation = project_source_for_transport(registry, restored)
    assert observation.projection(TEXT_EVIDENCE_PROJECTION_KEY) == evidence.model_dump(mode="json")
    manifest = manifest_for_definition(CSV_ROW_SOURCE_DEFINITION)
    assert manifest.name == "csv-row"
    assert manifest.version == "1"


def test_only_empty_records_are_ignored() -> None:
    rows = parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content=b"id\r\n\r\n1\r\n\n2\r\n"))
    assert tuple(row.values for row in rows) == (("1",), ("2",))
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content=b"id\n \n"))


def test_identity_limits_apply_after_percent_encoding() -> None:
    assert len(csv_row_source_id("d", "x" * 250)) == 256
    assert len(csv_row_source_id("d", "é" * 41 + "xxxx")) == 256
    for key in ("x" * 251, "é" * 42):
        with pytest.raises(ValueError):
            csv_row_source_id("d", key)
        with pytest.raises(ValueError):
            parse_csv_document(CSVDocumentInput(document_id="d", primary_key="id", content=f"id\n{key}\n".encode()))
    assert csv_row_source_id("a:b", "c") != csv_row_source_id("a", "b:c")
    assert csv_row_source_id("a%3Ab", "c") != csv_row_source_id("a:b", "c")


@pytest.mark.parametrize(
    ("columns", "values", "primary_key_value"),
    [
        ((), (), "1"),
        (("id", "id"), ("1", "2"), "1"),
        (("id", ""), ("1", "2"), "1"),
        (("id", "name"), ("1",), "1"),
        (("id",), ("1", "extra"), "1"),
        (("name",), ("1",), "1"),
        (("id",), ("1",), "2"),
        (("id",), (" ",), " "),
    ],
)
def test_direct_row_inputs_cannot_lose_fields_or_misidentify_records(
    columns: tuple[str, ...], values: tuple[str, ...], primary_key_value: str
) -> None:
    with pytest.raises(ValueError):
        row = CSVRowInput(
            document_id="doc", primary_key="id", primary_key_value=primary_key_value, columns=columns, values=values
        )
        asyncio.run(SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,)).resolve(row))
