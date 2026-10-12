"""Contract for views/login.ejs.

Invariants:
  - Email/username input present (at least one of input[type=email],
    input[name=email], input[name=username]).
  - Password input present.
  - CSRF token input present.
  - No hardcoded demo credentials visible in DOM:
    `admin@example.com`, `demo@`, `password123` as substring of source.

Every past MW build shipped with the placeholder login creds visible, which
then got pasted into the "forgot password" flow by accident.
"""

from __future__ import annotations

from cli.contracts import contains, contract


PATH = "views/login.ejs"


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
        from cli.contracts import ContractViolation

        msg = f"{PATH}: file missing"
        if coll is not None:
            coll.append(msg)
            return
        raise ContractViolation(msg)

    # At least one of email-style or username input.
    contract(
        ejs,
        'input[type="email"], input[name="email"], input[name="username"]',
        path=PATH,
        collector=coll,
    )
    contract(ejs, 'input[type="password"]', path=PATH, collector=coll)
    contract(ejs, 'input[name="_csrf"]', path=PATH, collector=coll)

    # Demo-cred blacklist. Use CSS for one selectable pattern and contains()
    # for the rest (substring-in-source is stronger than attr-only matching).
    contract(
        ejs,
        'input[value*="admin@example.com"]',
        absent=True,
        path=PATH,
        collector=coll,
    )
    contains(ejs, "admin@example.com", absent=True, path=PATH, collector=coll)
    contains(ejs, "demo@", absent=True, path=PATH, collector=coll)
    contains(ejs, "password123", absent=True, path=PATH, collector=coll)
