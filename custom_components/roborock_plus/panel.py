"""Frontend panel registration for Roborock Plus."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from .const import DOMAIN

PANEL_FRONTEND_URL_PATH = "roborock-plus-zones"
PANEL_WEBCOMPONENT_NAME = "roborock-plus-safe-zone-editor"
PANEL_MODULE_FILENAME = "roborock-plus-safe-zone-editor-v2.js"
PANEL_STATIC_URL = f"/api/{DOMAIN}/{PANEL_MODULE_FILENAME}"
DATA_PANEL_REGISTERED = f"{DOMAIN}_panel_registered"


async def async_setup_panel(hass: HomeAssistant) -> None:
    """Register static assets and the custom panel."""
    if hass.data.get(DATA_PANEL_REGISTERED):
        return
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                PANEL_STATIC_URL,
                str(
                    Path(__file__).resolve().parent
                    / "frontend"
                    / PANEL_MODULE_FILENAME
                ),
                False,
            )
        ]
    )
    await async_setup_component(hass, "panel_custom", {})
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_FRONTEND_URL_PATH,
        webcomponent_name=PANEL_WEBCOMPONENT_NAME,
        module_url=PANEL_STATIC_URL,
        sidebar_title="Roborock Plus Zones",
        sidebar_icon="mdi:vector-rectangle",
        require_admin=True,
        config_panel_domain=DOMAIN,
    )
    hass.data[DATA_PANEL_REGISTERED] = True
