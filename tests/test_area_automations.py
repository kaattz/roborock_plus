"""The per-area automations in this folder are real, loadable automation configs.

Each file is the copy that gets pasted into `automations.yaml`. Two things about
that copy are easy to get wrong and impossible to notice until the automation
mysteriously does nothing:

* it must be a **single mapping**, not a list. A list breaks the blueprint
  expansion silently and the automation shows up as `unavailable` with no
  `mode`/`current` attributes.
* it must not carry `categories:`. That key also breaks blueprint expansion; the
  category has to be set through the registry instead.

The master bedroom task (2026-11-08) adds a door check: the room has its own door
and the robot cannot get in while it is shut.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
AUTOMATIONS = ROOT / "automations"
CIWO = AUTOMATIONS / "ciwo_cleaning_task.yaml"
ZHUWO = AUTOMATIONS / "zhuwo_cleaning_task.yaml"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


ALL = [pytest.param(CIWO, id="ciwo"), pytest.param(ZHUWO, id="zhuwo")]


@pytest.mark.parametrize("path", ALL)
def test_it_is_a_single_mapping_not_a_list(path: Path) -> None:
    data = load(path)
    assert isinstance(data, dict), (
        "a list here breaks blueprint expansion; the automation becomes "
        "unavailable with no mode/current attributes")


@pytest.mark.parametrize("path", ALL)
def test_it_does_not_carry_categories(path: Path) -> None:
    assert "categories" not in load(path), (
        "`categories:` in automations.yaml breaks blueprint expansion; set the "
        "category in .storage/core.entity_registry instead")


@pytest.mark.parametrize("path", ALL)
def test_it_points_at_the_cleaning_blueprint(path: Path) -> None:
    assert load(path)["use_blueprint"]["path"] == "kaattz/扫地机分区定时清扫.yaml"


@pytest.mark.parametrize("path", ALL)
def test_it_targets_the_real_vacuum(path: Path) -> None:
    data = load(path)
    assert data["use_blueprint"]["input"]["vacuum_entity"] == "vacuum.g20s_ultra"


def test_the_two_automations_have_different_ids() -> None:
    """A duplicate id means one of them silently replaces the other."""
    assert load(CIWO)["id"] != load(ZHUWO)["id"]


def test_the_master_bedroom_run_is_gated_on_its_own_door() -> None:
    inputs = load(ZHUWO)["use_blueprint"]["input"]
    assert inputs["door_sensors"] == ["binary_sensor.master_bedroom_door"]
    assert inputs["door_open_state"] == "on", (
        "measured 2026-11-08: this sensor reads `on` while the door is open")


def test_the_master_bedroom_run_ignores_presence() -> None:
    """The household asked for no presence check here, as with 次卧."""
    assert load(ZHUWO)["use_blueprint"]["input"]["presence_sensors"] == []


def test_the_master_bedroom_runs_tuesday_thursday_saturday_at_ten() -> None:
    trigger = load(ZHUWO)["use_blueprint"]["input"]["my_trigger"][0]
    assert trigger["trigger"] == "time"
    assert trigger["at"] == "10:00:00"
    assert trigger["weekday"] == ["tue", "thu", "sat"]


def test_the_two_tasks_do_not_share_a_weekday_hour() -> None:
    """次卧 is Mon/Wed/Fri/Sun 15:00; 主卧 is Tue/Thu/Sat 10:00.

    They must not collide: the blueprint runs `mode: single` per automation, but
    two automations firing at once would both drive the same vacuum.
    """
    ciwo = load(CIWO)["use_blueprint"]["input"]["my_trigger"][0]
    zhuwo = load(ZHUWO)["use_blueprint"]["input"]["my_trigger"][0]
    assert not set(ciwo["weekday"]) & set(zhuwo["weekday"]), (
        "the weekday sets overlap, so the two tasks can start together")
    assert ciwo["at"] != zhuwo["at"]
