"""Exercise real garage entity code using only the HA entity boundary as a fake."""

import ast
import asyncio
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
        config_entry=SimpleNamespace(options={"garage_bindings": {duid: {"enabled": enabled, "cover_entity_id": "cover.door", "task_door_mode": "hold_open", "open_source": "timed", "close_source": "contact"}}}),
        garage_runtime=runtime, garage_setup_error=error,
    )


def runtime(*, quality="confirmed", reached=True, passage=True, phase="task_open", reason=None):
    endpoint = SimpleNamespace(quality=quality, reached=reached, source="position", reason=None)
    state = SimpleNamespace(
        open_confirmed=reached if quality == "confirmed" else None,
        closed_confirmed=False if quality == "confirmed" else None,
        open=endpoint, close=SimpleNamespace(quality="confirmed", source="contact", reason=None),
    )
    return SimpleNamespace(
        controller=SimpleNamespace(state=state, passage_allowed=passage, phase=phase, reason=reason, operation_id=7),
        session=SimpleNamespace(session_id="session-current"),
        binding={"cover_entity_id": "cover.door", "task_door_mode": "hold_open", "open_source": "position", "close_source": "contact"},
        stage_attributes=lambda: {
            "stage_runtime_id": "runtime-current", "stage_session_id": None,
            "stage_interruption_revision": 0, "stage_result": None, "stage_reason": None,
        },
    )


def keyed(values):
    return {value._attr_translation_key: value for value in values}


def test_factories_only_register_explicit_enabled_robot_bindings(entities):
    coords = [coordinator(), coordinator(enabled=False), coordinator(enabled="true")]
    binary = entities.build_garage_binary_entities(coords)
    sensors = entities.build_garage_sensor_entities(coords)
    assert set(keyed(binary)) == {"garage_open_confirmed", "garage_closed_confirmed", "garage_passage_allowed", "garage_problem"}
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


def test_confirmed_not_at_endpoint_is_false_and_manual_hold_is_a_problem(entities):
    binary = keyed(entities.build_garage_binary_entities([coordinator(runtime=runtime(reached=False, passage=False, phase="manual_hold", reason="manual_override"))]))
    assert binary["garage_open_confirmed"].is_on is False
    assert binary["garage_passage_allowed"].is_on is False
    assert binary["garage_problem"].is_on is True


def test_bad_enabled_config_still_exposes_problem_and_blocked_phase(entities):
    coord = coordinator(error="duplicate_binding")
    binary = keyed(entities.build_garage_binary_entities([coord]))
    phase = entities.build_garage_sensor_entities([coord])[0]
    assert binary["garage_problem"].is_on is True
    assert binary["garage_open_confirmed"].is_on is None
    assert binary["garage_passage_allowed"].is_on is None
    assert phase.native_value == "blocked"
    assert phase.extra_state_attributes["reason"] == "duplicate_binding"
    assert phase.extra_state_attributes["cover_entity_id"] == "cover.door"


def test_phase_attributes_explain_current_session_operation_and_evidence(entities):
    phase = entities.build_garage_sensor_entities([coordinator(runtime=runtime(quality="estimated"))])[0]
    assert phase.native_value == "task_open"
    attrs = phase.extra_state_attributes
    assert attrs["session_id"] == "session-current"
    assert attrs["operation_id"] == 7
    assert attrs["open_quality"] == "estimated"
    assert attrs["open_source"] == "position"
    assert attrs["close_source"] == "contact"
    assert attrs["task_door_mode"] == "hold_open"


@pytest.mark.parametrize("phase", ["pausing_return", "return_opening", "resuming_return"])
def test_return_interlock_phases_are_native_enum_values(entities, phase):
    sensor = entities.build_garage_sensor_entities([coordinator(runtime=runtime(phase=phase))])[0]
    assert sensor.native_value == phase
    assert phase in sensor._attr_options
    for name in ("strings.json", "translations/en.json", "translations/zh-Hans.json"):
        data = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
        assert data["entity"]["sensor"]["garage_phase"]["state"][phase]
        assert data["entity"]["sensor"]["garage_phase"]["state_attributes"]["phase"]["state"][phase]


def test_entity_listener_writes_state_and_is_removed_on_unload(entities):
    rt = runtime()
    callbacks = []
    removed = []
    rt.add_listener = lambda cb: callbacks.append(cb) or (lambda: removed.append(cb))
    entity = entities.build_garage_sensor_entities([coordinator(runtime=rt)])[0]
    asyncio.run(entity.async_added_to_hass())
    assert len(callbacks) == 1
    callbacks[0]()
    assert entity.writes == 1
    for remove in entity.removers:
        remove()
    assert removed == callbacks


@pytest.mark.parametrize("platform", ["binary_sensor", "sensor"])
def test_real_platform_setup_adds_garage_entities(entities, platform):
    path = COMPONENT / f"{platform}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry")
    scope = {node.id: [] for node in ast.walk(fn) if isinstance(node, ast.Name) and node.id.isupper()}
    scope.update({
        "build_garage_binary_entities": entities.build_garage_binary_entities,
        "build_garage_sensor_entities": entities.build_garage_sensor_entities,
        "build_safe_zone_entities": lambda *args: [],
        "RoborockHasDangerZoneBinarySensorEntity": object,
        "RoborockInDangerZoneBinarySensorEntity": object,
        "RoborockOutsideDangerZoneBinarySensorEntity": object,
        "RoborockStuckBinarySensorEntity": lambda co: ("old-stuck", co.duid),
        "RoborockCurrentRoom": lambda co: ("old-room", co.duid),
    })
    extract = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), fn], type_ignores=[])
    exec(compile(ast.fix_missing_locations(extract), str(path), "exec"), scope)
    entry = SimpleNamespace(runtime_data=SimpleNamespace(v1=[coordinator()], a01=[], b01_q7=[], b01_q10=[]))
    added = []
    asyncio.run(scope["async_setup_entry"](object(), entry, added.extend))
    assert any(getattr(entity, "_attr_translation_key", "").startswith("garage_") for entity in added)


def test_entity_phases_and_quality_descriptions_are_translated(entities):
    for name in ("strings.json", "translations/en.json", "translations/zh-Hans.json"):
        data = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
        for key in keyed(entities.build_garage_binary_entities([coordinator()])):
            assert data["entity"]["binary_sensor"][key]["name"]
        phase = data["entity"]["sensor"]["garage_phase"]
        for value in entities.GARAGE_PHASES:
            assert phase["state"][value]
        assert phase["state_attributes"]["open_quality"]["state"]["estimated"]
        assert phase["state_attributes"]["open_source"]["state"]["timed"]
