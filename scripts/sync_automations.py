"""Push the repo's vacuum automations into Home Assistant.

The repo is the source of truth; this is how it reaches the running system.
`fetch_automations.py` is the opposite direction, and it is deliberately a
separate script -- a single script that reads and writes in one run can quietly
accept its own output, so real drift would never be noticed.

Safety comes first here, because `automations.yaml` holds 109 automations and
only three of them belong to this repo:

* Only the three watched entries are replaced, matched by HA `id`. Every other
  automation is re-dumped unchanged rather than rebuilt.
* The whole file is round-tripped in memory first and compared. If pyyaml's
  output is not equivalent to its input, the script refuses to write: that would
  mean the dump is lossy and a write could corrupt unrelated automations.
* The existing file is copied to a timestamped backup before any write.
* After writing, the file is re-read and each watched automation is compared
  field-by-field against what was intended.

Usage:
    python scripts/sync_automations.py            # dry run (default)
    python scripts/sync_automations.py --apply    # write + verify
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import paramiko
import yaml

HOST = os.environ.get("ROBOROCK_HA_HOST", "192.168.166.68")
USER = os.environ.get("ROBOROCK_HA_USER", "hass")
# Never hardcode this. The repository is public, and an earlier revision of
# these scripts committed the password to it.
PASSWORD = os.environ.get("ROBOROCK_HA_PASSWORD")

REPO = Path(__file__).resolve().parent.parent
AUTOMATIONS_DIR = REPO / "automations"

WATCHED: dict[str, str] = {
    "1791392186966": "roborock_garage_door_statemachine.yaml",
    "1791390713215": "roborock_stuck_or_error_stop.yaml",
    "1791372324572": "roborock_clean_command_not_started.yaml",
}

# Runs in the container. argv: mode, path to the JSON payload, target path.
WORKER = r'''
import json, shutil, sys, time, yaml

mode = sys.argv[1]
with open(sys.argv[2], encoding="utf-8") as handle:
    replacements = json.load(handle)
path = sys.argv[3]

with open(path, encoding="utf-8") as handle:
    original_text = handle.read()
automations = yaml.safe_load(original_text)

# --- Guard 1: is pyyaml's dump faithful to its input? -------------------
round_tripped = yaml.safe_load(
    yaml.safe_dump(automations, allow_unicode=True, sort_keys=False,
                   default_flow_style=False, width=4096)
)
if round_tripped != automations:
    sys.exit("REFUSING: yaml round-trip is lossy for this file")

# --- Guard 2: does every replacement target exist, exactly once? --------
by_id = {}
for index, automation in enumerate(automations):
    key = str(automation.get("id"))
    if key in replacements:
        if key in by_id:
            sys.exit(f"REFUSING: duplicate id {key} in automations.yaml")
        by_id[key] = index

missing = [k for k in replacements if k not in by_id]
if missing:
    sys.exit("REFUSING: not found in automations.yaml: " + ", ".join(missing))

changed = []
for key, config in replacements.items():
    index = by_id[key]
    if automations[index] == config:
        continue
    changed.append((key, automations[index], config))
    automations[index] = config

if mode == "check":
    sys.stdout.write(json.dumps({
        "ok": True,
        "round_trip_ok": True,
        "total": len(automations),
        "changed": [{"id": k, "from": o.get("alias"), "to": n.get("alias")}
                    for k, o, n in changed],
    }, ensure_ascii=False))
    sys.exit(0)

if not changed:
    sys.stdout.write(json.dumps({
        "ok": True,
        "wrote": False,
        "reason": "already identical",
        "total": len(automations),
    }, ensure_ascii=False))
    sys.exit(0)

backup = f"{path}.bak-sync-{time.strftime('%Y%m%d-%H%M%S')}"
shutil.copy2(path, backup)

with open(path, "w", encoding="utf-8") as handle:
    yaml.safe_dump(automations, handle, allow_unicode=True, sort_keys=False,
                   default_flow_style=False, width=4096)

# --- Verify: re-read and compare the entries we meant to write ----------
with open(path, encoding="utf-8") as handle:
    written = yaml.safe_load(handle)

problems = []
if len(written) != len(automations):
    problems.append(f"count changed: {len(automations)} -> {len(written)}")

seen = {}
for automation in written:
    key = str(automation.get("id"))
    if key in replacements:
        seen[key] = automation

for key, config in replacements.items():
    if seen.get(key) != config:
        problems.append(f"id {key} does not match after write")

if problems:
    shutil.copy2(backup, path)
    sys.exit("WRITE VERIFICATION FAILED, restored backup: " + "; ".join(problems))

sys.stdout.write(json.dumps({
    "ok": True,
    "wrote": True,
    "backup": backup,
    "total": len(written),
    "changed": [{"id": k, "to": n.get("alias")} for k, o, n in changed],
}, ensure_ascii=False))
'''


def run_worker(
    client: paramiko.SSHClient, mode: str, replacements: dict, target: str
) -> str:
    """Ship the worker plus its payload into the container and run it.

    The payload goes in as a file rather than an argv string: it is ~15 KB of
    JSON, which overruns the command line and was silently truncated the first
    time this was tried.
    """
    worker_b64 = base64.b64encode(WORKER.encode()).decode()
    payload_b64 = base64.b64encode(
        json.dumps(replacements, ensure_ascii=False).encode()
    ).decode()
    step = (
        f"echo {worker_b64} | base64 -d | sudo -n tee /root/_sync.py > /dev/null && "
        f"echo {payload_b64} | base64 -d | sudo -n tee /root/_sync.json > /dev/null && "
        "sudo -n docker cp /root/_sync.py homeassistant:/config/_sync.py && "
        "sudo -n docker cp /root/_sync.json homeassistant:/config/_sync.json && "
        f"sudo -n docker exec homeassistant python /config/_sync.py "
        f"{json.dumps(mode)} /config/_sync.json {json.dumps(target)}"
    )
    _in, out, err = client.exec_command(f"bash -lc {json.dumps(step)}", timeout=300)
    text = out.read().decode("utf-8", "replace").strip()
    errors = err.read().decode("utf-8", "replace").strip()
    client.exec_command(
        "sudo -n rm -f /root/_sync.py /root/_sync.json; "
        "sudo -n docker exec homeassistant rm -f /config/_sync.py /config/_sync.json",
        timeout=60,
    )
    if not text:
        # The worker exits with a plain message when it refuses. Pass that
        # through as a structured refusal so the caller can print it.
        detail = errors or "no output from the container"
        return json.dumps({"refused": detail.splitlines()[-1] if errors else detail})
    return text


def main() -> int:
    if not PASSWORD:
        raise SystemExit(
            "ROBOROCK_HA_PASSWORD is not set.\n"
            "Set it in the environment; it is deliberately not stored in the "
            "repository, which is public."
        )

    apply = "--apply" in sys.argv
    target = "/config/automations.yaml"
    # A copy can be targeted so the write path itself is exercised during
    # development. Without this the only way to test a write is to write to the
    # live 109-automation file, which is not a test worth running.
    if "--target" in sys.argv:
        target = sys.argv[sys.argv.index("--target") + 1]

    replacements = {}
    for unique_id, filename in WATCHED.items():
        path = AUTOMATIONS_DIR / filename
        if not path.exists():
            raise SystemExit(f"missing repo file: {path}")
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        if str(config.get("id")) != unique_id:
            raise SystemExit(
                f"{filename}: id is {config.get('id')!r}, expected {unique_id!r}. "
                "The id must match the automation it replaces."
            )
        replacements[unique_id] = config

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    try:
        result = json.loads(
            run_worker(client, "apply" if apply else "check", replacements, target)
        )
    finally:
        client.close()

    print("target                   :", target)
    # A refusal (lossy round-trip, missing id) comes back as a message instead
    # of a payload. Surface that line rather than raising a KeyError, which
    # would hide the one line that explains what was wrong.
    if not result.get("ok"):
        print("refused:", result.get("refused"))
        return 1
    if "round_trip_ok" in result:
        print("yaml round-trip is faithful:", result["round_trip_ok"])
    print("automations in file      :", result["total"])
    print()
    if not apply:
        if result["changed"]:
            print("would update:")
            for item in result["changed"]:
                print(f"  {item['id']}  {item['from']!r} -> {item['to']!r}")
        else:
            print("repo and Home Assistant already agree; nothing to do")
        print()
        print("dry run -- nothing written. Re-run with --apply")
        return 0

    if result.get("wrote"):
        print("wrote. backup:", result["backup"])
    else:
        print("nothing written:", result["reason"])
    print()
    if target == "/config/automations.yaml":
        print("Reload Home Assistant's automations for the change to take effect:")
        print('  ha_reload_core(target="automations")')
    return 0


if __name__ == "__main__":
    sys.exit(main())
