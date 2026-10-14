"""Build the native single-button cycle for the garage door's BLE remote.

## Why this replaces the blueprint

The `无线窗帘电动窗单键循环控制` blueprint exists to *synthesize* a position that
the hardware did not report: Zigbee curtain motors and Mijia window openers expose
only open/close/stop, so the blueprint infers "still moving" from `total_duration`
and a time helper. That is a good workaround for those devices.

The garage door does not need it. `cover.vacuum_garage_door` is a
`zhenji.wopener.zj1q` via the native `xiaomi_home` integration and reports a real
`current_position`, including intermediate values observed on 2026-10-08
(0 / 11 / 77 / 100). The blueprint's timer is therefore unnecessary, and its
`total_duration` of 40s is wrong for a door that finishes in ~34s -- that mismatch
is what reopened the door 9.5s after the state machine closed it, twice on
2026-10-08. The blueprint subscribes to every `call_service` in the cover domain,
so it could not tell the state machine's command from a button press.

## The defect this version fixes

The first native version decided "is the door moving?" from the cover's state:

    opening / closing -> stop

That does not work on this device. It has **no status property**, so `opening` and
`closing` are optimistic flags the integration sets when it issues a command --
and `_position_changed_handler` clears them whenever `current_position` changes.
The device echoes the commanded position within ~0.5s, which clears the flag
almost immediately. Measured over 7 days: `opening`/`closing` last a mean of
**0.79s** (max 1.71s, n=20), while a full travel is ~34s.

So the stop branch was unreachable, and a press mid-travel fell through to the
reverse branch. Measured consequence:

    press while closing -> the entity reads `closed`, so it calls open_cover
    press while opening -> the entity reads `open`,   so it calls close_cover

The second row is the dangerous one: if the robot is in the doorway, closing is
the direction that can hit it. This automation was `off` throughout, which is the
only reason it never happened.

## What this version does instead

"Moving" is now judged by **time since the last command**, which is the one signal
the device cannot corrupt. A `call_service` trigger on the cover domain records
when travel should end:

    open_cover / close_cover -> moving_end = now + travel window
    stop_cover               -> moving_end = now (travel is over)

A press then reads:

    moving_end unknown        -> refuse and notify (cannot tell -> never guess)
    contact unknown           -> refuse and notify
    now < moving_end          -> stop
    contact off (at closed)   -> open
    contact on  (not closed)  -> close

The open/close judgement moved to the contact sensor fitted at the closed
position on 2026-10-14. It is a physical switch, so unlike `current_position` it
cannot be echoed, and it reports the door's real state ~5s before the cover does
(measured: contact -> off at 14:21:15.373, cover -> closed at 14:21:20.736).

## Why `mode: queued`, not `single`

The automation's own `cover.open_cover` fires the very `call_service` trigger that
records the window. Under `single`, that re-entry is dropped as a duplicate and the
window is never recorded -- so the next press during travel would reverse the door,
which is the defect this change exists to remove. `queued` lets the recording run
complete after the press run finishes.

## The asymmetry that sets the window length

Over-estimating the window is safe: a press just after the door stopped is read as
"still moving" and issues `stop_cover`, which on a stationary door is the clutch
release this house already uses deliberately (`vacuum_garage_door_auto_unlock`).
Under-estimating is dangerous: a press during the last moments of travel would
reverse the door. So the window is `ECHO_SETTLE_SECONDS` -- one travel (34s
measured, 35s earlier) plus a 10s margin -- and deliberately the same constant that
governs how long a position reading is untrustworthy, because both describe the
same interval: how long the motor may still be running.

## What is still deliberately absent

No last-action helper. The blueprint used one to work out which way to reverse;
the contact answers that directly now, so the helper would only be a second, staler
source of truth. `input_select.garage_door_last_action` stays unused.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

VACUUM_DOOR = "cover.vacuum_garage_door"
BUTTON = "event.vacuum_garage_door_click"
CONTACT = "binary_sensor.vacuum_garage_door_contact_state"
MOVING_END = "input_datetime.garage_door_moving_end_time"

# The event entity currently only ever emits this, but naming it keeps the
# automation correct if a double-click is added to the remote later.
SINGLE_CLICK = "单击"

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def _echo_settle_seconds() -> int:
    """Load the shared travel/echo constant by path.

    `custom_components.roborock_plus` cannot be imported here: its `__init__.py`
    pulls in the `roborock` library, which need not be installed. Loading the
    module file directly avoids that, and `door_timing.py` is intentionally
    import-free for exactly this reason.
    """
    path = (
        REPO / "custom_components" / "roborock_plus" / "door_timing.py"
    )
    spec = importlib.util.spec_from_file_location("door_timing_for_button", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ECHO_SETTLE_SECONDS


TRAVEL_WINDOW = _echo_settle_seconds()

# The event carries `service_data` with `target` already merged in (`core.py`:
# `if target: service_data.update(target)`), so this reaches the entity_id the
# caller passed. Both shapes occur: `target: {entity_id: x}` gives a string, a
# list target gives a list.
TARGETS_OUR_DOOR = (
    "{% set target = trigger.event.data.service_data.entity_id | default([], true) %}"
    "{% if target is string %}"
    "{{ target == '" + VACUUM_DOOR + "' }}"
    "{% else %}"
    "{{ '" + VACUUM_DOOR + "' in target }}"
    "{% endif %}"
)

# Only the three commands that change travel state are worth recording. A
# `set_cover_position` would also count, but nothing in this house calls it.
MOVES_OR_STOPS = (
    "{{ trigger.event.data.service in ['open_cover', 'close_cover', 'stop_cover'] }}"
)

RECORD_WINDOW = (
    "{% if trigger.event.data.service == 'stop_cover' %}"
    "{{ now().timestamp() }}"
    "{% else %}"
    f"{{{{ (now() + timedelta(seconds={TRAVEL_WINDOW})).timestamp() }}}}"
    "{% endif %}"
)

# Both readings must be usable before the door may move. Checked as one guard
# rather than relying on each branch to fail: `unavailable` satisfies neither
# `state: on` nor `state: off`, so without this the press would fall through to
# the default and be refused anyway -- but the owner would be told "unknown
# position" when the truth is "the clock helper is broken", and that difference
# matters when diagnosing it.
READINGS_UNKNOWN = (
    "{{ travel_end in ['unknown', 'unavailable', 'none', ''] "
    "or contact not in ['on', 'off'] }}"
)

# Compare epoch seconds, not datetimes. `as_datetime()` returns a *naive*
# datetime for a string without an offset while `now()` is timezone-aware, so
# `now() < as_datetime(...)` raises "can't compare offset-naive and offset-aware
# datetimes". Measured in Home Assistant's own template engine. An exception in a
# condition aborts the run, so the stop branch would never be reached and the
# original defect would be back with the fix seemingly in place.
#
# `input_datetime` exposes a numeric `timestamp` attribute precisely for this, so
# nothing needs parsing -- and it stays valid for a date+time helper.
IS_MOVING = (
    "{{ now().timestamp()"
    " < (state_attr('" + MOVING_END + "', 'timestamp') | float(0)) }}"
)

DESCRIPTION = "\n".join(
    [
        "车库门 BLE 物理按键的单键循环。",
        "",
        "怎么判断「门在动」：按「距上次命令的时间」判断，而不是看 cover 的",
        "opening/closing 状态。这台设备没有 status 属性，那两个状态是集成自己下的",
        "乐观标志，而设备在 ~0.5 秒内把命令的目标值回声进 current_position，",
        "触发 _position_changed_handler 把标志清掉。实测 7 天：opening/closing 平均",
        "只持续 0.79 秒（最大 1.71，n=20），而真实行程 ~34 秒。",
        "",
        "所以旧版「门在动 → 停止」这一支永远不可达，行进中按键会落到反向分支：",
        "  关门途中按 → 实体报 closed → 执行 open_cover（反向）",
        "  开门途中按 → 实体报 open   → 执行 close_cover（反向）",
        "第二行是危险的：扫地机在门口时，关门是唯一可能撞到它的方向。",
        "本自动化一直是 off，所以从未发生。",
        "",
        "现在的判据：",
        "  open_cover / close_cover 命令 → 记录 moving_end = 现在 + 行程窗口",
        "  stop_cover 命令               → 记录 moving_end = 现在（行程结束）",
        "",
        "按键时：",
        "  moving_end 读不到   → 什么都不做 + 通知（判断不了就不猜）",
        "  门磁读不到          → 什么都不做 + 通知",
        "  还在窗口内          → 停止",
        "  门磁 off（在关闭位）→ 打开",
        "  门磁 on （未关闭）  → 关闭",
        "",
        "开/关判据用 2026-10-14 装在全关位的门磁。它是物理触点，不像",
        "current_position 会被回声污染，而且比 cover 早约 5 秒报出真实状态",
        "（实测 14:21:15.373 门磁 off，14:21:20.736 cover 才 closed）。",
        "",
        "窗口取 ECHO_SETTLE_SECONDS（一次行程 + 10 秒余量）。偏大是安全的：门停下后",
        "立刻按会被当成「还在动」而发 stop_cover，那在静止的门上就是松开离合",
        "（本屋的 vacuum_garage_door_auto_unlock 正是刻意这么做的）。偏小是危险的：",
        "行程末尾按键会反向。",
        "",
        "mode 必须是 queued 而不是 single：本自动化自己发的 cover 命令会触发记录窗口",
        "的那条 call_service 触发器，single 会把这次重入当重复丢掉，窗口就记不上 ——",
        "于是行程中按键又变回反向。",
        "",
        "没有用 input_select.garage_door_last_action。蓝图用它来判断往哪个方向反向，",
        "现在门磁直接回答了这个问题，它只会变成第二个更滞后的真相来源。",
    ]
)

CONFIG = {
    "alias": "车库门单键循环（BLE 按键）",
    "description": DESCRIPTION,
    # `queued`, not `single` -- see the module docstring. The recording run is
    # triggered by this automation's own command and must not be dropped.
    "mode": "queued",
    "max": 10,
    "max_exceeded": "silent",
    # Kept off on every restart. Enabling it lets a button press move a real door,
    # which is the owner's decision, not something a config reload should make.
    "initial_state": False,
    "triggers": [
        {
            "trigger": "state",
            "entity_id": BUTTON,
            "id": "press",
        },
        {
            # Every cover command in the house arrives here; the branch below
            # filters to this door. Listening to the event rather than to the
            # entity's state is deliberate: the state is the same echo-polluted
            # value that made `opening`/`closing` useless.
            "trigger": "event",
            "event_type": "call_service",
            "event_data": {"domain": "cover"},
            "id": "command",
        },
    ],
    "conditions": [],
    "actions": [
        {
            "choose": [
                {
                    # ---- record when travel should end -------------------------
                    "conditions": [
                        {"condition": "trigger", "id": ["command"]},
                        {
                            "condition": "template",
                            "value_template": TARGETS_OUR_DOOR,
                        },
                        {
                            "condition": "template",
                            "value_template": MOVES_OR_STOPS,
                        },
                    ],
                    "sequence": [
                        {
                            "action": "input_datetime.set_datetime",
                            "target": {"entity_id": MOVING_END},
                            "data": {"timestamp": RECORD_WINDOW},
                        }
                    ],
                },
                {
                    # ---- a single click on the remote --------------------------
                    "conditions": [
                        {"condition": "trigger", "id": ["press"]},
                        {
                            "condition": "state",
                            "entity_id": BUTTON,
                            "attribute": "event_type",
                            "state": SINGLE_CLICK,
                        },
                    ],
                    "sequence": [
                        {
                            "variables": {
                                "travel_end": "{{ states('" + MOVING_END + "') }}",
                                "contact": "{{ states('" + CONTACT + "') }}",
                            }
                        },
                        {
                            "choose": [
                                {
                                    # Refuse before anything else, so neither an
                                    # unknown clock nor an unknown contact can
                                    # reach a door command.
                                    "conditions": [
                                        {
                                            "condition": "template",
                                            "value_template": READINGS_UNKNOWN,
                                        }
                                    ],
                                    "sequence": [
                                        {
                                            "action": "script.alert_notify",
                                            "data": {
                                                "level": "warning",
                                                "title": "车库门按键：读数不可用，未动作",
                                                "message": (
                                                    "按了车库门按键，但判断不出门的"
                                                    "状态，为避免误关门夹到扫地机，"
                                                    "没有执行任何动作。"
                                                    "moving_end={{ travel_end }}，"
                                                    "门磁={{ contact }}，"
                                                    "cover="
                                                    "{{ states('" + VACUUM_DOOR + "') }}"
                                                ),
                                            },
                                        }
                                    ],
                                },
                                {
                                    # Stop first: a press while the door is
                                    # travelling means "stop here". This is the
                                    # branch that used to be unreachable.
                                    "conditions": [
                                        {
                                            "condition": "template",
                                            "value_template": IS_MOVING,
                                        }
                                    ],
                                    "sequence": [
                                        {
                                            "action": "cover.stop_cover",
                                            "target": {"entity_id": VACUUM_DOOR},
                                        }
                                    ],
                                },
                                {
                                    "conditions": [
                                        {
                                            "condition": "state",
                                            "entity_id": CONTACT,
                                            "state": "off",
                                        }
                                    ],
                                    "sequence": [
                                        {
                                            "action": "cover.open_cover",
                                            "target": {"entity_id": VACUUM_DOOR},
                                        }
                                    ],
                                },
                                {
                                    "conditions": [
                                        {
                                            "condition": "state",
                                            "entity_id": CONTACT,
                                            "state": "on",
                                        }
                                    ],
                                    "sequence": [
                                        {
                                            "action": "cover.close_cover",
                                            "target": {"entity_id": VACUUM_DOOR},
                                        }
                                    ],
                                },
                            ],
                            # The guard above already returns for every unknown
                            # reading, so reaching here means a state appeared
                            # that neither branch claims. Refuse rather than
                            # fall through to a door command.
                            "default": [
                                {
                                    "action": "script.alert_notify",
                                    "data": {
                                        "level": "warning",
                                        "title": "车库门按键：状态意外，未动作",
                                        "message": (
                                            "按了车库门按键，但没有任何分支匹配，"
                                            "没有执行动作。"
                                            "moving_end={{ travel_end }}，"
                                            "门磁={{ contact }}"
                                        ),
                                    },
                                }
                            ],
                        },
                    ],
                },
            ]
        }
    ],
}


def main() -> int:
    """Write the config next to this script and report its shape."""
    out = HERE / "button_cycle.json"
    out.write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")

    blob = json.dumps(CONFIG, ensure_ascii=False)
    print(f"wrote {out.name} ({out.stat().st_size} bytes)")
    print(f"alias : {CONFIG['alias']}")
    print(f"mode  : {CONFIG['mode']} max={CONFIG['max']}")
    print(f"travel window: {TRAVEL_WINDOW}s (from door_timing.ECHO_SETTLE_SECONDS)")
    print()
    print("--- native-construct check ---")
    native = blob.count('"condition": "state"') + blob.count(
        '"condition": "numeric_state"'
    )
    print(f"  native conditions  : {native}")
    print(f"  template conditions: {blob.count('value_template')}")
    print()
    print("--- helper entities (logic only, not the description) ---")
    # Scan the parts that run. The description deliberately *names* the retired
    # helper to explain why it is not used, so scanning the whole document reports
    # a false "USED" for a helper the automation never touches.
    logic = json.dumps(
        {
            "triggers": CONFIG["triggers"],
            "conditions": CONFIG["conditions"],
            "actions": CONFIG["actions"],
        },
        ensure_ascii=False,
    )
    print(f"  {MOVING_END:48} {'USED' if MOVING_END in logic else 'not used'}")
    for unused in ("input_select.garage_door_last_action",):
        print(f"  {unused:48} {'USED' if unused in logic else 'not used'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
