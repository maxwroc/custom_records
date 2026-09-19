"""
CSV export/import for a single record type (backup/restore, bulk data entry).

Pure logic only - no HTTP/storage I/O (see export_view.py for the download
endpoint and store.py's `async_import_records` for the actual write path).
Shared by config_flow.py's "Export data"/"Import data" reconfigure steps and
services.py's `export_records`/`import_records` services, so both paths use
identical CSV formatting/parsing rules.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_RECORD_ID,
    ATTR_TIMESTAMP,
    ENVELOPE_DATA,
    ENVELOPE_ID,
    ENVELOPE_TIMESTAMP,
    FieldType,
)
from .schema import build_import_field_validators

if TYPE_CHECKING:
    from datetime import datetime

    from .models import RecordType

# multi_select values are joined into a single CSV cell with this separator
# (e.g. "red;blue"), per the confirmed CSV encoding decision.
MULTI_SELECT_SEPARATOR = ";"


def _format_field_value(value: Any, field_type: FieldType) -> str:
    """Format a single field's value for a CSV cell."""
    if value is None:
        return ""
    if field_type is FieldType.MULTI_SELECT:
        return MULTI_SELECT_SEPARATOR.join(value)
    if field_type is FieldType.IMAGE:
        # Just the stored filename - import does not validate the file
        # actually exists.
        return value if isinstance(value, str) else ""
    if field_type is FieldType.BOOLEAN:
        return "true" if value else "false"
    return str(value)


def build_export_csv(
    record_type: RecordType,
    records: list[dict[str, Any]],
    *,
    include_id: bool = True,
) -> str:
    """
    Build CSV text for a record type's records.

    Header is `[id?, timestamp, <field.key for field in record_type.fields>]`
    - the `id` column is omitted ENTIRELY (not merely left blank) when
    `include_id` is False, matching the "data only" export mode: it drops the
    internal record id but keeps `timestamp` (meaningful data, not an
    internal implementation detail like `id` is).
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    header = [
        *([ATTR_RECORD_ID] if include_id else []),
        ATTR_TIMESTAMP,
        *(f.key for f in record_type.fields),
    ]
    writer.writerow(header)
    for record in records:
        data = record[ENVELOPE_DATA]
        row = [
            *([record[ENVELOPE_ID]] if include_id else []),
            record[ENVELOPE_TIMESTAMP],
            *(_format_field_value(data.get(f.key), f.type) for f in record_type.fields),
        ]
        writer.writerow(row)
    return buffer.getvalue()


@dataclass
class ImportRow:
    """A single successfully-parsed CSV row, ready to be stored."""

    id: str | None
    timestamp: datetime | None
    fields: dict[str, Any]


@dataclass
class ImportParseResult:
    """Result of parsing an import CSV: successfully-parsed rows + row errors."""

    rows: list[ImportRow] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)


def _parse_row_timestamp(row: dict[str, str]) -> datetime | None:
    """Parse the `timestamp` column, if present/non-empty. Raises ValueError."""
    raw = (row.get(ATTR_TIMESTAMP) or "").strip()
    if not raw:
        return None
    parsed = dt_util.parse_datetime(raw)
    if parsed is None:
        msg = f"Invalid timestamp: '{raw}'"
        raise ValueError(msg)
    return parsed


def _parse_row_fields(
    row: dict[str, str],
    record_type: RecordType,
    validators: dict[str, Any],
) -> dict[str, Any]:
    """
    Parse+validate every field column for one row. Raises ValueError.

    A missing/blank optional cell falls back to the field's configured
    default (same as a service/WebSocket add_record omitting that key), so
    CSV import applies defaults identically to normal writes rather than
    always storing NULL (plan_sql.md Phase 2 pt.15).
    """
    fields: dict[str, Any] = {}
    for field_def in record_type.fields:
        raw = row.get(field_def.key)
        if not raw:
            if field_def.required:
                msg = f"Missing required field '{field_def.key}'"
                raise ValueError(msg)
            if field_def.default is not None:
                fields[field_def.key] = field_def.default
            continue
        if field_def.type is FieldType.IMAGE:
            fields[field_def.key] = raw
            continue
        value: Any = raw
        if field_def.type is FieldType.MULTI_SELECT:
            value = [v for v in raw.split(MULTI_SELECT_SEPARATOR) if v]
        try:
            fields[field_def.key] = validators[field_def.key](value)
        except vol.Invalid as err:
            msg = f"Field '{field_def.key}': {err}"
            raise ValueError(msg) from err
    return fields


def parse_import_csv(record_type: RecordType, csv_text: str) -> ImportParseResult:
    """
    Parse an exported (or hand-edited) CSV file into rows ready for storage.

    A non-empty `id` column is reused as-is (store.py decides duplicate
    handling); empty/missing `id` (e.g. a "data only" export, or a CSV with
    no id column at all) means "generate a new id at insert time" (left as
    `None` here). Unknown/extra columns (not `id`/`timestamp`/a current field
    key) are silently ignored - forward-compatible with CSVs exported before
    a field was removed. A row with a missing required field, an unparsable
    timestamp, or a value that fails field validation is recorded as an error
    (row number + message) and skipped; the rest of the file still imports.
    """
    result = ImportParseResult()
    validators = build_import_field_validators(record_type)

    reader = csv.DictReader(io.StringIO(csv_text))
    for row_number, row in enumerate(reader, start=2):  # header text is row 1
        try:
            timestamp = _parse_row_timestamp(row)
            fields = _parse_row_fields(row, record_type, validators)
        except ValueError as err:
            result.errors.append({"row": row_number, "message": str(err)})
            continue
        record_id = (row.get(ATTR_RECORD_ID) or "").strip() or None
        result.rows.append(ImportRow(id=record_id, timestamp=timestamp, fields=fields))
    return result
