"""
Shared helpers for the internal record envelope <-> public shape conversion.

Used by both services.py and websocket_api.py so the flattening logic lives in
one place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .const import (
    ATTR_MEDIA_SOURCE,
    ATTR_RECORD_ID,
    ATTR_TIMESTAMP,
    DOMAIN,
    ENVELOPE_DATA,
    ENVELOPE_ID,
    ENVELOPE_TIMESTAMP,
    FieldType,
)

if TYPE_CHECKING:
    from .models import RecordType


def to_public_record(record: dict[str, Any], record_type: RecordType) -> dict[str, Any]:
    """
    Flatten the internal envelope into the public {id, timestamp, ...} shape.

    Every IMAGE-type field that has a stored filename is replaced with a
    deterministic media_source link (never stored - generated here so it
    always reflects the current record/field identifiers).
    """
    public_record = {
        ATTR_RECORD_ID: record[ENVELOPE_ID],
        ATTR_TIMESTAMP: record[ENVELOPE_TIMESTAMP],
        **record[ENVELOPE_DATA],
    }
    for field_def in record_type.fields:
        if field_def.type is not FieldType.IMAGE:
            continue
        filename = public_record.get(field_def.key)
        if not filename:
            continue
        public_record[field_def.key] = {
            ATTR_MEDIA_SOURCE: (
                f"media-source://{DOMAIN}/{record_type.id}/"
                f"{record[ENVELOPE_ID]}/{field_def.key}"
            )
        }
    return public_record
