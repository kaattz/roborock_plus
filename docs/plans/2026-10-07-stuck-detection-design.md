# Stuck Detection Design

Goal: notice when the vacuum should be moving but is not, and report it.

This covers the second of three failure layers around the cabinet door:

1. The clean command never starts a task -- done in 0.5.0
   (`roborock_plus_clean_command_not_started`).
2. The task starts but the robot gets stuck -- this document.
3. The device reports its own error (`robot_trapped`, `return_to_dock_fail`) --
   left to an automation watching the error sensor, so the integration does not
   duplicate what the device already says.

The motivating failures are the robot pressing against a closed door, and the
robot unable to reach the dock because an open door leaf is in the way. In both
cases the task stays active, so nothing in Home Assistant notices.

## Why this belongs in the integration

Home Assistant has no position entity, only booleans (`in_safe_zone`,
`clear_of_garage`). The coordinates live behind the
`get_vacuum_current_position` service and in the integration's own sampler, so an
automation cannot observe "the position has not changed" at the required rate.
The integration already samples the map position on a timer, so the fact is
cheap to compute there.

The integration only reports. It does not call `vacuum.stop`: stopping is a
decision, and keeping it in an automation lets the user add their own
conditions and see it in the trace. It also matches the layer-1 design, where
the integration fires an event and the automation notifies.

## Detecting it

Keep an anchor (a position plus the time it was taken) and update it on every
sample:

* State is not one where movement is expected -> clear the anchor, clear stuck.
* No usable position -> restart the window, assess nothing.
* No anchor yet -> set the anchor, assess nothing.
* Moved further than the radius from the anchor -> moving; re-anchor, clear stuck.
* Still within the radius, for at least the window -> stuck.

### "Should be moving" is a whitelist

Only states where the robot is expected to be travelling are assessed:

```
cleaning, segment_cleaning, zoned_cleaning, spot_cleaning,
returning_home, docking, going_to_target, going_to_wash_the_mop,
manual_mode, remote_control_active, mapping, patrol,
robot_status_mopping, clean_mop_cleaning, clean_mop_mopping,
segment_mopping, segment_clean_mop_cleaning, segment_clean_mop_mopping,
zoned_mopping, zoned_clean_mop_cleaning, zoned_clean_mop_mopping,
back_to_dock_washing_duster
```

Everything else is not assessed, including `washing_the_mop`,
`emptying_the_bin`, `charging`, `paused`, `idle` and `error`.

`binary_sensor.<vacuum>_task_active` deliberately answers a different question:
it stays on while the robot is paused or washing the mop, because the task is
still alive. Using it here would report every mop wash and every mid-clean
recharge as stuck.

A whitelist fails in the safe direction. A blacklist of "legitimate idle"
states has to be complete: miss one and it raises a false alarm, which stops a
working clean. A whitelist only has to name the states where movement is
certain, and a state it does not name simply goes unassessed.

### Why a radius, not equality

Two samples taken while the robot sat still measured `(25728, 24233)` and
`(25727, 24232)`: the value jitters by a unit or two. Comparing for equality
would read that as movement. The default radius of 200 (millimetre-scale units,
as used by the map) is far above the jitter and far below the roughly 900 units
the robot covers in three seconds at cleaning speed.

### The radius rule cannot see "moving but going nowhere" (2026-10-14)

The anchor comparison is `hypot(x - anchor.x, y - anchor.y)`: distance **from the
anchor**, not path length. So any travel beyond the radius re-anchors and clears
the verdict -- which means a robot rubbing back and forth against an obstruction
is never reported, however long it does it.

Measured against the real tracker, positions 5s apart:

| oscillation amplitude | verdict |
| --- | --- |
| 30 | stuck after 120s |
| 190 | stuck after 120s |
| **300** | **never stuck** |

300 units is under a second of travel at cleaning speed, so a door that did not
open is enough to hide the robot from this rule forever. Raising the short
window's radius is not the answer -- it is the same knob that stops slow edge
work from being called stuck.

So a second, independent judgement runs alongside it: the **backstop**. It holds
its own anchor, re-set only when the robot genuinely leaves the region, and asks
whether the robot has spent `BACKSTOP_WINDOW` (900s) never getting further than
`BACKSTOP_RADIUS` (3000) from where that stretch began.

| | short window | backstop |
| --- | --- | --- |
| answers | has it stopped moving? | is it moving but getting nowhere? |
| window | 120s (configurable) | 900s (fixed) |
| radius | 200 (configurable) | 3000 (fixed) |
| re-anchors on | any travel beyond 200 | travel beyond 3000 |

The gap between the two radii is what makes them complementary rather than
redundant, so `BACKSTOP_RADIUS` must stay above `MAX_V1_STUCK_RADIUS`: a user who
raises the short radius to its 2000 maximum would otherwise silently disable the
backstop, and at 3000 against 2000 it still measures something new.

A genuine clean cannot trip it: 900s at ~300 units/s covers ~270000 units, so a
robot that never exceeds 3000 has effectively not travelled at all.

The verdict carries which rule fired, as `reason` on the event
(`stopped` / `trapped`) and on the entity's attributes. The two need different
wording in a notification, and conflating them is what let this gap go unnoticed.

## Reported interface

Entity: `binary_sensor.<vacuum>_stuck` (`on` / `off`, `unknown` when no
position has been readable or detection is disabled).

Event, fired once per stuck episode:

```yaml
event_type: roborock_plus_vacuum_stuck
data:
  entity_id: vacuum.g20s_ultra
  x: 25728
  y: 24233
  state: returning_home
  seconds_stuck: 132
```

## Options

| Option | Default | Range | Meaning |
| --- | --- | --- | --- |
| Stuck detection | on | -- | Master switch |
| Stuck window | 120 s | 30-600 | How long without movement counts as stuck |
| Stuck radius | 200 | 50-2000 | Position tolerance |

Detection quality depends on sampling density: the window needs several usable
samples inside it. `v1_map_position_poll_interval` therefore drops its
self-hosted default from 10 s to 5 s, giving 24 samples per default window. The
official-cloud floor of 30 s still applies and is a documented limitation of
that mode rather than something this feature can fix.

## Non-goals

* Do not close the door. The robot may be stuck in the doorway, so closing it
  could trap the robot or injure someone. The existing automations already fail
  safe by leaving the door open.
* Do not call `vacuum.stop` from the integration.
* Do not duplicate the device's own error reporting (layer 3).
* Do not schedule cleaning. That is a later step, and it depends on this one:
  a scheduled clean runs with nobody watching.

## Files

* `custom_components/roborock_plus/v1_stuck_detection.py`: whitelist, options
  resolution, anchor tracker and event payload.
* `custom_components/roborock_plus/coordinator.py`: feed samples, expose state,
  fire the event.
* `custom_components/roborock_plus/binary_sensor.py`: the `stuck` entity.
* `custom_components/roborock_plus/v1_map_position.py`: self-hosted default 5 s.
* `config_flow.py` and translations: the three options.
* tests: tracker logic, options clamping, and the wiring.
