"""The Open Graph preview images carry text, and that text is a claim.

`scripts/generate_og_images.py` draws a title and a subtitle onto each
`web/public/og-*.png`. A link shared to Slack or iMessage shows only that
image, so the copy has to say what the page itself says: a website is built
from a library of tested building blocks and every website is reviewed before
it goes live. It must not say who builds or reviews a site, that a person does
it by hand, or use the internal word for a customer.

The images cannot be read back as text, so these tests pin the strings the
script draws, the shape of the files it writes, and the one behavior that
could silently drop a claim (a subtitle too long for the card).
"""
import ast
import importlib.util
import re
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "generate_og_images.py"
PUBLIC = ROOT / "web" / "public"
OG_IMAGES_TS = ROOT / "web" / "src" / "services" / "og-images.ts"

EM_DASH = chr(0x2014)

# Claims the product copy no longer makes, and the internal vocabulary.
RETIRED = re.compile(
    r"by hand|handcrafted|hand-built|keeper|personally|designs and builds|"
    r"designs, builds|reviews every|reviewed by",
    re.IGNORECASE,
)


def _og_pages():
    """OG_PAGES read from the script's source, so no font or Pillow is needed."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "OG_PAGES":
            return ast.literal_eval(node.value)
    raise AssertionError("OG_PAGES not found in generate_og_images.py")


PAGES = {filename: (title, subtitle) for filename, title, subtitle in _og_pages()}


def _png_chunks(path: Path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    pos, chunks, size = 8, [], None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        name = data[pos + 4:pos + 8].decode("ascii")
        if name == "IHDR":
            size = struct.unpack(">II", data[pos + 8:pos + 16])
        chunks.append(name)
        pos += 12 + length
    return size, chunks


@pytest.mark.parametrize("filename", sorted(PAGES))
def test_no_card_makes_a_retired_claim(filename):
    title, subtitle = PAGES[filename]
    assert not RETIRED.search(title), f"{filename} title: {title!r}"
    assert not RETIRED.search(subtitle), f"{filename} subtitle: {subtitle!r}"
    assert EM_DASH not in title + subtitle


def test_the_cards_that_describe_how_a_site_is_made_say_what_the_pages_say():
    assert PAGES["og-companion.png"][0] == "Meet Haven, your companion"
    how = PAGES["og-how-it-works.png"][1]
    assert "built from tested building blocks" in how
    assert "Every website needs a recorded review approval before it goes live." in how
    assert "tested building blocks" in PAGES["og-gallery.png"][1]
    assert "each needs a recorded review approval before it goes live" in PAGES["og-gallery.png"][1]
    assert PAGES["og-about.png"][1] == "For healers, therapists, and event producers."
    assert PAGES["og-about-ocean.png"][1].startswith("Founder of Craft & Ship")


def test_the_script_draws_exactly_the_images_the_route_map_serves():
    served = set(re.findall(r"'/(og-[a-z-]+\.png)'", OG_IMAGES_TS.read_text(encoding="utf-8")))
    assert set(PAGES) == served


@pytest.mark.parametrize("filename", sorted(PAGES))
def test_each_image_is_1200_by_630_with_no_metadata_chunks(filename):
    size, chunks = _png_chunks(PUBLIC / filename)
    assert size == (1200, 630)
    assert chunks[0] == "IHDR" and chunks[-1] == "IEND"
    assert set(chunks) <= {"IHDR", "IDAT", "IEND"}, chunks


def _load_script():
    pytest.importorskip("PIL")
    spec = importlib.util.spec_from_file_location("generate_og_images_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_subtitle_too_long_for_the_card_is_refused_not_cut_off(tmp_path):
    module = _load_script()
    module.PUBLIC = str(tmp_path)
    too_long = " ".join(["Every website is reviewed before it goes live."] * 12)
    with pytest.raises(ValueError, match="subtitle wraps to"):
        module._compose_card("og-test.png", "A title", too_long)
    assert not (tmp_path / "og-test.png").exists()


def test_a_subtitle_that_fits_is_drawn(tmp_path):
    module = _load_script()
    module.PUBLIC = str(tmp_path)
    module._compose_card("og-test.png", "A title", "A short subtitle.")
    size, chunks = _png_chunks(tmp_path / "og-test.png")
    assert size == (1200, 630)
    assert set(chunks) <= {"IHDR", "IDAT", "IEND"}
