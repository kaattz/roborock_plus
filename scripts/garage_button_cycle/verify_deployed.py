"""Fetch the deployed button automation from HA's own storage and compare it.

The same reasoning as the garage state machine: what matters is the config Home
Assistant is actually holding, not the file this repo builds. A transform or a
schema normalisation could differ between them.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ha_ssh import connect  # noqa: E402

UNIQUE_ID = "1791500000000"
OUT = (
    Path(r"C:\Code\roborock_plus\scripts\garage_button_cycle")
    / "deployed_button_cycle.json"
)
SOURCE = Path(r"C:\Code\roborock_plus\scripts\garage_button_cycle\button_cycle.json")

READER = f'''
import json, sys, yaml

with open("/config/automations.yaml", encoding="utf-8") as handle:
    automations = yaml.safe_load(handle)

for automation in automations:
    if str(automation.get("id")) == "{UNIQUE_ID}":
        sys.stdout.write(json.dumps(automation, ensure_ascii=False))
        break
else:
    sys.exit("automation {UNIQUE_ID} not found")
'''

text = ""
errors = ""
with connect() as client:
    payload = base64.b64encode(READER.encode()).decode()
    step = (
        f"echo {payload} | base64 -d | sudo -n tee /root/read_btn.py > /dev/null && "
        "sudo -n docker cp /root/read_btn.py homeassistant:/config/read_btn.py && "
        "sudo -n docker exec homeassistant python /config/read_btn.py"
    )
    _in, out, err = client.exec_command(f"bash -lc {json.dumps(step)}", timeout=180)
    text = out.read().decode("utf-8", "replace")
    errors = err.read().decode("utf-8", "replace")

if not text.strip().startswith("{"):
    raise SystemExit(f"could not read the automation: {errors[:600] or text[:300]}")

deployed = json.loads(text)
OUT.write_text(json.dumps(deployed, ensure_ascii=False, indent=2), encoding="utf-8")
source = json.loads(SOURCE.read_text(encoding="utf-8"))

print(f"wrote {OUT}")
print(f"alias: {deployed.get('alias')}")
print()

mismatches = []
for key in ("triggers", "conditions", "actions", "mode", "max_exceeded"):
    left = json.dumps(source.get(key), sort_keys=True, ensure_ascii=False)
    right = json.dumps(deployed.get(key), sort_keys=True, ensure_ascii=False)
    same = left == right
    print(f"  {key:14} {'match' if same else 'DIFFERS'}")
    if not same:
        mismatches.append(key)

print()
if mismatches:
    print(f"FAIL: deployed differs in {mismatches}")
    for key in mismatches:
        print(f"  repo    : {json.dumps(source.get(key), ensure_ascii=False)[:300]}")
        print(f"  deployed: {json.dumps(deployed.get(key), ensure_ascii=False)[:300]}")
    raise SystemExit(1)

print("PASS: the deployed automation is identical to the config that was simulated.")
