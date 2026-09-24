"""Tests for custom_records.filter_query (compiles to SQL, plan_sql.md Phase 2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from homeassistant.core import HomeAssistant

from custom_components.custom_records.const import FieldType, RecordOrder
from custom_components.custom_records.filter_query import (
    FilterError,
    compile_record_filter,
)
from custom_components.custom_records.models import FieldDefinition, RecordType
from custom_components.custom_records.store import RecordStorage

RECORD_TYPE = RecordType(
    id="widgets",
    name="Widgets",
    fields=[
        FieldDefinition(key="count", label="Count", type=FieldType.NUMBER),
        FieldDefinition(key="name", label="Name", type=FieldType.TEXT),
        FieldDefinition(key="notes", label="Notes", type=FieldType.LONG_TEXT),
        FieldDefinition(key="active", label="Active", type=FieldType.BOOLEAN),
        FieldDefinition(key="seen_at", label="Seen At", type=FieldType.DATETIME),
        FieldDefinition(
            key="mood",
            label="Mood",
            type=FieldType.SINGLE_SELECT,
            options=["happy", "sad"],
        ),
        FieldDefinition(
            key="tags",
            label="Tags",
            type=FieldType.MULTI_SELECT,
            options=["a", "b", "c"],
        ),
        FieldDefinition(key="photo", label="Photo", type=FieldType.IMAGE),
    ],
)


# -- compilation-only tests (SQL fragment/params shape) -----------------------


def test_no_filter_configured_returns_none() -> None:
    """A falsy filter_list (None, [], omitted) means 'no filtering'."""
    assert compile_record_filter(RECORD_TYPE, None) is None
    assert compile_record_filter(RECORD_TYPE, []) is None


def test_native_scalar_value_implies_equals() -> None:
    """A non-string YAML value (int/float/bool) is used directly with implied '=='."""
    where = compile_record_filter(RECORD_TYPE, [{"count": 5}])
    assert where is not None
    assert where.params == [5.0]
    assert '"count"' in where.sql


def test_string_value_without_operator_implies_equals() -> None:
    """A plain string value (no operator prefix) means '=='."""
    where = compile_record_filter(RECORD_TYPE, [{"name": "Max"}])
    assert where is not None
    assert where.params == ["Max"]


@pytest.mark.parametrize("raw_value", ["== 5", "!= 5", "> 5", ">= 5", "< 5", "<= 5"])
def test_number_operators_compile(raw_value: str) -> None:
    """Every comparison operator compiles to a bound SQL fragment for NUMBER."""
    where = compile_record_filter(RECORD_TYPE, [{"count": raw_value}])
    assert where is not None
    assert where.params == [5.0]


def test_operator_prefix_is_longest_match_first() -> None:
    """'>=' must not be mis-split into '>' plus a leftover '=value'."""
    where_ge = compile_record_filter(RECORD_TYPE, [{"count": ">=30"}])
    assert where_ge is not None
    assert ">=" in where_ge.sql
    where_gt = compile_record_filter(RECORD_TYPE, [{"count": ">30"}])
    assert where_gt is not None
    assert ">=" not in where_gt.sql


def test_multiple_items_are_and_combined() -> None:
    """Every list item must match (AND-combined)."""
    where = compile_record_filter(RECORD_TYPE, [{"count": "> 10"}, {"name": "Max"}])
    assert where is not None
    assert " AND " in where.sql
    assert where.params == [10.0, "Max"]


def test_text_field_only_supports_eq_ne() -> None:
    """TEXT fields reject ordering operators."""
    where = compile_record_filter(RECORD_TYPE, [{"name": "!= Max"}])
    assert where is not None

    with pytest.raises(FilterError) as exc_info:
        compile_record_filter(RECORD_TYPE, [{"name": "> Max"}])
    assert exc_info.value.code == "unsupported_filter_operator"


def test_boolean_field_only_supports_eq_ne() -> None:
    """BOOLEAN fields support '==' / '!=' with true/false values, encoded as 0/1."""
    where = compile_record_filter(RECORD_TYPE, [{"active": "== true"}])
    assert where is not None
    assert where.params == [1]

    with pytest.raises(FilterError) as exc_info:
        compile_record_filter(RECORD_TYPE, [{"active": "> true"}])
    assert exc_info.value.code == "unsupported_filter_operator"


def test_single_select_field_only_supports_eq_ne() -> None:
    """SINGLE_SELECT fields support '==' / '!=' against one of its options."""
    where = compile_record_filter(RECORD_TYPE, [{"mood": "happy"}])
    assert where is not None
    assert where.params == ["happy"]

    with pytest.raises(FilterError) as exc_info:
        compile_record_filter(RECORD_TYPE, [{"mood": ">= happy"}])
    assert exc_info.value.code == "unsupported_filter_operator"


def test_multi_select_equals_compiles_to_json_membership() -> None:
    """MULTI_SELECT '==' compiles to a NULL-safe json_each EXISTS membership check."""
    where = compile_record_filter(RECORD_TYPE, [{"tags": "a"}])
    assert where is not None
    assert "json_each" in where.sql
    assert "IS NOT NULL" in where.sql
    assert "NOT EXISTS" not in where.sql
    assert where.params == ["a"]


def test_multi_select_not_equals_compiles_to_negated_membership() -> None:
    """MULTI_SELECT '!=' compiles to a NULL-safe NOT EXISTS membership check."""
    where = compile_record_filter(RECORD_TYPE, [{"tags": "!= a"}])
    assert where is not None
    assert "NOT EXISTS" in where.sql
    assert "IS NOT NULL" in where.sql


def test_multi_select_rejects_ordering_operators() -> None:
    """MULTI_SELECT does not support '>'/'>='/'<'/'<='."""
    with pytest.raises(FilterError) as exc_info:
        compile_record_filter(RECORD_TYPE, [{"tags": "> a"}])
    assert exc_info.value.code == "unsupported_filter_operator"


def test_datetime_compiles_to_epoch_micros_comparison() -> None:
    """DATETIME filters compile to an INTEGER microsecond comparison."""
    where = compile_record_filter(
        RECORD_TYPE, [{"seen_at": "> 2026-01-01T00:00:00+00:00"}]
    )
    assert where is not None
    expected = datetime(2026, 1, 1, tzinfo=UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    assert where.params == [int((expected - epoch).total_seconds() * 1_000_000)]


def test_unknown_field_error() -> None:
    """Filtering on a field that doesn't exist on the record type errors clearly."""
    with pytest.raises(FilterError) as exc_info:
        compile_record_filter(RECORD_TYPE, [{"nope": 1}])
    assert exc_info.value.code == "unknown_filter_field"


def test_image_field_is_unsupported() -> None:
    """IMAGE fields can never be filtered on (internal reference object)."""
    with pytest.raises(FilterError) as exc_info:
        compile_record_filter(RECORD_TYPE, [{"photo": "x"}])
    assert exc_info.value.code == "unsupported_filter_field"


def test_invalid_value_coercion_error() -> None:
    """A filter value that can't be coerced to the field's type errors clearly."""
    with pytest.raises(FilterError) as exc_info:
        compile_record_filter(RECORD_TYPE, [{"count": "> notanumber"}])
    assert exc_info.value.code == "invalid_filter_value"


def test_non_list_filter_is_rejected() -> None:
    """`filter` must be a list, not e.g. a bare dict."""
    with pytest.raises(FilterError) as exc_info:
        compile_record_filter(RECORD_TYPE, {"count": 5})
    assert exc_info.value.code == "invalid_filter_item"


def test_multi_key_item_is_rejected() -> None:
    """A list item with more than one key is a hard validation error."""
    with pytest.raises(FilterError) as exc_info:
        compile_record_filter(RECORD_TYPE, [{"count": 1, "name": "Max"}])
    assert exc_info.value.code == "invalid_filter_item"


def test_non_dict_item_is_rejected() -> None:
    """A list item that isn't a mapping at all is a hard validation error."""
    with pytest.raises(FilterError) as exc_info:
        compile_record_filter(RECORD_TYPE, ["count"])
    assert exc_info.value.code == "invalid_filter_item"


# -- end-to-end tests against a real SQLite table -----------------------------


async def test_multi_select_membership_end_to_end(hass: HomeAssistant) -> None:
    """A compiled MULTI_SELECT filter matches/excludes real stored rows."""
    storage = RecordStorage(hass, "filter_e2e")
    await storage.async_load({RECORD_TYPE.id: RECORD_TYPE})
    await storage.async_add_record(RECORD_TYPE.id, {"tags": ["a", "b"]})
    await storage.async_add_record(RECORD_TYPE.id, {"tags": ["c"]})

    where = compile_record_filter(RECORD_TYPE, [{"tags": "a"}])
    matched = (
        await storage.async_list_records(
            RECORD_TYPE.id, where=where, order=RecordOrder.ASC
        )
    ).records
    assert [r["d"]["tags"] for r in matched] == [["a", "b"]]


async def test_missing_optional_field_matches_neither_eq_nor_ne(
    hass: HomeAssistant,
) -> None:
    """A record with a NULL/missing optional field matches neither '==' nor '!='."""
    storage = RecordStorage(hass, "filter_e2e_null")
    await storage.async_load({RECORD_TYPE.id: RECORD_TYPE})
    await storage.async_add_record(RECORD_TYPE.id, {})  # count left NULL

    eq_where = compile_record_filter(RECORD_TYPE, [{"count": 5}])
    ne_where = compile_record_filter(RECORD_TYPE, [{"count": "!= 5"}])
    assert (
        await storage.async_list_records(
            RECORD_TYPE.id, where=eq_where, order=RecordOrder.ASC
        )
    ).records == []
    assert (
        await storage.async_list_records(
            RECORD_TYPE.id, where=ne_where, order=RecordOrder.ASC
        )
    ).records == []
