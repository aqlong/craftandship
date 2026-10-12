"""Generate favicon.ico + favicon-16x16.png + favicon-32x32.png from the SVG mark.

Source of truth: web/public/favicon.svg (also at web/public/brand-harbor/favicon.svg).
We do not have cairosvg/rsvg in CI, so this re-implements the SVG primitives in
Pillow at a 256x256 master and downsamples. Re-run after editing the SVG.

Usage: python3 scripts/generate_favicon_ico.py
"""
from __future__ import annotations

import os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC = os.path.join(ROOT, "web", "public")

MASTER = 256  # render the SVG at this resolution, then downsample
SCALE = MASTER / 64.0  # SVG viewBox is 64x64


def s(x: float) -> int:
    return int(round(x * SCALE))


def draw_lighthouse(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Background: rounded rect 0,0,64,64 rx=4, fill #F1E4C9
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=int(round(4 * size / 64)),
                        fill="#F1E4C9")

    # Base: rect 19,50 22x6 #4A3A2C
    d.rectangle((s(19), s(50), s(19 + 22), s(50 + 6)), fill="#4A3A2C")

    # Trapezoid tower body: M 19 50 L 23 24 L 37 24 L 41 50 Z, fill #EAD4A6
    d.polygon([(s(19), s(50)), (s(23), s(24)), (s(37), s(24)), (s(41), s(50))],
              fill="#EAD4A6")

    # Top deck: rect 23,20 14x4, #3A2A1F
    d.rectangle((s(23), s(20), s(23 + 14), s(20 + 4)), fill="#3A2A1F")

    # Lantern housing: pill-shaped from y=11.5 to y=20, x=24..36 (12 wide).
    # SVG path: M 24 20 L 24 11.5 Q 24 7.5 30 6.5 Q 36 7.5 36 11.5 L 36 20 Z
    # Approximate as a rounded rectangle at (24, 6.5, 36, 20) with radius=6.
    d.rounded_rectangle((s(24), s(6.5), s(36), s(20)), radius=s(6) - s(0),
                        fill="#3A2A1F")

    # Lantern glass: rect 25,12.5 10x6.5 with radial gradient.
    # Approximate gradient with concentric ellipses.
    gx0, gy0 = s(25), s(12.5)
    gx1, gy1 = s(25 + 10), s(12.5 + 6.5)
    cx = (gx0 + gx1) // 2
    cy = (gy0 + gy1) // 2
    r_outer = max(gx1 - cx, gy1 - cy)
    # Paint the ring stops outside-in.
    stops = [
        (1.00, "#C68A3A"),
        (0.75, "#F0B048"),
        (0.35, "#FFE2A6"),
        (0.00, "#FFF6DC"),
    ]
    # Clip to the lantern glass rect via masking.
    glass_mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(glass_mask)
    md.rectangle((gx0, gy0, gx1, gy1), fill=255)
    glass = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glass)
    for frac, color in stops:
        rr = max(1, int(round(r_outer * (frac if frac > 0 else 0.05))))
        gd.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=color)
    img.paste(glass, (0, 0), mask=glass_mask)

    # Antenna: rect 29.6,3.5 0.8x3.4, #2F2A24
    d.rectangle((s(29.6), s(3.5), s(29.6 + 0.8), s(3.5 + 3.4)), fill="#2F2A24")

    # Antenna ball: circle (30, 3.2) r=0.7, #2F2A24
    cx2, cy2 = s(30), s(3.2)
    rr2 = max(1, s(0.7))
    d.ellipse((cx2 - rr2, cy2 - rr2, cx2 + rr2, cy2 + rr2), fill="#2F2A24")

    return img


def main() -> None:
    master = draw_lighthouse(MASTER)

    # Multi-resolution PNG resamples for crisp small sizes.
    sizes = [16, 32, 48]
    images = [master.resize((sz, sz), Image.Resampling.LANCZOS) for sz in sizes]

    # Write favicon.ico (multi-resolution).
    ico_path = os.path.join(PUBLIC, "favicon.ico")
    images[0].save(
        ico_path,
        format="ICO",
        sizes=[(sz, sz) for sz in sizes],
        append_images=images[1:],
    )
    print(f"wrote {ico_path}")

    # Write favicon-16x16.png + favicon-32x32.png.
    for sz, im in zip(sizes, images):
        if sz in (16, 32):
            png_path = os.path.join(PUBLIC, f"favicon-{sz}x{sz}.png")
            im.save(png_path, format="PNG", optimize=True)
            print(f"wrote {png_path}")


if __name__ == "__main__":
    main()
