"""Re-render the map image when the robot's drawn position is not where it is.

The map PNG that Home Assistant serves has the robot marker *baked into it* by
`ImageGenerator.draw_map`, which reads `map_data.vacuum_position` at parse time.
The position services and the safe-zone entities go through
`v1_position_trust`, but the image does not: it keeps showing whatever the
payload said.

Measured on 2026-10-08, robot docked and charging: the white robot disc was
drawn at pixel (670, 1021) -- vacuum (25732, 24236), the living room -- while
the teal charger marker sat at (667, 677), vacuum (25688, 28538), which is where
the robot actually was. The two markers disagreed by 4306 units.

That matters beyond cosmetics. The map is how a person decides whether the robot
is clear of the cabinet door, so an image that places a docked robot in another
room argues for closing a door onto it.

There is no way to move a marker that is already drawn. `draw_map` paints onto
the existing raster, so calling it again would leave the stale disc in place and
add a second one. The fix is therefore to rewrite the `ROBOT_POSITION` block in
the raw map bytes and parse again, which redraws the whole image from corrected
input.

Everything here is pure byte handling: no Home Assistant, no I/O. Every function
returns None rather than raising when the input is not shaped as expected, so a
surprising map format degrades to the image Home Assistant would have produced
on its own instead of breaking map display.
"""

from __future__ import annotations

from typing import Any

# The Roborock block type that carries the robot's position.
ROBOT_POSITION_BLOCK_TYPE = 8

# Block header layout, from `RoborockMapDataParser.parse`.
_MAP_HEADER_LENGTH_OFFSET = 0x02
_BLOCK_TYPE_OFFSET = 0x00
_BLOCK_HEADER_LENGTH_OFFSET = 0x02
_BLOCK_DATA_LENGTH_OFFSET = 0x04

# Offsets inside a position block, from `_parse_object_position`.
_POSITION_X_OFFSET = 0x00
_POSITION_Y_OFFSET = 0x04

# A drawn marker that differs from the resolved position by less than this many
# vacuum units is the same place. Parked jitter measured 1-2 units.
SAME_POSITION_TOLERANCE = 50.0

# Blocks are walked by adding the data length to a header length that is stored
# as an int16 but consumed as a single byte for the stride. A header that claims
# to be longer than this cannot be walked safely.
_MAX_BLOCK_HEADER_LENGTH = 0xFF


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _gzip_unwrap(raw: bytes) -> bytes | None:
    """Return the map bytes, decompressing the gzip wrapper when present."""
    if raw[:2] == b"\x1f\x8b":
        import gzip

        try:
            return gzip.decompress(raw)
        except OSError:
            return None
    return raw


def find_robot_position_block(raw: bytes) -> tuple[int, int] | None:
    """Return (data offset, data length) of the robot position block.

    Returns None when the bytes do not parse as a map, which leaves the caller
    to serve the unmodified image.
    """
    if len(raw) < 4:
        return None
    header_length = int.from_bytes(
        raw[_MAP_HEADER_LENGTH_OFFSET : _MAP_HEADER_LENGTH_OFFSET + 2], "little"
    )
    if header_length <= 0 or header_length >= len(raw):
        return None

    position = header_length
    while position + 8 <= len(raw):
        block_type = int.from_bytes(
            raw[position + _BLOCK_TYPE_OFFSET : position + _BLOCK_TYPE_OFFSET + 2],
            "little",
        )
        block_header_length = int.from_bytes(
            raw[
                position
                + _BLOCK_HEADER_LENGTH_OFFSET : position
                + _BLOCK_HEADER_LENGTH_OFFSET
                + 2
            ],
            "little",
        )
        block_data_length = int.from_bytes(
            raw[
                position
                + _BLOCK_DATA_LENGTH_OFFSET : position
                + _BLOCK_DATA_LENGTH_OFFSET
                + 4
            ],
            "little",
        )
        if (
            block_header_length <= 0
            or block_header_length > _MAX_BLOCK_HEADER_LENGTH
            or block_data_length <= 0
        ):
            return None

        data_start = position + block_header_length
        if data_start + block_data_length > len(raw):
            return None

        if block_type == ROBOT_POSITION_BLOCK_TYPE:
            if block_data_length < 8:
                return None
            return data_start, block_data_length

        # The stride uses the header length's low byte, matching the parser.
        position += block_data_length + (block_header_length & 0xFF)

    return None


def patch_robot_position(raw: bytes, *, x: int, y: int) -> bytes | None:
    """Return the map bytes with the robot position block rewritten.

    Returns None when the block cannot be located, so the caller keeps the
    original image rather than guessing at a binary format.
    """
    unwrapped = _gzip_unwrap(raw)
    if unwrapped is None:
        return None

    found = find_robot_position_block(unwrapped)
    if found is None:
        return None
    data_start, _ = found

    patched = bytearray(unwrapped)
    patched[data_start + _POSITION_X_OFFSET : data_start + _POSITION_X_OFFSET + 4] = (
        int(x).to_bytes(4, "little", signed=True)
    )
    patched[data_start + _POSITION_Y_OFFSET : data_start + _POSITION_Y_OFFSET + 4] = (
        int(y).to_bytes(4, "little", signed=True)
    )

    if raw[:2] == b"\x1f\x8b":
        import gzip

        return gzip.compress(bytes(patched))
    return bytes(patched)


def read_robot_position(raw: bytes) -> tuple[float, float] | None:
    """Return the position carried by the map bytes, if it is readable."""
    unwrapped = _gzip_unwrap(raw)
    if unwrapped is None:
        return None
    found = find_robot_position_block(unwrapped)
    if found is None:
        return None
    data_start, _ = found
    x = int.from_bytes(unwrapped[data_start : data_start + 4], "little", signed=True)
    y = int.from_bytes(
        unwrapped[data_start + _POSITION_Y_OFFSET : data_start + _POSITION_Y_OFFSET + 4],
        "little",
        signed=True,
    )
    return float(x), float(y)


def render_position(
    *,
    drawn: tuple[float, float] | None,
    resolved_x: float | None,
    resolved_y: float | None,
    trusted: bool,
    from_dock: bool,
) -> tuple[int, int] | None:
    """Return the position the image should show, or None to keep the image.

    None means "the existing image is already right", which is the common case:
    a robot that is out cleaning is drawn where the payload says and nothing
    needs to change. A re-render happens only when the marker would otherwise
    contradict where the robot is.
    """
    if not trusted or resolved_x is None or resolved_y is None:
        # No better position is known, so the image is left alone. This is the
        # case where `v1_position_trust` refuses to answer: the entities go
        # unknown, and inventing a marker position here would be worse than
        # showing the payload's, because nothing corroborates it.
        return None

    if drawn is None:
        # Nothing was drawn, but the robot's place is known -- a docked robot
        # whose map carried no position block. Draw it.
        return int(resolved_x), int(resolved_y)

    if not from_dock:
        # The payload is trusted as-is for a robot that is not on its dock.
        return None

    offset = ((drawn[0] - resolved_x) ** 2 + (drawn[1] - resolved_y) ** 2) ** 0.5
    if offset <= SAME_POSITION_TOLERANCE:
        return None
    return int(resolved_x), int(resolved_y)


def rerender_map_image(
    *,
    raw: bytes | None,
    drawn: tuple[float, float] | None,
    resolved_x: float | None,
    resolved_y: float | None,
    trusted: bool,
    from_dock: bool,
    parse: Any,
) -> bytes | None:
    """Return a corrected PNG, or None to keep the image already in hand.

    `parse` is the callable that turns map bytes into an object with an
    `image_content` attribute -- the trait's own converter. It is injected
    rather than imported so this stays testable without Home Assistant.

    The patched bytes are read back and required to carry the intended
    position before the re-rendered image is returned. That self-check is what
    makes an unrecognised map format harmless: a wrong offset would show up as a
    mismatch here rather than as a corrupted map on screen.

    Every failure path returns None, so a map this does not recognise is served
    exactly as the library produced it.
    """
    target = render_position(
        drawn=drawn,
        resolved_x=resolved_x,
        resolved_y=resolved_y,
        trusted=trusted,
        from_dock=from_dock,
    )
    if target is None or raw is None:
        return None

    patched = patch_robot_position(raw, x=target[0], y=target[1])
    if patched is None:
        return None

    # Verify the write landed where it was meant to before trusting it.
    written = read_robot_position(patched)
    if written is None or (int(written[0]), int(written[1])) != target:
        return None

    try:
        reparsed = parse(patched)
    except Exception:  # noqa: BLE001 - any parse failure keeps the old image
        return None

    image = getattr(reparsed, "image_content", None)
    if not isinstance(image, bytes) or not image:
        return None
    return image
