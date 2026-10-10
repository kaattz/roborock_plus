"""Build the corrected garage-door state machine configuration.

Every rule below is tied to measured evidence:

* The zone contains the dock, so it is a *danger* zone: `outside_danger_zone`
  is `off` while docked and `on` once the robot has left. The previous version
  keyed the departure phase on `in_danger_zone` turning `on`, which happens *at*
  the dock -- so it fired while the robot was still parked.
* The robot has never reported `in_danger_zone` as a departure signal; it is `on`
  at the dock. Phase B therefore keys on `outside_danger_zone -> on`.
* On 2026-10-08 the robot never left the dock but reported `docking` while
  attaching the mop (01:26:30-39), and the return phase opened the door for no
  reason. Guards are added to both phases C and D.
* `lessons.md` records that pausing outside the cleaning states cancels the
  task, so phase B pauses only inside them and verifies the pause took.
"""

from __future__ import annotations

import json
from pathlib import Path

VACUUM = "vacuum.g20s_ultra"
STATUS = "sensor.g20s_ultra_status"
COVER = "cover.vacuum_garage_door"
TASK_ACTIVE = "binary_sensor.g20s_ultra_task_active"
OUTSIDE_DANGER_ZONE = "binary_sensor.g20s_ultra_outside_danger_zone"

# The device's own record of when it finished a clean. It is written when the
# cleaning stops, before the robot travels home: on 2026-10-08 it read 21:07:21
# while the entity updated at 21:07:24 and the status became `returning_home` at
# 21:07:26.
LAST_CLEAN_END = "sensor.sao_di_ji_v2_timestamp_2"

# How recently that marker must have been written for a dock visit to count as
# "the task is over". A genuine finish reaches the dock 1-2 minutes later; a
# mid-task visit leaves the marker at the *previous* task's time, which is
# normally hours earlier.
TASK_END_MAX_AGE_MINUTES = 10

# States in which a pause is documented as resumable. Outside these the pause
# cancels the task, which is why phase B refuses to close without one.
CLEANING_STATES = (
    "cleaning",
    "segment_cleaning",
    "zoned_cleaning",
    "spot_cleaning",
)

NOT_PARKED = (
    "{{ states('" + VACUUM + "') not in ['docked', 'charging'] }}"
)
IS_PARKED = "{{ states('" + VACUUM + "') in ['docked', 'charging'] }}"

# A status change must persist this long before the door may open. Measured
# blip on 2026-10-08: the robot reported `returning` for 2.02 s while docked.
# A genuine return holds the state for tens of seconds, so 10 s removes the
# blip and still leaves the door open well before the robot arrives.
RETURN_SUSTAIN_SECONDS = 10
DOOR_CLOSED = (
    "{{ (state_attr('" + COVER + "', 'current_position') | float(100)) < 5 }}"
)

# The device echoes the commanded position immediately, so a wait keyed on
# position alone is satisfied by the echo. Every door wait therefore sits behind
# a settle delay longer than one full travel.
DOOR_SETTLE = "00:00:45"

# Settle plus room for a genuinely slow door: the old 60s budget now has to
# cover the settle as well.
DOOR_WAIT_TIMEOUT = "00:01:30"

DESCRIPTION = "\n".join(
    [
        "合并原先三个 *_plus 自动化，用 mode: queued 串行化，避免它们同时抢柜门和 pause。",
        "",
        "危险区语义：配置的区域包含充电桩，是「不能关门」的危险区。",
        "所以停靠时 outside_danger_zone=off、in_danger_zone=on；出门后正好反过来。",
        "",
        "四个阶段：",
        "A 启动：集成的 garage_guard 在下发命令前就开门并等门全开，扫地机不会被",
        "   关着的门挡住。按约定只管 HA 发起的命令；从 Roborock App 启动不归这里管。",
        "B 离开：outside_danger_zone 从 off 变 on（扫地机真的走出危险区）后，暂停 → 关门 → 恢复。",
        "   暂停只在清扫状态里做，并确认真的进入 paused 才关门；否则只告警不关门，",
        "   因为「暂停没生效」意味着它可能还在往门口方向移动。",
        "C 返回：状态变成回基站/洗拖布，且扫地机确实不在基站上，才开门。",
        "   这个「不在基站」的守卫是必须的：2026-10-08 01:26 扫地机全程停在基站上",
        "   装拖布，状态却跳了 docking，把门白白开了又关、关了又开共 5 次。",
        "D 停靠：清扫已完成、已回基站、危险区传感器确认它在区内，才关门。",
        "   两个守卫都不能省：",
        "   · task_active 单独不够。它的状态表把 charging / attaching_the_mop /",
        "     detaching_the_mop / back_to_dock_washing_duster 都当作「无任务」，",
        "     所以「清扫中途回基站」（洗拖布、充电，任务没完成）会让它变成 off。",
        "     此时若关门，扫地机马上要出来继续扫，而阶段 C 不会重新开门 ——",
        "     它从 charging 直接变 cleaning，不会报 returning_home。",
        "     所以另外要求设备自己的「上次清扫结束」时间戳是最近的。",
        "   · 危险区传感器必须确认它在区内。停靠时它就在危险区里，若读数是过时的",
        "     on（「已离开」），关下去就会夹住机器。传感器没确认时门保持开启并告警。",
        "",
        "任何等待超时都只告警、不强行动作：门与扫地机状态不一致时，扫地机可能在门洞里，",
        "没有安全的自动恢复方式。",
    ]
)

TRIGGERS = [
    {
        # `from: off` is load-bearing, not decoration. Without it the trigger
        # fires on *every* transition into `on`, and the sensor does not only
        # go on when the robot leaves: while cleaning it alternates
        # `on -> unknown -> on` whenever a sample ages out before its
        # replacement lands (measured 2026-10-08: the poll cadence is 5s but a
        # map read takes a median of 6s and often 11s, while a sample is only
        # trusted for 2 x 5s = 10s).
        #
        # Each recovery was a fresh edge, so one departure became four and a
        # single clean was interrupted by three pause/close/resume cycles --
        # the robot was paused at 19:42:00, 19:42:38 and 19:43:21, each within
        # seconds of a recovery. Requiring `off` makes a real departure
        # (off -> on) fire and a flap recovery (unknown -> on) not.
        "trigger": "state",
        "entity_id": OUTSIDE_DANGER_ZONE,
        "id": "leave",
        "from": "off",
        "to": "on",
    },
    {
        # Sustained, not momentary. On 2026-10-08 the vacuum entity reported
        # `returning` for exactly 2.02 s while the robot sat on its dock
        # attaching the mop (01:26:37.807 -> 01:26:39.824), and that blip was
        # enough to open the door for no reason. A real return lasts tens of
        # seconds, so a short `for:` filters the blip without adding meaningful
        # latency to the case that matters.
        "trigger": "state",
        "entity_id": STATUS,
        "id": "return",
        "to": ["returning_home", "docking", "going_to_wash_the_mop"],
        "for": {"seconds": RETURN_SUSTAIN_SECONDS},
    },
    {"trigger": "state", "entity_id": TASK_ACTIVE, "id": "park_task", "to": "off"},
    {"trigger": "state", "entity_id": VACUUM, "id": "park_dock", "to": "docked"},
]


def alert(level: str, title: str, message: str) -> dict:
    """Build an alert_notify action."""
    return {
        "action": "script.alert_notify",
        "data": {"level": level, "title": title, "message": message},
    }


def wait_for(template: str, timeout: str) -> dict:
    """Build a non-fatal wait_template.

    Deliberately a template rather than `wait_for_trigger`. These waits follow a
    cover action and want "until the door has reached this position", which is
    already true the moment the cover reports it. `wait_for_trigger` fires on
    *changes* only, so a door that finished moving in the gap between the action
    and the wait would never fire it and the run would report a false failure.
    """
    return {
        "continue_on_timeout": True,
        "timeout": timeout,
        "wait_template": template,
    }


def door_wait(template: str) -> list[dict]:
    """Wait for a door position, past the window where the reading is an echo.

    The delay is not cosmetic. Without it the device's immediate echo of the
    command satisfies the wait, so the anti-crush guards in the closing phases
    never hold and the door can be moved while the robot is in the doorway.

    Returns a *list* because HA allows one action key per step: the delay and the
    wait must be separate list items. Splice it in with `*door_wait(...)`.
    """
    return [{"delay": DOOR_SETTLE}, wait_for(template, DOOR_WAIT_TIMEOUT)]


def condition_status_in(states: tuple[str, ...]) -> dict:
    """Native state condition over a list of acceptable statuses."""
    return {"condition": "state", "entity_id": STATUS, "state": list(states)}


def condition_not_parked() -> dict:
    """Native negation of 'the robot is on its dock'."""
    return {
        "condition": "not",
        "conditions": [
            {
                "condition": "state",
                "entity_id": VACUUM,
                "state": ["docked", "charging"],
            }
        ],
    }


def condition_parked() -> dict:
    """Native state condition for a robot sitting on its dock."""
    return {
        "condition": "state",
        "entity_id": VACUUM,
        "state": ["docked", "charging"],
    }


def condition_door_above(value: int) -> dict:
    """Native numeric check on the cover's position attribute."""
    return {
        "condition": "numeric_state",
        "entity_id": COVER,
        "attribute": "current_position",
        "above": value,
    }


def condition_task_really_ended() -> dict:
    """The clean must have finished, not merely paused at the dock.

    `task_active` alone cannot answer this. Its state list treats `charging`,
    `attaching_the_mop`, `detaching_the_mop` and `back_to_dock_washing_duster`
    as "no task", so a mid-task dock visit (to wash the mop or take on charge)
    drives it to `off` while the clean is still unfinished. Phase D closing on
    that would shut the door on a robot that is about to come back out -- and
    phase C would not reopen it, because a robot going straight from `charging`
    to `cleaning` never reports `returning_home`.

    The device's own end-of-clean marker resolves it. It is stamped when the
    cleaning stops, and a mid-task visit leaves it at the *previous* task's
    time. Requiring it to be recent separates a finished clean from a pause.

    The dashboard's `robot_status_mopping` and similar states are not used:
    they name the cleaning mode, not the task's life cycle.
    """
    return {
        "condition": "template",
        "value_template": (
            "{% set ended = states('" + LAST_CLEAN_END + "') %}"
            "{% if ended in ['unknown', 'unavailable', 'none', ''] %}"
            "false"
            "{% else %}"
            "{{ (as_timestamp(ended) | float(0)) > 0"
            " and (as_timestamp(now()) - as_timestamp(ended))"
            " < " + str(TASK_END_MAX_AGE_MINUTES * 60) + " }}"
            "{% endif %}"
        ),
    }


def guard(fail_title: str, fail_message: str, stop_reason: str) -> dict:
    """Alert and stop when the preceding wait timed out."""
    return {
        "if": [{"condition": "template", "value_template": "{{ not wait.completed }}"}],
        "then": [alert("warning", fail_title, fail_message), {"stop": stop_reason}],
    }


# Phase B: the robot has genuinely left the danger zone.
PHASE_LEAVE = {
    "conditions": [
        {"condition": "trigger", "id": ["leave"]},
        condition_status_in(CLEANING_STATES),
        condition_not_parked(),
        condition_door_above(5),
    ],
    "sequence": [
        {"action": "vacuum.pause", "target": {"entity_id": VACUUM}},
        wait_for("{{ is_state('" + VACUUM + "', 'paused') }}", "00:00:20"),
        guard(
            "柜门状态机：暂停失败",
            "扫地机已走出危险区，但暂停没有生效，柜门保持开启。"
            "它可能仍在移动，此时关门有夹机风险。"
            "vacuum={{ states('" + VACUUM + "') }}，"
            "status={{ states('" + STATUS + "') }}",
            "离开阶段暂停失败",
        ),
        {"action": "cover.close_cover", "target": {"entity_id": COVER}},
        *door_wait(DOOR_CLOSED),
        guard(
            "柜门状态机：关门失败",
            "扫地机已暂停、不再移动，但柜门未能在 90 秒内完全关闭。"
            "扫地机保持暂停，请手动处理。",
            "离开阶段关门失败",
        ),
        {"action": "roborock_plus.resume_task", "target": {"entity_id": VACUUM}},
        alert(
            "info",
            "扫地机已出门",
            "扫地机已离开危险区，柜门已关闭，任务已恢复。",
        ),
    ],
}

# Phase C: on the way back, and genuinely not parked.
PHASE_RETURN = {
    "conditions": [
        {"condition": "trigger", "id": ["return"]},
        # The guard that stops the 01:26 flapping: a robot sitting on the dock
        # cannot be "returning" to it, whatever the status blips say.
        condition_not_parked(),
        {
            "condition": "numeric_state",
            "entity_id": COVER,
            "attribute": "current_position",
            "below": 95,
        },
    ],
    "sequence": [
        {"action": "cover.open_cover", "target": {"entity_id": COVER}},
        *door_wait(
            "{{ (state_attr('" + COVER + "', 'current_position') | float(0)) >= 95 }}"
        ),
        {
            "if": [
                {
                    "condition": "template",
                    "value_template": "{{ not wait.completed }}",
                }
            ],
            "then": [
                alert(
                    "critical",
                    "柜门打开失败",
                    "扫地机正在回基站（{{ trigger.to_state.state }}），"
                    "但柜门未能在 90 秒内完全打开，有撞门风险。",
                ),
                {"stop": "返回阶段开门失败"},
            ],
        },
    ],
}

# Phase D: parked, task over, door open.
PHASE_PARK = {
    "conditions": [
        {"condition": "trigger", "id": ["park_task", "park_dock"]},
        {"condition": "state", "entity_id": TASK_ACTIVE, "state": "off"},
        condition_parked(),
        condition_door_above(5),
        # The guard that separates "finished" from "paused at the dock mid-task".
        condition_task_really_ended(),
    ],
    "sequence": [
        # Settle first: the state blips during mop attach/detach are shorter
        # than this, so a transient pairing does not reach the door.
        {"delay": "00:00:08"},
        {"condition": "state", "entity_id": TASK_ACTIVE, "state": "off"},
        condition_parked(),
        condition_door_above(5),
        condition_task_really_ended(),
        {
            "if": [
                # A docked robot stands inside the danger zone, so the sensor
                # must confirm it is there before the door may move.
                {"condition": "state", "entity_id": OUTSIDE_DANGER_ZONE, "state": "off"}
            ],
            "then": [
                {"action": "cover.close_cover", "target": {"entity_id": COVER}},
                *door_wait(DOOR_CLOSED),
                {
                    "if": [
                        {
                            "condition": "template",
                            "value_template": "{{ not wait.completed }}",
                        }
                    ],
                    "then": [
                        alert(
                            "warning",
                            "柜门状态机关门失败",
                            "清扫任务已结束、扫地机已在基站，"
                            "但柜门未能完全关闭，柜门保持开启。",
                        )
                    ],
                },
                alert(
                    "info",
                    "扫地机已回柜",
                    "任务已结束、扫地机已在基站，柜门已关闭。",
                ),
            ],
            "else": [
                # Fail safe and say so: closing here is the one mistake that
                # cannot be undone, so the door stays open and the owner is told.
                alert(
                    "warning",
                    "柜门状态机：未确认扫地机在危险区内，柜门保持开启",
                    "扫地机已停靠在基站、任务已结束，但 "
                    "outside_danger_zone={{ states('" + OUTSIDE_DANGER_ZONE + "') }}"
                    "（停靠时应为 off）。危险区读数未确认它在区内，"
                    "为避免夹机，柜门保持开启，请手动确认后关闭。",
                )
            ],
        },
    ],
}

CONFIG = {
    "alias": "扫地机柜门状态机",
    "description": DESCRIPTION,
    "mode": "queued",
    "max": 5,
    "max_exceeded": "silent",
    "triggers": TRIGGERS,
    "conditions": [],
    "actions": [{"choose": [PHASE_LEAVE, PHASE_RETURN, PHASE_PARK]}],
}

# Written next to this script, so the file that gets pushed to HA is always the
# one this builder produced. A temp path let an edit here silently fail to reach
# the config that was actually deployed.
out = Path(__file__).resolve().parent / "state_machine.json"
out.write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"wrote {out}")
print(f"triggers: {[t['id'] for t in TRIGGERS]}")
print(f"choose branches: {len(CONFIG['actions'][0]['choose'])}")
print(f"description lines: {len(DESCRIPTION.splitlines())}")
print()
print("--- template sanity (must not contain doubled quotes or 'None') ---")
blob = json.dumps(CONFIG, ensure_ascii=False)
for bad in ['""', "'None'", "None ", "{{ }}"]:
    status = "FOUND" if bad in blob else "ok"
    print(f"  {bad!r}: {status}")

print()
print("--- native-construct check ---")
native = blob.count('"condition": "state"') + blob.count('"condition": "numeric_state"')
templates = blob.count("value_template")
print(f"  native conditions: {native}")
print(f"  template conditions: {templates}")
print(f"  wait_template (intentional): {blob.count('wait_template')}")
