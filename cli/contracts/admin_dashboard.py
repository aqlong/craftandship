"""Contract for views/admin/dashboard.ejs.

Invariants:
  - NO inline <form action="/admin/settings"> on the dashboard.
    Settings go through a CTA link, never an inline form.
  - A <a href="/admin/settings"> CTA link IS present so admins can reach
    settings with one click.

Optional file: if views/admin/dashboard.ejs is missing from artifact.files,
check() returns silently rather than flagging. The dashboard is expected to
exist on MW-like archetypes but is not mandatory for every generated app.
"""

from __future__ import annotations

from cli.contracts import contract


PATH = "views/admin/dashboard.ejs"


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

    contract(
        ejs,
        'form[action="/admin/settings"]',
        absent=True,
        path=PATH,
        collector=coll,
    )
    contract(ejs, 'a[href="/admin/settings"]', path=PATH, collector=coll)
