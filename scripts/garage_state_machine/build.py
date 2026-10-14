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

# The magnet contact mounted at the *closed* position. Unlike `current_position`
# it is a physical fact: nothing writes to it but the magnet, so the device's
# habit of echoing a commanded position cannot reach it.
#
# Measured 2026-10-14, which is what makes it the better signal for "shut":
#
#   14:21:15.373  contact -> off      the door reached the closed position
#   14:21:20.736  cover   -> closed   the cover reported it 5.4s later
#
#   14:15:16.976  cover   -> open     echo: claimed "arrived" after 0.7s
#   14:15:23.499  contact -> on       the contact flips when the magnet parts
#
# Note what the contact does *not* answer: it flips `on` as soon as the door
# leaves the closed position (7.2s into a 34s travel), so it says nothing about
# whether the door reached the *open* position. The opening wait therefore keeps
# the position reading and its settle delay -- there is no contact at that end,
# which is the known boundary recorded in the design doc.
CONTACT = "binary_sensor.vacuum_garage_door_contact_state"
DOOR_CLOSED = "{{ " + f"is_state('{CONTACT}', 'off')" + " }}"

# The magnet makes contact slightly before the motor stops: measured 2026-10-14,
# contact at 14:21:15.373 and the cover reporting `closed` at 14:21:20.736. Both
# callers of door_shut_wait() act on something that expects a still door -- one
# resumes the robot, the other stops the motor to release the clutch -- so a
# short settle follows the contact rather than acting on the instant.
CONTACT_SETTLE = {"seconds": 8}


# Kept for the one wait that still reads `current_position`: the door *opening*
# in phase C. The device echoes the commanded position immediately, so a wait
# keyed on position alone is satisfied by the echo, and every such wait sits
# behind a settle delay longer than one full travel.
DOOR_SETTLE = "00:00:45"

# Settle plus room for a genuinely slow door: the old 60s budget now has to
# cover the settle as well.
DOOR_WAIT_TIMEOUT = "00:01:30"

# How long the vacuum gets to leave `paused` after the resume command. Unlike
# the door waits this needs no settle delay in front of it: the vacuum's state
# comes from the device's own status, not from an echo of the command, so there
# is no window in which a commanded-but-not-yet-happened change reads as done.
# A minute covers the slowest polling (the official cloud is 60s); the local
# server this house runs polls every second.
RESUME_WAIT_TIMEOUT = "00:01:00"

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
        "   恢复之后要确认它真的离开了 paused 才算成功 —— 失败（集成没加载、",
        "   或暂停中重启导致恢复命令推导不出来）时只告警不假装成功，因为此时",
        "   扫地机停在关着的柜门后，静默等于让你以为它在正常工作。",
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
        "",
        "门判据（2026-10-14 起）：「门关好了没」改用装在全关位的门磁（物理触点），",
        "不再依赖「命令后等 45 秒越过回声窗口」那个固定等待 —— 触点不会被回声污染，",
        "实测比 cover 早约 5 秒报出真实状态（14:21:15.373 门磁 off，14:21:20.736 cover",
        "才 closed）。开门那一侧仍用 position + settle：全开位装不了门磁，这是已知边界。",
        "门磁读 unavailable/unknown 时两个方向都不匹配，关门等待会超时并告警，",
        "而不是把「读数未知」当成「门已关」—— 后者会把扫地机夹在门里。",
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


def door_open_wait() -> list[dict]:
    """Wait for the door to be open, past the window where the reading is an echo.

    The delay is not cosmetic. Without it the device's immediate echo of the
    command satisfies the wait, and a robot returning home is let in at a door
    that has barely started moving.

    Only the *opening* wait still works this way. There is no contact at the open
    end of the travel -- the owner could fit one only at the closed position --
    so "has it opened far enough" has no physical answer and the position reading
    plus a settle delay is the best available. This is the known boundary in
    docs/plans/2026-10-10-garage-door-position-echo-design.md.
    """
    return [
        {"delay": DOOR_SETTLE},
        wait_for(
            "{{ (state_attr('" + COVER + "', 'current_position') | float(0)) >= 95 }}",
            DOOR_WAIT_TIMEOUT,
        ),
    ]


def door_shut_wait() -> list[dict]:
    """Wait until the contact says the door is at the closed position.

    No echo settle here, and that is the point of moving this judgement to the
    contact: the device cannot echo a physical switch, so the wait is already
    immune to the pollution that forced a 45-second delay in front of the
    position-based version. It returns as soon as the magnet makes contact, which
    on 2026-10-14 was 5.4s *before* the cover admitted the door had closed.

    The contact can make contact slightly before the motor stops (measured:
    magnet at 14:21:15.373, motor done by 14:21:20.736), and both callers resume
    or stop something that expects a still door, so a short settle follows.
    """
    return [
        wait_for("{{ " + _contact_is("off") + " }}", DOOR_WAIT_TIMEOUT),
        {"delay": CONTACT_SETTLE},
    ]


def _contact_is(state: str) -> str:
    """A template fragment that is true only for an exact contact reading.

    Equality against a literal, never a negated test: `unavailable` must not
    satisfy "the door is shut" (it would let a robot be shut in) nor "the door is
    open" (it would report a shut door as ready to receive one). Both directions
    fail by not matching.
    """
    return f"is_state('{CONTACT}', '{state}')"



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


def condition_door_open() -> dict:
    """The door has physically left its closed position.

    Replaces a `current_position > 5` test, which the echo could satisfy: the
    device writes the commanded position the moment it is commanded, so a door
    that is still shut can read 100. The contact cannot be echoed.

    `unavailable` deliberately fails this, which is the safe direction for its
    one purpose -- making a repeated departure edge harmless by refusing to pause
    a second time behind a door we cannot confirm is open.
    """
    return {
        "condition": "state",
        "entity_id": CONTACT,
        "state": "on",
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


def resume_step() -> dict:
    """Resume the task, and prove the resume actually took.

    Phase B closes the door behind a paused robot, so this step is what puts it
    back to work. It is also the only step here that calls an action the
    integration owns, which makes it the only one that can fail for a reason the
    automation cannot see coming.

    Two failures were observed or are documented, and a bare call handles
    neither:

    * **The integration is not loaded.** On 2026-10-13 21:47 -> 2026-10-14 01:18
      HA could not install `python-roborock==5.5.1` (pypi unreachable), so
      `async_setup` never ran, no `roborock_plus.*` action existed, and the
      vacuum entities read `unavailable`. `continue_on_error` does **not** cover
      this: `homeassistant/helpers/script.py` re-raises `ServiceNotFound`
      regardless, because a call to an action that does not exist is treated as
      a broken script rather than a runtime error. The only defence is to not
      call it -- hence the `unavailable` pre-check.
    * **The resume command cannot be derived.** `vacuum.py` raises
      `resume_not_available` when the device state cannot say which command
      continues the task; README documents this as the consequence of a restart
      while paused, because `last_resume_command` lives in memory.
      `continue_on_error` does cover this one, and it must be set, or the run
      ends before anything can tell the owner.

    And "the action did not raise" is not evidence that the robot moved, which
    is the same trap `task_active` exists to close for starting a clean. So the
    resume is followed by a wait on the vacuum leaving `paused`, and the success
    notice sits *after* this block, where no failure path can reach it.
    """
    return {
        "if": [
            {
                "condition": "template",
                # `states()` answers `unknown` for an entity that is absent
                # entirely and `unavailable` for one whose integration is not
                # loaded; both mean the action is not there to call.
                "value_template": (
                    "{{ states('" + VACUUM + "')"
                    " not in ['unavailable', 'unknown'] }}"
                ),
            }
        ],
        "then": [
            {
                "action": "roborock_plus.resume_task",
                "target": {"entity_id": VACUUM},
                # `resume_not_available` is a HomeAssistantError, so this is
                # what lets the guard below report it instead of the run dying
                # here. It cannot cover ServiceNotFound; the `if` above does.
                "continue_on_error": True,
            },
            wait_for(
                "{{ not is_state('" + VACUUM + "', 'paused')"
                " and states('" + VACUUM + "')"
                " not in ['unavailable', 'unknown'] }}",
                RESUME_WAIT_TIMEOUT,
            ),
            guard(
                "柜门状态机：恢复任务失败",
                "扫地机已暂停、柜门已关闭，但恢复命令没有让它动起来"
                "（仍在 paused）。它停在柜门后不会自行移动，"
                "请手动恢复任务或重新发起清扫。"
                "vacuum={{ states('" + VACUUM + "') }}，"
                "status={{ states('" + STATUS + "') }}",
                "离开阶段恢复任务失败",
            ),
        ],
        # Fail safe and say so. The robot is paused and the door is shut, which
        # is a stable state -- but nothing will move again until somebody looks,
        # so this must never be silent, and it must never reach the success
        # notice below.
        "else": [
            alert(
                "warning",
                "柜门状态机：集成未加载或设备离线，任务未恢复",
                "扫地机已暂停、柜门已关闭，但 roborock_plus 集成不可用或扫地机离线"
                "（vacuum={{ states('" + VACUUM + "') }}），恢复命令没有下发。"
                "扫地机停在柜门后不会自行移动，请先确认集成为何未加载。",
            ),
            {"stop": "离开阶段集成不可用"},
        ],
    }


# Phase B: the robot has genuinely left the danger zone.
PHASE_LEAVE = {
    "conditions": [
        {"condition": "trigger", "id": ["leave"]},
        condition_status_in(CLEANING_STATES),
        condition_not_parked(),
        condition_door_open(),
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
        *door_shut_wait(),
        guard(
            "柜门状态机：关门失败",
            "扫地机已暂停、不再移动，但柜门未能在 90 秒内确认关闭"
            "（门磁={{ states('" + CONTACT + "') }}）。"
            "扫地机保持暂停，请手动处理。",
            "离开阶段关门失败",
        ),
        resume_step(),
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
        *door_open_wait(),
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
        condition_door_open(),
        # The guard that separates "finished" from "paused at the dock mid-task".
        condition_task_really_ended(),
    ],
    "sequence": [
        # Settle first: the state blips during mop attach/detach are shorter
        # than this, so a transient pairing does not reach the door.
        {"delay": "00:00:08"},
        {"condition": "state", "entity_id": TASK_ACTIVE, "state": "off"},
        condition_parked(),
        condition_door_open(),
        condition_task_really_ended(),
        {
            "if": [
                # A docked robot stands inside the danger zone, so the sensor
                # must confirm it is there before the door may move.
                {"condition": "state", "entity_id": OUTSIDE_DANGER_ZONE, "state": "off"}
            ],
            "then": [
                {"action": "cover.close_cover", "target": {"entity_id": COVER}},
                *door_shut_wait(),
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
                            "但门磁未确认柜门关闭（门磁="
                            "{{ states('" + CONTACT + "') }}），柜门保持开启。",
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
    # The alias is the English name chosen on 2026-10-09. build.py owns it so the
    # generated JSON and the hand-paste YAML cannot disagree about it; the
    # unique_id is `id` and lives only in the YAML.
    "alias": "Roborock Garage Door State Machine",
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
