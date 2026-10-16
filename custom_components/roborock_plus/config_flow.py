"""Config flow for Roborock Plus."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import logging
import math
from typing import Any

from roborock.data import UserData
from roborock.exceptions import (
    RoborockAccountDoesNotExist,
    RoborockException,
    RoborockInvalidCode,
    RoborockInvalidEmail,
    RoborockTooFrequentCodeRequests,
    RoborockUrlException,
)
from roborock.web_api import RoborockApiClient
import voluptuous as vol

from homeassistant.components.cover import CoverEntityFeature
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.helpers.translation import async_get_translations

from . import RoborockConfigEntry
from .clean_command_watch import (
    CONF_CLEAN_COMMAND_WATCH_TIMEOUT,
    DEFAULT_CLEAN_COMMAND_WATCH_TIMEOUT,
    MAX_CLEAN_COMMAND_WATCH_TIMEOUT,
)
from .const import (
    CONF_BASE_URL,
    CONF_ENTRY_CODE,
    CONF_REGION,
    CONF_ROBOROCK_SERVER_URL,
    CONF_SHOW_BACKGROUND,
    CONF_SHOW_ROOMS,
    CONF_SHOW_WALLS,
    CONF_USER_DATA,
    DEFAULT_DRAWABLES,
    DOMAIN,
    DRAWABLES,
    REGION_AUTO,
    REGION_CUSTOM,
    REGION_OPTIONS,
)
from .garage_guard import (
    CONF_GARAGE_DOOR_ENTITY_ID,
    CONF_GARAGE_GUARD_ENABLED,
)
from .garage_config import (
    CONF_GARAGE_BINDINGS,
    CONF_GARAGE_CONTROL_VERSION,
    EVIDENCE_SOURCES,
    GARAGE_CONTROL_VERSION,
    NUMERIC_RANGES,
    GarageConfigError,
    longest_endpoint_wait,
    required_timing_fields,
    suggested_timing_values,
    validate_binding,
    validate_binding_ownership,
    validate_legacy_guard_ownership,
)
from .server_url import (
    is_official_cloud_url,
    is_valid_server_url,
    normalize_server_url,
)
from .v1_map_position import (
    CONF_V1_MAP_POSITION_POLL_INTERVAL,
    DEFAULT_V1_MAP_POSITION_POLL_INTERVAL,
    MAX_V1_MAP_POSITION_POLL_INTERVAL,
    MIN_V1_MAP_POSITION_POLL_INTERVAL,
    resolve_map_position_intervals,
)
from .v1_status_polling import (
    CONF_V1_LOCAL_STATUS_POLL_INTERVAL,
    DEFAULT_V1_LOCAL_STATUS_POLL_INTERVAL,
    MAX_V1_LOCAL_STATUS_POLL_INTERVAL,
    MIN_V1_LOCAL_STATUS_POLL_INTERVAL,
)
from .v1_stuck_detection import (
    CONF_V1_STUCK_DETECTION_ENABLED,
    CONF_V1_STUCK_RADIUS,
    CONF_V1_STUCK_WINDOW,
    DEFAULT_V1_STUCK_RADIUS,
    DEFAULT_V1_STUCK_WINDOW,
    MAX_V1_STUCK_RADIUS,
    MAX_V1_STUCK_WINDOW,
    MIN_V1_STUCK_RADIUS,
    MIN_V1_STUCK_WINDOW,
)

_LOGGER = logging.getLogger(__name__)


class _OptionsNumberSelector(NumberSelector):
    """Render a number box; let the options step return localized errors.

    FlowManager validates the wire schema before invoking the step. Preserve
    missing/invalid input here so the step can name every offending field.
    """

    def __call__(self, value: Any) -> Any:
        return value


class RoborockFlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Roborock Plus."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._username: str | None = None
        self._client: RoborockApiClient | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            region = user_input[CONF_REGION]
            self._username = username
            _LOGGER.debug("Requesting code for Roborock account")
            if region == REGION_CUSTOM:
                return await self.async_step_custom_url()
            base_url = None
            if region != REGION_AUTO:
                base_url = f"https://{region}iot.roborock.com"
            self._client = RoborockApiClient(
                username,
                base_url=base_url,
                session=async_get_clientsession(self.hass),
            )
            errors = await self._request_code()
            if not errors:
                return await self.async_step_code()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_REGION, default=REGION_AUTO): SelectSelector(
                        SelectSelectorConfig(
                            options=REGION_OPTIONS,
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="region",
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_custom_url(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle custom server URL entry.

        Used for self-hosted Roborock servers such as Roborock Local Server,
        where the account is served from a user-provided base URL rather than
        one of the fixed regional cloud endpoints.
        """
        errors: dict[str, str] = {}
        assert self._username
        if user_input is not None:
            url = user_input[CONF_ROBOROCK_SERVER_URL]
            if not is_valid_server_url(url):
                errors[CONF_ROBOROCK_SERVER_URL] = "invalid_url_format"
            else:
                self._client = RoborockApiClient(
                    self._username,
                    base_url=normalize_server_url(url),
                    session=async_get_clientsession(self.hass),
                )
                errors = await self._request_code()
                if not errors:
                    return await self.async_step_code()

        return self.async_show_form(
            step_id="custom_url",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ROBOROCK_SERVER_URL,
                        default=(
                            user_input[CONF_ROBOROCK_SERVER_URL]
                            if user_input is not None
                            else "https://usiot.roborock.com"
                        ),
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                }
            ),
            errors=errors,
        )

    async def _request_code(self) -> dict:
        assert self._client
        errors: dict[str, str] = {}
        try:
            await self._client.request_code_v4()
        except RoborockAccountDoesNotExist:
            errors["base"] = "invalid_email"
        except RoborockUrlException:
            errors["base"] = "unknown_url"
        except RoborockInvalidEmail:
            errors["base"] = "invalid_email_format"
        except RoborockTooFrequentCodeRequests:
            errors["base"] = "too_frequent_code_requests"
        except RoborockException:
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown_roborock"
        except Exception:
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        return errors

    async def async_step_code(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        errors: dict[str, str] = {}
        assert self._client
        assert self._username
        if user_input is not None:
            code = user_input[CONF_ENTRY_CODE]
            _LOGGER.debug("Logging into Roborock account using email provided code")
            try:
                user_data = await self._client.code_login_v4(code)
            except RoborockInvalidCode:
                errors["base"] = "invalid_code"
            except RoborockAccountDoesNotExist:
                errors["base"] = "invalid_email_or_region"
            except RoborockException:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown_roborock"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(user_data.rruid)
                if self.source == SOURCE_REAUTH:
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                    reauth_entry = self._get_reauth_entry()
                    return self.async_update_reload_and_abort(
                        reauth_entry, data_updates={CONF_USER_DATA: user_data.as_dict()}
                    )
                self._abort_if_unique_id_configured(error="already_configured_account")
                return await self._create_entry(self._client, self._username, user_data)

        return self.async_show_form(
            step_id="code",
            data_schema=vol.Schema({vol.Required(CONF_ENTRY_CODE): str}),
            errors=errors,
        )

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Handle a flow started by a dhcp discovery."""
        await self._async_handle_discovery_without_unique_id()
        device_registry = dr.async_get(self.hass)
        # A connection is only unique per config entry, so ask for every device
        # matching it: a match owned by this domain means the vacuum is already
        # configured.
        devices = device_registry.async_get_devices(
            connections={
                (dr.CONNECTION_NETWORK_MAC, dr.format_mac(discovery_info.macaddress))
            }
        )
        if any(
            identifier[0] == DOMAIN
            for device in devices
            for identifier in device.identifiers
        ):
            return self.async_abort(reason="already_configured")
        return await self.async_step_user()

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Perform reauth upon an API authentication error."""
        self._username = entry_data[CONF_USERNAME]
        assert self._username
        self._client = RoborockApiClient(
            self._username,
            base_url=entry_data[CONF_BASE_URL],
            session=async_get_clientsession(self.hass),
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reauth dialog."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._request_code()
            if not errors:
                return await self.async_step_code()
        return self.async_show_form(step_id="reauth_confirm", errors=errors)

    async def _create_entry(
        self, client: RoborockApiClient, username: str, user_data: UserData
    ) -> ConfigFlowResult:
        """Finished config flow and create entry."""
        return self.async_create_entry(
            title=username,
            data={
                CONF_USERNAME: username,
                CONF_USER_DATA: user_data.as_dict(),
                CONF_BASE_URL: await client.base_url,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: RoborockConfigEntry,
    ) -> RoborockOptionsFlowHandler:
        """Create the options flow."""
        return RoborockOptionsFlowHandler(config_entry)


class RoborockOptionsFlowHandler(OptionsFlow):
    """Handle options; the config-entry update listener owns reloading."""

    def __init__(self, config_entry: RoborockConfigEntry) -> None:
        """Initialize options flow."""
        self.options = deepcopy(dict(config_entry.options))
        self._garage_duid: str | None = None
        self._garage_draft: dict[str, Any] | None = None
        self._drawables_input: dict[str, Any] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        return await self.async_step_drawables(user_input)

    async def async_step_drawables(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage status polling and map drawable options."""
        errors: dict[str, str] = {}
        invalid_fields: dict[str, str] = {}
        configure_garage = False
        if user_input is not None:
            user_input = dict(user_input)
            # Sections are presentation only; keep the existing options format
            # and accept flat submissions from already-open older forms.
            for group in ("monitoring", "map_display", "legacy_compatibility"):
                values = user_input.pop(group, None)
                if values is not None:
                    user_input.update(values)
            configure_garage = user_input.pop("do_configure_garage", False)
            numeric_ranges = {
                CONF_V1_LOCAL_STATUS_POLL_INTERVAL: (MIN_V1_LOCAL_STATUS_POLL_INTERVAL, MAX_V1_LOCAL_STATUS_POLL_INTERVAL),
                CONF_V1_MAP_POSITION_POLL_INTERVAL: (MIN_V1_MAP_POSITION_POLL_INTERVAL, MAX_V1_MAP_POSITION_POLL_INTERVAL),
                CONF_CLEAN_COMMAND_WATCH_TIMEOUT: (0, MAX_CLEAN_COMMAND_WATCH_TIMEOUT),
                CONF_V1_STUCK_WINDOW: (MIN_V1_STUCK_WINDOW, MAX_V1_STUCK_WINDOW),
                CONF_V1_STUCK_RADIUS: (MIN_V1_STUCK_RADIUS, MAX_V1_STUCK_RADIUS),
            }
            entered = {field: user_input.pop(field, None) for field in numeric_ranges}
            for field, (minimum, maximum) in numeric_ranges.items():
                value = entered[field]
                if value is None or value == "":
                    invalid_fields[field] = "required_field"
                    continue
                try:
                    number = float(value)
                except (TypeError, ValueError, OverflowError):
                    invalid_fields[field] = "invalid_monitoring_number"
                    continue
                if isinstance(value, bool) or not math.isfinite(number) or not number.is_integer() or not minimum <= number <= maximum:
                    invalid_fields[field] = "invalid_monitoring_number"
                    continue
                self.options[field] = int(number)
            self._drawables_input = None
            if invalid_fields:
                self._drawables_input = {
                    field: None if (
                        not isinstance(value, (int, float, str)) or isinstance(value, bool)
                        or isinstance(value, float) and not math.isfinite(value)
                    ) else value
                    for field, value in entered.items()
                }
                errors.update({f"monitoring.{field}": reason for field, reason in invalid_fields.items()})
                errors["base"] = (
                    "missing_monitoring_fields" if set(invalid_fields.values()) == {"required_field"}
                    else "invalid_monitoring_fields"
                )
            self.options[CONF_V1_STUCK_DETECTION_ENABLED] = bool(
                user_input.pop(CONF_V1_STUCK_DETECTION_ENABLED, self.options.get(CONF_V1_STUCK_DETECTION_ENABLED, True))
            )
            self.options[CONF_GARAGE_GUARD_ENABLED] = bool(
                user_input.pop(CONF_GARAGE_GUARD_ENABLED, self.options.get(CONF_GARAGE_GUARD_ENABLED, False))
            )
            self.options[CONF_GARAGE_DOOR_ENTITY_ID] = user_input.pop(
                CONF_GARAGE_DOOR_ENTITY_ID, self.options.get(CONF_GARAGE_DOOR_ENTITY_ID, "")
            )
            self.options[CONF_SHOW_BACKGROUND] = user_input.pop(CONF_SHOW_BACKGROUND)
            self.options[CONF_SHOW_ROOMS] = user_input.pop(CONF_SHOW_ROOMS)
            self.options[CONF_SHOW_WALLS] = user_input.pop(CONF_SHOW_WALLS)
            self.options.setdefault(DRAWABLES, {}).update(user_input)
            if not errors and self.options[CONF_GARAGE_GUARD_ENABLED] and (
                not self.options[CONF_GARAGE_DOOR_ENTITY_ID]
            ):
                errors["base"] = "garage_guard_missing_config"
            elif not errors:
                try:
                    validate_legacy_guard_ownership(
                        self.options, entry_id=self.config_entry.entry_id,
                        entries=self._garage_ownership_entries(),
                    )
                except GarageConfigError as error:
                    errors["base"] = error.reason
            if not errors:
                if configure_garage:
                    return await self.async_step_garage_select()
                return self.async_create_entry(title="", data=self.options)

        current_interval = self.options.get(
            CONF_V1_LOCAL_STATUS_POLL_INTERVAL,
            DEFAULT_V1_LOCAL_STATUS_POLL_INTERVAL,
        )
        current_map_interval = self.options.get(
            CONF_V1_MAP_POSITION_POLL_INTERVAL,
            DEFAULT_V1_MAP_POSITION_POLL_INTERVAL,
        )
        current_watch_timeout = self.options.get(
            CONF_CLEAN_COMMAND_WATCH_TIMEOUT,
            DEFAULT_CLEAN_COMMAND_WATCH_TIMEOUT,
        )
        current_stuck_window = self.options.get(
            CONF_V1_STUCK_WINDOW,
            DEFAULT_V1_STUCK_WINDOW,
        )
        current_stuck_radius = self.options.get(
            CONF_V1_STUCK_RADIUS,
            DEFAULT_V1_STUCK_RADIUS,
        )
        form_values = {**self.options, **(self._drawables_input or {})}
        data_schema = {
            vol.Optional(
                CONF_V1_LOCAL_STATUS_POLL_INTERVAL,
                description={"suggested_value": form_values.get(CONF_V1_LOCAL_STATUS_POLL_INTERVAL, current_interval)},
            ): _OptionsNumberSelector(
                NumberSelectorConfig(
                    min=MIN_V1_LOCAL_STATUS_POLL_INTERVAL,
                    max=MAX_V1_LOCAL_STATUS_POLL_INTERVAL,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                ),
            ),
            vol.Optional(
                CONF_V1_MAP_POSITION_POLL_INTERVAL,
                description={"suggested_value": form_values.get(CONF_V1_MAP_POSITION_POLL_INTERVAL, current_map_interval)},
            ): _OptionsNumberSelector(
                NumberSelectorConfig(
                    min=MIN_V1_MAP_POSITION_POLL_INTERVAL,
                    max=MAX_V1_MAP_POSITION_POLL_INTERVAL,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                ),
            ),
            vol.Optional(
                CONF_CLEAN_COMMAND_WATCH_TIMEOUT,
                description={"suggested_value": form_values.get(CONF_CLEAN_COMMAND_WATCH_TIMEOUT, current_watch_timeout)},
            ): _OptionsNumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=MAX_CLEAN_COMMAND_WATCH_TIMEOUT,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                ),
            ),
            vol.Optional(
                CONF_V1_STUCK_WINDOW,
                description={"suggested_value": form_values.get(CONF_V1_STUCK_WINDOW, current_stuck_window)},
            ): _OptionsNumberSelector(
                NumberSelectorConfig(
                    min=MIN_V1_STUCK_WINDOW,
                    max=MAX_V1_STUCK_WINDOW,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                ),
            ),
            vol.Optional(
                CONF_V1_STUCK_RADIUS,
                description={"suggested_value": form_values.get(CONF_V1_STUCK_RADIUS, current_stuck_radius)},
            ): _OptionsNumberSelector(
                NumberSelectorConfig(
                    min=MIN_V1_STUCK_RADIUS,
                    max=MAX_V1_STUCK_RADIUS,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                ),
            ),
        }
        data_schema[
            vol.Required(
                CONF_V1_STUCK_DETECTION_ENABLED,
                default=self.options.get(CONF_V1_STUCK_DETECTION_ENABLED, True),
            )
        ] = bool
        for drawable, default_value in DEFAULT_DRAWABLES.items():
            data_schema[
                vol.Required(
                    drawable.value,
                    default=self.options.get(DRAWABLES, {}).get(
                        drawable, default_value
                    ),
                )
            ] = bool
        data_schema[
            vol.Required(
                CONF_SHOW_BACKGROUND,
                default=self.options.get(CONF_SHOW_BACKGROUND, False),
            )
        ] = bool
        data_schema[
            vol.Required(
                CONF_SHOW_ROOMS,
                default=self.options.get(CONF_SHOW_ROOMS, True),
            )
        ] = bool
        data_schema[
            vol.Required(
                CONF_SHOW_WALLS,
                default=self.options.get(CONF_SHOW_WALLS, True),
            )
        ] = bool
        monitoring_fields = {
            CONF_V1_LOCAL_STATUS_POLL_INTERVAL, CONF_V1_MAP_POSITION_POLL_INTERVAL,
            CONF_CLEAN_COMMAND_WATCH_TIMEOUT, CONF_V1_STUCK_DETECTION_ENABLED,
            CONF_V1_STUCK_WINDOW, CONF_V1_STUCK_RADIUS,
        }
        monitoring_schema = {
            marker: value for marker, value in data_schema.items()
            if marker.schema in monitoring_fields
        }
        map_schema = {
            marker: value for marker, value in data_schema.items()
            if marker.schema not in monitoring_fields
        }
        data_schema = {
            vol.Optional("do_configure_garage", default=configure_garage): bool,
            vol.Required("monitoring"): section(
                vol.Schema(monitoring_schema), {"collapsed": False}
            ),
            vol.Required("map_display"): section(
                vol.Schema(map_schema), {"collapsed": True}
            ),
        }
        is_official_cloud = is_official_cloud_url(
            self.config_entry.data.get(CONF_BASE_URL)
        )
        resolved_map_interval, _ = resolve_map_position_intervals(
            self.options,
            is_official_cloud=is_official_cloud,
        )
        placeholders = {
            "current_interval": str(current_interval),
            "current_map_interval": str(int(resolved_map_interval.total_seconds())),
        }
        if invalid_fields:
            translations = await async_get_translations(
                self.hass, self.hass.config.language, "options", {DOMAIN}
            )
            prefix = f"component.{DOMAIN}.options.step.{DRAWABLES}.sections.monitoring.data."
            placeholders["fields"] = ", ".join(translations[prefix + field] for field in invalid_fields)
        return self.async_show_form(
            step_id=DRAWABLES,
            data_schema=vol.Schema(data_schema),
            errors=errors,
            description_placeholders=placeholders,
        )

    def _garage_devices(self) -> dict[str, str]:
        """Offer only this entry's supported V1 coordinators."""
        runtime = getattr(self.config_entry, "runtime_data", None)
        return {
            coordinator.duid: coordinator.device.name
            for coordinator in getattr(runtime, "v1", ())
        }

    async def async_step_garage_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the robot explicitly, including for single-robot entries."""
        devices = self._garage_devices()
        if not devices:
            return self.async_abort(reason="no_supported_v1")
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("duid") not in devices:
                errors["base"] = "unsupported_robot"
            else:
                self._garage_duid = user_input["duid"]
                self._garage_draft = None
                return await self.async_step_garage()
        return self.async_show_form(
            step_id="garage_select",
            data_schema=vol.Schema({
                vol.Required("duid"): SelectSelector(SelectSelectorConfig(
                    options=[{"value": duid, "label": name} for duid, name in devices.items()],
                    mode=SelectSelectorMode.DROPDOWN,
                )),
            }),
            errors=errors,
        )

    def _garage_ownership_entries(self) -> list[tuple[str, Mapping[str, Any], tuple[str, ...]]]:
        """Use pending options for this entry and stored options for all others."""
        entries = []
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            runtime = getattr(entry, "runtime_data", None)
            entries.append((
                entry.entry_id,
                self.options if entry.entry_id == self.config_entry.entry_id else entry.options,
                tuple(coordinator.duid for coordinator in getattr(runtime, "v1", ())),
            ))
        return entries

    def _validate_garage_ownership(self, binding: dict[str, Any]) -> None:
        """Check saved bindings and unresolved legacy users across entries."""
        validate_binding_ownership(
            binding, duid=self._garage_duid,
            entry_id=self.config_entry.entry_id, entries=self._garage_ownership_entries(),
        )

    async def async_step_garage(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose basic settings and sources before showing hardware fields."""
        if self._garage_duid is None:
            return await self.async_step_garage_select()
        values = self._garage_values()
        if user_input is not None:
            values.update(user_input)
            try:
                if not isinstance(values.get("enabled", False), bool):
                    raise GarageConfigError("invalid_boolean_value", "enabled")
                validate_binding({**values, "enabled": False})
                if not values.get("enabled", False):
                    return self._save_garage_binding()
                if not values.get("cover_entity_id"):
                    raise GarageConfigError("missing_safety_configuration", "cover_entity_id")
                if not isinstance(values["cover_entity_id"], str) or not values["cover_entity_id"].startswith("cover."):
                    raise GarageConfigError("invalid_entity_reference", "cover_entity_id")
                if values.get("task_door_mode", "hold_open") not in ("hold_open", "after_exit"):
                    raise GarageConfigError("invalid_task_door_mode", "task_door_mode")
                for direction in ("open", "close"):
                    key = f"{direction}_source"
                    if key not in values:
                        raise GarageConfigError("missing_safety_configuration", key)
                    if values[key] not in EVIDENCE_SOURCES:
                        raise GarageConfigError("invalid_evidence_source", key)
            except GarageConfigError as error:
                return self._garage_error_form(error)
            if self._garage_feedback_fields():
                return await self.async_step_garage_feedback()
            return await self.async_step_garage_timing()
        return self._garage_form("garage")

    def _garage_values(self) -> dict[str, Any]:
        """Keep inactive hardware values while a user changes evidence sources."""
        if self._garage_draft is None:
            self._garage_draft = deepcopy(dict(
                self.options.get(CONF_GARAGE_BINDINGS, {}).get(self._garage_duid, {})
            ))
        return self._garage_draft

    def _garage_feedback_fields(self) -> tuple[str, ...]:
        values = self._garage_values()
        fields = []
        for direction in ("open", "close"):
            if values.get(f"{direction}_source") == "contact":
                fields.extend((f"{direction}_contact", f"{direction}_contact_state"))
        if "position" in (values.get("open_source"), values.get("close_source")):
            fields.extend(("position_kind", "open_position", "close_position"))
        return tuple(fields)

    async def async_step_garage_feedback(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for only the explicitly selected endpoint sensors and polarity."""
        values = self._garage_values()
        if user_input is not None:
            values.update(user_input)
            try:
                validate_binding({**values, "enabled": False})
                for direction in ("open", "close"):
                    if values.get(f"{direction}_source") == "contact":
                        key = f"{direction}_contact"
                        if not values.get(key) or values.get(f"{direction}_contact_state") not in ("on", "off"):
                            raise GarageConfigError("invalid_contact_configuration", key)
                if "position_kind" in self._garage_feedback_fields():
                    if values.get("position_kind") not in ("measured", "estimated"):
                        raise GarageConfigError("invalid_position_configuration", "position_kind")
                    for key in ("open_position", "close_position"):
                        if key not in values:
                            raise GarageConfigError("missing_safety_configuration", key)
            except GarageConfigError as error:
                return self._garage_error_form(error)
            return await self.async_step_garage_timing()
        return self._garage_form("garage_feedback")

    async def async_step_garage_timing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm only active-source timings, estimation risk and handover."""
        if user_input is not None:
            self._garage_values().update(user_input)
            try:
                return self._save_garage_binding()
            except GarageConfigError as error:
                return self._garage_error_form(error)
        return self._garage_form("garage_timing")

    def _save_garage_binding(self) -> ConfigFlowResult:
        binding = validate_binding(
            self._garage_values(), supported=self._garage_duid in self._garage_devices()
        )
        if binding["enabled"]:
            state = self.hass.states.get(binding["cover_entity_id"])
            # Command capabilities never stand in for physical feedback.
            if state is not None and state.state not in ("unknown", "unavailable"):
                features = state.attributes.get("supported_features", 0)
                required = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
                if not isinstance(features, int) or features & required != required:
                    raise GarageConfigError("unsupported_cover", "cover_entity_id")
            self._validate_garage_ownership(binding)
        self.options[CONF_GARAGE_CONTROL_VERSION] = GARAGE_CONTROL_VERSION
        self.options.setdefault(CONF_GARAGE_BINDINGS, {})[self._garage_duid] = binding
        return self.async_create_entry(title="", data=self.options)

    def _garage_fields(self, step_id: str) -> tuple[str, ...]:
        if step_id == "garage":
            return ("enabled", "cover_entity_id", "task_door_mode", "open_source", "close_source", "close_permission_entity")
        if step_id == "garage_feedback":
            return self._garage_feedback_fields()
        values = self._garage_values()
        fields = list(required_timing_fields(values))
        sources = (values.get("open_source"), values.get("close_source"))
        if "timed" in sources or ("position" in sources and values.get("position_kind") == "estimated"):
            fields.append("allow_estimated")
        fields.append("handover_confirmed")
        return tuple(fields)

    def _garage_error_form(self, error: GarageConfigError) -> ConfigFlowResult:
        errors = {"base": error.reason}
        if error.field:
            errors[error.field] = error.reason
        for step_id in ("garage", "garage_feedback", "garage_timing"):
            if error.field in self._garage_fields(step_id):
                return self._garage_form(step_id, errors)
        if error.field:
            step_id = "garage_feedback" if error.field in (
                "open_contact", "close_contact", "open_contact_state", "close_contact_state",
                "position_kind", "open_position", "close_position",
            ) else "garage_timing"
            # An invalid retained value must be editable even while its source
            # is inactive; otherwise a hidden field would prevent every save.
            return self._garage_form(step_id, errors, extra_fields=(error.field,))
        return self._garage_form("garage_timing", errors)

    def _garage_form(
        self, step_id: str, errors: dict[str, str] | None = None,
        *, extra_fields: tuple[str, ...] = (),
    ) -> ConfigFlowResult:
        values = self._garage_values()
        timing_suggestions = suggested_timing_values(values) if step_id == "garage_timing" else {}

        def field(key: str, default: Any = None) -> Any:
            marker = vol.Required if step_id in ("garage_feedback", "garage_timing") and key not in (
                "allow_estimated", "handover_confirmed",
            ) else vol.Optional
            if key in values and values[key] not in (None, ""):
                return marker(key, default=values[key])
            if default is not None:
                return marker(key, default=default)
            return marker(key)

        def choice(options: list[str], translation_key: str) -> SelectSelector:
            return SelectSelector(SelectSelectorConfig(
                options=options, mode=SelectSelectorMode.DROPDOWN,
                translation_key=translation_key,
            ))

        selectors = {
            field("enabled", False): bool,
            field("cover_entity_id"): EntitySelector(EntitySelectorConfig(domain="cover")),
            field("task_door_mode", "hold_open"): choice(["hold_open", "after_exit"], "garage_task_mode"),
            field("open_source"): choice(list(EVIDENCE_SOURCES), "garage_evidence_source"),
            field("close_source"): choice(list(EVIDENCE_SOURCES), "garage_evidence_source"),
            field("open_contact"): EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
            field("close_contact"): EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
            field("open_contact_state"): choice(["on", "off"], "garage_contact_state"),
            field("close_contact_state"): choice(["on", "off"], "garage_contact_state"),
            field("position_kind"): choice(["measured", "estimated"], "garage_position_kind"),
            field("close_permission_entity"): EntitySelector(EntitySelectorConfig(domain=["input_boolean", "binary_sensor"])),
            field("allow_estimated", False): bool,
            field("handover_confirmed", False): bool,
        }
        for key, (minimum, maximum) in {
            **NUMERIC_RANGES, "open_position": (0, 100), "close_position": (0, 100)
        }.items():
            default = timing_suggestions.get(key) if key not in values else None
            selectors[field(key, default)] = NumberSelector(NumberSelectorConfig(
                min=minimum, max=maximum, step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="%" if key.endswith("_position") else "s",
            ))
        visible = (*self._garage_fields(step_id), *extra_fields)
        data_schema = {key: selector for key, selector in selectors.items() if key.schema in visible}
        placeholders = {"robot": self._garage_devices().get(self._garage_duid, self._garage_duid)}
        if errors and "operation_timeout_too_short" in errors.values():
            placeholders.update(
                current_timeout=f"{values['operation_timeout_seconds']:g}",
                required_wait=f"{longest_endpoint_wait(values):g}",
            )
        return self.async_show_form(
            step_id=step_id, data_schema=vol.Schema(data_schema), errors=errors or {},
            description_placeholders=placeholders,
        )
