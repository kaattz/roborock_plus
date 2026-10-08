"""Cross-check the map byte surgery against the real Roborock parser.

`tests/test_v1_map_render.py` builds maps from my own understanding of the block
format and checks my own reader against them. That is circular: if my model of
the format were wrong, the tests would still pass.

This script closes that gap by handing the same bytes to the library's actual
`RoborockMapDataParser` and requiring the two to agree:

* the parser must report the robot position my walk located, and
* after patching, the parser must report the patched position while still
  reading the charger from its own untouched block.

It needs `python-roborock` and `vacuum-map-parser-roborock`, so it is written to
run inside a Home Assistant container. Point it at a host with SSH access:

    python scripts/validate_map_block_against_library.py

Any mismatch means the offsets or the block stride are wrong and the image
correction must not be trusted.
"""

from __future__ import annotations

import base64
import json
import os
import sys

HOST = os.environ.get("ROBOROCK_HA_HOST", "192.168.166.68")
USER = os.environ.get("ROBOROCK_HA_USER", "hass")
PASSWORD = os.environ.get("ROBOROCK_HA_PASSWORD", "gsdjsj")
CONTAINER = os.environ.get("ROBOROCK_HA_CONTAINER", "homeassistant")

# Distinct from both the charger and each other, so a block mix-up cannot pass.
DRAWN = (25727, 24232)
CHARGER = (25688, 28538)
TARGET = (20001, 21002)

PROBE = r'''
import struct, sys

sys.path.insert(0, "/config/probe_mod")
import v1_map_render as R

from vacuum_map_parser_roborock.map_data_parser import RoborockMapDataParser
from vacuum_map_parser_base.config.color import ColorsPalette
from vacuum_map_parser_base.config.image_config import ImageConfig
from vacuum_map_parser_base.config.size import Sizes

MAP_HEADER_LENGTH = 24


def build(robot, charger):
    """Build a map in the documented block format."""
    blocks = bytearray()

    def add(btype, data, hlen=8):
        header = bytearray(hlen)
        header[0:2] = struct.pack("<H", btype)
        header[2:4] = struct.pack("<H", hlen)
        header[4:8] = struct.pack("<I", len(data))
        blocks.extend(header)
        blocks.extend(data)

    add(1, struct.pack("<ii", *charger))
    add(8, struct.pack("<iii", robot[0], robot[1], 90))

    head = bytearray(MAP_HEADER_LENGTH)
    head[0x02:0x04] = struct.pack("<H", MAP_HEADER_LENGTH)
    return bytes(head) + bytes(blocks)


def real_parse(raw):
    """Parse with the library's own parser, without image rendering."""
    parser = RoborockMapDataParser(
        ColorsPalette({}), Sizes({}), [], ImageConfig(), []
    )
    return parser.parse(raw)


DRAWN = (25727, 24232)
CHARGER = (25688, 28538)
TARGET = (20001, 21002)

raw = build(DRAWN, CHARGER)
failures = []

mine = R.read_robot_position(raw)
if mine != (float(DRAWN[0]), float(DRAWN[1])):
    failures.append(f"my walk read {mine}, expected {DRAWN}")

md = real_parse(raw)
theirs = (md.vacuum_position.x, md.vacuum_position.y)
if theirs != DRAWN:
    failures.append(f"the library read {theirs}, expected {DRAWN}")
if (md.charger.x, md.charger.y) != CHARGER:
    failures.append("the library read the charger from the wrong block")
if mine is not None and (int(mine[0]), int(mine[1])) != theirs:
    failures.append(f"my walk {mine} disagrees with the library {theirs}")

print(f"  before patch: mine={mine} library={theirs} charger={(md.charger.x, md.charger.y)}")

patched = R.patch_robot_position(raw, x=TARGET[0], y=TARGET[1])
if patched is None:
    failures.append("patch returned None for a well-formed map")
else:
    md2 = real_parse(patched)
    got = (md2.vacuum_position.x, md2.vacuum_position.y)
    charge_after = (md2.charger.x, md2.charger.y)
    if got != TARGET:
        failures.append(f"after patch the library read {got}, expected {TARGET}")
    if charge_after != CHARGER:
        failures.append(f"the patch corrupted the charger block: {charge_after}")
    print(f"  after patch : library={got} charger={charge_after}")

print()
if failures:
    print("FAILED:")
    for item in failures:
        print(" -", item)
    raise SystemExit(1)
print("PASS: the byte walk and the real parser agree, before and after patching.")
'''


def ssh(command: str, timeout: int = 180) -> str:
    """Run a command on the HA host."""
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    try:
        _in, out, err = client.exec_command(command, timeout=timeout)
        return out.read().decode("utf-8", "replace") + err.read().decode(
            "utf-8", "replace"
        )
    finally:
        client.close()


def main() -> int:
    """Stage the module and probe into the container and run them."""
    component = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "custom_components",
        "roborock_plus",
        "v1_map_render.py",
    )
    if not os.path.exists(component):
        raise SystemExit(f"not found: {component}")

    with open(component, "rb") as handle:
        module_b64 = base64.b64encode(handle.read()).decode()
    probe_b64 = base64.b64encode(PROBE.encode()).decode()

    steps = [
        f"echo {module_b64} | base64 -d | sudo -n tee /root/v1_map_render.py > /dev/null",
        f"echo {probe_b64} | base64 -d | sudo -n tee /root/validate_map.py > /dev/null",
        f"sudo -n docker exec {CONTAINER} mkdir -p /config/probe_mod",
        f"sudo -n docker cp /root/v1_map_render.py "
        f"{CONTAINER}:/config/probe_mod/v1_map_render.py",
        f"sudo -n docker cp /root/validate_map.py {CONTAINER}:/config/validate_map.py",
    ]
    for step in steps:
        result = ssh(step)
        if "error" in result.lower() and "syntax" in result.lower():
            raise SystemExit(f"staging failed: {result}")

    output = ssh(f"sudo -n docker exec {CONTAINER} python /config/validate_map.py 2>&1")
    print(output.rstrip())
    return 0 if "PASS:" in output else 1


if __name__ == "__main__":
    sys.exit(main())
