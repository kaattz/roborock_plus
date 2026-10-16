"""The manifest's library pins must be satisfiable on the running HA version.

v0.7.6 shipped ``python-roborock==5.5.1``, whose ``protobuf<7`` cap conflicts
with Home Assistant 2026.10.1's ``protobuf==7.36.0``. The integration then
failed at setup with::

    Requirements for roborock_plus not found: ['python-roborock==5.5.1']

These tests pin the properties that made that happen, so a future edit cannot
quietly reintroduce an unsatisfiable requirement set. They are deliberately
static -- no network, no installed HA -- so they run in the default suite.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "roborock_plus"
MANIFEST = COMPONENT / "manifest.json"


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _requirement(name: str) -> str:
    for requirement in _manifest()["requirements"]:
        if requirement.split("==")[0].split(">=")[0].strip() == name:
            return requirement
    raise AssertionError(f"{name} is not in manifest requirements")


def _pinned_version(name: str) -> str:
    match = re.fullmatch(rf"{re.escape(name)}==([0-9][^;,\s]*)", _requirement(name))
    assert match, (
        f"{name} must be exact-pinned with '=='; got {_requirement(name)!r}. "
        "Home Assistant installs the manifest requirement verbatim, so an "
        "open range lets the integration and the core integration disagree."
    )
    return match.group(1)


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value))


class TestRequirementsAreSatisfiableOnCurrentHA:
    """HA 2026.10.1 pins protobuf==7.36.0; our pin must not forbid it.

    ``python-roborock==7.12.0`` requires ``protobuf<8,>=6.31.1``, verified to
    resolve against both ends of the range this integration declares support
    for: HA 2026.8.0 (``protobuf==6.32.0``, the floor in ``hacs.json``) and HA
    2026.10.1 (``protobuf==7.36.0``). The old ``5.5.1`` pin only resolved on
    the older end, which is why upgrading HA broke the integration.
    """

    # The constraint that broke v0.7.6: 5.5.1 required protobuf<7, but HA
    # 2026.10.1 ships protobuf 7.36.0 in package_constraints.txt.
    HA_PROTOBUF_MAJOR = 7

    def test_python_roborock_is_pinned(self) -> None:
        assert _pinned_version("python-roborock")

    def test_python_roborock_major_allows_protobuf_7(self) -> None:
        """python-roborock >=6 widened protobuf to <8, which admits 7.x."""
        pinned = _pinned_version("python-roborock")
        major = _version_tuple(pinned)[0]
        assert major >= 6, (
            f"python-roborock {pinned} predates the protobuf<8 widening "
            f"(protobuf<7 through 5.6.0). It cannot install alongside "
            f"protobuf {self.HA_PROTOBUF_MAJOR}.x."
        )

    def test_map_parser_matches_the_version_used_by_the_audited_pin(self) -> None:
        """The pin pair is only verified together; bump them as a unit."""
        assert _pinned_version("vacuum-map-parser-roborock") == "0.1.5"


class TestDockTypeCodeUsesTheCurrentMemberName:
    """``RoborockDockTypeCode.no_dock`` was removed in python-roborock 6.x/7.x.

    The class still imports, so an import-only check passes while the sensor
    value function raises ``AttributeError`` on every update. HA core's own
    roborock integration uses ``o0_dock`` for the same "no dock" condition.
    """

    def test_sensor_module_does_not_use_removed_no_dock_member(self) -> None:
        source = (COMPONENT / "sensor.py").read_text(encoding="utf-8")
        assert "RoborockDockTypeCode.no_dock" not in source, (
            "RoborockDockTypeCode.no_dock no longer exists in python-roborock "
            "7.x; use RoborockDockTypeCode.o0_dock (value 0), matching "
            "homeassistant/components/roborock/sensor.py."
        )

    def test_sensor_module_uses_o0_dock(self) -> None:
        source = (COMPONENT / "sensor.py").read_text(encoding="utf-8")
        assert "RoborockDockTypeCode.o0_dock" in source


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
