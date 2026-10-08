"""Pull the vacuum automations out of Home Assistant and into the repo.

The repo copy is the source of truth for review and history; Home Assistant is
where they run. `sync_automations.py` pushes the repo copy back, and this script
is how the repo copy is refreshed from a live system.

They are deliberately separate: a round-trip that both reads and writes in one
run can silently accept its own output, so drift would never surface.

Every automation here is matched by its HA `id` (the automation's unique_id),
never by alias or entity_id. The `id` is the only stable handle -- the alias was
renamed to English and the entity_id was renamed with it, and both can change
again without the automation being a different automation.

Usage:
    python scripts/fetch_automations.py           # check for drift
    python scripts/fetch_automations.py --write   # update the repo copies
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import paramiko

HOST = os.environ.get("ROBOROCK_HA_HOST", "192.168.166.68")
USER = os.environ.get("ROBOROCK_HA_USER", "hass")
# Never hardcode this. The repository is public.
PASSWORD = os.environ.get("ROBOROCK_HA_PASSWORD")

REPO = Path(__file__).resolve().parent.parent
AUTOMATIONS_DIR = REPO / "automations"

# HA unique_id -> repo filename. The filename tracks the entity_id so a reader
# can find the file from the running system and vice versa.
WATCHED: dict[str, str] = {
    "1791392186966": "roborock_garage_door_statemachine.yaml",
    "1791390713215": "roborock_stuck_or_error_stop.yaml",
    "1791372324572": "roborock_clean_command_not_started.yaml",
}

READER = r'''
import json, sys, yaml

with open("/config/automations.yaml", encoding="utf-8") as handle:
    automations = yaml.safe_load(handle)

wanted = json.loads(sys.argv[1])
found = {}
for automation in automations:
    key = str(automation.get("id"))
    if key in wanted:
        found[key] = automation

missing = [k for k in wanted if k not in found]
if missing:
    sys.exit("not found in automations.yaml: " + ", ".join(missing))

sys.stdout.write(json.dumps(found, ensure_ascii=False))
'''


def read_remote() -> dict[str, dict]:
    """Read the watched automations out of the HA container."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    try:
        payload = base64.b64encode(READER.encode()).decode()
        wanted = json.dumps(sorted(WATCHED))
        step = (
            f"echo {payload} | base64 -d | sudo -n tee /root/_fetch.py > /dev/null && "
            "sudo -n docker cp /root/_fetch.py homeassistant:/config/_fetch.py && "
            f"sudo -n docker exec homeassistant python /config/_fetch.py {json.dumps(wanted)}"
        )
        _in, out, err = client.exec_command(f"bash -lc {json.dumps(step)}", timeout=180)
        text = out.read().decode("utf-8", "replace")
        errors = err.read().decode("utf-8", "replace")
        client.exec_command(
            "sudo -n rm -f /root/_fetch.py; "
            "sudo -n docker exec homeassistant rm -f /config/_fetch.py",
            timeout=60,
        )
    finally:
        client.close()

    if not text.strip():
        raise SystemExit(f"no output from the container: {errors[:800]}")
    return json.loads(text)


def main() -> int:
    import yaml

    if not PASSWORD:
        raise SystemExit(
            "ROBOROCK_HA_PASSWORD is not set.\n"
            "Set it in the environment; it is deliberately not stored in the "
            "repository, which is public."
        )

    write = "--write" in sys.argv
    live = read_remote()
    AUTOMATIONS_DIR.mkdir(exist_ok=True)

    drifted: list[str] = []
    for unique_id, filename in WATCHED.items():
        config = live[unique_id]
        rendered = yaml.safe_dump(
            config,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=4096,
        )
        path = AUTOMATIONS_DIR / filename

        if not path.exists():
            path.write_text(rendered, encoding="utf-8")
            print(f"created  {filename}")
            continue

        existing = yaml.safe_load(path.read_text(encoding="utf-8"))
        if existing == config:
            print(f"same     {filename}")
            continue

        drifted.append(filename)
        if write:
            path.write_text(rendered, encoding="utf-8")
            print(f"updated  {filename}")
        else:
            print(f"DRIFT    {filename}")

    if drifted and not write:
        print()
        print("Home Assistant has changes the repo does not.")
        print("Review them, then re-run with --write to accept.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
