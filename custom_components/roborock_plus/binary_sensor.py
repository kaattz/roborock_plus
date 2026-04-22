"""Support for Roborock sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from roborock.data import CleanFluidStatus, RoborockStateCode
from roborock.roborock_message import RoborockZeoProtocol

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import ATTR_BATTERY_CHARGING, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .coordinator import (
    RoborockConfigEntry,
    RoborockDataUpdateCoordinator,
    RoborockDataUpdateCoordinatorA01,
    RoborockWashingMachineUpdateCoordinator,
)
from .entity import RoborockCoordinatedEntityA01, RoborockCoordinatedEntityV1
from .models import DeviceState
from .safe_zone import point_in_safe_zone
from .safe_zone_store import DISPATCH_SAFE_ZONE_UPDATED, get_safe_zone_store

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class RoborockBinarySensorDescription(BinarySensorEntityDescription):
    """A class that describes Roborock binary sensors."""

    value_fn: Callable[[DeviceState], bool | int | None]
    """A function that extracts the sensor value from DeviceState."""

    is_dock_entity: bool = False
    """Whether this sensor is for the dock."""


@dataclass(frozen=True, kw_only=True)
class RoborockBinarySensorDescriptionA01(BinarySensorEntityDescription):
    """A class that describes Roborock A01 binary sensors."""

    data_protocol: RoborockZeoProtocol
    value_fn: Callable[[StateType], bool]


BINARY_SENSOR_DESCRIPTIONS = [
    RoborockBinarySensorDescription(
        key="dry_status",
        translation_key="mop_drying_status",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.status.dry_status,
        is_dock_entity=True,
    ),
    RoborockBinarySensorDescription(
        key="water_box_carriage_status",
        translation_key="mop_attached",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.status.water_box_carriage_status,
    ),
    RoborockBinarySensorDescription(
        key="water_box_status",
        translation_key="water_box_attached",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.status.water_box_status,
    ),
    RoborockBinarySensorDescription(
        key="water_shortage",
        translation_key="water_shortage",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.status.water_shortage_status,
    ),
    RoborockBinarySensorDescription(
        key="dirty_box_full",
        translation_key="dirty_box_full",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.status.dirty_water_box_status,
        is_dock_entity=True,
    ),
    RoborockBinarySensorDescription(
        key="clean_box_empty",
        translation_key="clean_box_empty",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.status.clear_water_box_status,
        is_dock_entity=True,
    ),
    RoborockBinarySensorDescription(
        key="clean_fluid_empty",
        translation_key="clean_fluid_empty",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (
            data.status.clean_fluid_status == CleanFluidStatus.empty_not_installed
            if data.status.clean_fluid_status is not None
            else None
        ),
        is_dock_entity=True,
    ),
    RoborockBinarySensorDescription(
        key="in_cleaning",
        translation_key="in_cleaning",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.status.in_cleaning,
    ),
    RoborockBinarySensorDescription(
        key=ATTR_BATTERY_CHARGING,
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: (
            data.status.state
            in (RoborockStateCode.charging, RoborockStateCode.charging_complete)
        ),
    ),
]


ZEO_BINARY_SENSOR_DESCRIPTIONS: list[RoborockBinarySensorDescriptionA01] = [
    RoborockBinarySensorDescriptionA01(
        key="detergent_empty",
        data_protocol=RoborockZeoProtocol.DETERGENT_EMPTY,
        device_class=BinarySensorDeviceClass.PROBLEM,
        translation_key="detergent_empty",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=bool,
    ),
    RoborockBinarySensorDescriptionA01(
        key="softener_empty",
        data_protocol=RoborockZeoProtocol.SOFTENER_EMPTY,
        device_class=BinarySensorDeviceClass.PROBLEM,
        translation_key="softener_empty",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=bool,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: RoborockConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Roborock vacuum binary sensors."""
    entities: list[BinarySensorEntity] = [
        RoborockBinarySensorEntity(
            coordinator,
            description,
        )
        for coordinator in config_entry.runtime_data.v1
        for description in BINARY_SENSOR_DESCRIPTIONS
        # Note: Currently coordinator.data is always available on startup but won't be in the future
        if (
            coordinator.data is not None
            and description.value_fn(coordinator.data) is not None
        )
    ]
    entities.extend(
        RoborockBinarySensorEntityA01(
            coordinator,
            description,
        )
        for coordinator in config_entry.runtime_data.a01
        if isinstance(coordinator, RoborockWashingMachineUpdateCoordinator)
        for description in ZEO_BINARY_SENSOR_DESCRIPTIONS
        if description.data_protocol in coordinator.request_protocols
    )
    entities.extend(
        [
            RoborockHasSafeZoneBinarySensorEntity(coordinator),
            RoborockInSafeZoneBinarySensorEntity(coordinator),
        ]
        for coordinator in config_entry.runtime_data.v1
    )
    async_add_entities(entities)


class RoborockBinarySensorEntity(RoborockCoordinatedEntityV1, BinarySensorEntity):
    """Representation of a Roborock binary sensor."""

    entity_description: RoborockBinarySensorDescription

    def __init__(
        self,
        coordinator: RoborockDataUpdateCoordinator,
        description: RoborockBinarySensorDescription,
    ) -> None:
        """Initialize the entity."""
        super().__init__(
            f"{description.key}_{coordinator.duid_slug}",
            coordinator,
            is_dock_entity=description.is_dock_entity,
        )
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return the value reported by the sensor."""
        if (data := self.coordinator.data) is not None:
            return bool(self.entity_description.value_fn(data))
        return None


class RoborockBinarySensorEntityA01(RoborockCoordinatedEntityA01, BinarySensorEntity):
    """Representation of a A01 Roborock binary sensor."""

    entity_description: RoborockBinarySensorDescriptionA01

    def __init__(
        self,
        coordinator: RoborockDataUpdateCoordinatorA01,
        description: RoborockBinarySensorDescriptionA01,
    ) -> None:
        """Initialize the entity."""
        self.entity_description = description
        super().__init__(f"{description.key}_{coordinator.duid_slug}", coordinator)

    @property
    def is_on(self) -> bool:
        """Return the value reported by the sensor."""
        value = self.coordinator.data[self.entity_description.data_protocol]
        return self.entity_description.value_fn(value)


class RoborockSafeZoneBinarySensorBase(RoborockCoordinatedEntityV1, BinarySensorEntity):
    """Base entity for safe-zone binary sensors."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_added_to_hass(self) -> None:
        """Load store state and subscribe to changes."""
        await super().async_added_to_hass()
        await get_safe_zone_store(self.hass).async_load()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DISPATCH_SAFE_ZONE_UPDATED}_{self.coordinator.duid}",
                self._handle_safe_zone_update,
            )
        )

    @callback
    def _handle_safe_zone_update(self) -> None:
        """Update state after the safe zone changes."""
        self.async_write_ha_state()


class RoborockHasSafeZoneBinarySensorEntity(RoborockSafeZoneBinarySensorBase):
    """Whether a safe zone is configured."""

    _attr_translation_key = "has_safe_zone"

    def __init__(self, coordinator: RoborockDataUpdateCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(f"has_safe_zone_{coordinator.duid_slug}", coordinator)

    @property
    def is_on(self) -> bool:
        """Return True if a safe zone exists."""
        return (
            get_safe_zone_store(self.hass).get_loaded(self.coordinator.duid) is not None
        )


class RoborockInSafeZoneBinarySensorEntity(RoborockSafeZoneBinarySensorBase):
    """Whether the robot is inside the configured safe zone."""

    _attr_translation_key = "in_safe_zone"

    def __init__(self, coordinator: RoborockDataUpdateCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(f"in_safe_zone_{coordinator.duid_slug}", coordinator)

    @property
    def is_on(self) -> bool | None:
        """Return True if the robot position is inside the configured safe zone."""
        if (
            (stored := get_safe_zone_store(self.hass).get_loaded(self.coordinator.duid))
            is None
        ):
            return False
        map_content_trait = self.coordinator.properties_api.map_content
        if (
            map_content_trait.map_data is None
            or map_content_trait.map_data.vacuum_position is None
        ):
            return None
        position = map_content_trait.map_data.vacuum_position
        return point_in_safe_zone(position.x, position.y, stored.zone)
