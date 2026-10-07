"""Roborock Coordinator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any, TypeVar

from propcache.api import cached_property
from roborock import B01Props
from roborock.data import HomeDataScene
from roborock.devices.device import RoborockDevice
from roborock.devices.traits.a01 import DyadApi, ZeoApi
from roborock.devices.traits.b01 import Q7PropertiesApi, Q10PropertiesApi
from roborock.devices.traits.v1 import PropertiesApi
from roborock.exceptions import RoborockDeviceBusy, RoborockException
from roborock.roborock_message import (
    RoborockB01Props,
    RoborockDyadDataProtocol,
    RoborockZeoProtocol,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_CONNECTIONS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util, slugify

from .const import (
    A01_UPDATE_INTERVAL,
    CONF_BASE_URL,
    DOMAIN,
    IMAGE_CACHE_INTERVAL,
    Q10_UPDATE_INTERVAL,
    V1_CLOUD_IN_CLEANING_INTERVAL,
    V1_CLOUD_NOT_CLEANING_INTERVAL,
    V1_LOCAL_NOT_CLEANING_INTERVAL,
)
from .models import DeviceState, get_device_info
from .server_url import is_official_cloud_url
from .v1_diagnostics import install_v1_raw_message_diagnostics
from .v1_map_position import (
    is_v1_map_position_fresh,
    resolve_map_position_intervals,
    should_refresh_v1_map_position,
)
from .v1_status_polling import (
    get_v1_local_status_poll_interval,
    should_refresh_full_v1_data,
)
from .v1_stuck_detection import (
    EVENT_VACUUM_STUCK,
    StuckTracker,
    build_stuck_event_data,
    resolve_stuck_options,
    should_assess_movement,
)
from .v1_task_state import is_v1_task_active

SCAN_INTERVAL = timedelta(seconds=30)

# Roborock devices have a known issue where they go offline for a short period
# around 3AM local time for ~1 minute and reset both the local connection
# and MQTT connection. To avoid log spam, we will avoid reporting failures refreshing
# data until this duration has passed.
MIN_UNAVAILABLE_DURATION = timedelta(minutes=2)

_LOGGER = logging.getLogger(__name__)


@dataclass
class RoborockCoordinators:
    """Roborock coordinators type."""

    v1: list[RoborockDataUpdateCoordinator]
    a01: list[RoborockDataUpdateCoordinatorA01]
    b01_q7: list[RoborockB01Q7UpdateCoordinator]
    b01_q10: list[RoborockB01Q10UpdateCoordinator]

    def values(
        self,
    ) -> list[
        RoborockDataUpdateCoordinator
        | RoborockDataUpdateCoordinatorA01
        | RoborockB01Q7UpdateCoordinator
        | RoborockB01Q10UpdateCoordinator
    ]:
        """Return all coordinators."""
        return self.v1 + self.a01 + self.b01_q7 + self.b01_q10


type RoborockConfigEntry = ConfigEntry[RoborockCoordinators]


class RoborockDataUpdateCoordinator(DataUpdateCoordinator[DeviceState | None]):
    """Class to manage fetching data from the API."""

    config_entry: RoborockConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: RoborockConfigEntry,
        device: RoborockDevice,
        properties_api: PropertiesApi,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            # Assume we can use the local api.
            update_interval=get_v1_local_status_poll_interval(config_entry.options),
        )
        self._device = device
        self.properties_api = properties_api
        self._local_status_poll_interval = get_v1_local_status_poll_interval(
            config_entry.options
        )
        self.device_info = get_device_info(device)
        if mac := properties_api.network_info.mac:
            self.device_info[ATTR_CONNECTIONS] = {
                (dr.CONNECTION_NETWORK_MAC, dr.format_mac(mac))
            }
        self.last_update_state: str | None = None
        # Keep track of last attempt to refresh maps/rooms to know when to try again.
        self._last_home_update_attempt: datetime
        self.last_home_update: datetime | None = None
        # Tracks when the vacuum map position was last sampled, so safe-zone
        # entities can reject a position that is too old to act on.
        self._last_map_position_attempt: datetime | None = None
        self.last_map_position_update: datetime | None = None
        self._map_position_task: asyncio.Task[None] | None = None
        # Reports a robot that should be moving but is not, which is how a
        # robot pressed against the cabinet door shows up.
        self._stuck_options = resolve_stuck_options(config_entry.options)
        self._stuck_tracker = StuckTracker(
            window=self._stuck_options.window,
            radius=self._stuck_options.radius,
        )
        self._stuck_event_sent = False
        # Tracks the last successful update to control when we report failure
        # to the base class. This is reset on successful data update.
        self._last_update_success_time: datetime | None = None
        self._last_full_update_success_time: datetime | None = None
        self._has_connected_locally: bool = False
        self.last_resume_command: str | None = None
        self._remove_v1_diagnostics_ready_callback = None
        self._setup_v1_raw_message_diagnostics()

    def _setup_v1_raw_message_diagnostics(self) -> None:
        """Install raw V1 message logging when the device channel is ready."""
        install_v1_raw_message_diagnostics(self._device)
        add_ready_callback = getattr(self._device, "add_ready_callback", None)
        if callable(add_ready_callback):
            self._remove_v1_diagnostics_ready_callback = add_ready_callback(
                install_v1_raw_message_diagnostics
            )

    @cached_property
    def is_official_cloud(self) -> bool:
        """Return whether this entry talks to Roborock's own servers.

        Map content is fetched over MQTT with no local fallback (the trait is
        marked `map_rpc_channel` upstream), so every map read counts against
        whichever server is configured. Only a base URL we can prove is
        self-hosted is allowed to poll aggressively.
        """
        return is_official_cloud_url(self.config_entry.data.get(CONF_BASE_URL))

    @cached_property
    def dock_device_info(self) -> DeviceInfo:
        """Gets the device info for the dock.

        This must happen after the coordinator does the first update.
        Which will be the case when this is called.
        """
        dock_type = self.properties_api.status.dock_type
        return DeviceInfo(
            name=f"{self._device.device_info.name} Dock",
            identifiers={(DOMAIN, f"{self.duid}_dock")},
            manufacturer="Roborock",
            model=f"{self._device.product.model} Dock",
            model_id=str(dock_type.value) if dock_type is not None else "Unknown",
            sw_version=self._device.device_info.fv,
        )

    async def _async_setup(self) -> None:
        """Set up the coordinator."""
        await self._verify_api()
        try:
            await self.properties_api.status.refresh()
        except RoborockException as err:
            _LOGGER.debug("Failed to update data during setup: %s", err)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_data_fail",
            ) from err

        self._last_home_update_attempt = dt_util.utcnow()

        # This populates a cache of maps/rooms so we have the information
        # even for maps that are inactive but is a no-op if we already have
        # the information. This will cycle through all the available maps and
        # requires the device to be idle. If the device is busy cleaning, then
        # we'll retry later in `update_map` and in the mean time we won't have
        # all map/room information.
        try:
            await self.properties_api.home.discover_home()
        except RoborockDeviceBusy:
            _LOGGER.info("Home discovery skipped while device is busy/cleaning")
        except RoborockException as err:
            _LOGGER.debug("Failed to get maps: %s", err)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="map_failure",
                translation_placeholders={"error": str(err)},
            ) from err
        else:
            # Force a map refresh on first setup
            self.last_home_update = dt_util.utcnow() - IMAGE_CACHE_INTERVAL

    async def update_map(self) -> None:
        """Update the currently selected map."""
        try:
            await self.properties_api.home.discover_home()
            await self.properties_api.home.refresh()
        except RoborockException as ex:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="map_failure",
            ) from ex
        else:
            self.last_home_update = dt_util.utcnow()
            # A full home refresh also refreshes the current map content, which
            # carries the vacuum position used by the safe-zone entities.
            self.last_map_position_update = dt_util.utcnow()
            self._last_map_position_attempt = dt_util.utcnow()

    async def _verify_api(self) -> None:
        """Verify that the api is reachable."""
        if self._device.is_connected:
            self._has_connected_locally |= self._device.is_local_connected
            if self._has_connected_locally:
                async_delete_issue(
                    self.hass, DOMAIN, f"cloud_api_used_{self.duid_slug}"
                )
            else:
                self.update_interval = V1_CLOUD_NOT_CLEANING_INTERVAL
                async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"cloud_api_used_{self.duid_slug}",
                    is_fixable=False,
                    severity=IssueSeverity.WARNING,
                    translation_key="cloud_api_used",
                    translation_placeholders={"device_name": self._device.name},
                    learn_more_url="https://www.home-assistant.io/integrations/roborock/#the-integration-tells-me-it-cannot-reach-my-vacuum-and-is-using-the-cloud-api-and-that-this-is-not-supported-or-i-am-having-any-networking-issues",
                )

    async def _update_device_prop(self) -> None:
        """Update device properties."""
        await _refresh_traits(
            [
                trait
                for trait in (
                    self.properties_api.status,
                    self.properties_api.consumables,
                    self.properties_api.clean_summary,
                    self.properties_api.dnd,
                    self.properties_api.dust_collection_mode,
                    self.properties_api.wash_towel_mode,
                    self.properties_api.smart_wash_params,
                    self.properties_api.sound_volume,
                    self.properties_api.child_lock,
                    self.properties_api.flow_led_status,
                    self.properties_api.valley_electricity_timer,
                )
                if trait is not None
            ]
        )
        _LOGGER.debug("Updated device properties")

    async def _update_status(self) -> None:
        """Update only the status trait used by vacuum/status/running entities."""
        await _refresh_traits([self.properties_api.status])
        _LOGGER.debug("Updated device status")

    async def _async_update_data(self) -> DeviceState | None:
        """Update data via library."""
        await self._verify_api()
        update_time = dt_util.utcnow()
        try:
            # Update device props and standard api information
            if self._should_refresh_full_data(update_time):
                await self._update_device_prop()
                self._last_full_update_success_time = update_time
            else:
                await self._update_status()
        except UpdateFailed:
            if self._should_suppress_update_failure():
                _LOGGER.debug(
                    "Suppressing update failure until unavailable duration passed"
                )
                return self.data
            raise

        # If the vacuum is currently cleaning and it has been IMAGE_CACHE_INTERVAL
        # since the last map update, you can update the map.
        new_status = self.properties_api.status
        if (
            new_status.in_cleaning
            and (dt_util.utcnow() - self._last_home_update_attempt)
            > IMAGE_CACHE_INTERVAL
        ) or self.last_update_state != new_status.state_name:
            await self._async_update_map_if_due()

        self._schedule_map_position_update(new_status)

        if self._device.is_local_connected:
            self.update_interval = self._local_status_poll_interval
        elif self.properties_api.status.in_cleaning:
            self.update_interval = V1_CLOUD_IN_CLEANING_INTERVAL
        else:
            self.update_interval = V1_CLOUD_NOT_CLEANING_INTERVAL
        self.last_update_state = self.properties_api.status.state_name
        self._last_update_success_time = dt_util.utcnow()
        _LOGGER.debug("Data update successful %s", self._last_update_success_time)
        return DeviceState(
            status=self.properties_api.status,
            dnd_timer=self.properties_api.dnd,
            consumable=self.properties_api.consumables,
            clean_summary=self.properties_api.clean_summary,
        )

    def _should_refresh_full_data(self, update_time: datetime) -> bool:
        """Return whether this poll should refresh all V1 traits."""
        if not self._device.is_local_connected:
            return True
        return should_refresh_full_v1_data(
            now=update_time,
            last_full_update=self._last_full_update_success_time,
            full_update_interval=V1_LOCAL_NOT_CLEANING_INTERVAL,
        )

    async def _async_update_map_if_due(self) -> None:
        """Refresh the full home/map data and record the attempt."""
        self._last_home_update_attempt = dt_util.utcnow()
        try:
            await self.update_map()
        except HomeAssistantError as err:
            _LOGGER.debug("Failed to update map: %s", err)

    def _schedule_map_position_update(self, status: Any) -> None:
        """Sample the vacuum position in the background when it is due.

        The safe-zone entities decide whether it is safe to close the garage
        door from the vacuum's position, which is only carried in the map
        content. That content is otherwise refreshed only on a full map update,
        which is skipped while the device is busy cleaning -- exactly when the
        position matters most.

        Map reads always travel to the configured server (the trait is marked
        `map_rpc_channel` upstream and has no local fallback), so the cadence
        comes from `is_official_cloud`: aggressive only against a self-hosted
        server, conservative against Roborock's own.

        The read runs as a background task so a slow (or timing out) map RPC
        cannot delay the fast status poll that the same automations rely on.
        """
        if not should_refresh_v1_map_position(
            now=dt_util.utcnow(),
            last_attempt=self._last_map_position_attempt,
            task_active=is_v1_task_active(
                state=status.state,
                in_cleaning=status.in_cleaning,
                in_returning=status.in_returning,
            ),
            is_official_cloud=self.is_official_cloud,
            options=self.config_entry.options,
        ):
            return
        if self._map_position_task is not None and not self._map_position_task.done():
            return

        self._last_map_position_attempt = dt_util.utcnow()
        self._map_position_task = self.config_entry.async_create_background_task(
            self.hass,
            self._async_update_map_position(),
            name=f"{DOMAIN}_map_position_{self.duid_slug}",
        )

    async def _async_update_map_position(self) -> None:
        """Read the map content so the vacuum position is current."""
        try:
            await self.properties_api.map_content.refresh()
        except (RoborockException, ValueError) as err:
            # A failed sample leaves the previous position in place; the age
            # check in `is_map_position_fresh` stops trusting it in time.
            _LOGGER.debug("Failed to refresh map position: %s", err)
        else:
            self.last_map_position_update = dt_util.utcnow()
            self._observe_stuck_detection()

    def _observe_stuck_detection(self) -> None:
        """Feed the freshly sampled position to the stuck detector.

        Runs only after a successful read, so a failed refresh cannot supply a
        repeated position and be mistaken for the robot standing still.
        """
        options = self._stuck_options
        if not options.enabled:
            return

        status = self.properties_api.status
        position = self.properties_api.map_content.map_data
        position = None if position is None else position.vacuum_position

        was_stuck = self._stuck_tracker.is_stuck
        stuck = self._stuck_tracker.observe(
            now=dt_util.utcnow(),
            sample_time=self.last_map_position_update,
            x=None if position is None else position.x,
            y=None if position is None else position.y,
            should_move=should_assess_movement(state=status.state),
        )

        if not stuck:
            self._stuck_event_sent = False
            return

        if was_stuck and self._stuck_event_sent:
            return
        self._stuck_event_sent = True
        self.hass.bus.async_fire(
            EVENT_VACUUM_STUCK,
            build_stuck_event_data(
                entity_id=f"vacuum.{self.duid_slug}",
                x=None if position is None else position.x,
                y=None if position is None else position.y,
                state=status.state,
                seconds_stuck=self._stuck_tracker.seconds_stuck(dt_util.utcnow()),
                entry_id=getattr(self.config_entry, "entry_id", None),
            ),
        )

    @property
    def is_stuck(self) -> bool:
        """Return whether the robot is believed to be stuck."""
        if not self._stuck_options.enabled:
            return False
        return self._stuck_tracker.is_stuck

    @property
    def is_stuck_detection_enabled(self) -> bool:
        """Return whether stuck detection is configured on."""
        return self._stuck_options.enabled

    @property
    def stuck_seconds(self) -> float | None:
        """Return how long the robot has been stuck, if it is."""
        return self._stuck_tracker.seconds_stuck(dt_util.utcnow())

    def is_map_position_fresh(self) -> bool:
        """Return whether the last sampled vacuum position may be trusted."""
        status = self.properties_api.status
        active_interval, _ = resolve_map_position_intervals(
            self.config_entry.options,
            is_official_cloud=self.is_official_cloud,
        )
        return is_v1_map_position_fresh(
            now=dt_util.utcnow(),
            position_time=self.last_map_position_update,
            task_active=is_v1_task_active(
                state=status.state,
                in_cleaning=status.in_cleaning,
                in_returning=status.in_returning,
            ),
            is_official_cloud=self.is_official_cloud,
            active_interval=active_interval,
        )

    def _should_suppress_update_failure(self) -> bool:
        """Determine if we should suppress update failure reporting.

        We suppress reporting update failures until a minimum duration has
        passed since the last successful update. This is used to avoid reporting
        the device as unavailable for short periods, a known issue.

        The intent is to apply to routine background state refreshes and not
        other failures such as the first update or map updates.
        """
        if self._last_update_success_time is None:
            # Never had a successful update, do not suppress
            return False
        failure_duration = dt_util.utcnow() - self._last_update_success_time
        _LOGGER.debug("Update failure duration: %s", failure_duration)
        return failure_duration < MIN_UNAVAILABLE_DURATION

    async def get_routines(self) -> list[HomeDataScene]:
        """Get routines."""
        try:
            return await self.properties_api.routines.get_routines()
        except RoborockException as err:
            _LOGGER.error("Failed to get routines %s", err)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={
                    "command": "get_scenes",
                },
            ) from err

    async def execute_routines(self, routine_id: int) -> None:
        """Execute routines."""
        try:
            await self.properties_api.routines.execute_routine(routine_id)
        except RoborockException as err:
            _LOGGER.error("Failed to execute routines %s %s", routine_id, err)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={
                    "command": "execute_scene",
                },
            ) from err

    @cached_property
    def duid(self) -> str:
        """Get the unique id of the device as specified by Roborock."""
        return self._device.duid

    @cached_property
    def duid_slug(self) -> str:
        """Get the slug of the duid."""
        return slugify(self.duid)

    @property
    def device(self) -> RoborockDevice:
        """Get the RoborockDevice."""
        return self._device


async def _refresh_traits(traits: list[Any]) -> None:
    """Refresh a list of traits serially.

    We refresh traits serially to avoid overloading the cloud servers or device
    with requests. If any single trait fails to refresh, we stop the whole
    update process and raise UpdateFailed.
    """
    for trait in traits:
        try:
            await trait.refresh()
        except RoborockException as ex:
            _LOGGER.debug(
                "Failed to update data (%s): %s", trait.__class__.__name__, ex
            )
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_data_fail",
            ) from ex


_V = TypeVar("_V", bound=RoborockDyadDataProtocol | RoborockZeoProtocol)


class RoborockDataUpdateCoordinatorA01(DataUpdateCoordinator[dict[_V, StateType]]):
    """Class to manage fetching data from the API for A01 devices."""

    config_entry: RoborockConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: RoborockConfigEntry,
        device: RoborockDevice,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=A01_UPDATE_INTERVAL,
        )
        self._device = device
        self.device_info = get_device_info(device)
        self.request_protocols: list[_V] = []

    @cached_property
    def duid(self) -> str:
        """Get the unique id of the device as specified by Roborock."""
        return self._device.duid

    @cached_property
    def duid_slug(self) -> str:
        """Get the slug of the duid."""
        return slugify(self.duid)

    @property
    def device(self) -> RoborockDevice:
        """Get the RoborockDevice."""
        return self._device


class RoborockWashingMachineUpdateCoordinator(
    RoborockDataUpdateCoordinatorA01[RoborockZeoProtocol]
):
    """Coordinator for Zeo devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: RoborockConfigEntry,
        device: RoborockDevice,
        api: ZeoApi,
    ) -> None:
        """Initialize."""
        super().__init__(hass, config_entry, device)
        self.api = api
        self.request_protocols: list[RoborockZeoProtocol] = []
        # This currently only supports the washing machine protocols
        self.request_protocols = [
            RoborockZeoProtocol.STATE,
            RoborockZeoProtocol.COUNTDOWN,
            RoborockZeoProtocol.WASHING_LEFT,
            RoborockZeoProtocol.ERROR,
            RoborockZeoProtocol.TIMES_AFTER_CLEAN,
            RoborockZeoProtocol.DETERGENT_EMPTY,
            RoborockZeoProtocol.SOFTENER_EMPTY,
            RoborockZeoProtocol.DETERGENT_TYPE,
            RoborockZeoProtocol.SOFTENER_TYPE,
            RoborockZeoProtocol.MODE,
            RoborockZeoProtocol.PROGRAM,
            RoborockZeoProtocol.TEMP,
            RoborockZeoProtocol.RINSE_TIMES,
            RoborockZeoProtocol.SPIN_LEVEL,
            RoborockZeoProtocol.DRYING_MODE,
            RoborockZeoProtocol.SOUND_SET,
        ]

    async def _async_update_data(
        self,
    ) -> dict[RoborockZeoProtocol, StateType]:
        try:
            return await self.api.query_values(self.request_protocols)
        except RoborockException as ex:
            _LOGGER.debug("Failed to update washing machine data: %s", ex)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_data_fail",
            ) from ex


class RoborockWetDryVacUpdateCoordinator(
    RoborockDataUpdateCoordinatorA01[RoborockDyadDataProtocol]
):
    """Coordinator for Dyad devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: RoborockConfigEntry,
        device: RoborockDevice,
        api: DyadApi,
    ) -> None:
        """Initialize."""
        super().__init__(hass, config_entry, device)
        self.api = api
        # This currenltly only supports the WetDryVac protocols
        self.request_protocols: list[RoborockDyadDataProtocol] = [
            RoborockDyadDataProtocol.STATUS,
            RoborockDyadDataProtocol.POWER,
            RoborockDyadDataProtocol.MESH_LEFT,
            RoborockDyadDataProtocol.BRUSH_LEFT,
            RoborockDyadDataProtocol.ERROR,
            RoborockDyadDataProtocol.TOTAL_RUN_TIME,
        ]

    async def _async_update_data(
        self,
    ) -> dict[RoborockDyadDataProtocol, StateType]:
        try:
            return await self.api.query_values(self.request_protocols)
        except RoborockException as ex:
            _LOGGER.debug("Failed to update wet dry vac data: %s", ex)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_data_fail",
            ) from ex


class RoborockDataUpdateCoordinatorB01(DataUpdateCoordinator[B01Props]):
    """Class to manage fetching data from the API for B01 devices."""

    config_entry: RoborockConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: RoborockConfigEntry,
        device: RoborockDevice,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=A01_UPDATE_INTERVAL,
        )
        self._device = device
        self.device_info = get_device_info(device)

    @cached_property
    def duid(self) -> str:
        """Get the unique id of the device as specified by Roborock."""
        return self._device.duid

    @cached_property
    def duid_slug(self) -> str:
        """Get the slug of the duid."""
        return slugify(self.duid)

    @property
    def device(self) -> RoborockDevice:
        """Get the RoborockDevice."""
        return self._device


class RoborockB01Q7UpdateCoordinator(RoborockDataUpdateCoordinatorB01):
    """Coordinator for B01 Q7 devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: RoborockConfigEntry,
        device: RoborockDevice,
        api: Q7PropertiesApi,
    ) -> None:
        """Initialize."""
        super().__init__(hass, config_entry, device)
        self.api = api
        self.request_protocols: list[RoborockB01Props] = [
            RoborockB01Props.STATUS,
            RoborockB01Props.MAIN_BRUSH,
            RoborockB01Props.SIDE_BRUSH,
            RoborockB01Props.DUST_BAG_USED,
            RoborockB01Props.MOP_LIFE,
            RoborockB01Props.MAIN_SENSOR,
            RoborockB01Props.CLEANING_TIME,
            RoborockB01Props.REAL_CLEAN_TIME,
            RoborockB01Props.HYPA,
            RoborockB01Props.WIND,
            RoborockB01Props.WATER,
            RoborockB01Props.MODE,
            RoborockB01Props.QUANTITY,
        ]

    async def _async_update_data(
        self,
    ) -> B01Props:
        try:
            data = await self.api.query_values(self.request_protocols)
        except RoborockException as ex:
            _LOGGER.debug("Failed to update Q7 data: %s", ex)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_data_fail",
            ) from ex
        if data is None:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_data_fail",
            )
        return data


class RoborockB01Q10UpdateCoordinator(DataUpdateCoordinator[None]):
    """Coordinator for B01 Q10 devices.

    The Q10 uses push-based MQTT status updates. The `refresh()` call sends a
    REQUEST_DPS command (fire-and-forget) to solicit a status push from the
    device; the response arrives asynchronously through the MQTT subscribe loop.

    Entities manage their own state updates through listening to individual
    traits on the Q10PropertiesApi. Each trait has its own update listener
    that will notify the entity of changes.
    """

    config_entry: RoborockConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: RoborockConfigEntry,
        device: RoborockDevice,
        api: Q10PropertiesApi,
    ) -> None:
        """Initialize RoborockB01Q10UpdateCoordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=Q10_UPDATE_INTERVAL,
        )
        self._device = device
        self.api = api
        self.device_info = get_device_info(device)

    async def _async_update_data(self) -> None:
        """Request a status push from the device.

        This coordinator does not wait for any specific MQTT payload because
        push messages are asynchronous and not guaranteed to contain every
        field. Entities subscribe to trait updates and update as values arrive.
        """
        try:
            await self.api.refresh()
        except RoborockException as ex:
            _LOGGER.debug("Failed to request Q10 data: %s", ex)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="request_fail",
            ) from ex

    @cached_property
    def duid(self) -> str:
        """Get the unique id of the device as specified by Roborock."""
        return self._device.duid

    @cached_property
    def duid_slug(self) -> str:
        """Get the slug of the duid."""
        return slugify(self.duid)

    @property
    def device(self) -> RoborockDevice:
        """Get the RoborockDevice."""
        return self._device
