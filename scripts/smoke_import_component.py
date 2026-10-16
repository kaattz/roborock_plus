"""Import every component module inside a real Home Assistant install.

Static audits prove the library *symbols* exist. They cannot prove the modules
still import, that Home Assistant's own API is used correctly, or that the
platform files build their entity descriptions. This runs the real thing::

    .analysis/venvha/Scripts/python.exe scripts/smoke_import_component.py

It is deliberately separate from the pytest suite, which is hermetic and runs
without Home Assistant or python-roborock installed.
"""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = ROOT / "custom_components"

# Import order matters: logbook/recorder pull heavy optional deps lazily.
MODULES = [
    "const",
    "models",
    "server_url",
    "door_timing",
    "garage_config",
    "safe_zone",
    "resume_logic",
    "v1_task_state",
    "v1_status_polling",
    "door_evidence",
    "v1_position_trust",
    "v1_diagnostics",
    "clean_command_watch",
    "garage_commands",
    "garage_session_store",
    "roborock_storage",
    "safe_zone_store",
    "garage_guard",
    "garage_control",
    "garage_entities",
    "garage_runtime",
    "garage_task",
    "entity",
    "coordinator",
    "v1_map_position",
    "v1_map_render",
    "v1_stuck_detection",
    "safe_zone_entities",
    "image",
    "sensor",
    "binary_sensor",
    "button",
    "number",
    "select",
    "switch",
    "time",
    "vacuum",
    "panel",
    "services",
    "diagnostics",
    "config_flow",
    "__init__",
]


def main() -> int:
    sys.path.insert(0, str(PACKAGE_PARENT))

    for name in ("homeassistant", "roborock", "vacuum_map_parser_base"):
        try:
            mod = importlib.import_module(name)
            version = getattr(mod, "__version__", None)
            if version is None:
                from importlib.metadata import version as dist_version

                try:
                    version = dist_version(name.replace("_", "-"))
                except Exception:  # noqa: BLE001
                    version = "unknown"
            print(f"{name}: {version}")
        except Exception as exc:  # noqa: BLE001 - report any import failure as fatal
            print(f"FATAL: cannot import {name}: {type(exc).__name__}: {exc}")
            return 2

    failures: list[tuple[str, str]] = []
    ok = 0
    for name in MODULES:
        target = f"roborock_plus.{name}"
        try:
            importlib.import_module(target)
            ok += 1
        except Exception:  # noqa: BLE001
            failures.append((target, traceback.format_exc()))

    print(f"\nmodules imported: {ok}/{len(MODULES)}")
    for target, tb in failures:
        print(f"\n=== FAILED: {target} ===")
        print(tb)

    # The entity descriptions are built at import time; surface their size.
    try:
        sensor = importlib.import_module("roborock_plus.sensor")
        for attr in (
            "SENSOR_DESCRIPTIONS",
            "DYAD_SENSOR_DESCRIPTIONS",
            "ZEO_SENSOR_DESCRIPTIONS",
            "Q7_B01_SENSOR_DESCRIPTIONS",
            "Q10_B01_SENSOR_DESCRIPTIONS",
        ):
            descriptions = getattr(sensor, attr, None)
            if descriptions is not None:
                print(f"sensor.{attr}: {len(descriptions)}")
        # The function that broke on 7.12.0 -- exercise both branches.
        from types import SimpleNamespace

        from roborock.data import RoborockDockErrorCode, RoborockDockTypeCode

        docked = SimpleNamespace(
            status=SimpleNamespace(
                dock_error_status=RoborockDockErrorCode(0),
                dock_type=RoborockDockTypeCode.o0_dock,
            )
        )
        undocked = SimpleNamespace(
            status=SimpleNamespace(
                dock_error_status=RoborockDockErrorCode(0),
                dock_type=RoborockDockTypeCode.o1_dock,
            )
        )
        print(f"_dock_error_value_fn(no dock):   {sensor._dock_error_value_fn(docked)!r}")
        print(f"_dock_error_value_fn(with dock): {sensor._dock_error_value_fn(undocked)!r}")
    except Exception:  # noqa: BLE001
        failures.append(("roborock_plus.sensor behaviour", traceback.format_exc()))
        print("\n=== FAILED: sensor behaviour ===")
        print(traceback.format_exc())

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
