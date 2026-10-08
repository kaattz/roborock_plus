"""Re-derive the charger's vacuum coordinates from the parser's own calibration.

The tolerance only ever compares two vacuum-space values, so it does not depend
on this transform. This check exists to confirm the zone/charger claim, which
does depend on it: calibration() yields authoritative vacuum<->image pairs from
the parser itself, so the inverse can be validated rather than assumed.
"""

# Calibration pairs taken from map_data.calibration() on the live map.
CAL = [
    (25500, 25500, 652.0, 920.0),
    (35500, 25500, 1452.0, 920.0),
    (25500, 35500, 652.0, 120.0),
]

(vx0, vy0, mx0, my0), (vx1, _, mx1, _), (_, vy2, _, my2) = CAL
scale_x = (mx1 - mx0) / (vx1 - vx0)
scale_y = (my2 - my0) / (vy2 - vy0)
print(f"scale from calibration: x={scale_x} px/unit, y={scale_y} px/unit")
assert abs(scale_x) == abs(scale_y), "isotropic expected"

# The teal charger blob measured in the rendered map image.
blob_cx, blob_cy = 667.0, 677.0
charger_x = vx0 + (blob_cx - mx0) / scale_x
charger_y = vy0 + (blob_cy - my0) / scale_y
print(f"charger from image blob: ({charger_x:.0f}, {charger_y:.0f})")

ZONE = {"min_x": 24997, "max_x": 26069, "min_y": 27123, "max_y": 28764}
inside = (
    ZONE["min_x"] <= charger_x <= ZONE["max_x"]
    and ZONE["min_y"] <= charger_y <= ZONE["max_y"]
)
print(f"configured zone: x {ZONE['min_x']}..{ZONE['max_x']}  y {ZONE['min_y']}..{ZONE['max_y']}")
print(f"charger inside the configured zone? {inside}")

stale_x, stale_y = 25727, 24232
print(f"stale reported position ({stale_x}, {stale_y}) inside?",
      ZONE["min_y"] <= stale_y <= ZONE["max_y"])
print()
print(f"offset between stale position and charger: "
      f"{((stale_x - charger_x) ** 2 + (stale_y - charger_y) ** 2) ** 0.5:.0f} units")
