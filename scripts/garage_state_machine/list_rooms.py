"""Work out which room clean will cross the danger zone fastest.

The point of the test is the door's behaviour as the robot leaves and returns,
so the shortest possible real task is best: it exercises phases B, C and D
without an hour of cleaning.
"""

from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://192.168.166.68:8123"


def call(token: str, path: str, payload: dict | None = None) -> object:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read().decode()
    return json.loads(body) if body.strip() else None


def main() -> int:
    token = sys.argv[1] if len(sys.argv) > 1 else None
    if not token:
        raise SystemExit("usage: list_rooms.py <ha-token>")

    context = call(
        token,
        "/api/services/roborock_plus/get_safe_zone_editor_context?return_response",
        {"entity_id": "vacuum.g20s_ultra"},
    )
    data = context["service_response"]["vacuum.g20s_ultra"]
    print("current_position:", data["current_position"])
    print("safe_zone       :", data["safe_zone"])
    print("current_map     :", data["current_map"])
    print("position_source :", data.get("position_source"))

    rooms = call(
        token,
        "/api/services/roborock_plus/get_vacuum_rooms?return_response",
        {"entity_id": "vacuum.g20s_ultra"},
    )
    print()
    print("rooms:")
    print(json.dumps(rooms, ensure_ascii=False, indent=2)[:1500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
