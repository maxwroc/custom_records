"""
Serve and auto-register the bundled custom Lovelace card.

Uses hass.http.async_register_static_paths to serve the built JS file and
homeassistant.components.frontend.add_extra_js_url to inject it globally, so
the card is available as `type: custom:custom-records-card` in any dashboard
without the user ever needing to add a Lovelace "Resource" manually.

Note: es5=True must NOT be used here. HA's index.html only evaluates the
es5 extra-script list inside `if (!window.latestJS) { ... }`, i.e. it is a
legacy-browser-only fallback; in any modern browser it never runs at all,
which would silently break the card for virtually every real user.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

CARD_URL_PATH = f"/{DOMAIN}/custom-records-card.js"
CARD_FILE_PATH = Path(__file__).parent / "www" / "custom-records-card.js"
_FRONTEND_REGISTERED_KEY = f"{DOMAIN}_frontend_registered"


def _card_version_hash() -> str:
    """
    Return a short content hash of the card JS, used to bust browser/SW caches.

    Home Assistant's frontend service worker aggressively caches custom card
    URLs the first time they're loaded in a browser tab and never revisits
    them, so without a cache-busting query param, users (and devs iterating
    on the card) can keep seeing a stale copy indefinitely after an update.
    """
    digest = hashlib.sha256(CARD_FILE_PATH.read_bytes()).hexdigest()
    return digest[:8]


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Register the static path + extra JS module, once, hass-wide."""
    if hass.data.get(_FRONTEND_REGISTERED_KEY):
        return
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL_PATH, str(CARD_FILE_PATH), cache_headers=False)]
    )
    version = await hass.async_add_executor_job(_card_version_hash)
    add_extra_js_url(hass, f"{CARD_URL_PATH}?v={version}")
    hass.data[_FRONTEND_REGISTERED_KEY] = True
