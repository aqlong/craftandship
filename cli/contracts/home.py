"""Contract for views/home.ejs.

Invariants:
  - No live <iframe src*="youtube.com"> embeds. MW regression v12: YouTube
    must be click-to-play, never rendered on page load.
  - No <a href="/register"> unless /register is mounted in server.js.
    The "Sign up to create yours" CTA regression shipped for v13.
  - No inline <form action="/admin/settings"> on the home page.
"""

from __future__ import annotations

from cli.contracts import contract


PATH = "views/home.ejs"


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


def _route_mounted(artifact, path: str) -> bool:
    """Return True if server.js mounts the given path.

    Detection: substring search for the canonical mount patterns
    `app.use('<path>'`, `app.get('<path>'`, `app.post('<path>'` (both
    quote styles supported).
    """
    server = _get_file(artifact, "server.js") or ""
    if not server:
        return False
    patterns = (
        f"app.use('{path}'", f'app.use("{path}"',
        f"app.get('{path}'", f'app.get("{path}"',
        f"app.post('{path}'", f'app.post("{path}"',
    )
    return any(pat in server for pat in patterns)


def check(artifact, *, collect_all: bool = False, violations: list[str] | None = None) -> None:
    coll = violations if collect_all else None
    ejs = _get_file(artifact, PATH)
    if ejs is None:
        from cli.contracts import ContractViolation

        msg = f"{PATH}: file missing"
        if coll is not None:
            coll.append(msg)
            return
        raise ContractViolation(msg)

    contract(
        ejs,
        'iframe[src*="youtube.com"]',
        absent=True,
        path=PATH,
        collector=coll,
    )

    if not _route_mounted(artifact, "/register"):
        contract(
            ejs,
            'a[href="/register"]',
            absent=True,
            path=PATH,
            collector=coll,
        )

    contract(
        ejs,
        'form[action="/admin/settings"]',
        absent=True,
        path=PATH,
        collector=coll,
    )
