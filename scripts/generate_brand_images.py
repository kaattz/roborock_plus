"""Draw the integration's brand images.

Home Assistant 2026.3+ lets a custom integration ship its own brand images in a
`brand/` directory inside the integration folder; local images take priority over
the brands CDN. This integration has none, so the config-flow page and the
integration list showed the generic placeholder.

Sizes come from the brands repository's specification:

    icon.png      256x256, 1:1, PNG with transparency
    icon@2x.png   512x512
    logo.png      landscape, shortest side 128-256
    logo@2x.png   landscape, shortest side 256-512

The artwork is drawn here rather than taken from Roborock's press material: their
logo is their trademark, and shipping it in a third-party integration is the
owner's call, not something to slip in with a code change. What this draws is a
robot-vacuum mark with the mop/water motif the integration's own entities use
(see `icons.json`: `mdi:vacuum`, `mdi:water`, `mdi:spray-bottle`).

Run:  python scripts/generate_brand_images.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[1]
BRAND = REPO / "custom_components" / "roborock_plus" / "brand"

# Roborock's own palette is a light blue; this keeps the mark recognisable as the
# same family without reproducing their logo.
BODY = (33, 150, 243, 255)
BODY_DARK = (21, 101, 192, 255)
WATER = (129, 212, 250, 255)
ACCENT = (255, 255, 255, 255)


def _draw_icon(size: int) -> Image.Image:
    """A robot vacuum seen from above, with a water droplet.

    Drawn at 4x and downsampled: PIL has no anti-aliasing, so lines drawn at the
    final size come out visibly jagged, and the icon is displayed small.
    """
    scale = 4
    canvas = size * scale
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # A disc with a small margin. The spec asks for images trimmed of empty
    # space, and the first attempt at 1/12 of the canvas per side read as
    # noticeably small once the UI scaled it down to a list row.
    margin = canvas // 24
    body = (margin, margin, canvas - margin, canvas - margin)

    draw.ellipse(body, fill=BODY)
    # A darker rim gives the disc an edge against a white background without
    # needing a border, which would look heavy at 24px.
    draw.ellipse(body, outline=BODY_DARK, width=max(1, canvas // 64))

    # The lidar turret: a small circle sitting above centre.
    cx, cy = canvas // 2, canvas // 2
    turret_r = canvas // 9
    turret_y = cy - canvas // 14
    draw.ellipse(
        (
            cx - turret_r,
            turret_y - turret_r,
            cx + turret_r,
            turret_y + turret_r,
        ),
        fill=ACCENT,
        outline=BODY_DARK,
        width=max(1, canvas // 96),
    )

    # The water droplet, centred under the turret: the mop motif, and what
    # distinguishes this from a generic appliance at small sizes.
    #
    # Built the standard way -- a circle with a point on top -- with the point
    # widened by putting the triangle's base at the circle's centre and its sides
    # tangent-ish, so it reads as a droplet rather than a spike. Two earlier
    # attempts rendered as a cone and then as a spike with a bead under it.
    drop_w = canvas // 5
    drop_r = drop_w // 2
    drop_top = turret_y + turret_r + canvas // 18
    drop_cx = cx
    circle_cy = drop_top + drop_w

    draw.polygon(
        [
            (drop_cx, drop_top),
            (drop_cx - drop_r, circle_cy),
            (drop_cx + drop_r, circle_cy),
        ],
        fill=WATER,
    )
    draw.ellipse(
        (
            drop_cx - drop_r,
            circle_cy - drop_r,
            drop_cx + drop_r,
            circle_cy + drop_r,
        ),
        fill=WATER,
    )

    return image.resize((size, size), Image.LANCZOS)


def _draw_logo(width: int, height: int) -> Image.Image:
    """The icon beside a wordmark, on transparent ground.

    Drawn wide rather than square because the spec prefers a landscape logo and
    the integration list gives it horizontal room.

    The wordmark is real text in a system sans-serif, not the two rounded bars an
    earlier version used -- those rendered as an obvious placeholder. It is kept
    to the integration's own name so no third-party trademark is reproduced.
    """
    scale = 4
    image = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    canvas_h = height * scale
    mark_size = int(canvas_h * 0.80)
    mark = _draw_icon(mark_size)
    top = (canvas_h - mark_size) // 2
    left = int(canvas_h * 0.10)
    image.alpha_composite(mark, (left, top))

    text = "Roborock Plus"
    text_left = left + mark_size + int(canvas_h * 0.16)
    available = width * scale - text_left - int(canvas_h * 0.10)

    # Shrink to fit rather than overflow: the wordmark must fit the frame at
    # whichever size this is called with.
    font_size = int(canvas_h * 0.34)
    font = _load_font(font_size)
    while font_size > 8:
        box = draw.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= available:
            break
        font_size -= 2
        font = _load_font(font_size)

    box = draw.textbbox((0, 0), text, font=font)
    draw.text(
        (text_left, (canvas_h - (box[3] - box[1])) // 2 - box[1]),
        text,
        font=font,
        fill=BODY_DARK,
    )

    return image.resize((width, height), Image.LANCZOS)


def _load_font(size: int):
    """A system sans-serif at `size`, falling back to PIL's bitmap default.

    The default is only reached on a machine with none of these fonts; it is
    tiny and unscalable, so it would look wrong, but shipping a font file inside
    the integration to avoid that is not worth the licence questions.
    """
    from PIL import ImageFont

    candidates = (
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    )
    for path in candidates:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def main() -> int:
    BRAND.mkdir(parents=True, exist_ok=True)
    written = []

    for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
        path = BRAND / name
        _draw_icon(size).save(path, "PNG", optimize=True)
        written.append(path)

    for name, dims in (
        ("logo.png", (640, 256)),
        ("logo@2x.png", (1280, 512)),
    ):
        path = BRAND / name
        _draw_logo(*dims).save(path, "PNG", optimize=True)
        written.append(path)

    for path in written:
        with Image.open(path) as check:
            print(f"  {path.name:16} {check.size[0]}x{check.size[1]}  {check.mode}")
    print()
    print(f"wrote {len(written)} files to {BRAND.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
