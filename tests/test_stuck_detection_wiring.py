"""Guard the wiring between the stuck tracker, the coordinator and the entity.

Source-level assertions, because Home Assistant is not importable here. The
failure mode they protect against only appears on real hardware: a robot
pressed against the door, with nothing in Home Assistant noticing.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "roborock_plus"
COORDINATOR = COMPONENT / "coordinator.py"
BINARY_SENSOR = COMPONENT / "binary_sensor.py"
CONFIG_FLOW = COMPONENT / "config_flow.py"


def test_coordinator_feeds_the_tracker_only_after_a_successful_sample() -> None:
    """A failed refresh must not be counted as the robot standing still.

    The trait keeps the previous position when a read fails, so observing on
    failure would feed the tracker a repeated position and manufacture a stuck
    verdict out of a broken read.
    """
    text = COORDINATOR.read_text(encoding="utf-8")
    method = text.split("async def _async_update_map_position", 1)[1].split(
        "\n    def ", 1
    )[0]

    assert "except (RoborockException, ValueError)" in method

    # The call must be *inside* the `else:` block. Checking indentation is what
    # separates "feeds the tracker on success" from "feeds it unconditionally":
    # a call dedented to the method body would run after a failed read too.
    calls = [
        line
        for line in method.splitlines()
        if line.strip() == "self._observe_stuck_detection()"
    ]
    assert calls, "no stuck observation call found"
    indents = {len(line) - len(line.lstrip()) for line in calls}
    assert indents == {12}, f"observation not indented under `else:`: {indents}"


def test_coordinator_gates_the_tracker_on_should_assess_movement() -> None:
    """The whitelist, not `task_active`, has to decide whether to assess.

    `task_active` stays on while the robot pauses or washes the mop, so using
    it here would report every mop wash as stuck.
    """
    text = COORDINATOR.read_text(encoding="utf-8")
    observe = text.split("def _observe_stuck_detection", 1)[1].split(
        "\n    @property", 1
    )[0]

    assert "should_assess_movement" in observe
    assert "is_v1_task_active" not in observe


def test_coordinator_respects_the_enabled_option() -> None:
    text = COORDINATOR.read_text(encoding="utf-8")
    observe = text.split("def _observe_stuck_detection", 1)[1].split(
        "\n    @property", 1
    )[0]

    assert "options.enabled" in observe


def test_coordinator_fires_the_event_once_per_episode() -> None:
    """A stuck robot must not emit an event on every sample.

    Asserting the guard's own condition, not just that the flag is mentioned:
    the flag is also reset elsewhere in the same method, so a looser check
    passes even when the guard is disabled.
    """
    text = COORDINATOR.read_text(encoding="utf-8")
    observe = text.split("def _observe_stuck_detection", 1)[1].split(
        "\n    @property", 1
    )[0]

    assert "if was_stuck and self._stuck_event_sent:" in observe
    assert "self.hass.bus.async_fire(" in observe
    assert "EVENT_VACUUM_STUCK" in observe


def test_coordinator_clears_the_sent_flag_when_not_stuck() -> None:
    """Without the reset, a second episode would never be reported."""
    text = COORDINATOR.read_text(encoding="utf-8")
    observe = text.split("def _observe_stuck_detection", 1)[1].split(
        "\n    @property", 1
    )[0]

    assert "self._stuck_event_sent = False" in observe


def test_event_reports_the_real_entity_id_not_a_guessed_one() -> None:
    """The reported entity_id must be one an automation can target.

    The vacuum entity_id is generated from the device name, not the duid, so
    deriving it as `vacuum.{duid_slug}` produces an id that does not exist --
    and a follow-up `vacuum.stop` on it fails with nothing in the log.
    """
    text = COORDINATOR.read_text(encoding="utf-8")
    observe = text.split("def _observe_stuck_detection", 1)[1].split(
        "\n    @property", 1
    )[0]

    assert "self.vacuum_entity_id" in observe
    assert 'f"vacuum.{self.duid_slug}"' not in observe
    assert "f'vacuum.{self.duid_slug}'" not in observe

    # And the resolver reads the registry rather than rebuilding the id.
    resolver = text.split("def vacuum_entity_id", 1)[1].split("\n    @property", 1)[0]
    assert "er.async_get" in resolver
    assert "async_entries_for_config_entry" in resolver
    assert 'domain == "vacuum"' in resolver


def test_stuck_entity_exists_and_reads_the_coordinator() -> None:
    text = BINARY_SENSOR.read_text(encoding="utf-8")

    assert "class RoborockStuckBinarySensorEntity" in text
    assert 'translation_key = "stuck"' in text
    assert "coordinator.is_stuck" in text
    # It has to be registered, or the entity never appears.
    assert "RoborockStuckBinarySensorEntity(coordinator)" in text


def test_options_flow_exposes_the_stuck_settings() -> None:
    text = CONFIG_FLOW.read_text(encoding="utf-8")
    step = text.split("async def async_step_drawables", 1)[1].split(
        "return self.async_show_form", 1
    )[0]

    for key in (
        "CONF_V1_STUCK_DETECTION_ENABLED",
        "CONF_V1_STUCK_WINDOW",
        "CONF_V1_STUCK_RADIUS",
    ):
        # Shown in the form...
        assert f"vol.Required(\n                {key}," in step, key
        # ...and persisted. Both halves matter: a field rendered but never
        # written back leaves the user's value silently discarded.
        assert f"self.options[{key}] =" in step, key


def test_stuck_settings_are_translated_everywhere() -> None:
    import json

    translations = COMPONENT / "translations"
    for path in [*translations.glob("*.json"), COMPONENT / "strings.json"]:
        data = json.loads(path.read_text(encoding="utf-8"))
        step = data["options"]["step"]["drawables"]
        for key in (
            "v1_stuck_detection_enabled",
            "v1_stuck_window",
            "v1_stuck_radius",
        ):
            assert key in step["data"], (path.name, key)
            assert key in step["data_description"], (path.name, key)

        names = data["entity"]["binary_sensor"]
        assert "stuck" in names, path.name
