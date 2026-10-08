"""Snapshot the states the garage test will be judged against.

Run before and after a full-chain test so the door's movements can be attributed
to a phase rather than inferred.
"""

from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://192.168.166.68:8123"

ENTITIES = [
    "automation.sao_di_ji_ju_men_zhuang_tai_ji",
    "cover.vacuum_garage_door",
    "vacuum.g20s_ultra",
    "sensor.g20s_ultra_status",
    "binary_sensor.g20s_ultra_clear_of_garage",
    "binary_sensor.g20s_ultra_in_safe_zone",
    "binary_sensor.g20s_ultra_task_active",
    "binary_sensor.ke_ting_sao_di_ji_stuck",
    "image.vacuum_full_home_map",
]


def get(token: str, path: str) -> dict:
    request = urllib.request.Request(
        f"{BASE}{path}", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode())


def main() -> int:
    token = sys.argv[1] if len(sys.argv) > 1 else None
    if not token:
        raise SystemExit("usage: snapshot.py <ha-token>")

    print(f"{'entity':52} {'state':22} last_changed")
    for entity in ENTITIES:
        try:
            data = get(token, f"/api/states/{entity}")
        except Exception as err:  # noqa: BLE001 - report and continue
            print(f"{entity:52} ERROR {err}")
            continue
        print(
            f"{entity:52} {data['state']:22} {data.get('last_changed', '')}"
        )

    position = get(token, "/api/states/sensor.vacuum_current_room")
    print(f"\ncurrent_room: {position['state']}")

    door = get(token, "/api/states/cover.vacuum_garage_door")
    print(
        "door current_position: "
        f"{door['attributes'].get('current_position')} ({door['state']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
