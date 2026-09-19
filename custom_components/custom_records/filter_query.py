"""
Compile a card's `filter` config into a parameterized SQL WHERE fragment.

Config shape: a list of single-key {field_key: value} maps, AND-combined
(every item must match), e.g.:

    filter:
      - name: "!= Max"
      - age: "> 30"

Each item's value is either a native YAML/JSON scalar (int/float/bool - used
directly with an implied "==") or a string optionally prefixed with one of
"==", "!=", ">=", "<=", ">", "<" (checked longest-token-first so ">="/"<="
aren't mis-split into ">"/"<" plus a leftover "="). No operator prefix means
"==". No quoting is parsed inside string values - YAML's own quoting already
delivers a plain Python string, so there's nothing further to strip.

Compiled to SQL (plan_sql.md Phase 2 pt.14) rather than a Python predicate, so
`list_records`/`aggregate_records` can push filtering into the database
instead of fetching an unbounded row set. Missing-value semantics are
preserved exactly: SQL's NULL propagation already makes every ordinary
comparison (including `!=`) fail against a NULL/missing column, and
MULTI_SELECT membership checks explicitly require the column to be non-NULL
too, so a record missing an optional field never matches `==` OR `!=`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol

from .const import FieldType
from .schema import validate_filter_value
from .sql_encoding import CompiledFilter, encode_field, quote_identifier

if TYPE_CHECKING:
    from .models import RecordType

# Operator tokens, longest first so ">="/"<=" aren't mis-split into ">"/"<".
_OPERATOR_TOKENS = ("==", "!=", ">=", "<=", ">", "<")

# Which operators are valid for each field type. MULTI_SELECT only supports
# ==/!= with membership semantics (does the stored list contain this value),
# not full-list equality. IMAGE fields are never filterable (rejected before
# this table is even consulted - see _compile_condition).
_ALLOWED_OPERATORS: dict[FieldType, frozenset[str]] = {
    FieldType.NUMBER: frozenset({"==", "!=", ">", ">=", "<", "<="}),
    FieldType.TEXT: frozenset({"==", "!="}),
    FieldType.LONG_TEXT: frozenset({"==", "!="}),
    FieldType.BOOLEAN: frozenset({"==", "!="}),
    FieldType.DATETIME: frozenset({"==", "!=", ">", ">=", "<", "<="}),
    FieldType.SINGLE_SELECT: frozenset({"==", "!="}),
    FieldType.MULTI_SELECT: frozenset({"==", "!="}),
}

# Maps a filter operator token to its SQL comparison operator (identical
# spelling for every one except "==" -> "=").
_SQL_OPERATORS: dict[str, str] = {
    "==": "=",
    "!=": "!=",
    ">": ">",
    ">=": ">=",
    "<": "<",
    "<=": "<=",
}


# Machine-readable FilterError codes (also asserted on directly in tests).
ERR_UNKNOWN_FIELD = "unknown_filter_field"
ERR_UNSUPPORTED_FIELD = "unsupported_filter_field"
ERR_UNSUPPORTED_OPERATOR = "unsupported_filter_operator"
ERR_INVALID_VALUE = "invalid_filter_value"
ERR_INVALID_ITEM = "invalid_filter_item"


class FilterError(Exception):
    """Raised when a `filter` config value can't be compiled into SQL."""

    def __init__(self, code: str, message: str) -> None:
        """Store a machine-readable error code alongside the message."""
        super().__init__(message)
        self.code = code
        self.message = message


def _split_operator(raw_value: Any) -> tuple[str, Any]:
    """Return (operator, raw literal) for one filter item's value."""
    if not isinstance(raw_value, str):
        # Native YAML/JSON scalar (int/float/bool) - implied "==", no parsing.
        return "==", raw_value
    for token in _OPERATOR_TOKENS:
        if raw_value.startswith(token):
            return token, raw_value[len(token) :].strip()
    return "==", raw_value.strip()


def _compile_condition(
    record_type: RecordType, field_key: str, raw_value: Any
) -> tuple[str, list[Any]]:
    """Compile one {field_key: raw_value} filter item into (sql, params)."""
    field_def = record_type.get_field(field_key)
    if field_def is None:
        msg = f"Unknown filter field '{field_key}'"
        raise FilterError(ERR_UNKNOWN_FIELD, msg)
    if field_def.type is FieldType.IMAGE:
        msg = f"Field '{field_key}' (image) cannot be filtered on"
        raise FilterError(ERR_UNSUPPORTED_FIELD, msg)

    op, literal = _split_operator(raw_value)
    if op not in _ALLOWED_OPERATORS[field_def.type]:
        msg = (
            f"Operator '{op}' is not supported for field '{field_key}' "
            f"({field_def.type.value})"
        )
        raise FilterError(ERR_UNSUPPORTED_OPERATOR, msg)

    try:
        coerced = validate_filter_value(field_def, literal)
    except vol.Invalid as err:
        msg = f"Invalid filter value for field '{field_key}': {err}"
        raise FilterError(ERR_INVALID_VALUE, msg) from err

    col = quote_identifier(field_def.sql_column)

    if field_def.type is FieldType.MULTI_SELECT:
        # col is a validated/double-quoted identifier; the value is always a
        # "?" parameter below, never interpolated.
        exists_sql = f"EXISTS (SELECT 1 FROM json_each({col}) WHERE value = ?)"  # noqa: S608
        if op == "==":
            return f"{col} IS NOT NULL AND {exists_sql}", [coerced]
        return f"{col} IS NOT NULL AND NOT {exists_sql}", [coerced]

    try:
        bound_value = encode_field(field_def, coerced)
    except ValueError as err:
        msg = f"Invalid filter value for field '{field_key}': {err}"
        raise FilterError(ERR_INVALID_VALUE, msg) from err

    return f"{col} {_SQL_OPERATORS[op]} ?", [bound_value]


def compile_record_filter(
    record_type: RecordType, filter_list: Any
) -> CompiledFilter | None:
    """
    Compile a card's `filter` config into a parameterized SQL WHERE fragment.

    `filter_list` must be a list of single-key {field_key: value} maps,
    AND-combined (every item must match). Returns None if filter_list is
    falsy (no filtering configured). Raises FilterError - never a bare
    exception - on any problem, so callers can map it straight to a
    websocket `send_error`.
    """
    if not filter_list:
        return None
    if not isinstance(filter_list, list):
        msg = "'filter' must be a list of single-key field maps"
        raise FilterError(ERR_INVALID_ITEM, msg)

    conditions: list[str] = []
    params: list[Any] = []
    try:
        for item in filter_list:
            if not isinstance(item, dict) or len(item) != 1:
                msg = (
                    "Each 'filter' item must be a single-key map, "
                    "e.g. {field_key: value}"
                )
                raise FilterError(ERR_INVALID_ITEM, msg)  # noqa: TRY301
            ((field_key, raw_value),) = item.items()
            if not isinstance(field_key, str):
                msg = "Filter field keys must be strings"
                raise FilterError(ERR_INVALID_ITEM, msg)  # noqa: TRY301
            sql, cond_params = _compile_condition(record_type, field_key, raw_value)
            conditions.append(f"({sql})")
            params.extend(cond_params)
    except FilterError:
        raise
    except Exception as err:
        msg = f"Invalid filter: {err}"
        raise FilterError(ERR_INVALID_ITEM, msg) from err

    return CompiledFilter(sql=" AND ".join(conditions), params=params)
