"""How long the garage door's position reading stays untrustworthy.

The device (zhenji.wopener.zj1q) answers any motor command by writing the
*target* position into `current-position` immediately, while the door is still
at its old position. Home Assistant's cover entity reads that same property,
and derives `is_closed` from it, so both the position and the state are wrong
for the whole travel.

Measured 2026-10-10: `open_cover` reported 100 within 0.5s, `close_cover`
reported 0 within 0.8s, and a full travel takes 35 seconds (user, stopwatch).
The device reports the real position only once the motor stops.

Consumers must therefore wait out `ECHO_SETTLE_SECONDS` after issuing a command
before believing any position they read.
"""

# One full travel, measured with a stopwatch by the owner on 2026-10-10.
DOOR_TRAVEL_SECONDS = 35

# A travel plus margin. The margin also absorbs a door that is slower than
# measured (cold, added resistance) -- it can be slower, never faster.
ECHO_SETTLE_SECONDS = 45
