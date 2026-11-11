"""The blueprint, replayed against the two endings recorded on 2026-11-10.

Both are real sequences read out of the recorder, and both produced a wrong
notification:

* the afternoon clean **finished** (0 -> 4.2 m², device error sensor `none`) but
  the completion block sampled `vacuum` at 15:09:21, five seconds before it
  reached `docked`, saw `returning`, and announced "清扫异常结束";
* every successful clean, finished or not, was followed by "扫地机定时清扫未执行",
  because that block asked `task_active` *now* instead of whether a clean had
  ever started in this round.

These run the repository blueprint through Home Assistant's own Script engine.
Only service calls and state changes are stubs; every template, wait, repeat and
condition is the shipped YAML. No device is touched.

Expected after the fix:

* a clean that ends while the robot is still driving home is reported as
  finished once it docks;
* a clean that stops in the room is still reported as abnormal -- the settle wait
  must not launder a genuine failure into a success;
* "未执行" appears only when no clean ever started.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.script import Script, _ScriptRun, async_validate_actions_config
from homeassistant.helpers.script_variables import ScriptVariables

BLUEPRINT = Path(__file__).resolve().parents[1] / "blueprints" / "扫地机分区定时清扫.yaml"

#: Point the replay at another copy of the blueprint. Set to the file HA was
#: actually serving when checking a deployment; unset for the repository file.
BLUEPRINT_OVERRIDE = "ROBOROCK_BLUEPRINT_PATH"

STARTED = "已开始清扫"
FINISHED = "已完成"
BROKEN = "清扫异常结束"
NEVER_RAN = "未执行"


def _inputs(retry_window: int = 30) -> dict:
    return {
        "vacuum_entity": "vacuum.memory_robot",
        "presence_sensors": [],
        "cleaning_mode": "仅扫地",
        "mop_intensity": "medium",
        "mop_route": "standard",
        "areas": [],
        "retry_window_minutes": retry_window,
        "retry_interval_minutes": 5,
        "notify_script": "script.memory_alert",
        "notify_on_start": True,
        "notify_on_skip": False,
        "door_sensors": [],
        "door_open_state": "on",
        "my_trigger": [],
    }


class _Loader(yaml.SafeLoader):
    pass


def _loader_for(retry_window: int):
    inputs = _inputs(retry_window)

    class Loader(_Loader):
        pass

    Loader.add_constructor("!input", lambda loader, node: inputs[loader.construct_scalar(node)])
    return Loader


def _titles(alerts: list[dict]) -> list[str]:
    return [item.get("title", "") for item in alerts]


def _matching(alerts: list[dict], needle: str) -> list[str]:
    return [title for title in _titles(alerts) if needle in title]


async def _replay(
    scenario: str,
    *,
    retry_window: int | None = None,
    max_wait: float = 3.0,
) -> list[dict]:
    """Run the real blueprint once, with the robot behaving as recorded.

    Real durations would make this test sit through the blueprint's 8-hour wait
    and 120-second settle window. Every bounded wait is capped at ``max_wait``
    instead. That keeps the decision honest, because the branch taken is decided
    by the *states*: a wait that is satisfied by a state change still completes
    as soon as that change happens, and a wait that is meant to expire still
    expires -- just sooner. No state is altered by the compression.
    """
    original = _ScriptRun._async_futures_with_timeout

    def capped(run, timeout):
        futures, handle, future = original(run, min(timeout, max_wait))
        return futures, handle, future

    _ScriptRun._async_futures_with_timeout = capped
    try:
        return await _replay_with_states(scenario, retry_window)
    finally:
        _ScriptRun._async_futures_with_timeout = original


def _blueprint_path() -> Path:
    override = os.environ.get(BLUEPRINT_OVERRIDE)
    return Path(override) if override else BLUEPRINT


async def _replay_with_states(scenario: str, retry_window: int | None) -> list[dict]:
    with TemporaryDirectory() as directory:
        hass = HomeAssistant(directory)
        alerts: list[dict] = []

        def set_state(entity: str, state: str, attributes: dict | None = None) -> None:
            hass.states.async_set(entity, state, attributes)

        def start_idle() -> None:
            set_state("vacuum.memory_robot", "docked")
            set_state("binary_sensor.memory_task_active", "off")
            set_state(
                "select.memory_intensity",
                "medium",
                {"options": ["off", "medium", "extreme"]},
            )

        start_idle()

        async def setting(call) -> None:
            return None

        async def clean(call) -> None:
            if scenario == "never_started":
                return
            set_state("binary_sensor.memory_task_active", "on")
            set_state("vacuum.memory_robot", "cleaning")

            async def ending() -> None:
                await asyncio.sleep(0.2)
                if scenario == "ends_while_returning":
                    # 2026-11-10 15:09:21: task over, robot still 5 s from the dock.
                    set_state("binary_sensor.memory_task_active", "off")
                    set_state("vacuum.memory_robot", "returning")
                    await asyncio.sleep(0.5)
                    set_state("vacuum.memory_robot", "docked")
                elif scenario == "stops_in_the_room":
                    # 2026-11-10 08:35:31: stopped by `vacuum.stop`, mid-room.
                    set_state("binary_sensor.memory_task_active", "off")
                    set_state("vacuum.memory_robot", "idle")
                else:
                    set_state("vacuum.memory_robot", "docked")
                    set_state("binary_sensor.memory_task_active", "off")

            hass.async_create_task(ending())

        async def alert(call) -> None:
            alerts.append(dict(call.data))

        hass.services.async_register("select", "select_option", setting)
        hass.services.async_register("vacuum", "set_fan_speed", setting)
        hass.services.async_register("vacuum", "clean_area", clean)
        hass.services.async_register("script", "memory_alert", alert)

        config = yaml.load(
            _blueprint_path().read_text(encoding="utf-8"),
            Loader=_loader_for(30 if retry_window is None else retry_window),
        )
        sequence = await async_validate_actions_config(hass, cv.SCRIPT_SCHEMA(config["actions"]))
        script = Script(
            hass,
            sequence,
            "replay",
            "automation",
            variables=ScriptVariables(cv.template_complex(config["variables"])),
            log_exceptions=False,
        )
        await asyncio.wait_for(
            script.async_run(
                {
                    "device_entities_list": [],
                    "task_active_entity": "binary_sensor.memory_task_active",
                    "mop_intensity_entity": "select.memory_intensity",
                    "mop_route_entity": "",
                },
                context=Context(),
            ),
            timeout=30,
        )
        await hass.async_block_till_done()
        return alerts


def test_a_clean_that_ends_while_the_robot_is_driving_home_is_reported_as_finished() -> None:
    alerts = asyncio.run(_replay("ends_while_returning"))

    assert _matching(alerts, STARTED), "the clean really started"
    assert _matching(alerts, FINISHED), (
        "the robot reached the dock five seconds later; that is a finished clean"
    )
    assert not _matching(alerts, BROKEN), (
        "sampling `vacuum` once, mid-return, turned a finished clean into a failure"
    )


def test_a_clean_that_stops_in_the_room_is_still_reported_as_abnormal() -> None:
    """The settle wait must not wait its way to a success."""
    alerts = asyncio.run(_replay("stops_in_the_room"))

    assert _matching(alerts, BROKEN), "stopped away from the dock is a failure"
    assert not _matching(alerts, FINISHED)


def test_a_normal_finish_is_not_followed_by_a_never_ran_notice() -> None:
    alerts = asyncio.run(_replay("finishes_at_the_dock"))

    assert _matching(alerts, FINISHED)
    assert not _matching(alerts, NEVER_RAN), (
        "the round did start and did finish; `未执行` asked `task_active` now and "
        "fired on every completed clean"
    )


async def _replay_twice() -> tuple[list[dict], list[dict]]:
    """Run the same Script object twice: a finished clean, then one that never starts."""
    original = _ScriptRun._async_futures_with_timeout

    def capped(run, timeout):
        return original(run, min(timeout, 2.0))

    _ScriptRun._async_futures_with_timeout = capped
    try:
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            alerts: list[dict] = []
            behaviour = {"start": True}

            hass.states.async_set("vacuum.memory_robot", "docked")
            hass.states.async_set("binary_sensor.memory_task_active", "off")
            hass.states.async_set(
                "select.memory_intensity",
                "medium",
                {"options": ["off", "medium", "extreme"]},
            )

            async def setting(call) -> None:
                return None

            async def clean(call) -> None:
                if not behaviour["start"]:
                    return

                async def run_clean() -> None:
                    hass.states.async_set("binary_sensor.memory_task_active", "on")
                    hass.states.async_set("vacuum.memory_robot", "cleaning")
                    await asyncio.sleep(0.5)
                    hass.states.async_set("vacuum.memory_robot", "docked")
                    hass.states.async_set("binary_sensor.memory_task_active", "off")

                hass.async_create_task(run_clean())

            async def alert(call) -> None:
                alerts.append(dict(call.data))

            hass.services.async_register("select", "select_option", setting)
            hass.services.async_register("vacuum", "set_fan_speed", setting)
            hass.services.async_register("vacuum", "clean_area", clean)
            hass.services.async_register("script", "memory_alert", alert)

            config = yaml.load(
                _blueprint_path().read_text(encoding="utf-8"),
                Loader=_loader_for(0),
            )
            sequence = await async_validate_actions_config(
                hass, cv.SCRIPT_SCHEMA(config["actions"])
            )
            script = Script(
                hass,
                sequence,
                "reuse",
                "automation",
                variables=ScriptVariables(cv.template_complex(config["variables"])),
                log_exceptions=False,
            )
            run_vars = {
                "device_entities_list": [],
                "task_active_entity": "binary_sensor.memory_task_active",
                "mop_intensity_entity": "select.memory_intensity",
                "mop_route_entity": "",
            }

            await asyncio.wait_for(script.async_run(run_vars, context=Context()), timeout=30)
            await hass.async_block_till_done()
            first = list(alerts)

            alerts.clear()
            behaviour["start"] = False
            await asyncio.wait_for(script.async_run(run_vars, context=Context()), timeout=30)
            await hass.async_block_till_done()
            return first, list(alerts)
    finally:
        _ScriptRun._async_futures_with_timeout = original


def test_a_round_that_never_started_still_reports_never_ran() -> None:
    """The fix must not silence the case the notice exists for."""
    alerts = asyncio.run(_replay("never_started", retry_window=0))

    assert len(_matching(alerts, NEVER_RAN)) == 1
    assert not _matching(alerts, STARTED)
    assert not _matching(alerts, FINISHED)


def test_the_round_record_does_not_survive_into_the_next_round() -> None:
    """`clean_started` is a script-level variable. If HA rendered it once and
    reused it, the day's *second* round would inherit "already started" from the
    first -- and 未执行 would never fire again, silently losing the alarm this
    release exists to make honest.

    One Script object, two runs: a clean that completes, then a round that never
    starts. The second must still announce 未执行.
    """
    alerts = asyncio.run(_replay_twice())

    first, second = alerts
    assert _matching(first, STARTED), "round one really ran"
    assert _matching(first, FINISHED)
    assert not _matching(first, NEVER_RAN), "round one did start"

    assert _matching(second, NEVER_RAN), (
        "the flag must reset per run; otherwise a real 'never ran' goes unreported")
    assert not _matching(second, STARTED)
