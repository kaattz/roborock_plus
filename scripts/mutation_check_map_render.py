"""Mutation check for the map-image correction.

The image is the artefact a person actually reads to decide whether the door is
clear, so a silent regression here matters as much as one in the entities. Each
mutation breaks the correction a different way and the suite must notice.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "roborock_plus"

# Removing the block-length validation makes the walk loop forever instead of
# failing, so the suite has to be given a deadline.
SUITE_TIMEOUT_SECONDS = 90

MUTATIONS = [
    (
        "stop correcting the image at all",
        COMPONENT / "image.py",
        "        corrected = await self.hass.async_add_executor_job(\n",
        "        corrected = None\n        _unused = await self.hass.async_add_executor_job(\n",
    ),
    (
        "pass the drawn position as the resolved one (no redraw)",
        COMPONENT / "image.py",
        "                resolved_x=resolved.x,\n                resolved_y=resolved.y,\n",
        "                resolved_x=map_data.vacuum_position.x,\n"
        "                resolved_y=map_data.vacuum_position.y,\n",
    ),
    (
        "hardcode from_dock so a docked robot is never redrawn",
        COMPONENT / "image.py",
        "                from_dock=resolved.from_dock,\n",
        "                from_dock=False,\n",
    ),
    (
        "return the library image even after a successful correction",
        COMPONENT / "image.py",
        "        self._corrected_cache = (cache_key, corrected)\n        return corrected\n",
        "        self._corrected_cache = (cache_key, corrected)\n        return image\n",
    ),
    (
        "drop the cache so every fetch re-renders",
        COMPONENT / "image.py",
        "        if (\n"
        "            self._corrected_cache is not None\n"
        "            and self._corrected_cache[0] == cache_key\n"
        "        ):\n"
        "            return self._corrected_cache[1]\n",
        "        if False:\n            return b''\n",
    ),
    (
        "run the render on the event loop",
        COMPONENT / "image.py",
        "        corrected = await self.hass.async_add_executor_job(\n",
        "        corrected = rerender_map_image(\n",
    ),
    (
        "drop the self-check on the patched bytes",
        COMPONENT / "v1_map_render.py",
        "    written = read_robot_position(patched)\n"
        "    if written is None or (int(written[0]), int(written[1])) != target:\n"
        "        return None\n",
        "    pass\n",
    ),
    (
        "redraw a robot that is out cleaning",
        COMPONENT / "v1_map_render.py",
        "    if not from_dock:\n"
        "        # The payload is trusted as-is for a robot that is not on its dock.\n"
        "        return None\n",
        "    if False:\n        return None\n",
    ),
    (
        "redraw even when the position is untrusted",
        COMPONENT / "v1_map_render.py",
        "    if not trusted or resolved_x is None or resolved_y is None:\n",
        "    if resolved_x is None or resolved_y is None:\n",
    ),
    (
        "ignore the tolerance and redraw on every call",
        COMPONENT / "v1_map_render.py",
        "    if offset <= SAME_POSITION_TOLERANCE:\n"
        "        return None\n",
        "    if offset <= 0:\n        return None\n",
    ),
    (
        "walk blocks without validating their lengths",
        COMPONENT / "v1_map_render.py",
        "        if (\n"
        "            block_header_length <= 0\n"
        "            or block_header_length > _MAX_BLOCK_HEADER_LENGTH\n"
        "            or block_data_length <= 0\n"
        "        ):\n"
        "            return None\n",
        "        if False:\n            return None\n",
    ),
]


def run_suite() -> int | None:
    """Run the suite; return the exit code, or None when it timed out.

    A timeout is a caught mutation, not a hang: removing the block-length
    validation makes the parser walk in a circle, which is precisely the defect
    the check exists to detect.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-q", "--no-header", "-x"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=SUITE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None
    return proc.returncode


escaped = []
for label, path, original, mutated in MUTATIONS:
    text = path.read_text(encoding="utf-8")
    if original not in text:
        print(f"{'SETUP?':8} {label}")
        escaped.append(f"anchor missing: {label}")
        continue
    path.write_text(text.replace(original, mutated, 1), encoding="utf-8")
    try:
        code = run_suite()
    finally:
        # Always restore, even if the suite is killed: a mutation left in place
        # would silently ship.
        path.write_text(text, encoding="utf-8")
    caught = code != 0
    note = " (timed out: infinite walk)" if code is None else ""
    print(f"{'CAUGHT' if caught else 'ESCAPED':8} {label}{note}")
    if not caught:
        escaped.append(label)

print()
if escaped:
    print("FAILURES:")
    for item in escaped:
        print(" -", item)
    sys.exit(1)
print(f"all {len(MUTATIONS)} mutations caught")
