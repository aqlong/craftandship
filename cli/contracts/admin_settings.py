"""Contract for views/admin/settings.ejs.

Invariants (all derived from past MW regressions):
  - Form action is literal "/admin/settings" method="POST" exactly once.
  - CSRF token input present.
  - For every image_url field in spec.settings:
      .upload-zone[data-field="<name>"] must exist AND no raw
      <input type="url|text|file" name="<name>"> exists.
  - No snake_case <label> text (e.g. <label>hero_image_url</label>).
  - Helper declarations _v( and _has( present in top 400 chars.
"""

from __future__ import annotations

import re

from cli.contracts import contains, contract


PATH = "views/admin/settings.ejs"


_IMAGE_KEY_SUFFIXES = ("_image_url", "_photo_url", "_logo_url")
_IMAGE_KEY_BARE = {"image_url", "photo_url", "logo_url"}


def _is_image_key(key: str) -> bool:
    return key in _IMAGE_KEY_BARE or key.endswith(_IMAGE_KEY_SUFFIXES)


def _image_keys_from_spec(spec: dict) -> list[str]:
    """Detect image fields in the spec settings, defensive on shape.

    Supports:
      - spec['settings']: dict of key -> value (heuristic: image-suffix key).
      - spec['settings']: dict of key -> {"type": "image_url", ...}.
      - spec['_seed_settings']: list of {"key": ..., "value": ...}.
    """
    keys: list[str] = []
    seen: set[str] = set()

    settings = spec.get("settings") if isinstance(spec, dict) else None
    if isinstance(settings, dict):
        for key, value in settings.items():
            if not isinstance(key, str):
                continue
            if isinstance(value, dict) and value.get("type") == "image_url":
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
                continue
            if _is_image_key(key) and key not in seen:
                seen.add(key)
                keys.append(key)

    for item in spec.get("_seed_settings") or []:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not isinstance(key, str):
            continue
        if _is_image_key(key) and key not in seen:
            seen.add(key)
            keys.append(key)

    return keys


# Snake-case label text: one or more lowercase words joined by underscores.
# Requires at least one underscore so plain-English single words don't trip.
_SNAKE_LABEL_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")


def _get_file(artifact, path: str) -> str | None:
    """Return file content (str) or None if missing. Handles dict or list shape."""
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
    """Run all assertions for views/admin/settings.ejs."""
    coll = violations if collect_all else None
    ejs = _get_file(artifact, PATH)
    if ejs is None:
        msg = f"{PATH}: file missing"
        if coll is not None:
            coll.append(msg)
            return
        from cli.contracts import ContractViolation

        raise ContractViolation(msg)

    # 1. Literal form action exactly once.
    contract(
        ejs,
        'form[action="/admin/settings"][method="POST"]',
        count=1,
        path=PATH,
        collector=coll,
    )

    # 2. CSRF input present.
    contract(ejs, 'input[name="_csrf"]', path=PATH, collector=coll)

    # 3. Image-field assertions. The widget emits:
    #       <div class="image-upload-zone" data-target="<name>" ...>
    #       <input type="hidden" name="<name>" value="<%= _v(...) %>">
    # so the contract selector must target .image-upload-zone[data-target=...]
    # (not data-field=...) and the absent-raw-input check must exempt the
    # paired hidden input - a raw <input type="url|text|file"> for the
    # field is still a regression.
    spec = getattr(artifact, "spec", None) or {}
    for name in _image_keys_from_spec(spec):
        contract(
            ejs,
            (
                f'.upload-zone[data-field="{name}"], '
                f'.image-upload-zone[data-target="{name}"]'
            ),
            path=PATH,
            collector=coll,
        )
        for bad_type in ("url", "text", "file"):
            contract(
                ejs,
                f'input[name="{name}"][type="{bad_type}"]',
                absent=True,
                path=PATH,
                collector=coll,
            )

    # 4. No snake_case label text.
    from cli.contracts import _parse_ejs

    soup = _parse_ejs(ejs)
    for label in soup.select("label"):
        text = (label.get_text() or "").strip()
        if text and _SNAKE_LABEL_RE.match(text):
            msg = f"{PATH}: snake_case label text {text!r} - expected plain English, got snake_case"
            if coll is not None:
                coll.append(msg)
            else:
                from cli.contracts import ContractViolation

                raise ContractViolation(msg)

    # 5. Helper declarations present in top 400 chars. Accept both the
    # invocation form (`_v(`, `_has(`) and the widget-emitted declaration
    # form (`_v = function`, `_has = function`) since the widget template
    # uses the latter. Either one satisfies the helper-presence contract.
    head = ejs[:400]
    has_v = "_v(" in head or "_v =" in head
    if not has_v:
        contains(head, "_v(", path=PATH, collector=coll)
    has_has = "_has(" in head or "_has =" in head
    if not has_has:
        contains(head, "_has(", path=PATH, collector=coll)
