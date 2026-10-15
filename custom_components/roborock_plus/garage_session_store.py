"""Versioned HA storage for garage-session correlation facts only."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .garage_task import GarageTaskError, normalize_session_payload


STORAGE_VERSION = 1
STORAGE_KEY = "roborock_plus_garage_sessions"


def _check_duid(duid: Any) -> None:
    if not isinstance(duid, str) or not duid.strip():
        raise GarageTaskError("session_recovery_required")


class GarageSessionStore:
    """Keep per-device facts; reads and failed writes never authorize motion."""

    def __init__(self, hass: Any, *, store: Any = None) -> None:
        self.hass = hass
        if store is None:
            # Pure schema/session tests do not need a Home Assistant runtime.
            from homeassistant.helpers.storage import Store

            store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._store = store
        self._sessions: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    async def _async_load_locked(self) -> None:
        if self._loaded:
            return
        raw = await self._store.async_load()
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise GarageTaskError("session_recovery_required")
        loaded = {}
        for duid, payload in raw.items():
            _check_duid(duid)
            loaded[duid] = normalize_session_payload(payload)
        self._sessions = loaded
        self._loaded = True

    async def async_load(self) -> None:
        """Load once; errors propagate and a failed load remains retryable."""
        async with self._lock:
            await self._async_load_locked()

    def get(self, duid: str) -> dict[str, Any] | None:
        """Return detached loaded facts; None means this duid is absent."""
        _check_duid(duid)
        if not self._loaded:
            raise GarageTaskError("session_recovery_required")
        return deepcopy(self._sessions.get(duid))

    async def async_save(self, duid: str, payload: Mapping[str, Any]) -> None:
        """Atomically save valid facts, preserving sessions for other robots."""
        _check_duid(duid)
        record = normalize_session_payload(payload)
        async with self._lock:
            await self._async_load_locked()
            updated = {**self._sessions, duid: record}
            await self._store.async_save(deepcopy(updated))
            self._sessions = updated
