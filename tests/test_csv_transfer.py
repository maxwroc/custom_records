"""Tests for custom_records.csv_transfer (pure CSV build/parse logic)."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.custom_records.const import FieldType
from custom_components.custom_records.csv_transfer import (
    build_export_csv,
    parse_import_csv,
)
from custom_components.custom_records.models import FieldDefinition, RecordType

RECORD_TYPE = RecordType(
    id="bp",
    name="Blood Pressure",
    fields=[
        FieldDefinition(
            key="systolic", label="Systolic", type=FieldType.NUMBER, required=True
        ),
        FieldDefinition(key="notes", label="Notes", type=FieldType.TEXT),
        FieldDefinition(
            key="tags", label="Tags", type=FieldType.MULTI_SELECT, options=["a", "b"]
        ),
        FieldDefinition(key="ok", label="OK", type=FieldType.BOOLEAN),
        FieldDefinition(key="photo", label="Photo", type=FieldType.IMAGE),
    ],
)

RECORDS = [
    {
        "id": "rec-1",
        "t": "2026-01-01T10:00:00+00:00",
        "d": {
            "systolic": 120.0,
            "notes": "fine",
            "tags": ["a", "b"],
            "ok": True,
            "photo": "abc123.jpg",
        },
    },
    {
        "id": "rec-2",
        "t": "2026-01-02T10:00:00+00:00",
        "d": {"systolic": 130.0, "ok": False},
    },
]


def test_build_export_csv_includes_id_by_default() -> None:
    """The header/rows include the id column when include_id defaults True."""
    csv_text = build_export_csv(RECORD_TYPE, RECORDS)
    lines = csv_text.splitlines()
    assert lines[0] == "id,timestamp,systolic,notes,tags,ok,photo"
    assert lines[1] == "rec-1,2026-01-01T10:00:00+00:00,120.0,fine,a;b,true,abc123.jpg"
    assert lines[2] == "rec-2,2026-01-02T10:00:00+00:00,130.0,,,false,"


def test_build_export_csv_include_id_false_omits_id_column_entirely() -> None:
    """include_id=False drops the id column from the header AND every row."""
    csv_text = build_export_csv(RECORD_TYPE, RECORDS, include_id=False)
    lines = csv_text.splitlines()
    assert lines[0] == "timestamp,systolic,notes,tags,ok,photo"
    assert lines[1] == "2026-01-01T10:00:00+00:00,120.0,fine,a;b,true,abc123.jpg"


def test_export_import_round_trip_full_mode() -> None:
    """Exporting then re-importing (full mode) reproduces the same field data."""
    csv_text = build_export_csv(RECORD_TYPE, RECORDS)
    result = parse_import_csv(RECORD_TYPE, csv_text)

    assert result.errors == []
    assert len(result.rows) == 2
    assert result.rows[0].id == "rec-1"
    assert result.rows[0].fields == {
        "systolic": 120.0,
        "notes": "fine",
        "tags": ["a", "b"],
        "ok": True,
        "photo": "abc123.jpg",
    }
    assert result.rows[0].timestamp == datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)


def test_import_data_only_csv_has_no_id_and_generates_new_records() -> None:
    """A data-only export (no id column) parses with id=None for every row."""
    csv_text = build_export_csv(RECORD_TYPE, RECORDS, include_id=False)
    result = parse_import_csv(RECORD_TYPE, csv_text)

    assert result.errors == []
    assert all(row.id is None for row in result.rows)
    # Timestamp is still preserved even though id is dropped.
    assert result.rows[0].timestamp == datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)


def test_import_missing_required_field_is_a_row_error() -> None:
    """A row missing a required field is skipped and reported as an error."""
    csv_text = "id,timestamp,systolic\nrec-1,2026-01-01T10:00:00+00:00,\n"
    result = parse_import_csv(RECORD_TYPE, csv_text)

    assert result.rows == []
    assert len(result.errors) == 1
    assert result.errors[0]["row"] == 2
    assert "systolic" in result.errors[0]["message"]


def test_import_invalid_timestamp_is_a_row_error() -> None:
    """An unparsable timestamp is a row error, not a crash."""
    csv_text = "id,timestamp,systolic\nrec-1,not-a-date,120\n"
    result = parse_import_csv(RECORD_TYPE, csv_text)

    assert result.rows == []
    assert len(result.errors) == 1
    assert "timestamp" in result.errors[0]["message"].lower()


def test_import_invalid_field_value_is_a_row_error() -> None:
    """A value that fails field validation (e.g. non-numeric) is a row error."""
    csv_text = "id,timestamp,systolic\nrec-1,2026-01-01T10:00:00+00:00,not-a-number\n"
    result = parse_import_csv(RECORD_TYPE, csv_text)

    assert result.rows == []
    assert len(result.errors) == 1
    assert result.errors[0]["row"] == 2


def test_import_multi_select_value_not_in_options_is_accepted() -> None:
    """
    A multi_select value no longer in the field's `options` still imports.

    `options` is only a UI convenience (dropdown to reduce typos) - it is not
    a hard data constraint on import, e.g. a backup CSV taken before options
    were edited/removed must still import cleanly.
    """
    csv_text = "id,timestamp,systolic,tags\nrec-1,2026-01-01T10:00:00+00:00,120,c;d\n"
    result = parse_import_csv(RECORD_TYPE, csv_text)

    assert result.errors == []
    assert result.rows[0].fields["tags"] == ["c", "d"]


def test_import_single_select_value_not_in_options_is_accepted() -> None:
    """A single_select value no longer in the field's `options` still imports."""
    record_type = RecordType(
        id="mood_log",
        name="Mood Log",
        fields=[
            FieldDefinition(
                key="mood",
                label="Mood",
                type=FieldType.SINGLE_SELECT,
                options=["happy", "sad"],
            ),
        ],
    )
    csv_text = "id,timestamp,mood\nrec-1,2026-01-01T10:00:00+00:00,excited\n"
    result = parse_import_csv(record_type, csv_text)

    assert result.errors == []
    assert result.rows[0].fields["mood"] == "excited"


def test_import_unknown_columns_are_ignored() -> None:
    """Extra columns not matching id/timestamp/a current field key are ignored."""
    csv_text = (
        "id,timestamp,systolic,some_removed_field\n"
        "rec-1,2026-01-01T10:00:00+00:00,120,leftover-value\n"
    )
    result = parse_import_csv(RECORD_TYPE, csv_text)

    assert result.errors == []
    assert result.rows[0].fields == {"systolic": 120.0}


def test_import_bad_row_does_not_block_rest_of_file() -> None:
    """A malformed row is skipped, but subsequent good rows still import."""
    csv_text = (
        "id,timestamp,systolic\n"
        "rec-1,2026-01-01T10:00:00+00:00,not-a-number\n"
        "rec-2,2026-01-02T10:00:00+00:00,130\n"
    )
    result = parse_import_csv(RECORD_TYPE, csv_text)

    assert len(result.errors) == 1
    assert len(result.rows) == 1
    assert result.rows[0].id == "rec-2"
