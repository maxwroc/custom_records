"""Data models for custom_records record types and field definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .const import (
    MAX_IDENTIFIER_LENGTH,
    RESERVED_FIELD_KEYS,
    RESERVED_SQL_KEYWORDS,
    SELECT_FIELD_TYPES,
    SQL_TABLE_PREFIX,
    FieldType,
    is_valid_field_key,
    is_valid_record_type_id,
)


def _validate_identifier(kind: str, value: str, *, reserved: set[str]) -> None:
    """
    Validate a logical record-type id or field key (plan_sql.md Phase 1 pt.3).

    Physical SQL names (`sql_table`/`sql_column`) are generated directly from
    these logical identifiers, so this is the single place enforcing the
    restricted identifier pattern, reserved names, and length limit shared by
    both.
    """
    if len(value) > MAX_IDENTIFIER_LENGTH:
        msg = f"{kind} '{value}' exceeds the maximum length of {MAX_IDENTIFIER_LENGTH}"
        raise ValueError(msg)
    if value in reserved:
        msg = f"{kind} '{value}' is a reserved name"
        raise ValueError(msg)


def _validate_default(
    field_type: FieldType, default: Any, options: list[str] | None
) -> None:
    """Validate a configured default value matches its field's type/options."""
    if default is None:
        return
    if field_type is FieldType.NUMBER and not isinstance(default, (int, float)):
        msg = f"Default value {default!r} is not a valid number"
        raise ValueError(msg)
    if field_type is FieldType.BOOLEAN and not isinstance(default, bool):
        msg = f"Default value {default!r} is not a valid boolean"
        raise ValueError(msg)
    if field_type in (FieldType.TEXT, FieldType.LONG_TEXT) and not isinstance(
        default, str
    ):
        msg = f"Default value {default!r} is not a valid string"
        raise ValueError(msg)
    if field_type is FieldType.SINGLE_SELECT and default not in (options or []):
        msg = f"Default value {default!r} is not one of the configured options"
        raise ValueError(msg)
    if field_type is FieldType.MULTI_SELECT and (
        not isinstance(default, list)
        or any(item not in (options or []) for item in default)
    ):
        msg = f"Default value {default!r} is not a list of configured options"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class FieldDefinition:
    """Definition of a single field within a record type."""

    key: str
    label: str
    type: FieldType
    required: bool = False
    unit: str | None = None
    default: Any = None
    options: list[str] | None = None
    # Immutable physical SQL column name (plan_sql.md Phase 1 pt.3/6) - kept
    # separate from `key` even though it is always generated equal to it in
    # P0, so a future release can introduce a deliberate logical/physical
    # alias without a breaking schema change. Empty string means "derive from
    # key", handled in __post_init__ so existing call sites that don't pass
    # it explicitly (tests, fresh construction) keep working.
    sql_column: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type.value,
            "required": self.required,
            "unit": self.unit,
            "default": self.default,
            "options": self.options,
            "sql_column": self.sql_column,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FieldDefinition:
        """Deserialize from a JSON-compatible dict."""
        return cls(
            key=data["key"],
            label=data["label"],
            type=FieldType(data["type"]),
            required=data.get("required", False),
            unit=data.get("unit"),
            default=data.get("default"),
            options=data.get("options"),
            sql_column=data.get("sql_column") or "",
        )

    def __post_init__(self) -> None:
        """Validate the field's identifier/options/default and fill sql_column."""
        _validate_identifier(
            "Field key",
            self.key,
            reserved=RESERVED_FIELD_KEYS | RESERVED_SQL_KEYWORDS,
        )
        if not is_valid_field_key(self.key):
            msg = f"Field key '{self.key}' is not a valid identifier"
            raise ValueError(msg)
        if self.type in SELECT_FIELD_TYPES and not self.options:
            msg = f"Field '{self.key}' ({self.type}) requires a non-empty options list"
            raise ValueError(msg)
        _validate_default(self.type, self.default, self.options)
        if not self.sql_column:
            object.__setattr__(self, "sql_column", self.key)


@dataclass(frozen=True, slots=True)
class RecordType:
    """A user-defined record type (e.g. 'Blood Pressure')."""

    id: str
    name: str
    fields: list[FieldDefinition] = field(default_factory=list)
    timestamp_field: str = "timestamp"
    retention_days: int | None = None
    max_records: int | None = None
    warn_at: int | None = None
    # Immutable physical SQL table name (plan_sql.md Phase 1 pt.3/6), always
    # `records_<id>` in P0. Empty string means "derive from id", filled in by
    # __post_init__.
    sql_table: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "id": self.id,
            "name": self.name,
            "fields": [f.to_dict() for f in self.fields],
            "timestamp_field": self.timestamp_field,
            "retention_days": self.retention_days,
            "max_records": self.max_records,
            "warn_at": self.warn_at,
            "sql_table": self.sql_table,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RecordType:
        """Deserialize from a JSON-compatible dict."""
        return cls(
            id=data["id"],
            name=data["name"],
            fields=[FieldDefinition.from_dict(f) for f in data.get("fields", [])],
            timestamp_field=data.get("timestamp_field", "timestamp"),
            retention_days=data.get("retention_days"),
            max_records=data.get("max_records"),
            warn_at=data.get("warn_at"),
            sql_table=data.get("sql_table") or "",
        )

    def __post_init__(self) -> None:
        """Validate the record type's identifier and fill sql_table."""
        _validate_identifier("Record type id", self.id, reserved=set())
        if not is_valid_record_type_id(self.id):
            msg = f"Record type id '{self.id}' is not a valid identifier"
            raise ValueError(msg)
        field_keys = [f.key for f in self.fields]
        if len(field_keys) != len(set(field_keys)):
            msg = f"Record type '{self.id}' has duplicate field keys"
            raise ValueError(msg)
        if not self.sql_table:
            object.__setattr__(self, "sql_table", f"{SQL_TABLE_PREFIX}{self.id}")

    def get_field(self, key: str) -> FieldDefinition | None:
        """Return the field definition matching key, if any."""
        return next((f for f in self.fields if f.key == key), None)

    def to_subentry_data(self) -> dict[str, Any]:
        """Serialize everything except id/name, for a ConfigSubentry's `data`."""
        data = self.to_dict()
        del data["id"]
        del data["name"]
        return data

    @classmethod
    def from_subentry(
        cls, record_type_id: str, name: str, data: dict[str, Any]
    ) -> RecordType:
        """Build a RecordType from a ConfigSubentry's unique_id/title/data."""
        return cls.from_dict({**data, "id": record_type_id, "name": name})
