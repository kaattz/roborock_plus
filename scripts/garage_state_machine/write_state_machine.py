"""Write the rebuilt state machine to HA without disturbing the door.

Reads the JSON built by `build.py` and writes it through the HA websocket API,
so the Chinese text is transferred as UTF-8 rather than through a console that
mangles it.

The automation is written **disabled** and left that way: enabling it is a
deliberate step after the trace is inspected.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://192.168.166.68:8123"
TOKEN = sys.argv[1] if len(sys.argv) > 1 else None
CONFIG_PATH = Path(__file__).resolve().parent / "state_machine.json"
ENTITY_ID = "automation.sao_di_ji_ju_men_zhuang_tai_ji"

if not TOKEN:
    raise SystemExit("usage: write_state_machine.py <ha-token>")


def api(path: str, payload: dict | None = None) -> object:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
        method="GET" if payload is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode()
    except urllib.error.HTTPError as err:
        raise SystemExit(f"{path} -> HTTP {err.code}: {err.read().decode()[:400]}")
    return json.loads(body) if body.strip() else None


config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
print(f"alias: {config['alias']}")
print(f"triggers: {[t['id'] for t in config['triggers']]}")
print(f"branches: {len(config['actions'][0]['choose'])}")

# Write through the automation config API, which accepts the full config body.
result = api(
    f"/api/config/automation/config/{ENTITY_ID.split('.', 1)[1]}",
    config,
)
print(f"write result: {result}")

state = api(f"/api/states/{ENTITY_ID}")
assert isinstance(state, dict)
print(f"state after write: {state['state']}")
print(f"last_triggered: {state['attributes'].get('last_triggered')}")
