"""Support for Roborock image."""

from datetime import datetime
from functools import partial
import logging
from typing import Any

from roborock.devices.traits.v1.home import HomeTrait
from roborock.devices.traits.v1.map_content import MapContent

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import RoborockConfigEntry, RoborockDataUpdateCoordinator
from .entity import RoborockCoordinatedEntityV1
from .v1_map_render import rerender_map_image

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: RoborockConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Roborock image platform."""

    async_add_entities(
        (
            RoborockMap(
                config_entry,
                coord,
                coord.properties_api.home,
                map_info.map_flag,
                map_info.name,
            )
            for coord in config_entry.runtime_data.v1
            if coord.properties_api.home is not None
            for map_info in (coord.properties_api.home.home_map_info or {}).values()
        ),
    )


class RoborockMap(RoborockCoordinatedEntityV1, ImageEntity):
    """A class to let you visualize the map."""

    _attr_has_entity_name = True
    image_last_updated: datetime
    _attr_name: str

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: RoborockDataUpdateCoordinator,
        home_trait: HomeTrait,
        map_flag: int,
        map_name: str,
    ) -> None:
        """Initialize a Roborock map."""
        map_name = map_name or f"Map {map_flag}"
        # Note: Map names are not a valid unique id since they can be changed
        # in the roborock app. This should be migrated to use map flag for
        # the unique id.
        unique_id = f"{coordinator.duid_slug}_map_{map_name}"
        RoborockCoordinatedEntityV1.__init__(self, unique_id, coordinator)
        ImageEntity.__init__(self, coordinator.hass)
        self.config_entry = config_entry
        self._attr_name = map_name
        self._home_trait = home_trait
        self.map_flag = map_flag
        self.cached_map: bytes | None = None
        # Keyed on the inputs that decide the rendering, so a dashboard polling
        # the image does not re-render an identical map on every fetch.
        self._corrected_cache: tuple[tuple[Any, ...], bytes] | None = None
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_image_last_updated = None

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass load any previously cached maps from disk."""
        await super().async_added_to_hass()
        self._attr_image_last_updated = self.coordinator.last_home_update
        self.async_write_ha_state()

    @property
    def _map_content(self) -> MapContent | None:
        if self._home_trait.home_map_content and (
            map_content := self._home_trait.home_map_content.get(self.map_flag)
        ):
            return map_content
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator.

        If the coordinator has updated the map, we can update the image.
        """
        if self.coordinator.data is None or (map_content := self._map_content) is None:
            return
        if self.cached_map != map_content.image_content:
            self.cached_map = map_content.image_content
            self._attr_image_last_updated = self.coordinator.last_home_update
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        """Get the map image, with the robot marker corrected if it is wrong.

        The library bakes the robot marker into the PNG at parse time, from the
        same `map_data.vacuum_position` that `v1_position_trust` distrusts. That
        leaves the image showing a docked robot in whichever room the previous
        task ended in, which is how someone reading the map concludes the door
        is clear to close. A marker that is already drawn cannot be moved, so
        the raw map bytes are patched and re-parsed instead; see
        `v1_map_render`. Any failure returns the library's own image.
        """
        if (map_content := self._map_content) is None:
            raise HomeAssistantError("Map flag not found in coordinator maps")

        image = map_content.image_content
        map_data = map_content.map_data
        resolved = self.coordinator.resolve_vacuum_position()

        drawn = (
            None
            if map_data is None or map_data.vacuum_position is None
            else (map_data.vacuum_position.x, map_data.vacuum_position.y)
        )

        cache_key = (
            id(map_content.raw_api_response),
            drawn,
            resolved.x,
            resolved.y,
            resolved.trusted,
            resolved.from_dock,
        )
        if (
            self._corrected_cache is not None
            and self._corrected_cache[0] == cache_key
        ):
            return self._corrected_cache[1]

        # Re-parsing and re-encoding a map is real CPU work -- PNG decode, a
        # full redraw, PNG encode -- and `async_image` runs on the event loop,
        # so it goes to an executor. `rerender_map_image` is pure byte handling
        # with no I/O, so it is safe to hand across threads.
        corrected = await self.hass.async_add_executor_job(
            partial(
                rerender_map_image,
                raw=map_content.raw_api_response,
                drawn=drawn,
                resolved_x=resolved.x,
                resolved_y=resolved.y,
                trusted=resolved.trusted,
                from_dock=resolved.from_dock,
                parse=self._parse_map_bytes,
            )
        )
        if corrected is None:
            return image
        self._corrected_cache = (cache_key, corrected)
        return corrected

    def _parse_map_bytes(self, raw: bytes) -> Any:
        """Parse map bytes through the same converter the library uses."""
        converter = getattr(self._map_content_trait, "converter", None)
        if converter is None:
            raise HomeAssistantError("Map converter unavailable")
        return converter.parse_map_content(raw)

    @property
    def _map_content_trait(self) -> Any:
        """Return the map content trait that owns the map parser."""
        return self.coordinator.properties_api.map_content
