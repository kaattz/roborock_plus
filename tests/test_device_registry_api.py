"""Guard against deprecated Home Assistant device-registry APIs.

Identifiers and connections became unique per config entry in HA 2026.8, which
deprecated the unscoped lookup and the config-entry update parameters. These
calls only log a warning for custom integrations today but raise from HA
2027.8, so they are pinned here.

See https://developers.home-assistant.io/blog/2026/07/21/device-registry-single-config-entry/
and https://developers.home-assistant.io/blog/2026/08/24/device-registry-follow-up-changes/
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "roborock_plus"

# Patterns that are deprecated and must not appear in the integration.
FORBIDDEN = {
    # Ambiguous: identifiers are only unique per config entry now.
    "async_get_device(": "use async_get_device_by_identifier / async_get_devices",
    "async_is_composite_device_id": "removed in 2027.9",
    "deleted_devices": "internal implementation detail, removed in 2027.9",
    # A device belongs to a single config entry; adding/removing is gone.
    "remove_config_entry_id": "use async_remove_device / new_config_entry_id",
    "add_config_entry_id": "use new_config_entry_id",
    "remove_config_subentry_id": "use new_config_subentry_id",
    "add_config_subentry_id": "use new_config_subentry_id",
    # Merge parameters are deprecated in favour of the full desired set.
    "merge_connections": "pass new_connections instead",
    "merge_identifiers": "pass new_identifiers instead",
    # DeviceInfo["via_device"] was removed from the TypedDict.
    "via_device=": "use via_device_id",
}


def _sources() -> list[Path]:
    return sorted(COMPONENT.rglob("*.py"))


def test_no_deprecated_device_registry_calls() -> None:
    offenders: list[str] = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for pattern, advice in FORBIDDEN.items():
            if pattern in text:
                offenders.append(f"{path.name}: {pattern} -> {advice}")

    assert offenders == [], "\n".join(offenders)


def test_removing_a_stale_device_uses_async_remove_device() -> None:
    """Removing a device is a removal, not an unlink from a config entry."""
    text = (COMPONENT / "__init__.py").read_text(encoding="utf-8")

    assert "async_remove_device(" in text


def test_device_lookup_is_scoped_to_the_config_entry() -> None:
    text = (COMPONENT / "__init__.py").read_text(encoding="utf-8")

    assert "async_get_device_by_identifier(" in text
    # The identifier and the owning entry are both passed, so it cannot be
    # ambiguous across config entries.
    assert "(DOMAIN, device.duid)" in text


def test_dhcp_discovery_searches_all_matching_devices() -> None:
    """A connection can match devices from other config entries too."""
    text = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
    dhcp_step = text.split("async def async_step_dhcp", 1)[1].split(
        "async def async_step_reauth", 1
    )[0]

    assert "async_get_devices(" in dhcp_step
    assert "CONNECTION_NETWORK_MAC" in dhcp_step
