"""Safe-zone storage for Roborock Plus."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .safe_zone import SafeZone

STORAGE_KEY = f"{DOMAIN}_safe_zone"
STORAGE_VERSION = 1
DISPATCH_SAFE_ZONE_UPDATED = f"{DOMAIN}_safe_zone_updated"


@dataclass(frozen=True)
class StoredSafeZone:
    """Persisted safe-zone record."""

    duid: str
    zone: SafeZone
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        """Convert the record to a serializable mapping."""
        return {
            "duid": self.duid,
            "zone": self.zone.as_dict(),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StoredSafeZone":
        """Restore a record from storage."""
        return cls(
            duid=str(data["duid"]),
            zone=SafeZone.from_dict(data["zone"]),
            updated_at=str(data["updated_at"]),
        )


class SafeZoneStore:
    """Safe-zone persistence wrapper."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the store."""
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._zones: dict[str, StoredSafeZone] = {}
        self._loaded = False

    async def async_load(self) -> None:
        """Load storage once."""
        if self._loaded:
            return
        self._loaded = True
        raw = await self._store.async_load() or {}
        self._zones = {
            duid: StoredSafeZone.from_dict(record) for duid, record in raw.items()
        }

    async def async_get(self, duid: str) -> StoredSafeZone | None:
        """Get a safe zone by device id."""
        await self.async_load()
        return self._zones.get(duid)

    @callback
    def get_loaded(self, duid: str) -> StoredSafeZone | None:
        """Get a safe zone without triggering I/O."""
        return self._zones.get(duid)

    async def async_set(self, duid: str, zone: SafeZone) -> StoredSafeZone:
        """Persist a safe zone."""
        await self.async_load()
        record = StoredSafeZone(
            duid=duid,
            zone=zone,
            updated_at=dt_util.utcnow().isoformat(),
        )
        self._zones[duid] = record
        await self._async_save()
        async_dispatcher_send(self.hass, f"{DISPATCH_SAFE_ZONE_UPDATED}_{duid}")
        return record

    async def async_clear(self, duid: str) -> None:
        """Remove a safe zone."""
        await self.async_load()
        self._zones.pop(duid, None)
        await self._async_save()
        async_dispatcher_send(self.hass, f"{DISPATCH_SAFE_ZONE_UPDATED}_{duid}")

    async def _async_save(self) -> None:
        """Write the store."""
        await self._store.async_save(
            {duid: record.as_dict() for duid, record in self._zones.items()}
        )


@callback
def get_safe_zone_store(hass: HomeAssistant) -> SafeZoneStore:
    """Return the singleton safe-zone store."""
    store = hass.data.get(STORAGE_KEY)
    if store is None:
        store = hass.data[STORAGE_KEY] = SafeZoneStore(hass)
    return store
