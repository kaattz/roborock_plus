"""The brand images must match Home Assistant's spec, or the UI shows nothing.

Since HA 2026.3 a custom integration can ship its own brand images in a `brand/`
directory, and local images take priority over the brands CDN. This integration
shipped none, so the integration page used the generic placeholder.

The requirements are hard, not stylistic -- the brands specification states them
exactly:

    icon.png      256x256, 1:1, PNG, transparency preferred
    icon@2x.png   512x512
    logo.png      landscape, shortest side 128-256
    logo@2x.png   landscape, shortest side 256-512

A wrong size is not a cosmetic problem: the frontend requests these by name and
scales them, so an icon at the wrong aspect ratio comes out distorted and one in
the wrong format is simply not served.

The PNG header is parsed directly rather than through Pillow. The dimensions live
at fixed offsets in the IHDR chunk, so this needs no image library -- and adding
one to the test requirements to check four numbers would be a poor trade.
"""

from __future__ import annotations

import struct
from pathlib import Path

BRAND = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "roborock_plus"
    / "brand"
)

# PNG color type 6 is truecolour with alpha.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
COLOR_TYPE_RGBA = 6


def _png_header(path: Path) -> tuple[int, int, int, int]:
    """Return (width, height, bit_depth, colour_type) from the IHDR chunk."""
    data = path.read_bytes()
    assert data[:8] == PNG_SIGNATURE, f"{path.name} is not a PNG"

    length, chunk_type = struct.unpack(">I4s", data[8:16])
    assert chunk_type == b"IHDR", f"{path.name} has no IHDR first"
    assert length == 13, f"{path.name} has a malformed IHDR"

    width, height, bit_depth, colour_type = struct.unpack(">IIBB", data[16:26])
    return width, height, bit_depth, colour_type


def test_the_four_required_files_exist() -> None:
    """HA looks for these by name; a missing one falls back to a placeholder."""
    for name in ("icon.png", "icon@2x.png", "logo.png", "logo@2x.png"):
        assert (BRAND / name).is_file(), f"brand/{name} is missing"


def test_icons_are_square_at_the_specified_sizes() -> None:
    """Icon must be 1:1, 256 for the normal version and 512 for the hDPI one."""
    for name, expected in (("icon.png", 256), ("icon@2x.png", 512)):
        width, height, _, _ = _png_header(BRAND / name)
        assert (width, height) == (expected, expected), (
            f"brand/{name} is {width}x{height}; the spec requires "
            f"{expected}x{expected} and the UI scales it into a square"
        )


def test_logos_are_landscape_with_the_shortest_side_in_range() -> None:
    """Landscape, and the short side bounded as the spec states.

    An icon-shaped logo would leave the integration page's header mostly empty,
    and a too-small one is upscaled and looks soft.
    """
    for name, low, high in (
        ("logo.png", 128, 256),
        ("logo@2x.png", 256, 512),
    ):
        width, height, _, _ = _png_header(BRAND / name)
        shortest = min(width, height)
        assert width > height, f"brand/{name} is {width}x{height}, not landscape"
        assert low <= shortest <= high, (
            f"brand/{name} has a shortest side of {shortest}; the spec allows "
            f"{low}-{high}"
        )


def test_images_carry_an_alpha_channel() -> None:
    """Transparency is preferred, and the mark is drawn on a transparent ground.

    Opaque images would show a white square behind the mark wherever the theme
    puts it on a coloured surface.
    """
    for name in ("icon.png", "icon@2x.png", "logo.png", "logo@2x.png"):
        _, _, bit_depth, colour_type = _png_header(BRAND / name)
        assert colour_type == COLOR_TYPE_RGBA, (
            f"brand/{name} has colour type {colour_type}, not RGBA (6)"
        )
        assert bit_depth == 8, f"brand/{name} has bit depth {bit_depth}, not 8"


def test_the_generator_exists_so_the_assets_are_reproducible() -> None:
    """Binary assets with no source are unreviewable and unmaintainable.

    The script is the source of truth for these files; without it a future change
    would mean hand-editing PNGs.
    """
    generator = Path(__file__).resolve().parents[1] / "scripts" / "generate_brand_images.py"
    assert generator.is_file(), (
        "the brand images have no generator, so they cannot be regenerated or "
        "reviewed as a diff"
    )
    text = generator.read_text(encoding="utf-8")
    assert "brand" in text, "the generator does not mention where it writes"


def test_no_home_assistant_branding_is_reused() -> None:
    """The spec forbids it: it would imply the integration is official.

    Checked as a property of the generator's source, since the images themselves
    cannot be inspected for provenance. What it draws is stated in the file.
    """
    generator = Path(__file__).resolve().parents[1] / "scripts" / "generate_brand_images.py"
    text = generator.read_text(encoding="utf-8").lower()
    assert "home assistant's press" not in text
    for forbidden in ("home-assistant logo", "homeassistant logo"):
        assert forbidden not in text, f"the generator references {forbidden!r}"
