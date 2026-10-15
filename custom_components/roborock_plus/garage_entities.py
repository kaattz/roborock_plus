"""Native, push-updated garage diagnostics on the bound robot's device."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory

from .entity import RoborockEntity


GARAGE_PHASES = (
    "disabled", "recovering", "idle", "opening", "awaiting_start", "task_open",
    "outside_closed", "returning", "docked_intermission", "closing",
    "waiting_close_permission", "blocked", "manual_hold",
)
BINARY_KEYS = (
    "garage_open_confirmed", "garage_closed_confirmed",
    "garage_passage_allowed", "garage_problem",
)


def _configured_binding(coordinator: Any) -> Mapping[str, Any] | None:
    bindings = coordinator.config_entry.options.get("garage_bindings", {})
    binding = bindings.get(coordinator.duid) if isinstance(bindings, Mapping) else None
    if isinstance(binding, Mapping) and binding.get("enabled") is True:
        return binding
    return None


class GarageDiagnosticEntity(RoborockEntity):
    """Keep faults visible even if robot data or garage setup is unavailable."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Any, key: str) -> None:
        super().__init__(f"{key}_{coordinator.duid_slug}", coordinator.device_info)
        self.coordinator = coordinator
        self._attr_translation_key = key

    @property
    def _runtime(self) -> Any:
        return getattr(self.coordinator, "garage_runtime", None)

    @property
    def _controller(self) -> Any:
        return getattr(self._runtime, "controller", None)

    @property
    def _setup_error(self) -> str | None:
        return getattr(self.coordinator, "garage_setup_error", None)

    @property
    def _phase(self) -> str:
        if self._setup_error:
            return "blocked"
        return getattr(self._controller, "phase", "recovering")

    async def async_added_to_hass(self) -> None:
        """Dispose the runtime subscription along with the entity."""
        await super().async_added_to_hass()
        if (runtime := self._runtime) is not None:
            self.async_on_remove(runtime.add_listener(self.async_write_ha_state))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Show source and quality separately from physical confirmation."""
        runtime = self._runtime
        controller = self._controller
        state = getattr(controller, "state", None)
        binding = getattr(runtime, "binding", None) or _configured_binding(self.coordinator) or {}
        attributes = {
            "session_id": getattr(getattr(runtime, "session", None), "session_id", None),
            "operation_id": getattr(controller, "operation_id", None),
            "phase": self._phase,
            "reason": self._setup_error or getattr(controller, "reason", None),
            "cover_entity_id": binding.get("cover_entity_id"),
            "task_door_mode": binding.get("task_door_mode", "hold_open"),
        }
        for direction in ("open", "close"):
            endpoint = getattr(state, direction, None)
            attributes[f"{direction}_quality"] = getattr(endpoint, "quality", "unknown")
            attributes[f"{direction}_source"] = getattr(endpoint, "source", binding.get(f"{direction}_source"))
            attributes[f"{direction}_reason"] = getattr(endpoint, "reason", None)
        return attributes


class GarageBinarySensor(GarageDiagnosticEntity, BinarySensorEntity):
    """Independent confirmed endpoints, policy permission, and blocking faults."""

    def __init__(self, coordinator: Any, key: str) -> None:
        super().__init__(coordinator, key)
        if key == "garage_problem":
            self._attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self) -> bool | None:
        controller = self._controller
        if self._attr_translation_key == "garage_problem":
            if self._setup_error:
                return True
            if controller is None:
                return None
            return bool(controller.reason or controller.phase in ("blocked", "manual_hold"))
        if controller is None:
            return None
        if self._attr_translation_key == "garage_passage_allowed":
            return controller.passage_allowed
        state = controller.state
        if state is None:
            return None
        if self._attr_translation_key == "garage_open_confirmed":
            return state.open_confirmed
        return state.closed_confirmed


class GaragePhaseSensor(GarageDiagnosticEntity, SensorEntity):
    """The garage controller's current workflow phase."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(GARAGE_PHASES)

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, "garage_phase")

    @property
    def native_value(self) -> str:
        return self._phase


def build_garage_binary_entities(coordinators: Iterable[Any]) -> list[GarageBinarySensor]:
    """Include invalid enabled bindings so their setup fault stays visible."""
    return [
        GarageBinarySensor(coordinator, key)
        for coordinator in coordinators if _configured_binding(coordinator) is not None
        for key in BINARY_KEYS
    ]


def build_garage_sensor_entities(coordinators: Iterable[Any]) -> list[GaragePhaseSensor]:
    return [
        GaragePhaseSensor(coordinator)
        for coordinator in coordinators if _configured_binding(coordinator) is not None
    ]
