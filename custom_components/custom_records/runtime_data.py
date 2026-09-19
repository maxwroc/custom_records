"""Runtime data attached to the custom_records config entry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from collections.abc import Callable

    from .media_store import MediaStore
    from .models import RecordType
    from .store import RecordStorage

type CustomRecordsConfigEntry = ConfigEntry[CustomRecordsRuntimeData]


@dataclass
class CustomRecordsRuntimeData:
    """Runtime (non-persisted) data for a config entry."""

    storage: RecordStorage
    media_store: MediaStore
    record_types: dict[str, RecordType] = field(default_factory=dict)
    unsub_purge_interval: Callable[[], None] | None = None
