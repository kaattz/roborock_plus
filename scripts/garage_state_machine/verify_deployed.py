"""Run the full replay against the automation as deployed in Home Assistant.

`replay_docked_flapping.py` reads the config this repo builds. That is the right
thing to test while editing, but it cannot show that the *deployed* automation
has the same behaviour: a transform or a schema normalisation could have changed
it on the way in.

This points the same checks at `deployed_state_machine.json`, which is read out
of Home Assistant's own automations.yaml, and requires the two configs to be
equivalent before trusting the result.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    source = json.loads((HERE / "state_machine.json").read_text(encoding="utf-8"))
    deployed = json.loads(
        (HERE / "deployed_state_machine.json").read_text(encoding="utf-8")
    )

    print("Comparing the repo build with what Home Assistant holds")
    mismatches = []
    for key in ("triggers", "actions", "mode", "max", "max_exceeded"):
        left = json.dumps(source.get(key), sort_keys=True, ensure_ascii=False)
        right = json.dumps(deployed.get(key), sort_keys=True, ensure_ascii=False)
        same = left == right
        print(f"  {key:14} {'match' if same else 'DIFFERS'}")
        if not same:
            mismatches.append(key)

    if mismatches:
        print()
        print(f"FAIL: deployed config differs in {mismatches}")
        return 1

    print()
    print("The deployed config is equivalent, so the replay's guarantees carry")
    print("over. Running the replay with the deployed file substituted:")
    print()

    # Point the replay at the deployed file by swapping it in temporarily.
    source_path = HERE / "state_machine.json"
    backup = source_path.read_text(encoding="utf-8")
    try:
        source_path.write_text(
            json.dumps(deployed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result = subprocess.run(
            [sys.executable, str(HERE / "replay_docked_flapping.py")],
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        source_path.write_text(backup, encoding="utf-8")

    print(result.stdout.rstrip())
    if result.returncode != 0:
        print(result.stderr[-800:])
        return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
