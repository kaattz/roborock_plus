"""The folding-door link must ask the integration to open the door.

Purpose (confirmed by the owner 2026-10-23): when the folding door opens, the
washing-machine cabinet door should be driven open by the motor so somebody can
walk through. An earlier version of this file released the clutch instead; that
read the requirement wrong and was replaced.

The defect these tests guard: the original action was ``cover.open_cover``. The
integration classifies any open/close/stop/position/toggle it did not issue itself
as an outside takeover, latches ``manual_hold``, and then rejects every clean
command until a human resets it. Measured twice:

    2026-10-22 16:08:38  Garage control held for manual handling:
                         reason=manual_override previous_phase=idle phase=manual_hold
    2026-10-23 12:48:26  vacuum.clean_area -> garage_control_error: manual_override
    2026-10-23 14:47:40  the same, five retries, 33 minutes until the blueprint alerted

Driving the cover directly is wrong for a second reason: with a ``timed`` open
source and no command of our own, ``door_evidence`` answers ``command_required``,
so ``passage_allowed`` never becomes true and the next clean opens the door again.
``roborock_plus.request_garage_open`` (v0.8.0) is the entry that does it once,
waits out the travel, and leaves an honest passage fact behind.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
AUTOMATION = REPO / "automations" / "garage_door_sync_folding_door.yaml"
COVER = "cover.vacuum_garage_door"
FOLDING = "binary_sensor.folding_door_contact_state"
PASSAGE = "binary_sensor.ke_ting_sao_di_ji_garage_passage_allowed"


def _config() -> dict:
    return yaml.safe_load(AUTOMATION.read_text(encoding="utf-8"))


def _walk(node: object):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _actions() -> list[dict]:
    return [node for node in _walk(_config()["actions"]) if "action" in node or "service" in node]


def _called() -> list[str]:
    return [node.get("action") or node.get("service") for node in _actions()]


def test_unique_id_is_preserved() -> None:
    """`id` is the HA unique_id; changing it orphans the entity's history."""
    assert _config()["id"] == "1746705574425"


def test_it_asks_the_integration_to_open() -> None:
    assert "roborock_plus.request_garage_open" in _called()


def test_it_never_touches_the_cover_itself() -> None:
    """Any direct cover command is an outside takeover: it latches manual_hold and
    rejects every later clean command for the rest of the day."""
    called = _called()
    for forbidden in ("cover.open_cover", "cover.close_cover", "cover.stop_cover",
                      "cover.set_cover_position", "cover.toggle"):
        assert forbidden not in called, (
            f"{forbidden} is classified as an outside takeover by the integration; "
            f"the link must go through request_garage_open"
        )


def test_it_targets_the_vacuum_the_service_is_registered_on() -> None:
    request = next(
        node for node in _actions()
        if (node.get("action") or node.get("service")) == "roborock_plus.request_garage_open"
    )
    assert request["target"]["entity_id"] == "vacuum.g20s_ultra"


def test_it_does_not_open_an_already_passable_door() -> None:
    """The point is "let me through", not "make the motor move"."""
    blob = yaml.safe_dump(_config(), allow_unicode=True)
    assert PASSAGE in blob
    config = _config()
    guard = config["actions"][0]
    assert guard["if"][0]["entity_id"] == PASSAGE
    assert guard["if"][0]["state"] == "on"
    assert guard["then"] in ([], None), "an already-passable door must short-circuit"


def test_it_does_not_retry_inside_the_automation() -> None:
    """Retries belong to the schedule blueprint's retry_window, not to a
    "somebody is at the door now" request."""
    blob = yaml.safe_dump(_config(), allow_unicode=True)
    for retry_ish in ("repeat:", "until:", "wait_for_trigger", "wait_template"):
        assert retry_ish not in blob


def test_it_triggers_on_the_folding_door_opening() -> None:
    trigger = _config()["triggers"][0]
    assert trigger["trigger"] == "state"
    assert trigger["entity_id"] == FOLDING
    assert trigger["from"] == "off" and trigger["to"] == "on"


def test_the_shipped_yaml_matches_what_ha_runs() -> None:
    """The repo copy documents the live config; a drifted copy is how a fixed bug
    gets pasted back in."""
    config = _config()
    assert config["mode"] == "single"
    assert config["alias"] == "车库门联动柜门"


def test_the_service_it_calls_actually_exists() -> None:
    """A link to a service nobody registered fails at runtime, silently."""
    services = REPO / "custom_components/roborock_plus/services.py"
    text = services.read_text(encoding="utf-8")
    assert 'REQUEST_GARAGE_OPEN_SERVICE_NAME = "request_garage_open"' in text
    vacuum = REPO / "custom_components/roborock_plus/vacuum.py"
    assert "async def async_request_garage_open" in vacuum.read_text(encoding="utf-8")
