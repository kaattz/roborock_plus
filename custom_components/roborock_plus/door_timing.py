"""How long the garage door's position reading stays untrustworthy.

The device (zhenji.wopener.zj1q) answers any motor command by writing the
*target* position into its `current-position` property immediately, while the
door is still at its old position. Home Assistant exposes that property as the
`current_position` attribute, reads it for the cover entity, and derives
`is_closed` from it -- so both the position and the state are wrong for the
whole travel.

Measured 2026-10-10: `open_cover` reported 100 within 0.5s, `close_cover`
reported 0 within 0.8s, and a full travel takes 35 seconds (owner, stopwatch,
single sample). The device reports the real position only once the motor stops.

Consumers must therefore wait out `ECHO_SETTLE_SECONDS` after issuing a command
that is allowed to run to completion before believing any position they read.
The wait alone does not make a reading true: a door physically stalled while the
motor still turns keeps reporting the echo, so it would read as "arrived". Only
a real limit switch can rule that out.

Intentionally import-free. The package __init__ pulls in the `roborock` library,
which need not be installed, and tests load this file by path rather than
importing the package; a relative import here would raise "attempted relative
import with no known parent package". Consumers keep their own literal, kept in
step by tests that load this file by path.
"""

# One full travel, measured with a stopwatch by the owner on 2026-10-10.
# A single sample, so treat it as a floor: a cold door or added resistance
# makes the travel longer, never shorter.
DOOR_TRAVEL_SECONDS = 35

# One travel plus a 10-second margin, which absorbs the door being slower than
# measured. Bounded from above by tests/test_door_timing.py -- this window is
# waited on every command, so an arbitrarily large value costs real latency.
ECHO_SETTLE_SECONDS = 45
