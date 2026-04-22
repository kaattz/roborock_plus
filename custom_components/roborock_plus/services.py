"""Roborock Plus services."""

import voluptuous as vol

from homeassistant.components.vacuum import DOMAIN as VACUUM_DOMAIN
from homeassistant.core import HomeAssistant, SupportsResponse, callback
from homeassistant.helpers import config_validation as cv, service

from .const import DOMAIN

GET_MAPS_SERVICE_NAME = "get_maps"
SET_VACUUM_GOTO_POSITION_SERVICE_NAME = "set_vacuum_goto_position"
GET_VACUUM_CURRENT_POSITION_SERVICE_NAME = "get_vacuum_current_position"
RESUME_TASK_SERVICE_NAME = "resume_task"
GET_DOCK_POSITION_SERVICE_NAME = "get_dock_position"
GET_SAFE_ZONE_SERVICE_NAME = "get_safe_zone"
GET_SAFE_ZONE_SUGGESTION_SERVICE_NAME = "get_safe_zone_suggestion"
GET_SAFE_ZONE_EDITOR_CONTEXT_SERVICE_NAME = "get_safe_zone_editor_context"
SET_SAFE_ZONE_SERVICE_NAME = "set_safe_zone"
CLEAR_SAFE_ZONE_SERVICE_NAME = "clear_safe_zone"

SAFE_ZONE_SUGGESTION_SCHEMA = cv.make_entity_service_schema(
    {
        vol.Required("cabinet_direction"): vol.In(
            ["north", "south", "east", "west"]
        ),
        vol.Required("safe_distance_front"): vol.Coerce(int),
        vol.Required("safe_half_width"): vol.Coerce(int),
        vol.Required("close_margin"): vol.Coerce(int),
    }
)

SET_SAFE_ZONE_SCHEMA = cv.make_entity_service_schema(
    {
        vol.Required("min_x"): vol.Coerce(int),
        vol.Required("max_x"): vol.Coerce(int),
        vol.Required("min_y"): vol.Coerce(int),
        vol.Required("max_y"): vol.Coerce(int),
    }
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services."""

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        GET_MAPS_SERVICE_NAME,
        entity_domain=VACUUM_DOMAIN,
        schema=None,
        func="get_maps",
        supports_response=SupportsResponse.ONLY,
    )

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        GET_VACUUM_CURRENT_POSITION_SERVICE_NAME,
        entity_domain=VACUUM_DOMAIN,
        schema=None,
        func="get_vacuum_current_position",
        supports_response=SupportsResponse.ONLY,
    )

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SET_VACUUM_GOTO_POSITION_SERVICE_NAME,
        entity_domain=VACUUM_DOMAIN,
        schema=cv.make_entity_service_schema(
            {
                vol.Required("x"): vol.Coerce(int),
                vol.Required("y"): vol.Coerce(int),
            },
        ),
        func="async_set_vacuum_goto_position",
        supports_response=SupportsResponse.NONE,
    )

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        RESUME_TASK_SERVICE_NAME,
        entity_domain=VACUUM_DOMAIN,
        schema=None,
        func="async_resume_task",
        supports_response=SupportsResponse.NONE,
    )

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        GET_DOCK_POSITION_SERVICE_NAME,
        entity_domain=VACUUM_DOMAIN,
        schema=None,
        func="get_dock_position",
        supports_response=SupportsResponse.ONLY,
    )

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        GET_SAFE_ZONE_SERVICE_NAME,
        entity_domain=VACUUM_DOMAIN,
        schema=None,
        func="get_safe_zone",
        supports_response=SupportsResponse.ONLY,
    )

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        GET_SAFE_ZONE_SUGGESTION_SERVICE_NAME,
        entity_domain=VACUUM_DOMAIN,
        schema=SAFE_ZONE_SUGGESTION_SCHEMA,
        func="get_safe_zone_suggestion",
        supports_response=SupportsResponse.ONLY,
    )

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        GET_SAFE_ZONE_EDITOR_CONTEXT_SERVICE_NAME,
        entity_domain=VACUUM_DOMAIN,
        schema=None,
        func="get_safe_zone_editor_context",
        supports_response=SupportsResponse.ONLY,
    )

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SET_SAFE_ZONE_SERVICE_NAME,
        entity_domain=VACUUM_DOMAIN,
        schema=SET_SAFE_ZONE_SCHEMA,
        func="async_set_safe_zone",
        supports_response=SupportsResponse.NONE,
    )

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        CLEAR_SAFE_ZONE_SERVICE_NAME,
        entity_domain=VACUUM_DOMAIN,
        schema=None,
        func="async_clear_safe_zone",
        supports_response=SupportsResponse.NONE,
    )
