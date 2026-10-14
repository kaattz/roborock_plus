"""Refresh the deployed snapshot from the build, after verifying HA matches it.

`deployed_state_machine.json` is meant to be evidence of what Home Assistant
actually holds, so that a divergence from the repo shows up. It used to be written
by `fetch_deployed.py`, which reads `automations.yaml` over SSH -- but the SSH
password was rotated on 2026-10-11 and that path no longer works.

On 2026-10-14 the live config was instead read with `ha_config_get_automation` and
compared field by field against `state_machine.json`: triggers, conditions,
actions, mode, max, max_exceeded and description all matched. This script records
what that comparison established.

Run it only after confirming the live config equals the build -- it copies the
build, it does not read HA, so it cannot detect drift on its own.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
HERE = REPO / "scripts" / "garage_state_machine"
BUILT = HERE / "state_machine.json"
MIRROR = REPO / "automations" / "roborock_garage_door_statemachine.yaml"
SNAPSHOT = HERE / "deployed_state_machine.json"

SCALARS = ("alias", "description", "mode", "max", "max_exceeded")


def main() -> int:
    built = json.loads(BUILT.read_text(encoding="utf-8"))
    unique_id = yaml.safe_load(MIRROR.read_text(encoding="utf-8"))["id"]

    snapshot: dict = {"id": unique_id}
    for key in SCALARS:
        if key in built:
            snapshot[key] = built[key]
    snapshot["triggers"] = built["triggers"]
    snapshot["conditions"] = built.get("conditions", [])
    snapshot["actions"] = built["actions"]

    SNAPSHOT.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"wrote {SNAPSHOT.relative_to(REPO)}")
    print(f"  id: {unique_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
