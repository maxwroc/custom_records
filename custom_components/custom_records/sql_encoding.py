"""
Shared SQL identifier quoting and field value encoding (plan_sql.md).

Used by both store.py (writing/reading rows) and filter_query.py (compiling
filter literals into SQL parameters), so the physical encoding of each field
type lives in exactly one place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .const import FieldType

if TYPE_CHECKING:
    from .models import FieldDefinition

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass
class CompiledFilter:
    """A `filter` config compiled into a parameterized SQL WHERE fragment."""

    sql: str
    params: list[Any] = field(default_factory=list)


def quote_identifier(name: str) -> str:
    """Double-quote a validated SQL identifier for safe interpolation."""
    return '"' + name.replace('"', '""') + '"'


def to_epoch_micros(value: datetime) -> int:
    """Convert an offset-aware datetime to signed UTC Unix microseconds."""
    if value.tzinfo is None:
        msg = "Naive datetimes are not supported; timestamps must be offset-aware"
        raise ValueError(msg)
    delta = value.astimezone(UTC) - _EPOCH
    return delta // timedelta(microseconds=1)


def from_epoch_micros(value: int) -> datetime:
    """Convert signed UTC Unix microseconds back to an offset-aware datetime."""
    return _EPOCH + timedelta(microseconds=value)


def is_finite_number(value: float) -> bool:
    """Return whether value is a finite (non-NaN, non-infinite) float."""
    return value == value and value not in (float("inf"), float("-inf"))  # noqa: PLR0124


def encode_field(field_def: FieldDefinition, value: Any) -> Any:
    """Encode one validated Python field value into its SQL storage form."""
    if value is None:
        return None
    if field_def.type is FieldType.BOOLEAN:
        return 1 if value else 0
    if field_def.type is FieldType.DATETIME:
        return to_epoch_micros(value)
    if field_def.type is FieldType.MULTI_SELECT:
        return json.dumps(list(value), separators=(",", ":"))
    if field_def.type is FieldType.NUMBER:
        number = float(value)
        if not is_finite_number(number):
            msg = f"Field '{field_def.key}' value must be a finite number"
            raise ValueError(msg)
        return number
    return value


def decode_field(field_def: FieldDefinition, value: Any) -> Any:
    """Decode one SQL storage value back into its public Python form."""
    if value is None:
        return None
    if field_def.type is FieldType.BOOLEAN:
        return bool(value)
    if field_def.type is FieldType.DATETIME:
        return from_epoch_micros(value).isoformat()
    if field_def.type is FieldType.MULTI_SELECT:
        decoded = json.loads(value)
        if not isinstance(decoded, list):
            msg = "Stored multi_select value is not a JSON array"
            raise ValueError(msg)
        return decoded
    return value
