"""Contract for views/admin/<slug>/new.ejs + edit.ejs.

For every writable data model:
  - new.ejs must exist in artifact.files.
  - edit.ejs must exist in artifact.files.
  - new.ejs has <form action="/admin/<slug>" method="POST"> (literal URL).
  - edit.ejs contains the /admin/<slug>/ URL prefix (literal, since the
    <%= item.id %> suffix breaks CSS attribute-value matching) AND has
    <form method="POST">.
  - Both include CSRF input.
  - For every image field: .upload-zone[data-field=...] present and no
    orphan <input type="file" name=...> without the paired hidden input.

Read-only models are flagged if they have new/edit files present.
"""

from __future__ import annotations

from cli.contracts import ContractViolation, _parse_ejs, contains, contract


def _model_is_read_only(model: dict) -> bool:
    """Detect read-only models under both spec conventions."""
    access = str(model.get("access", "")).lower()
    if access in {"read-only", "readonly", "ro"}:
        return True
    if model.get("read_only"):
        return True
    perms = model.get("permissions")
    if isinstance(perms, str) and perms.lower() in {"read", "view", "read-only"}:
        return True
    return False


def _slug_from_model(model: dict) -> str:
    """Derive URL slug for a model. Mirrors cli.widgets.forms._slug_from_model."""
    explicit = str(model.get("slug") or "").strip().lower()
    if explicit:
        return explicit
    name = str(model.get("name") or "").strip()
    if not name:
        return ""
    parts: list[str] = []
    cur = ""
    for ch in name:
        if ch.isupper() and cur:
            parts.append(cur.lower())
            cur = ch
        else:
            cur += ch
    if cur:
        parts.append(cur.lower())
    slug = "-".join(parts) if parts else name.lower()
    if slug.endswith("y") and len(slug) > 1 and slug[-2] not in "aeiou":
        return slug[:-1] + "ies"
    if slug.endswith(("ch", "sh", "ss")) or slug[-1:] in ("s", "x", "z"):
        return slug + "es"
    return slug + "s"


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


def _image_fields_for(model: dict) -> list[str]:
    """Pick out image fields that require an upload-zone.

    Only fields explicitly typed as `image_url` in the spec qualify here.
    Name-based heuristics (e.g. a field named `image_url` with `type=text`
    in the canonical MW spec) do NOT trigger the CRUD-layer upload-zone
    requirement because widgets render those as plain textareas today and
    the conversion to an upload zone happens later via
    `_convert_raw_file_inputs_to_upload_zones`. A stricter contract here
    would flag every MW CRUD build as a violation.

    When the spec eventually migrates image_url field types to `image_url`
    (vs `text`), the widget will emit upload zones directly and this
    contract enforces the shape.
    """
    names: list[str] = []
    for field in model.get("fields") or []:
        if not isinstance(field, dict):
            continue
        ftype = str(field.get("type", "")).lower()
        fname = field.get("name")
        if not isinstance(fname, str):
            continue
        if ftype == "image_url":
            names.append(fname)
    return names


def _check_one_model(artifact, model: dict, coll: list[str] | None) -> None:
    slug = _slug_from_model(model)
    if not slug:
        return
    new_path = f"views/admin/{slug}/new.ejs"
    edit_path = f"views/admin/{slug}/edit.ejs"

    if _model_is_read_only(model):
        # Flag if either new/edit file is present for a read-only model.
        for p in (new_path, edit_path):
            if _get_file(artifact, p) is not None:
                _append_or_raise(
                    f"{p}: file present for read-only model {model.get('name')!r} - "
                    "expected absent, got present",
                    coll,
                )
        return

    # Writable: both files must exist.
    new_ejs = _get_file(artifact, new_path)
    edit_ejs = _get_file(artifact, edit_path)
    if new_ejs is None:
        _append_or_raise(f"{new_path}: file missing", coll)
    if edit_ejs is None:
        _append_or_raise(f"{edit_path}: file missing", coll)

    if new_ejs is not None:
        contract(
            new_ejs,
            f'form[action="/admin/{slug}"][method="POST"]',
            path=new_path,
            collector=coll,
        )
        contract(new_ejs, 'input[name="_csrf"]', path=new_path, collector=coll)
        _check_image_widgets(new_ejs, new_path, model, coll)

    if edit_ejs is not None:
        contains(edit_ejs, f"/admin/{slug}/", path=edit_path, collector=coll)
        contract(edit_ejs, 'form[method="POST"]', path=edit_path, collector=coll)
        contract(edit_ejs, 'input[name="_csrf"]', path=edit_path, collector=coll)
        _check_image_widgets(edit_ejs, edit_path, model, coll)


def _check_image_widgets(
    ejs: str,
    path: str,
    model: dict,
    coll: list[str] | None,
) -> None:
    """For each image field in the model, upload-zone present and no orphan
    <input type="file"> without a paired hidden input of the same name."""
    fields = _image_fields_for(model)
    if not fields:
        return
    soup = _parse_ejs(ejs)
    # Collect hidden input names once for the orphan-file-input check.
    hidden_names = {
        inp.get("name")
        for inp in soup.select('input[type="hidden"]')
        if inp.get("name")
    }
    for name in fields:
        contract(
            ejs,
            (
                f'.upload-zone[data-field="{name}"], '
                f'.image-upload-zone[data-target="{name}"]'
            ),
            path=path,
            collector=coll,
        )
        # Orphan file-input detection: <input type="file" name="<name>">
        # that is NOT paired with a <input type="hidden" name="<name>">.
        file_inputs = soup.select(f'input[type="file"][name="{name}"]')
        if file_inputs and name not in hidden_names:
            _append_or_raise(
                f"{path}: orphan <input type=\"file\" name=\"{name}\"> "
                "without paired hidden input - expected upload-zone pattern, "
                "got raw file input",
                coll,
            )


def check(artifact, *, collect_all: bool = False, violations: list[str] | None = None) -> None:
    """Iterate spec.data_models and validate CRUD view invariants."""
    coll = violations if collect_all else None
    spec = getattr(artifact, "spec", None) or {}
    for model in spec.get("data_models") or []:
        if not isinstance(model, dict):
            continue
        _check_one_model(artifact, model, coll)
