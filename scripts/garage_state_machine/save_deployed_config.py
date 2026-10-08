"""Write the config returned by ha_config_get_automation into the repo.

The MCP tool prefixes a status line, so the JSON is located by finding the first
`{` rather than assumed to start at byte zero.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if len(sys.argv) < 2:
    raise SystemExit("usage: save_deployed_config.py <file with the MCP output>")

raw = Path(sys.argv[1]).read_text(encoding="utf-8")
start = raw.find("{")
if start < 0:
    raise SystemExit("no JSON object found in the input")

payload = json.loads(raw[start:])
config = payload.get("config", payload)

out = Path(__file__).resolve().parent / "deployed_state_machine.json"
out.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

blob = json.dumps(config, ensure_ascii=False)
print(f"saved {out} ({out.stat().st_size} bytes)")
print(f"alias: {config.get('alias')}")
print(f"trigger ids: {[t.get('id') for t in config['triggers']]}")
print(f"triggers with for: {[t['id'] for t in config['triggers'] if t.get('for')]}")
print(f"choose branches: {len(config['actions'][0]['choose'])}")
print(f"value_template uses: {blob.count('value_template')}")
print(f"wait_template uses:  {blob.count('wait_template')}")
