"""Extract the deployed state machine from Home Assistant's own storage.

The config is read out of automations.yaml inside the container and written as
JSON locally, so the verification compares against what HA actually holds rather
than a copy typed from a tool result. The console mangles Chinese, so the file is
transferred as base64.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ha_ssh import connect  # noqa: E402

UNIQUE_ID = "1791392186966"
OUT = (
    Path(r"C:\Code\roborock_plus\scripts\garage_state_machine")
    / "deployed_state_machine.json"
)

READER = f'''
import json, sys, yaml

with open("/config/automations.yaml", encoding="utf-8") as handle:
    automations = yaml.safe_load(handle)

for automation in automations:
    if str(automation.get("id")) == "{UNIQUE_ID}":
        sys.stdout.write(json.dumps(automation, ensure_ascii=False))
        break
else:
    sys.exit("automation not found")
'''

text = ""
errors = ""
with connect() as client:
    payload = base64.b64encode(READER.encode()).decode()
    step = (
        f"echo {payload} | base64 -d | sudo -n tee /root/read_sm.py > /dev/null && "
        "sudo -n docker cp /root/read_sm.py homeassistant:/config/read_sm.py && "
        "sudo -n docker exec homeassistant python /config/read_sm.py"
    )
    _in, out, err = client.exec_command(f"bash -lc {json.dumps(step)}", timeout=180)
    text = out.read().decode("utf-8", "replace")
    errors = err.read().decode("utf-8", "replace")

if not text.strip():
    raise SystemExit(f"no output from the container: {errors[:600]}")

config = json.loads(text)
OUT.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
print()
print(f"alias: {config.get('alias')}")
print("triggers:")
for trigger in config["triggers"]:
    print(
        f"  {trigger.get('id'):10} from={trigger.get('from')!r:8} "
        f"to={trigger.get('to')!r:50} for={trigger.get('for')}"
    )
print(f"mode: {config.get('mode')} max={config.get('max')}")
print()

# Compare the trigger shape with the repo's source of truth.
source = json.loads(
    (Path(__file__).resolve().parent / "state_machine.json").read_text(encoding="utf-8")
)
deployed_triggers = json.dumps(config["triggers"], sort_keys=True, ensure_ascii=False)
source_triggers = json.dumps(source["triggers"], sort_keys=True, ensure_ascii=False)
print(f"deployed triggers match the repo build: {deployed_triggers == source_triggers}")
if deployed_triggers != source_triggers:
    print("  deployed:", deployed_triggers)
    print("  repo    :", source_triggers)

deployed_actions = json.dumps(config["actions"], sort_keys=True, ensure_ascii=False)
source_actions = json.dumps(source["actions"], sort_keys=True, ensure_ascii=False)
print(f"deployed actions match the repo build : {deployed_actions == source_actions}")
