"""Run the repository cleaning blueprint in an offline HA Script.

Only service I/O, entity discovery inputs and timer completion are in memory.
The deadline, repeat, conditions, wait templates and final alert are the actual
YAML expressions, evaluated by Home Assistant rather than a Python copy.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import yaml

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.script import Script, _ScriptRun, async_validate_actions_config
from homeassistant.helpers.script_variables import ScriptVariables
from homeassistant.helpers.template import Template
from homeassistant.util import dt as dt_util


BLUEPRINT = Path(__file__).parents[1] / "blueprints/扫地机分区定时清扫.yaml"


def load_blueprint(window=5, interval=1):
    inputs = {
        "vacuum_entity": "vacuum.memory_robot", "presence_sensors": ["binary_sensor.memory_presence"],
        "cleaning_mode": "扫拖一体", "mop_intensity": "medium", "mop_route": "standard",
        "areas": [], "retry_window_minutes": window, "retry_interval_minutes": interval,
        "notify_script": "script.memory_alert", "my_trigger": [],
    }

    class BlueprintLoader(yaml.SafeLoader):
        pass

    BlueprintLoader.add_constructor("!input", lambda loader, node: inputs[loader.construct_scalar(node)])
    return yaml.load(BLUEPRINT.read_text(encoding="utf-8"), Loader=BlueprintLoader)


def test_deadline_and_both_comparisons_use_native_numeric_values(monkeypatch):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            config = load_blueprint()
            rendered = ScriptVariables(cv.template_complex(config["variables"])).async_render(hass, {
                "device_entities_list": [], "task_active_entity": "binary_sensor.memory_task_active",
                "mop_intensity_entity": "select.memory_intensity", "mop_route_entity": "",
            })
            assert type(rendered["deadline"]) in (int, float)
            sequence = config["actions"][1]["repeat"]["sequence"]
            variables = {**rendered, "repeat": {"index": 2}}
            comparisons = (
                sequence[0]["variables"]["should_act"],
                sequence[1]["then"][-1]["if"][0]["value_template"],
            )
            for text in comparisons:
                assert Template(text, hass).async_render(variables) is True
            variables["deadline"] = 2000
            for seconds, expected in ((2000, True), (2000.1, False)):
                monkeypatch.setattr(dt_util, "now", lambda time_zone=None, seconds=seconds:
                                    datetime.fromtimestamp(seconds, timezone.utc))
                for text in comparisons:
                    assert Template(text, hass).async_render(variables) is expected

    asyncio.run(exercise())


@pytest.mark.parametrize("scenario,window", [
    ("success_first", 5), ("success_after_failure", 5), ("failure", 5),
    ("zero_window", 0), ("busy", 2), ("occupied", 2),
])
def test_actual_blueprint_script_retries_or_reaches_final_alert(monkeypatch, scenario, window):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            origin = datetime(2026, 10, 17, 4, 0, tzinfo=timezone.utc)
            elapsed = 0.0
            calls = []
            timers = []
            original_timeout = _ScriptRun._async_futures_with_timeout

            def utcnow():
                return origin + timedelta(seconds=elapsed)

            monkeypatch.setattr(dt_util, "utcnow", utcnow)
            monkeypatch.setattr(dt_util, "now", lambda time_zone=None: utcnow().astimezone(time_zone))

            def accelerated_timeout(run, timeout):
                futures, handle, future = original_timeout(run, timeout)
                if handle is None:
                    return futures, handle, future
                handle.cancel()

                def complete():
                    nonlocal elapsed
                    if future.done():
                        return
                    elapsed += timeout
                    timers.append(timeout)
                    future.set_result(None)

                return futures, hass.loop.call_later(0, complete), future

            monkeypatch.setattr(_ScriptRun, "_async_futures_with_timeout", accelerated_timeout)
            hass.states.async_set("vacuum.memory_robot", "cleaning" if scenario == "busy" else "docked")
            hass.states.async_set("binary_sensor.memory_task_active", "off")
            hass.states.async_set("binary_sensor.memory_presence", "on" if scenario == "occupied" else "off")
            hass.states.async_set("select.memory_intensity", "medium", {"options": ["off", "medium", "extreme"]})

            async def setting(call):
                calls.append(("setting", elapsed, dict(call.data)))

            async def clean(call):
                calls.append(("clean", elapsed, dict(call.data)))
                attempts = sum(kind == "clean" for kind, *_ in calls)
                if scenario == "success_first" or (scenario == "success_after_failure" and attempts == 2):
                    hass.states.async_set("binary_sensor.memory_task_active", "on")
                    hass.states.async_set("vacuum.memory_robot", "cleaning")
                    return
                raise HomeAssistantError("simulated manual_override")

            async def alert(call):
                calls.append(("alert", elapsed, dict(call.data)))

            hass.services.async_register("select", "select_option", setting)
            hass.services.async_register("vacuum", "set_fan_speed", setting)
            hass.services.async_register("vacuum", "clean_area", clean)
            hass.services.async_register("script", "memory_alert", alert)
            config = load_blueprint(window=window)
            sequence = await async_validate_actions_config(hass, cv.SCRIPT_SCHEMA(config["actions"]))
            script = Script(
                hass, sequence, "Memory cleaning retry", "automation",
                variables=ScriptVariables(cv.template_complex(config["variables"])), log_exceptions=False,
            )
            await asyncio.wait_for(script.async_run({
                "device_entities_list": [], "task_active_entity": "binary_sensor.memory_task_active",
                "mop_intensity_entity": "select.memory_intensity", "mop_route_entity": "",
            }, context=Context()), timeout=5)
            attempts = [item for item in calls if item[0] == "clean"]
            alerts = [item for item in calls if item[0] == "alert"]
            if scenario.startswith("success"):
                assert len(attempts) == (1 if scenario == "success_first" else 2)
                assert not alerts
                if scenario == "success_after_failure":
                    assert attempts[1][1] >= 90 + 60
                    assert attempts[1][1] <= window * 60
            else:
                assert len(alerts) == 1
                assert alerts[0][2]["title"] == "扫地机定时清扫未执行"
                assert len(attempts) == (0 if scenario in ("busy", "occupied") else 3 if scenario == "failure" else 1)
                assert alerts[0][1] >= window * 60
            if scenario == "zero_window":
                assert attempts[0][1] == 0
                assert timers == [90]

    asyncio.run(exercise())
