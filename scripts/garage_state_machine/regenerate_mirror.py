"""Regenerate the paste-into-HA mirror from the built state machine.

`automations/roborock_garage_door_statemachine.yaml` is the copy that gets pasted
into Home Assistant by hand. `scripts/garage_state_machine/build.py` produces
`state_machine.json`. `tests/test_system_composition.py` requires the two to carry
identical actions, triggers and scalar settings -- an anti-drift guard added after
2026-10-10, when the JSON gained a settle delay and the mirror did not, so pasting
it would have shipped the position-echo bug back into HA.

This writes the mirror from the JSON, preserving the header comment and the `id`
(which the builder deliberately does not emit, because it is the HA unique_id and
changing it would orphan the entity's history).

Run after build.py, then run the tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
GENERATED = REPO / "scripts" / "garage_state_machine" / "state_machine.json"
MIRROR = REPO / "automations" / "roborock_garage_door_statemachine.yaml"

HEADER = """\
# 车库门状态机 —— 这份是要**手工粘进 HA** 的副本。
#
# 生成物是 scripts/garage_state_machine/state_machine.json，
# 由 scripts/garage_state_machine/build.py 产出。
# 两者必须一致：tests/test_system_composition.py 会比对 actions / triggers /
# 标量设置，不一致就红。改完 build.py 后要重新生成本文件，否则粘进 HA 的是旧步骤。
#
# 本文件由 scripts/garage_state_machine/regenerate_mirror.py 生成，不要手改。
#
# id 不能改：它是 HA 的 unique_id，改了等于换一个自动化，历史和实体注册表身份都会丢。
"""

SCALARS = ("alias", "description", "mode", "max", "max_exceeded")


def main() -> int:
    generated = json.loads(GENERATED.read_text(encoding="utf-8"))
    current = yaml.safe_load(MIRROR.read_text(encoding="utf-8"))
    unique_id = current["id"]

    config: dict = {"id": unique_id}
    for key in SCALARS:
        if key in generated:
            config[key] = generated[key]
    config["triggers"] = generated["triggers"]
    # Always emit it, even when empty: tests/test_system_composition.py compares
    # `mirror.get("conditions")` against `generated.get("conditions")`, so omitting
    # the key turns `[]` into `None` and fails the comparison.
    config["conditions"] = generated.get("conditions", [])
    config["actions"] = generated["actions"]

    body = yaml.safe_dump(
        config,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=1000,
    )
    MIRROR.write_text(HEADER + body, encoding="utf-8")

    print(f"wrote {MIRROR.relative_to(REPO)}")
    print(f"  id         : {unique_id}")
    print(f"  triggers   : {len(config['triggers'])}")
    print(f"  choose     : {len(config['actions'][0]['choose'])} branches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
