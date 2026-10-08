"""Build the native single-button cycle for the garage door's BLE remote.

## Why this replaces the blueprint

The `无线窗帘电动窗单键循环控制` blueprint exists to *synthesize* a position
that the hardware did not report: Zigbee curtain motors and Mijia window
openers expose only open/close/stop, so the blueprint infers "still moving" from
`total_duration` and a time helper. That is a good workaround for those devices.

The garage door does not need it. `cover.vacuum_garage_door` is a
`zhenji.wopener.zj1q` via the native `xiaomi_home` integration and reports a real
`current_position`, including intermediate values observed on 2026-10-08:

    0    closed
    11   stopped partway
    77   a resting partially-open position
    100  open

So the blueprint's timer is both unnecessary and wrong for this cover. Its
`total_duration` is 40s while the door reports reaching an end in under a second,
and the mismatch is what reopened the door 9.5s after the state machine closed
it (20:53:44.950 close -> 20:53:55.377 open, and again at 21:09:14). The
blueprint subscribes to every `call_service` in the cover domain, so it could
not tell the state machine's command from a button press.

## What this automation does instead

One button, position-based:

* moving        -> stop
* at/below 1    -> open
* at/above 2    -> close
* position gone -> do nothing and say so

The last branch matters. If the position is unknown the automation must not
guess, because "close" is the one direction that can hit the robot. Doing
nothing leaves the door as it is, which the owner can see and correct.

## What was deliberately left out

No time helper and no last-action helper. Both existed only to compensate for a
missing position, and both would now be a second, staler source of truth for
something the hardware reports directly.
"""

from __future__ import annotations

import json
from pathlib import Path

VACUUM_DOOR = "cover.vacuum_garage_door"
BUTTON = "event.vacuum_garage_door_click"

# The event entity currently only ever emits this, but naming it keeps the
# automation correct if a double-click is added to the remote later.
SINGLE_CLICK = "单击"

# Position at or below which the door counts as closed. The hardware reports
# integers, and 0 is what a closed door shows; 1 is tolerated so a single-unit
# rounding difference does not read as "not closed".
CLOSED_AT_OR_BELOW = 1

DESCRIPTION = "\n".join(
    [
        "车库门 BLE 物理按键的单键循环，用门自己上报的 current_position 判断，",
        "取代原先的「无线窗帘电动窗单键循环控制」蓝图自动化。",
        "",
        "为什么换掉蓝图：蓝图是为了给「不报 position 的设备」补一个位置而做的 ——",
        "Zigbee 窗帘电机和米家开窗器只暴露 开/关/停，所以蓝图用 total_duration",
        "加一个 input_datetime 去推算「还在动」，这是对那类设备合理的变通。",
        "",
        "但这扇门不需要：cover.vacuum_garage_door 是 zhenji.wopener.zj1q，",
        "走原生 xiaomi_home，直接上报真实 position（实测见过 0 / 11 / 77 / 100，",
        "包含停在半路的中间值）。所以蓝图那套计时器对本门既多余又算错：",
        "它配的 total_duration 是 40 秒，而门不到 1 秒就报到位。",
        "",
        "这个错配就是 2026-10-08 两次「关门 9.5 秒后又被打开」的原因：",
        "20:53:44.950 状态机关门 → 20:53:55.377 门被打开；21:09 同样。",
        "蓝图监听 cover 域的每一次 call_service，分不清状态机的命令和按键。",
        "",
        "按键行为：",
        "  门在动（opening/closing） → 停止",
        "  位置 ≤ 1（已关）          → 打开",
        "  位置 ≥ 2（未关到底）      → 关闭",
        "  位置读不到                → 什么都不做，并发通知",
        "",
        "最后一条是刻意的：位置未知时不能猜。猜「关闭」是唯一可能夹到扫地机的方向。",
        "什么都不做至少保持原状，用户看得见也能自己处理。",
        "",
        "没有用 input_datetime / input_select 辅助实体 —— 它们只是为了补 position",
        "而存在，现在只会变成第二个更滞后的真相来源。",
    ]
)

CONFIG = {
    "alias": "车库门单键循环（BLE 按键）",
    "description": DESCRIPTION,
    # A button press is a discrete user action. `single` keeps a double press
    # from issuing two conflicting commands; the sequence is a single service
    # call, so it completes immediately and cannot block a later press.
    "mode": "single",
    "max_exceeded": "silent",
    "triggers": [
        {
            "trigger": "state",
            "entity_id": BUTTON,
            "id": "press",
        }
    ],
    "conditions": [
        # Only single clicks act. The entity currently emits nothing else, but
        # this keeps a future double-click from silently doing the same thing.
        {
            "condition": "state",
            "entity_id": BUTTON,
            "attribute": "event_type",
            "state": SINGLE_CLICK,
        }
    ],
    "actions": [
        {
            "choose": [
                {
                    # Stop first: a press while the door is moving means "stop
                    # here", which is the same intent the blueprint encoded as
                    # its first branch.
                    "conditions": [
                        {
                            "condition": "state",
                            "entity_id": VACUUM_DOOR,
                            "state": ["opening", "closing"],
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
                            "condition": "numeric_state",
                            "entity_id": VACUUM_DOOR,
                            "attribute": "current_position",
                            "below": CLOSED_AT_OR_BELOW + 1,
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
                    # Anything else that reports a position is somewhere between
                    # the ends, so the useful action is to shut it.
                    "conditions": [
                        {
                            "condition": "numeric_state",
                            "entity_id": VACUUM_DOOR,
                            "attribute": "current_position",
                            "above": CLOSED_AT_OR_BELOW,
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
            # No branch matched: the position is missing or the cover is
            # unavailable. Refuse to guess, and tell the owner why nothing
            # happened -- closing on an unknown position is the one mistake that
            # cannot be undone.
            "default": [
                {
                    "action": "script.alert_notify",
                    "data": {
                        "level": "warning",
                        "title": "车库门按键：位置未知，未动作",
                        "message": (
                            "按了车库门按键，但读不到门的位置，为避免误关门夹到扫地机，"
                            "没有执行任何动作。"
                            "cover={{ states('" + VACUUM_DOOR + "') }}，"
                            "position={{ state_attr('" + VACUUM_DOOR + "', "
                            "'current_position') }}"
                        ),
                    },
                }
            ],
        }
    ],
}


def main() -> int:
    """Write the config next to this script and report its shape."""
    out = Path(__file__).resolve().parent / "button_cycle.json"
    out.write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")

    blob = json.dumps(CONFIG, ensure_ascii=False)
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    print(f"alias : {CONFIG['alias']}")
    print(f"mode  : {CONFIG['mode']} max_exceeded={CONFIG['max_exceeded']}")
    print(f"trigger: {CONFIG['triggers'][0]['entity_id']}")
    print(f"choose branches: {len(CONFIG['actions'][0]['choose'])}")
    print()
    print("--- native-construct check ---")
    native = blob.count('"condition": "state"') + blob.count(
        '"condition": "numeric_state"'
    )
    print(f"  native conditions : {native}")
    print(f"  template conditions: {blob.count('value_template')}")
    print()
    print("--- helpers used (must be none) ---")
    for helper in (
        "input_datetime",
        "input_select",
        "garage_door_moving_end_time",
        "garage_door_last_action",
    ):
        present = helper in blob
        print(f"  {helper:32} {'USED' if present else 'not used'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
