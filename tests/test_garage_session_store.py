"""HA storage keeps correlation facts, and exposes corrupt/lost storage."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pytest


ROOT = Path(__file__).parents[1] / "custom_components/roborock_plus"
PACKAGE = "garage_store_under_test"
package = ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
SPEC = importlib.util.spec_from_file_location(f"{PACKAGE}.garage_session_store", ROOT / "garage_session_store.py")
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
GarageSessionStore = MODULE.GarageSessionStore
from garage_store_under_test.garage_task import GarageTaskSession, GarageTaskError  # noqa: E402


class MemoryStore:
    def __init__(self, raw=None):
        self.raw = raw
        self.loads = 0
        self.saves = []
        self.load_error = None
        self.save_error = None

    async def async_load(self):
        self.loads += 1
        if self.load_error:
            raise self.load_error
        return deepcopy(self.raw)

    async def async_save(self, value):
        if self.save_error:
            raise self.save_error
        self.saves.append(deepcopy(value))
        self.raw = deepcopy(value)


def payload():
    task = GarageTaskSession(5)
    task.begin({"records": [10], "clean_count": 3}, utc=100)
    return task.to_dict()


def run(coro):
    return asyncio.run(coro)


def test_missing_store_is_distinct_from_existing_device_session():
    backend = MemoryStore()
    store = GarageSessionStore(None, store=backend)
    run(store.async_load())
    assert store.get("robot-1") is None
    assert backend.loads == 1
    run(store.async_load())
    assert backend.loads == 1


def test_round_trip_by_duid_without_mutable_aliases():
    backend = MemoryStore()
    store = GarageSessionStore(None, store=backend)
    data = payload()
    run(store.async_save("robot-1", data))
    saved = deepcopy(data)
    data["baseline_records"].append(99)
    assert store.get("robot-1") == saved
    value = store.get("robot-1")
    value["baseline_records"].append(98)
    assert store.get("robot-1") == saved
    assert backend.raw == {"robot-1": saved}
    reopened = GarageSessionStore(None, store=backend)
    run(reopened.async_load())
    assert reopened.get("robot-1") == saved


def test_load_validation_is_atomic_and_failed_load_can_be_retried():
    backend = MemoryStore({"robot-1": payload(), "broken": {"version": 999}})
    store = GarageSessionStore(None, store=backend)
    with pytest.raises(GarageTaskError):
        run(store.async_load())
    with pytest.raises(GarageTaskError):
        store.get("robot-1")
    backend.raw.pop("broken")
    run(store.async_load())
    assert store.get("robot-1") == backend.raw["robot-1"]
    assert backend.loads == 2


@pytest.mark.parametrize("raw", [[], "garbage", 0, False, {"": payload()},
    {1: payload()}, {"robot-1": None}, {"robot-1": {**payload(), "deadline": 1000}}])
def test_invalid_storage_is_never_silently_empty(raw):
    store = GarageSessionStore(None, store=MemoryStore(raw))
    with pytest.raises(GarageTaskError):
        run(store.async_load())


def test_io_errors_are_visible_and_failed_save_does_not_change_cache():
    backend = MemoryStore()
    store = GarageSessionStore(None, store=backend)
    backend.load_error = OSError("disk read failed")
    with pytest.raises(OSError, match="disk read failed"):
        run(store.async_load())
    backend.load_error = None
    run(store.async_load())
    backend.save_error = OSError("disk write failed")
    with pytest.raises(OSError, match="disk write failed"):
        run(store.async_save("robot-1", payload()))
    assert store.get("robot-1") is None
    assert backend.raw is None


def test_get_before_load_cannot_masquerade_as_first_install():
    store = GarageSessionStore(None, store=MemoryStore())
    with pytest.raises(GarageTaskError) as exc:
        store.get("robot-1")
    assert exc.value.reason == "session_recovery_required"


def test_saving_one_device_preserves_other_sessions():
    old = payload()
    backend = MemoryStore({"robot-1": old})
    store = GarageSessionStore(None, store=backend)
    new = payload()
    run(store.async_save("robot-2", new))
    assert backend.raw == {"robot-1": old, "robot-2": new}


def test_concurrent_saves_are_serialized_without_lost_updates():
    backend = MemoryStore()
    store = GarageSessionStore(None, store=backend)
    first, second = payload(), payload()

    async def save_both():
        await asyncio.gather(store.async_save("one", first), store.async_save("two", second))

    run(save_both())
    assert backend.loads == 1
    assert backend.raw == {"one": first, "two": second}


def test_only_ha_store_backend_is_loaded_lazily(monkeypatch):
    storage = ModuleType("homeassistant.helpers.storage")
    calls = []

    def make_store(hass, version, key):
        calls.append((hass, version, key))
        return MemoryStore()

    storage.Store = make_store
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.storage", storage)
    store = GarageSessionStore("hass")
    run(store.async_load())
    assert calls == [("hass", 1, "roborock_plus_garage_sessions")]


@pytest.mark.parametrize("duid", [None, 1, "", " "])
def test_invalid_device_keys_are_rejected(duid):
    store = GarageSessionStore(None, store=MemoryStore())
    with pytest.raises(GarageTaskError):
        run(store.async_save(duid, payload()))
