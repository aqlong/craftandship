"""Contract for views/about.ejs.

Invariants:
  - The about-photo <img> src must not be a 1x1 inline SVG placeholder.
    FIX-O fallback regression: when about_photo_url was empty, the
    placeholder SVG shipped to customers instead of the stock photo.

Scope: .about-photo img and #about img only, so a decorative SVG inline
elsewhere on the page doesn't trip the contract. This surface is optional
- if about.ejs doesn't exist, silent early return.
"""

from __future__ import annotations

from cli.contracts import contract


PATH = "views/about.ejs"


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


def check(artifact, *, collect_all: bool = False, violations: list[str] | None = None) -> None:
    coll = violations if collect_all else None
    ejs = _get_file(artifact, PATH)
    if ejs is None:
        # Optional surface: silent early return.
        return

    # Ban 1x1 SVG placeholder in about-photo scope.
    contract(
        ejs,
        '.about-photo img[src^="data:image/svg"], #about img[src^="data:image/svg"]',
        absent=True,
        path=PATH,
        collector=coll,
    )
