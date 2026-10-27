"""Exercise the real garage entity code with only the HA boundary faked.

The entity keys are unchanged so dashboards and the existing notification
automation keep working. What the entities *mean* changed with the position-driven
rewrite:

* ``garage_problem`` now reports unusable evidence, not a latch a human must
  clear. There is no ``manual_hold`` phase any more, by design.
* the phase vocabulary matches the seven-step controller.

These tests pin the meanings that a household actually sees.
"""

import ast
from collections.abc import Mapping
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/roborock_plus"


class FakeEntity:
    def __init__(self, unique_id, device_info):
        self._attr_unique_id = unique_id
        self._attr_device_info = device_info
        self.removers = []
        self.writes = 0

    async def async_added_to_hass(self):
        pass

    def async_on_remove(self, callback):
        self.removers.append(callback)

    def async_write_ha_state(self):
        self.writes += 1


@pytest.fixture
def entities():
    path = COMPONENT / "garage_entities.py"
    assert path.is_file(), "Garage diagnostic entities have not been implemented"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    tree.body.insert(0, ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0))
    scope = {
        "RoborockEntity": FakeEntity,
        "BinarySensorEntity": type("BinarySensorEntity", (), {}),
        "SensorEntity": type("SensorEntity", (), {}),
        "BinarySensorDeviceClass": SimpleNamespace(PROBLEM="problem"),
        "SensorDeviceClass": SimpleNamespace(ENUM="enum"),
        "EntityCategory": SimpleNamespace(DIAGNOSTIC="diagnostic"),
        "Mapping": Mapping,
        "callback": lambda fn: fn,
    }
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), scope)
    return SimpleNamespace(**scope)


def coordinator(*, enabled=True, runtime=None, error=None, duid="robot-a"):
    return SimpleNamespace(
        duid=duid, duid_slug=duid.replace("-", "_"),
        device_info={"identifiers": {("roborock_plus", duid)}}, data=None,
        config_entry=SimpleNamespace(options={"garage_bindings": {duid: {
            "enabled": enabled, "cover_entity_id": "cover.door",
            "task_door_mode": "after_exit", "open_source": "contact",
            "close_source": "contact"}}}),
        garage_runtime=runtime, garage_setup_error=error,
    )


def runtime(*, quality="confirmed", reached=True, passage=True,
            phase="outside", last_error=None):
    endpoint = SimpleNamespace(quality=quality, reached=reached, source="position", reason=None)
    state = SimpleNamespace(
        open_confirmed=reached if quality == "confirmed" else None,
        closed_confirmed=False if quality == "confirmed" else None,
        open=endpoint, close=SimpleNamespace(quality="confirmed", source="contact", reason=None),
    )
    return SimpleNamespace(
        controller=SimpleNamespace(state=state, passage_allowed=passage, phase=phase,
                                   last_error=last_error, operation_id=7),
        binding={"cover_entity_id": "cover.door", "task_door_mode": "after_exit",
                 "open_source": "position", "close_source": "contact"},
        stage_attributes=lambda: {
            "stage_runtime_id": None, "stage_session_id": None,
            "stage_interruption_revision": 0, "stage_result": None, "stage_reason": None,
        },
    )


def keyed(values):
    return {value._attr_translation_key: value for value in values}


def test_factories_only_register_explicit_enabled_robot_bindings(entities):
    coords = [coordinator(), coordinator(enabled=False), coordinator(enabled="true")]
    binary = entities.build_garage_binary_entities(coords)
    sensors = entities.build_garage_sensor_entities(coords)
    assert set(keyed(binary)) == {"garage_open_confirmed", "garage_closed_confirmed",
                                 "garage_passage_allowed", "garage_problem"}
    assert len(binary) == 4
    assert len(sensors) == 1
    for entity in binary + sensors:
        assert entity._attr_unique_id == f"{entity._attr_translation_key}_robot_a"
        assert entity._attr_device_info == coords[0].device_info


def test_unobserved_endpoints_and_passage_are_unknown(entities):
    rt = runtime()
    rt.controller.state = None
    rt.controller.passage_allowed = None
    binary = keyed(entities.build_garage_binary_entities([coordinator(runtime=rt)]))
    for name in ("garage_open_confirmed", "garage_closed_confirmed", "garage_passage_allowed"):
        assert binary[name].is_on is None


def test_estimated_passage_never_makes_confirmed_endpoint_on(entities):
    binary = keyed(entities.build_garage_binary_entities([coordinator(runtime=runtime(quality="estimated"))]))
    assert binary["garage_open_confirmed"].is_on is None
    assert binary["garage_passage_allowed"].is_on is True
    assert binary["garage_problem"].is_on is False


def test_confirmed_endpoint_not_reached_is_false(entities):
    binary = keyed(entities.build_garage_binary_entities([coordinator(runtime=runtime(reached=False, passage=False))]))
    assert binary["garage_open_confirmed"].is_on is False
    assert binary["garage_passage_allowed"].is_on is False


def test_a_problem_is_unusable_evidence_not_a_latch(entities):
    """``garage_problem`` reports evidence trouble, and clears on its own.

    The old meaning was "someone must reset this". That latch is gone; what
    remains is "the door cannot be read right now", which resolves itself.
    """
    broken = keyed(entities.build_garage_binary_entities(
        [coordinator(runtime=runtime(phase="blocked", last_error="feedback_unavailable"))]))
    assert broken["garage_problem"].is_on is True

    healthy = keyed(entities.build_garage_binary_entities(
        [coordinator(runtime=runtime(phase="outside", last_error=None))]))
    assert healthy["garage_problem"].is_on is False


def test_bad_enabled_config_still_exposes_problem_and_blocked_phase(entities):
    coord = coordinator(error="duplicate_binding")
    binary = keyed(entities.build_garage_binary_entities([coord]))
    phase = entities.build_garage_sensor_entities([coord])[0]
    assert binary["garage_problem"].is_on is True
    assert binary["garage_open_confirmed"].is_on is None
    assert binary["garage_passage_allowed"].is_on is None
    assert phase.native_value == "blocked"
    assert phase.extra_state_attributes["reason"] == "duplicate_binding"


def test_phase_attributes_expose_evidence_without_a_session(entities):
    """The diagnostics still explain the state; the session id is gone by design."""
    sensor = entities.build_garage_sensor_entities([coordinator(runtime=runtime(phase="outside"))])[0]
    attrs = sensor.extra_state_attributes
    assert attrs["phase"] == "outside"
    assert attrs["cover_entity_id"] == "cover.door"
    assert attrs["task_door_mode"] == "after_exit"
    assert attrs["open_source"] == "position"
    assert attrs["open_quality"] == "confirmed"
    assert "session_id" not in attrs, 'task attribution was removed from the design'


@pytest.mark.parametrize("phase", ["idle", "opening", "outside", "closing",
                                   "closed_outside", "pausing_return",
                                   "resuming_return", "controlled_exit", "blocked"])
def test_every_controller_phase_is_a_valid_enum_value(entities, phase):
    sensor = entities.build_garage_sensor_entities([coordinator(runtime=runtime(phase=phase))])[0]
    assert sensor.native_value == phase
    assert phase in sensor._attr_options


@pytest.mark.parametrize("name", ["zh-Hans", "en"])
def test_entity_phases_and_quality_descriptions_are_translated(entities, name):
    data = json.loads((COMPONENT / "translations" / f"{name}.json").read_text(encoding="utf-8"))
    for key in ("garage_open_confirmed", "garage_closed_confirmed",
                "garage_passage_allowed", "garage_problem"):
        assert data["entity"]["binary_sensor"][key]["name"]
    phase = data["entity"]["sensor"]["garage_phase"]["state_attributes"]["phase"]["state"]
    for value in entities.GARAGE_PHASES:
        assert phase.get(value), f'{name}: phase {value} has no label'
