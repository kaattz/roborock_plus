"""Tests for re-rendering the map image with a corrected robot marker.

The integration's other position fixes (`v1_position_trust`) correct what the
services and entities report. They do not touch the PNG, because the library
bakes the marker into the image at parse time. These tests cover the byte-level
patch that closes that gap.
"""

from __future__ import annotations

import gzip
import importlib.util
import struct
import sys
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "roborock_plus"
    / "v1_map_render.py"
)

SPEC = importlib.util.spec_from_file_location("v1_map_render_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

find_robot_position_block = MODULE.find_robot_position_block
patch_robot_position = MODULE.patch_robot_position
read_robot_position = MODULE.read_robot_position
render_position = MODULE.render_position
rerender_map_image = MODULE.rerender_map_image

MAP_HEADER_LENGTH = 24


def build_map(
    *,
    robot: tuple[int, int] | None = (25727, 24232),
    charger: tuple[int, int] | None = (25688, 28538),
    robot_angle: int | None = 90,
    extra_payload: int = 8,
    gzipped: bool = False,
) -> bytes:
    """Build a minimal map in the parser's block format."""
    blocks = bytearray()

    def add_block(block_type: int, data: bytes, header_length: int = 8) -> None:
        header = bytearray(header_length)
        header[0:2] = struct.pack("<H", block_type)
        header[2:4] = struct.pack("<H", header_length)
        header[4:8] = struct.pack("<I", len(data))
        blocks.extend(header)
        blocks.extend(data)

    if charger is not None:
        add_block(1, struct.pack("<ii", charger[0], charger[1]))
    # An IMAGE block keeps the walk honest about skipping non-target blocks.
    add_block(2, b"\x00" * extra_payload)
    if robot is not None:
        payload = struct.pack("<ii", robot[0], robot[1])
        if robot_angle is not None:
            payload += struct.pack("<i", robot_angle)
        add_block(8, payload)

    header = bytearray(MAP_HEADER_LENGTH)
    header[0x02:0x04] = struct.pack("<H", MAP_HEADER_LENGTH)
    raw = bytes(header) + bytes(blocks)
    return gzip.compress(raw) if gzipped else raw


class TestFindBlock:
    def test_locates_the_robot_block(self) -> None:
        raw = build_map()
        found = find_robot_position_block(raw)
        assert found is not None
        offset, length = found
        assert length == 12  # x, y, angle
        assert struct.unpack("<ii", raw[offset : offset + 8]) == (25727, 24232)

    def test_skips_the_charger_block_to_reach_the_robot(self) -> None:
        """The charger block is the same type shape and must not be mistaken."""
        raw = build_map(charger=(111, 222), robot=(333, 444))
        found = find_robot_position_block(raw)
        assert found is not None
        assert struct.unpack("<ii", raw[found[0] : found[0] + 8]) == (333, 444)

    def test_returns_none_without_a_robot_block(self) -> None:
        assert find_robot_position_block(build_map(robot=None)) is None

    def test_returns_none_for_empty_bytes(self) -> None:
        assert find_robot_position_block(b"") is None

    def test_returns_none_for_a_header_length_past_the_end(self) -> None:
        assert find_robot_position_block(b"\x00" * 4) is None

    def test_returns_none_for_a_truncated_block(self) -> None:
        raw = bytearray(build_map())
        del raw[MAP_HEADER_LENGTH + 10 :]
        assert find_robot_position_block(bytes(raw)) is None

    def test_returns_none_for_a_zero_length_block(self) -> None:
        """A zero data length would loop forever; it must be refused."""
        header = bytearray(MAP_HEADER_LENGTH)
        header[0x02:0x04] = struct.pack("<H", MAP_HEADER_LENGTH)
        block = bytearray(8)
        block[0:2] = struct.pack("<H", 8)
        block[2:4] = struct.pack("<H", 8)
        block[4:8] = struct.pack("<I", 0)
        assert find_robot_position_block(bytes(header) + bytes(block)) is None

    def test_tolerates_a_block_header_longer_than_eight(self) -> None:
        """The stride uses the header length's low byte, so it must match."""
        blocks = bytearray()
        header = bytearray(16)
        header[0:2] = struct.pack("<H", 2)
        header[2:4] = struct.pack("<H", 16)
        header[4:8] = struct.pack("<I", 4)
        blocks.extend(header)
        blocks.extend(b"\x00" * 4)

        robot = bytearray(8)
        robot[0:2] = struct.pack("<H", 8)
        robot[2:4] = struct.pack("<H", 8)
        robot[4:8] = struct.pack("<I", 12)
        blocks.extend(robot)
        blocks.extend(struct.pack("<iii", 11, 22, 33))

        head = bytearray(MAP_HEADER_LENGTH)
        head[0x02:0x04] = struct.pack("<H", MAP_HEADER_LENGTH)
        found = find_robot_position_block(bytes(head) + bytes(blocks))
        assert found is not None
        raw = bytes(head) + bytes(blocks)
        assert struct.unpack("<ii", raw[found[0] : found[0] + 8]) == (11, 22)


class TestPatch:
    def test_rewrites_the_position(self) -> None:
        raw = build_map()
        patched = patch_robot_position(raw, x=25688, y=28538)
        assert patched is not None
        assert read_robot_position(patched) == (25688.0, 28538.0)

    def test_leaves_every_other_byte_alone(self) -> None:
        """Only bytes inside the 8-byte position may change."""
        raw = build_map()
        found = find_robot_position_block(raw)
        assert found is not None
        offset, _ = found

        patched = patch_robot_position(raw, x=25688, y=28538)
        assert patched is not None
        assert len(patched) == len(raw)

        changed = [i for i, (a, b) in enumerate(zip(patched, raw)) if a != b]
        assert changed, "the position should have changed"
        span = range(offset, offset + 8)
        assert all(i in span for i in changed), (
            f"bytes outside the position span changed: "
            f"{[i for i in changed if i not in span]}"
        )

    def test_rewrites_all_four_bytes_of_each_axis(self) -> None:
        """High bytes matter too, so a large jump is written in full."""
        raw = build_map()
        patched = patch_robot_position(raw, x=70000, y=-40000)
        assert patched is not None
        assert read_robot_position(patched) == (70000.0, -40000.0)

        found = find_robot_position_block(raw)
        assert found is not None
        offset, _ = found
        changed = [
            i for i, (a, b) in enumerate(zip(patched, raw)) if a != b
        ]
        assert all(offset <= i < offset + 8 for i in changed)

    def test_preserves_the_angle(self) -> None:
        raw = build_map(robot_angle=90)
        found = find_robot_position_block(raw)
        assert found is not None
        patched = patch_robot_position(raw, x=1, y=2)
        assert patched is not None
        angle = struct.unpack("<i", patched[found[0] + 8 : found[0] + 12])[0]
        assert angle == 90

    def test_handles_negative_coordinates(self) -> None:
        raw = build_map()
        patched = patch_robot_position(raw, x=-1234, y=-5678)
        assert patched is not None
        assert read_robot_position(patched) == (-1234.0, -5678.0)

    def test_round_trips_a_gzipped_map(self) -> None:
        raw = build_map(gzipped=True)
        assert raw[:2] == b"\x1f\x8b"
        patched = patch_robot_position(raw, x=25688, y=28538)
        assert patched is not None
        assert patched[:2] == b"\x1f\x8b"
        assert read_robot_position(patched) == (25688.0, 28538.0)

    def test_returns_none_without_a_robot_block(self) -> None:
        assert patch_robot_position(build_map(robot=None), x=1, y=2) is None

    def test_returns_none_for_unparseable_bytes(self) -> None:
        assert patch_robot_position(b"not a map", x=1, y=2) is None


class TestReadPosition:
    def test_reads_the_carried_position(self) -> None:
        assert read_robot_position(build_map()) == (25727.0, 24232.0)

    def test_returns_none_without_a_block(self) -> None:
        assert read_robot_position(build_map(robot=None)) is None

    def test_returns_none_for_unparseable_bytes(self) -> None:
        assert read_robot_position(b"") is None


class TestRenderDecision:
    def test_keeps_the_image_when_the_marker_is_already_right(self) -> None:
        assert (
            render_position(
                drawn=(25688.0, 28538.0),
                resolved_x=25689.0,
                resolved_y=28537.0,
                trusted=True,
                from_dock=True,
            )
            is None
        )

    def test_redraws_when_a_docked_robot_is_drawn_elsewhere(self) -> None:
        """The exact live failure: docked, drawn in the living room."""
        result = render_position(
            drawn=(25727.0, 24232.0),
            resolved_x=25688.0,
            resolved_y=28538.0,
            trusted=True,
            from_dock=True,
        )
        assert result == (25688, 28538)

    def test_keeps_the_image_for_a_robot_that_is_not_docked(self) -> None:
        """Out cleaning, the payload is the truth and nothing is redrawn."""
        assert (
            render_position(
                drawn=(25727.0, 24232.0),
                resolved_x=(25727.0),
                resolved_y=(24232.0),
                trusted=True,
                from_dock=False,
            )
            is None
        )

    def test_keeps_the_image_when_the_position_is_not_trusted(self) -> None:
        """Nothing corroborates a better spot, so do not invent one."""
        assert (
            render_position(
                drawn=(25727.0, 24232.0),
                resolved_x=None,
                resolved_y=None,
                trusted=False,
                from_dock=False,
            )
            is None
        )

    def test_draws_when_nothing_was_drawn_but_the_place_is_known(self) -> None:
        assert render_position(
            drawn=None,
            resolved_x=25688.0,
            resolved_y=28538.0,
            trusted=True,
            from_dock=True,
        ) == (25688, 28538)

    def test_keeps_the_image_when_nothing_is_known_at_all(self) -> None:
        assert (
            render_position(
                drawn=None,
                resolved_x=None,
                resolved_y=None,
                trusted=True,
                from_dock=False,
            )
            is None
        )

    def test_tolerance_boundary_is_inside_is_kept(self) -> None:
        assert (
            render_position(
                drawn=(25688.0 + MODULE.SAME_POSITION_TOLERANCE, 28538.0),
                resolved_x=25688.0,
                resolved_y=28538.0,
                trusted=True,
                from_dock=True,
            )
            is None
        )

    def test_tolerance_boundary_just_outside_is_redrawn(self) -> None:
        assert render_position(
            drawn=(25688.0 + MODULE.SAME_POSITION_TOLERANCE + 1, 28538.0),
            resolved_x=25688.0,
            resolved_y=28538.0,
            trusted=True,
            from_dock=True,
        ) == (25688, 28538)

    def test_live_offset_is_far_outside_the_tolerance(self) -> None:
        offset = ((25727 - 25688) ** 2 + (24232 - 28538) ** 2) ** 0.5
        assert offset > 4000
        assert MODULE.SAME_POSITION_TOLERANCE < offset / 10

class FakeParsed:
    """Stand-in for the library's parsed map content."""

    def __init__(self, image: bytes | None) -> None:
        self.image_content = image


class TestRerender:
    def _parse(self, raw: bytes):
        """A parse stand-in that reports the position it was handed."""
        position = read_robot_position(raw)
        FakeParsed.last_position = position
        return FakeParsed(b"PNG:" + str(position).encode())

    def test_returns_none_when_no_redraw_is_needed(self) -> None:
        assert (
            rerender_map_image(
                raw=build_map(),
                drawn=(25727.0, 24232.0),
                resolved_x=25727.0,
                resolved_y=24232.0,
                trusted=True,
                from_dock=False,
                parse=self._parse,
            )
            is None
        )

    def test_redraws_and_reports_the_corrected_position(self) -> None:
        result = rerender_map_image(
            raw=build_map(),
            drawn=(25727.0, 24232.0),
            resolved_x=25688.0,
            resolved_y=28538.0,
            trusted=True,
            from_dock=True,
            parse=self._parse,
        )
        assert result is not None
        assert result.startswith(b"PNG:")
        assert FakeParsed.last_position == (25688.0, 28538.0)

    def test_returns_none_when_raw_bytes_are_missing(self) -> None:
        assert (
            rerender_map_image(
                raw=None,
                drawn=(25727.0, 24232.0),
                resolved_x=25688.0,
                resolved_y=28538.0,
                trusted=True,
                from_dock=True,
                parse=self._parse,
            )
            is None
        )

    def test_returns_none_when_the_map_has_no_robot_block(self) -> None:
        assert (
            rerender_map_image(
                raw=build_map(robot=None),
                drawn=(25727.0, 24232.0),
                resolved_x=25688.0,
                resolved_y=28538.0,
                trusted=True,
                from_dock=True,
                parse=self._parse,
            )
            is None
        )

    def test_returns_none_when_parsing_fails(self) -> None:
        """A map this cannot parse must not break image display."""

        def boom(_raw: bytes):
            raise ValueError("unexpected map format")

        assert (
            rerender_map_image(
                raw=build_map(),
                drawn=(25727.0, 24232.0),
                resolved_x=25688.0,
                resolved_y=28538.0,
                trusted=True,
                from_dock=True,
                parse=boom,
            )
            is None
        )

    def test_returns_none_when_the_parser_yields_no_image(self) -> None:
        assert (
            rerender_map_image(
                raw=build_map(),
                drawn=(25727.0, 24232.0),
                resolved_x=25688.0,
                resolved_y=28538.0,
                trusted=True,
                from_dock=True,
                parse=lambda _raw: FakeParsed(None),
            )
            is None
        )

    def test_returns_none_when_the_parser_yields_an_empty_image(self) -> None:
        assert (
            rerender_map_image(
                raw=build_map(),
                drawn=(25727.0, 24232.0),
                resolved_x=25688.0,
                resolved_y=28538.0,
                trusted=True,
                from_dock=True,
                parse=lambda _raw: FakeParsed(b""),
            )
            is None
        )

    def test_self_check_rejects_a_write_that_did_not_land(self) -> None:
        """If the patched bytes do not read back as intended, keep the image.

        The patch is verified before it is rendered, so a wrong block offset
        degrades to the library's own image instead of a corrupted map.
        """
        real_patch = MODULE.patch_robot_position

        def bad_patch(raw: bytes, *, x: int, y: int) -> bytes:
            return raw  # writes nothing

        MODULE.patch_robot_position = bad_patch
        try:
            assert (
                rerender_map_image(
                    raw=build_map(),
                    drawn=(25727.0, 24232.0),
                    resolved_x=25688.0,
                    resolved_y=28538.0,
                    trusted=True,
                    from_dock=True,
                    parse=self._parse,
                )
                is None
            )
        finally:
            MODULE.patch_robot_position = real_patch

    def test_self_check_catches_a_partially_applied_write(self) -> None:
        """An offset that lands on the wrong axis must also be refused."""
        real_patch = MODULE.patch_robot_position

        def half_patch(raw: bytes, *, x: int, y: int) -> bytes:
            return real_patch(raw, x=x, y=y + 1)

        MODULE.patch_robot_position = half_patch
        try:
            assert (
                rerender_map_image(
                    raw=build_map(),
                    drawn=(25727.0, 24232.0),
                    resolved_x=25688.0,
                    resolved_y=28538.0,
                    trusted=True,
                    from_dock=True,
                    parse=self._parse,
                )
                is None
            )
        finally:
            MODULE.patch_robot_position = real_patch

    def test_gzipped_input_round_trips_through_a_redraw(self) -> None:
        result = rerender_map_image(
            raw=build_map(gzipped=True),
            drawn=(25727.0, 24232.0),
            resolved_x=25688.0,
            resolved_y=28538.0,
            trusted=True,
            from_dock=True,
            parse=self._parse,
        )
        assert result is not None
        assert FakeParsed.last_position == (25688.0, 28538.0)

class TestMalformedLengthsAreRefused:
    """A malformed block length must be refused, not walked past.

    The walk advances by a block's data length plus the low byte of its header
    length, so a zero in either field either stalls the walk or makes the next
    "block" start inside another one. Skipping the check lets a malformed map
    present arbitrary bytes as a position and have them drawn.
    """

    def _map_with_leading_block(
        self, *, block_type: int, header_length: int, data_length: int
    ) -> bytes:
        """Build a map whose first block has the given (possibly bogus) lengths."""
        blocks = bytearray()
        header = bytearray(header_length)
        header[0:2] = struct.pack("<H", block_type)
        header[2:4] = struct.pack("<H", header_length)
        header[4:8] = struct.pack("<I", data_length)
        blocks.extend(header)
        blocks.extend(b"\x00" * max(data_length, 8))

        robot = bytearray(8)
        robot[0:2] = struct.pack("<H", 8)
        robot[2:4] = struct.pack("<H", 8)
        robot[4:8] = struct.pack("<I", 12)
        blocks.extend(robot)
        blocks.extend(struct.pack("<iii", 11, 22, 33))

        head = bytearray(MAP_HEADER_LENGTH)
        head[0x02:0x04] = struct.pack("<H", MAP_HEADER_LENGTH)
        return bytes(head) + bytes(blocks)

    def test_header_length_above_one_byte_is_refused(self) -> None:
        """The stride only reads the low byte, so a wider header is unreliable."""
        raw = self._map_with_leading_block(
            block_type=2, header_length=300, data_length=8
        )
        assert find_robot_position_block(raw) is None

    def test_zero_data_length_is_refused(self) -> None:
        raw = self._map_with_leading_block(
            block_type=2, header_length=8, data_length=0
        )
        assert find_robot_position_block(raw) is None

    def test_zero_header_length_is_refused(self) -> None:
        raw = self._map_with_leading_block(
            block_type=2, header_length=0, data_length=8
        )
        assert find_robot_position_block(raw) is None

    def test_a_valid_map_still_walks_past_a_leading_block(self) -> None:
        """Guards against the refusal above becoming a blanket rejection."""
        ok_blocks = bytearray()
        header = bytearray(8)
        header[0:2] = struct.pack("<H", 2)
        header[2:4] = struct.pack("<H", 8)
        header[4:8] = struct.pack("<I", 8)
        ok_blocks.extend(header)
        ok_blocks.extend(b"\x00" * 8)

        robot = bytearray(8)
        robot[0:2] = struct.pack("<H", 8)
        robot[2:4] = struct.pack("<H", 8)
        robot[4:8] = struct.pack("<I", 8)
        ok_blocks.extend(robot)
        ok_blocks.extend(struct.pack("<ii", 11, 22))

        head = bytearray(MAP_HEADER_LENGTH)
        head[0x02:0x04] = struct.pack("<H", MAP_HEADER_LENGTH)
        found = find_robot_position_block(bytes(head) + bytes(ok_blocks))
        assert found is not None


class TestDefensiveContracts:
    """Pins behaviour that today's callers cannot trigger but must still hold."""

    def test_untrusted_position_is_not_drawn_even_with_coordinates(self) -> None:
        """`trusted` alone decides, so a future caller cannot leak a value."""
        assert (
            render_position(
                drawn=(25727.0, 24232.0),
                resolved_x=25688.0,
                resolved_y=28538.0,
                trusted=False,
                from_dock=True,
            )
            is None
        )

    def test_a_robot_that_is_not_docked_is_never_redrawn(self) -> None:
        """Out cleaning, the payload is the truth even if the two differ."""
        assert (
            render_position(
                drawn=(1000.0, 2000.0),
                resolved_x=25688.0,
                resolved_y=28538.0,
                trusted=True,
                from_dock=False,
            )
            is None
        )
