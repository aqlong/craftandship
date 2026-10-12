"""Contract for views/layout.ejs.

Invariants:
  - <nav> landmark present (brand nav).
  - <main> landmark present (a11y skip target).
  - Brand element (logo <img> OR word-mark span/h1) inside the nav.
  - U+2014 EM DASH is banned in brand nav text. U+2013 EN DASH,
    U+2012 FIGURE DASH, U+2015 HORIZONTAL BAR are allowed in content.

This is CLAUDE.md code-style rule #2 lifted into a contract.
"""

from __future__ import annotations

from cli.contracts import ContractViolation, _parse_ejs, contract


PATH = "views/layout.ejs"


def _get_file(artifact, path: str) -> str | None:
    files = getattr(artifact, "files", None)
    if files is None:
        return None
    if isinstance(files, dict):
        val = files.get(path)
    else:
        val = None
        for item in files:
            if item and item[0] == path:
                val = item[1]
                break
    if val is None:
        return None
    if isinstance(val, bytes):
        try:
            return val.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return val


def _append_or_raise(msg: str, coll: list[str] | None) -> None:
    if coll is not None:
        coll.append(msg)
        return
    raise ContractViolation(msg)


def check(artifact, *, collect_all: bool = False, violations: list[str] | None = None) -> None:
    coll = violations if collect_all else None
    ejs = _get_file(artifact, PATH)
    if ejs is None:
        _append_or_raise(f"{PATH}: file missing", coll)
        return

    contract(ejs, "nav", path=PATH, collector=coll)
    contract(ejs, "main", path=PATH, collector=coll)

    soup = _parse_ejs(ejs)
    nav_list = soup.select("nav")
    if not nav_list:
        # The contract(nav) above already flagged this; skip further nav
        # inspection to avoid duplicate noise.
        return

    # Brand element: logo img or word-mark (span/h1/div) inside first nav.
    first_nav = nav_list[0]
    brand_candidates = first_nav.select(
        "img, .brand, .logo, .wordmark, h1, span.site-name, a.brand"
    )
    if not brand_candidates:
        _append_or_raise(
            f"{PATH}: <nav> missing brand element (logo img or word-mark) - "
            "expected at least one, got none",
            coll,
        )

    # U+2014 em-dash check in brand nav text.
    nav_text = first_nav.get_text() or ""
    if "\u2014" in nav_text:
        _append_or_raise(
            f"{PATH}: U+2014 em-dash present in <nav> text - expected "
            "absent (CLAUDE.md code style bans em dashes), got present",
            coll,
        )
