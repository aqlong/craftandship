"""compose.utils.brand_assets: favicon family + OG asset PIL pipeline.

Every v2 build emits, from a single brand-mark concept (per-customer monogram
in the primary palette color) the full favicon family browsers and OS
launchers expect, plus a multi-resolution favicon.ico for the legacy
`/favicon.ico` auto-fetch and a 180x180 apple-touch-icon for iOS home-screen.

PNG sizes emitted: 16, 32, 48, 64, 96, 128, 180, 192, 256, 512.
favicon.ico bundles 16, 32, 48, 64, 128, 256.

For builds where the customer has uploaded a brand-mark image (e.g. an
admin-set `nav_logo_url`) we expose `crop_to_brand_mark()` which auto-crops
whitespace via threshold + dilation (the MW pipeline pattern: threshold > 40
+ 3px max-filter dilation to skip JPEG noise). The default v2 path renders
the auto-generated initial monogram disc directly with PIL primitives
(byte-identical across rebuilds, no font dependency for the disc itself).

All functions return None when Pillow is not importable so callers can
graceful-degrade. The Railway worker image carries Pillow >= 10 so this
always emits in production.
"""

from __future__ import annotations

import io
import re
from typing import List, Optional, Tuple


# Public PNG size set. 16/32/48 are the desktop-favicon classics; 64/96
# cover modern Chrome/Edge tab faces and Android Chrome; 128 is a common
# Linux desktop launcher size; 180 is iOS apple-touch-icon; 192/512 are
# PWA manifest standards; 256 covers Windows pinned tiles.
PNG_SIZES: Tuple[int, ...] = (16, 32, 48, 64, 96, 128, 180, 192, 256, 512)
# favicon.ico subset: only sizes that legacy / RSS reader stacks use.
ICO_SIZES: Tuple[int, ...] = (16, 32, 48, 64, 128, 256)


def _hex_to_rgb(hex_str: str, fallback: tuple = (17, 17, 17)) -> tuple:
    """Tolerant `#rrggbb` / `#rgb` → RGB tuple parser."""
    if not hex_str or not isinstance(hex_str, str):
        return fallback
    s = hex_str.strip().lstrip("#")
    try:
        if len(s) == 3:
            return (int(s[0] * 2, 16), int(s[1] * 2, 16), int(s[2] * 2, 16))
        if len(s) == 6:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        pass
    return fallback


def initials_from_site_name(site_name: str) -> str:
    """Extract 1-2 letter monogram. Skips non-alphanumeric "words" so
    "Wren & Rye" -> "WR" (not "W&"). "Fern" -> "FE". Empty -> "•".
    Mirrors compose/build.py:_logo_initials_from_site_name."""
    words = [w for w in re.split(r"\s+", site_name or "") if w and w[0].isalnum()]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    if words:
        return (words[0][:2] if len(words[0]) >= 2 else words[0][:1]).upper()
    return "•"


def _load_font(paths, size):
    """Try a list of TrueType paths, fall back to PIL's bundled default."""
    from PIL import ImageFont
    for p in paths:
        try:
            return ImageFont.truetype(p, size=size)
        except (OSError, IOError):
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _font_paths(is_serif: bool):
    serif = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
        "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
        "/Library/Fonts/Georgia.ttf",
    ]
    sans = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    return serif if is_serif else sans


def render_brand_mark_png(
    size_px: int,
    site_name: str,
    palette_vars: dict,
    is_serif: bool = False,
) -> Optional[bytes]:
    """Render the per-customer brand mark (initial-monogram disc) as a
    square PNG of `size_px`. Returns PNG bytes or None when PIL is missing.

    Visual: rounded-rect (rx scales with size) of `--color-primary`, with
    site initials in `--color-on-primary` centered. Tiny sizes (≤32px)
    use a square (no corner radius) so the monogram stays legible at
    16x16 in browser tab faces."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    primary = _hex_to_rgb(palette_vars.get("--color-primary", "#111111"), (17, 17, 17))
    on_primary = _hex_to_rgb(
        palette_vars.get("--color-on-primary", "#ffffff"), (255, 255, 255)
    )

    img = Image.new("RGBA", (size_px, size_px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background plate. Rounded corners scale with size; <= 32px squares so
    # the monogram fills more of the visible pixels at tab-face scale.
    if size_px <= 32:
        draw.rectangle([0, 0, size_px, size_px], fill=primary)
    else:
        radius = max(2, int(size_px * 0.18))
        try:
            draw.rounded_rectangle(
                [0, 0, size_px, size_px], radius=radius, fill=primary
            )
        except AttributeError:
            # Older Pillow (<8.2): fall back to plain rectangle.
            draw.rectangle([0, 0, size_px, size_px], fill=primary)

    # Drawn as pixels by Pillow, so the text is used as-is. (It used to be
    # XML-escaped here, which is wrong for a raster context: a name like
    # "A&B" was drawn as the literal text "A&amp;".)
    initials = initials_from_site_name(site_name)

    # Font sizing: 60% of plate height for 1 letter, 48% for 2 letters.
    # Capped to a minimum of 8px so PIL has something to rasterize at 16x16.
    if len(initials) == 1:
        font_size = max(8, int(size_px * 0.62))
    else:
        font_size = max(8, int(size_px * 0.48))
    font = _load_font(_font_paths(is_serif), font_size)

    if font is not None:
        try:
            l, t, r, b = draw.textbbox((0, 0), initials, font=font)
            tw, th = r - l, b - t
        except Exception:
            tw, th = font_size * len(initials), font_size
            l, t = 0, 0
        # textbbox accounts for the glyph's actual ink box, including the
        # ascender offset; subtract the bbox origin so the glyph's visual
        # center lands on the plate's geometric center.
        x = (size_px - tw) // 2 - l
        # Slight optical lift (~6%) so initials read as centered against
        # the glyph's visual baseline rather than its full em-box.
        y = (size_px - th) // 2 - t - max(1, int(size_px * 0.04))
        draw.text((x, y), initials, fill=on_primary, font=font)

    # Flatten alpha onto the primary background so JPEG ICO converters
    # (Pillow's PNG-encoded ICO writer is fine, but this also lets the
    # apple-touch-icon ship as opaque RGB which iOS prefers).
    out = Image.new("RGB", (size_px, size_px), primary)
    out.paste(img, (0, 0), img)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_favicon_ico(
    site_name: str,
    palette_vars: dict,
    is_serif: bool = False,
    sizes: Tuple[int, ...] = ICO_SIZES,
) -> Optional[bytes]:
    """Render a multi-resolution favicon.ico bundling each size in `sizes`.
    Returns ICO bytes or None when PIL is missing.

    Pillow's ICO writer handles the multi-image container natively. Sizes
    are validated by the ICO spec to be ≤ 256; we cap at 256 in ICO_SIZES."""
    try:
        from PIL import Image
    except ImportError:
        return None

    # Render the largest size first and downsample to populate the ICO
    # pages: Pillow's `sizes=` arg does this automatically when the
    # base image is at least the largest requested size.
    largest = max(sizes)
    base_png = render_brand_mark_png(
        largest, site_name, palette_vars, is_serif=is_serif
    )
    if base_png is None:
        return None
    base = Image.open(io.BytesIO(base_png))
    buf = io.BytesIO()
    base.save(
        buf,
        format="ICO",
        sizes=[(s, s) for s in sorted(sizes)],
    )
    return buf.getvalue()


def crop_to_brand_mark(
    image_bytes: bytes,
    threshold: int = 40,
    dilation_px: int = 3,
) -> Optional[bytes]:
    """Auto-crop a customer-uploaded brand mark to its content bbox.

    Pipeline: detect non-background pixels (luma > threshold OR alpha > 0
    when present), dilate the mask by `dilation_px` to absorb JPEG noise +
    anti-aliasing, take bbox, crop. Returns PNG bytes of the cropped image
    or None when PIL is missing OR the input has no detectable mark
    (entirely-uniform image).

    Mirrors the MW redesign pipeline (threshold > 40 + 3px max-filter
    dilation). Used when a customer-uploaded brand image needs to be
    centered before downsizing into the favicon set."""
    try:
        from PIL import Image, ImageFilter
    except ImportError:
        return None
    try:
        src = Image.open(io.BytesIO(image_bytes))
    except Exception:
        return None

    # Detect which mask basis to use:
    #   - If the source has a meaningful alpha channel (PNG with real
    #     transparency, e.g. white-on-transparent brand marks), foreground
    #     = alpha > threshold.
    #   - Otherwise (opaque JPEG/PNG with whitespace, the MW pipeline
    #     anchor), foreground = pixel darker than (255 - threshold).
    rgba = src.convert("RGBA")
    _, _, _, a = rgba.split()
    a_min, a_max = a.getextrema()
    if a_max <= threshold:
        # Fully (or near-fully) transparent: nothing visible.
        return None
    alpha_has_variation = (a_max - a_min) > threshold
    if alpha_has_variation:
        fg = a.point(lambda v: 255 if v > threshold else 0)
    else:
        luma = rgba.convert("L")
        # Foreground = darker than (255 - threshold) so a JPEG-noise floor
        # of ~10/255 doesn't register as content on near-white backgrounds.
        fg = luma.point(lambda v: 255 if v < (255 - threshold) else 0)

    # Dilate to absorb JPEG noise + anti-aliasing artifacts. MaxFilter's
    # `size` arg is the diameter of the kernel; pass an odd number ≥ 3.
    if dilation_px > 0:
        fg = fg.filter(ImageFilter.MaxFilter(2 * dilation_px + 1))
    bbox = fg.getbbox()
    if not bbox:
        return None
    cropped = rgba.crop(bbox)
    buf = io.BytesIO()
    cropped.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def compose_favicon_assets(
    site_name: str,
    palette_vars: dict,
    is_serif: bool = False,
) -> List[Tuple[str, bytes]]:
    """Compose the full favicon family + apple-touch-icon + favicon.ico.

    Returns a list of (relative-path, bytes) tuples ready for compose's
    file-emit pipeline. Skips entries when a PNG render fails (PIL
    missing); callers should treat the empty result as graceful degrade.

    Path layout (matches MW redesign reference):
      public/brand/logo-<size>.png   (one per PNG_SIZES)
      public/apple-touch-icon.png    (180x180 alias for iOS launcher fetch)
      public/favicon.ico             (multi-res ICO at the canonical /favicon.ico path)
    """
    out: List[Tuple[str, bytes]] = []
    for size in PNG_SIZES:
        png = render_brand_mark_png(
            size, site_name, palette_vars, is_serif=is_serif
        )
        if png is None:
            return []  # PIL missing: bail entirely
        out.append((f"public/brand/logo-{size}.png", png))
    # iOS / Safari fetches /apple-touch-icon.png by default. Emit a 180px
    # alias at the public root so it succeeds without an explicit <link>.
    apple = render_brand_mark_png(
        180, site_name, palette_vars, is_serif=is_serif
    )
    if apple is not None:
        out.append(("public/apple-touch-icon.png", apple))
    ico = render_favicon_ico(site_name, palette_vars, is_serif=is_serif)
    if ico is not None:
        out.append(("public/favicon.ico", ico))
    return out
