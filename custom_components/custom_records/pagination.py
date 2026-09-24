"""Bounded, runtime-local cursors for live record pagination."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sys
from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING

from .const import (
    PAGINATION_CURSOR_IDLE_SECONDS,
    PAGINATION_CURSOR_MAX_BYTES,
    PAGINATION_CURSOR_MAX_ENTRIES,
    RecordOrder,
)
from .sql_encoding import to_epoch_micros

if TYPE_CHECKING:
    from datetime import datetime

    from .sql_encoding import CompiledFilter

_HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_ENTRY_OVERHEAD_BYTES = 256
_INVALID_CURSOR = "invalid_cursor"
_CURSOR_EXPIRED = "cursor_expired"
_CURSOR_QUERY_MISMATCH = "cursor_query_mismatch"
_RESOURCE_LIMIT = "pagination_resource_limit"


class CursorError(ValueError):
    """A cursor failure with a public WebSocket error code."""

    def __init__(self, code: str, message: str) -> None:
        """Preserve the error code and user-facing explanation."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RecordPosition:
    """An exact SQLite ordering key, independent of the row's continued existence."""

    timestamp_micros: int
    record_id: str


@dataclass(slots=True)
class _CursorState:
    scope: str
    position: RecordPosition
    last_used: float
    size: int = 0


def query_scope(  # noqa: PLR0913 (one argument per query dimension)
    entry_id: str,
    record_type_id: str,
    start: datetime | None,
    end: datetime | None,
    where: CompiledFilter | None,
    order: RecordOrder,
) -> str:
    """Fingerprint the validated query, excluding page size and cursor."""
    encoded = json.dumps(
        [
            order.value,
            entry_id,
            record_type_id,
            to_epoch_micros(start) if start is not None else None,
            to_epoch_micros(end) if end is not None else None,
            [where.sql, where.params] if where is not None else None,
        ],
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CursorCache:
    """Retryable opaque handles, isolated by each integration runtime's lifetime."""

    def __init__(
        self,
        *,
        max_entries: int = PAGINATION_CURSOR_MAX_ENTRIES,
        max_bytes: int = PAGINATION_CURSOR_MAX_BYTES,
        idle_seconds: float = PAGINATION_CURSOR_IDLE_SECONDS,
    ) -> None:
        """Initialize a bounded LRU cache; no database snapshots are retained."""
        if max_entries < 1 or max_bytes < 1 or idle_seconds <= 0:
            msg = "Cursor cache limits must be positive"
            raise ValueError(msg)
        self._entries: OrderedDict[str, _CursorState] = OrderedDict()
        self._bytes = 0
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._idle_seconds = idle_seconds

    def _evict_oldest(self) -> None:
        _, state = self._entries.popitem(last=False)
        self._bytes -= state.size

    def _expire(self, now: float) -> None:
        while self._entries:
            state = next(iter(self._entries.values()))
            if now - state.last_used < self._idle_seconds:
                break
            self._evict_oldest()

    def resolve(self, handle: object, scope: str) -> RecordPosition:
        """Look up and renew a matching handle without consuming its position."""
        if not isinstance(handle, str) or not _HANDLE_PATTERN.fullmatch(handle):
            raise CursorError(
                _INVALID_CURSOR, "Cursor must be an opaque pagination handle"
            )
        now = monotonic()
        self._expire(now)
        state = self._entries.get(handle)
        if state is None:
            raise CursorError(
                _CURSOR_EXPIRED,
                "Cursor expired or is no longer available; restart history",
            )
        if state.scope != scope:
            raise CursorError(
                _CURSOR_QUERY_MISMATCH, "Cursor does not belong to this query"
            )
        state.last_used = now
        self._entries.move_to_end(handle)
        return state.position

    def issue(self, scope: str, position: RecordPosition) -> str:
        """Store an exact position, evicting old handles within fixed budgets."""
        now = monotonic()
        self._expire(now)
        handle = secrets.token_urlsafe(32)
        while handle in self._entries:
            handle = secrets.token_urlsafe(32)
        state = _CursorState(scope, position, now)
        state.size = _ENTRY_OVERHEAD_BYTES + sum(
            sys.getsizeof(value)
            for value in (
                handle,
                state,
                scope,
                position,
                position.timestamp_micros,
                position.record_id,
                now,
                state.size,
            )
        )
        if state.size > self._max_bytes:
            raise CursorError(
                _RESOURCE_LIMIT,
                "Record cursor exceeds the pagination memory budget",
            )
        while self._entries and (
            len(self._entries) >= self._max_entries
            or self._bytes + state.size > self._max_bytes
        ):
            self._evict_oldest()
        self._entries[handle] = state
        self._bytes += state.size
        return handle
