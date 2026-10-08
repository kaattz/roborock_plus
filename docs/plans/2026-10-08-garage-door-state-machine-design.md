# Garage Door State Machine Design

Goal: replace the three `*_plus` automations with one state machine that
actually matches how the cabinet door and the vacuum have to cooperate.

The three automations run today and appear to work, but the checks they rely on
are inverted or absent, so the phases they are supposed to guard have never run
as designed. This document records the evidence, the required flow, and the
shape of the replacement.

> **Correction (2026-10-08, second pass).** An earlier revision of this document
> claimed the dock sits *outside* the zone, and that `clear_of_garage` was
> therefore "already `on` at the dock" and guarded nothing. That was wrong, and
> it was wrong because it used two numbers that are not the dock: the
> `DEFAULT_DOCK_X/Y = 25500, 25500` constant (a suggestion default, never the
> real dock) and `(25727, 24232)`, which is a *stale* reading rather than a rest
> position. The dock is measured at **(25688, 28538)**, which is **inside** the
> zone. `clear_of_garage` is the distance-from-dock-exit signal, and it was
> correct modulo the staleness fixed in `v1_position_trust`. See "Measured
> geometry" below.

## Measured geometry

Coordinates are in vacuum units. The dock is the map's own charger marker,
located by decoding the rendered map and inverting the parser's
`map_data.calibration()` transform (`scripts/derive_charger_position.py`).

| Thing | Value |
| --- | --- |
| Real dock (charger marker) | x 25688, y 28538 |
| Configured zone | x 24997-26069, y 27123-28764 |
| Dock inside the zone? | **yes** |
| `DEFAULT_DOCK_X/Y` constant | 25500, 25500 -- *not* the dock; outside the zone |
| Stale reading seen for 13 h | 25727, 24232 -- the living room, outside the zone |

Offset between the stale reading and the real dock: **4306 units**. Sample-to-
sample jitter of a parked robot is 1-2 units, which is the margin the trust
tolerance uses.

### What the zone is

The zone contains the dock and the path out of it, so it is a **danger zone**:
the door must not close while the robot is inside it. The names say this and are
correct -- `SafeZone` is misnamed internally, `clear_of_garage` and the
"危险区" translations are right. A rename in the other direction was attempted
and reverted.

Consequently:

| Robot | `in_safe_zone` | `clear_of_garage` |
| --- | --- | --- |
| Docked | `on` | `off` |
| Out cleaning | `off` | `on` |

`in_safe_zone` is `on` **at the dock**. Any phase that treats `in_safe_zone`
turning `on` as "the robot has left" is inverted: the robot leaves the zone by
turning it `off`.

## What the hardware does

Measured on 2026-10-07/08:

| Thing | Value |
| --- | --- |
| `cover.vacuum_garage_door` when closed | `current_position` 0, state `closed` |
| ... when open | `current_position` 100, state `open` |

## The required flow

Stated by the owner:

1. A clean task starts. The door must open **immediately**.
2. The door opens slowly, so the vacuum must not be allowed to drive out while
   it is still moving. Wait until `current_position >= 95` before the vacuum
   leaves the dock.
3. Once out, the vacuum must travel far enough from the door before the door
   closes; closing while it is still near the opening means the door and the
   vacuum collide.
4. On the way back -- final return, or a mid-clean trip to wash the mop -- the
   door must be open before the vacuum arrives.
5. When the task is finished and the vacuum is parked, close the door.

## What is broken today

### `vacuum_departing_from_the_base_plus` -- phases 2 and 3, both wrong

Its trigger is `docked|charging|unknown -> cleaning`, i.e. it starts *after* the
vacuum has already begun moving. Phase 1-2 ("open first, hold the vacuum until
the door is clear") therefore does not exist here.

The integration does cover the common case: `garage_guard` opens the door and
waits for it before the Roborock command is sent. That only applies to commands
issued from Home Assistant, and it is the reason the missing phase has not been
obvious.

Its first wait is `binary_sensor.<vacuum>_clear_of_garage == 'on'`, commented as
"wait until the vacuum has left the danger zone". That sensor is `off` at the
dock (the dock is inside the zone), so the wait *is* the correct condition --
but it passed instantly anyway, because the position it reads was stale: the map
kept returning the living room `(25727, 24232)`, which lies outside the zone, so
`clear_of_garage` reported `on` while the robot stood on its dock. The guard was
satisfied by a reading from a previous task, not by the robot leaving. That is
the defect fixed in `v1_position_trust`.

Its close step calls `cover.open_cover` while waiting for
`current_position < 5` and reporting "the door did not fully close". The action
name contradicts the wait and the message; the step cannot close the door.

Observed consequence, 2026-10-07:

```
16:59:22.400  vacuum: docked -> cleaning      departing triggered (.404)
16:59:25.998  vacuum: cleaning -> paused      the automation paused it
16:59:27.782  vacuum: paused -> docked
```

The event trigger is the only thing that pauses the vacuum, and
`vacuum_returning_to_base_plus` did not fire that day, so the pause came from
this automation. It stopped a clean 3.6 seconds in. `vacuum.stop` and an alert
followed once the 60-second wait for a door that was already open expired.
(Automation traces were cleared by the restarts, so the branch taken is
inferred from the entity history rather than read from a trace.)

### `vacuum_returning_to_base_plus` -- phase 4, broadly correct

Pauses, opens the door, waits for `>= 95`, resumes, then waits up to 10 minutes
for the vacuum to arrive and alerts if it does not. The door action and the
wait agree with each other here.

### `vacuum_close_garage_after_final_dock_plus` -- phase 5, correct

Requires the task to be over, the vacuum parked, and the door open, then closes
and verifies. Consistent with the door closing at 17:00:35 after the task ended.

### The gap between them

`mode: single` is per automation, so nothing serialises the three against each
other. `departing` can be inside its wait chain while `returning` starts, and
both will drive `vacuum.pause` and the same cover. Merging them into one
automation with `mode: single` removes the overlap by construction.

## Decisions

1. **Home Assistant only.** Commands started from the Roborock app are out of
   scope; no belt-and-braces watcher. The integration's `garage_guard` already
   opens the door before an HA-issued command, which also means phase A needs no
   pause/resume: the vacuum is never told to move while the door is closed.
2. **One automation, not a blueprint.** A blueprint only pays off with more
   than one vacuum, and these are GitHub-hosted, so every logic change would
   need a re-import.
3. **Keep the danger-zone naming.** The zone contains the dock, so it is where
   closing is *unsafe*: `clear_of_garage` and the "危险区" translations are
   correct, and entity ids stay. (An earlier pass renamed these to
   `in_safe_zone`/"安全区" on the mistaken belief that the dock was outside the
   zone. It was reverted; `SafeZone` remains misnamed internally.)

## Design

One automation, four phases, one serialised run.

| Phase | Trigger | Action |
| --- | --- | --- |
| A. Start | (none -- the integration's `garage_guard`) | opens the door before the command is sent |
| B. Leave | `clear_of_garage -> on` (the robot has left the zone) | pause, close, wait `< 5`, resume |
| C. Return | `-> returning_home` / `docking` / `going_to_wash_the_mop` | open, wait `>= 95` |
| D. Park | task over and parked | close, wait `< 5` |
| E. Timeout | any wait expires | alert, leave the door open |

`mode: queued` (not `single`): `single` *drops* a trigger that arrives while a
run is active, and phase B's close takes up to 90 seconds, during which a phase
D trigger can easily arrive. `queued` runs it afterwards instead of losing it.
Every phase re-checks its own precondition when it runs, so a queued run that
has gone stale aborts rather than acting on old assumptions.

A queued run is not a free pass, though. Observed on 2026-10-08 while this
automation was enabled: four triggers fired within six seconds at 01:26:34-39
(`task_active` twice, `status`, `vacuum`), and their queued runs then drove the
door `open -> closed -> open -> closed -> open` between 01:26:26 and 01:28:24.
The per-phase re-checks bound what each run may do but do not stop the runs from
contradicting each other, so the trigger set must not fire several phases at
once for a single physical event.

### Phase C does not pause

The previous `vacuum_returning_to_base_plus` paused the vacuum before opening
the door. It is dropped here, for two reasons: the trigger *is* the returning
status, and `lessons.md` records that pausing outside `cleaning` cancels the
task instead of holding it. Opening the door is the action that matters, and
the vacuum is still in the room when the return starts.

### Phase B uses `clear_of_garage`, not `in_safe_zone`

`clear_of_garage` is `off` at the dock and `on` once the robot has left the
zone, which is exactly "far enough that closing is safe". `in_safe_zone` is
`on` **at** the dock, so a phase keyed on it turning `on` fires while the robot
is still parked and closes the door in its face.

The close step also moves a pause in front of it, and verifies the pause took
effect before the door moves. `lessons.md` records that a pause outside the
cleaning states cancels the task, so the status is checked first and the pause
is confirmed before anything closes. If either check fails the door is left
open and the run alerts -- there is no safe way to close on a vacuum whose
state is unknown.

### Failure handling

An expired wait means the door state and the vacuum state disagree, and there is
no safe automatic recovery: the vacuum may be in the doorway. So phase E leaves
the door open and alerts. It does not close.

The 10-minute "did the vacuum arrive" wait from the old returning automation is
dropped: the stuck detector added in 0.6.1 covers the same failure, reports it
in about 120 seconds instead of 10 minutes, and names the state the vacuum was
stuck in.

### Non-goals

* Do not take over `garage_door_sync_folding_door`,
  `vacuum_garage_door_auto_unlock`, `che_ku_men_chang_shi_jian_wu_ren_zi_dong_guan_bi`
  or the manual button automation. Those are orthogonal to the vacuum's phases.
* Do not close the door on a timeout, for the reason 0.5.0 already records.
* Do not schedule cleaning. That is the next step and depends on this one.

## Files

* One automation replaces the three `*_plus` automations.
* `custom_components/roborock_plus/v1_position_trust.py` (new): resolves a
  docked robot's position against the map's charger marker, so
  `clear_of_garage` reflects where the robot *is* rather than where a previous
  task left it.
* `coordinator.py`, `binary_sensor.py`, `vacuum.py`: route the safe-zone
  entities and both position services through that resolver, and report
  provenance (`position_source`, `from_dock`) instead of a `stale` flag that
  only said whether the read succeeded.
* `lessons.md`: the stale-position trap, the `DEFAULT_DOCK` trap, and the
  queued-trigger-storm trap.
* No zone rename: the danger-zone naming was already correct.

