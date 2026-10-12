# cli/validate.py - Spec validation before build
#
# Validates a JSON app spec against the v0.1 schema used by the Craft & Ship
# platform.  All validation is pure Python with no I/O side effects; callers
# can safely invoke validate_spec() in tests without any mocking.

import json
import sys
from pathlib import Path


SPEC_VERSION = "0.1"

# Allowed field types in data model definitions.
#
# Extended in section-02-foundation of the B+C rewrite to the full 14-type
# widget-aware set. Sections 03/04 introduce the widget registry that maps
# each of these to a concrete input renderer (text_input, enum_select,
# image_upload_zone, datetime_input, etc.). coordinator._finalize_spec
# continues to pass field types through unchanged, so any spec that validates
# today continues to validate; the new additions just become accepted inputs.
VALID_FIELD_TYPES = {
    "text", "string", "integer", "float", "boolean",
    "timestamp", "datetime",
    "email", "password",
    "image_url", "url",
    "enum", "json", "foreign_key",
}

# Allowed action triggers.
VALID_TRIGGERS = {"form_submit", "button_click", "scheduled"}


def validate_spec(spec):
    """Validate a spec dict against the v0.1 schema.

    Returns a list of error strings. Empty list means valid.
    """
    errors = []

    # Top-level required fields
    required_top = ["spec_version", "app_name", "description", "user_roles", "pages", "data_models", "actions", "settings"]
    for field in required_top:
        if field not in spec:
            errors.append(f"Missing top-level field: {field}")

    if errors:
        return errors

    # Version check
    if spec["spec_version"] != SPEC_VERSION:
        errors.append(f"Unsupported spec_version: {spec['spec_version']} (expected {SPEC_VERSION})")

    # App name format
    app_name = spec["app_name"]
    if not app_name or not all(char.isalnum() or char == "-" for char in app_name):
        errors.append(f"app_name must be kebab-case (letters, numbers, hyphens): got '{app_name}'")

    # User roles
    if not spec["user_roles"]:
        errors.append("user_roles must not be empty")

    role_names = set()
    for index, role in enumerate(spec["user_roles"]):
        prefix = f"user_roles[{index}]"
        for field in ["role", "description", "can"]:
            if field not in role:
                errors.append(f"{prefix}: missing field '{field}'")
        if "role" in role:
            if role["role"] in role_names:
                errors.append(f"{prefix}: duplicate role name '{role['role']}'")
            role_names.add(role["role"])
        if "can" in role and not role["can"]:
            errors.append(f"{prefix}: 'can' list must not be empty")

    # Pages
    if not spec["pages"]:
        errors.append("pages must not be empty")

    page_paths = set()
    for index, page in enumerate(spec["pages"]):
        prefix = f"pages[{index}]"
        for field in ["path", "title", "purpose", "auth_required", "allowed_roles"]:
            if field not in page:
                errors.append(f"{prefix}: missing field '{field}'")
        if "path" in page:
            if not page["path"].startswith("/"):
                errors.append(f"{prefix}: path must start with /")
            if page["path"] in page_paths:
                errors.append(f"{prefix}: duplicate path '{page['path']}'")
            page_paths.add(page["path"])
        if "allowed_roles" in page and "auth_required" in page:
            if page["auth_required"] and not page["allowed_roles"]:
                errors.append(f"{prefix}: auth_required is true but allowed_roles is empty")
            for role in page.get("allowed_roles", []):
                if role not in role_names:
                    errors.append(f"{prefix}: allowed_role '{role}' not defined in user_roles")

    # Data models
    if not spec["data_models"]:
        errors.append("data_models must not be empty")

    model_names = set()
    for index, model in enumerate(spec["data_models"]):
        prefix = f"data_models[{index}]"
        for field in ["name", "fields"]:
            if field not in model:
                errors.append(f"{prefix}: missing field '{field}'")
        if "name" in model:
            if model["name"] in model_names:
                errors.append(f"{prefix}: duplicate model name '{model['name']}'")
            model_names.add(model["name"])
        if "fields" in model:
            if not model["fields"]:
                errors.append(f"{prefix}: fields must not be empty")
            field_names = set()
            for field_index, field in enumerate(model["fields"]):
                field_prefix = f"{prefix}.fields[{field_index}]"
                for key in ["name", "type", "required"]:
                    if key not in field:
                        errors.append(f"{field_prefix}: missing '{key}'")
                if "name" in field:
                    if field["name"] in field_names:
                        errors.append(f"{field_prefix}: duplicate field name '{field['name']}'")
                    field_names.add(field["name"])
                if "type" in field and field["type"] not in VALID_FIELD_TYPES:
                    errors.append(f"{field_prefix}: invalid type '{field['type']}' (valid: {', '.join(sorted(VALID_FIELD_TYPES))})")

    # Validate belongs_to / has_many references
    for index, model in enumerate(spec["data_models"]):
        prefix = f"data_models[{index}]"
        belongs_to = model.get("belongs_to")
        if belongs_to:
            # Normalize: belongs_to can be a string, comma-separated string, or list
            if isinstance(belongs_to, str):
                # Handle comma-separated strings like "User,InventoryItem"
                belongs_to = [r.strip() for r in belongs_to.split(",") if r.strip()]
            if isinstance(belongs_to, list):
                for ref in belongs_to:
                    if not isinstance(ref, str):
                        errors.append(f"{prefix}: belongs_to contains non-string value: {ref}")
                    else:
                        # Split on comma in case list elements are also comma-separated
                        sub_refs = [r.strip() for r in ref.split(",") if r.strip()]
                        for sub_ref in sub_refs:
                            if sub_ref not in model_names and sub_ref != "User":
                                errors.append(f"{prefix}: belongs_to references unknown model '{sub_ref}'")
            else:
                errors.append(f"{prefix}: belongs_to must be a string or list of strings")
        has_many = model.get("has_many")
        if has_many:
            # Normalize: same comma-separated handling as belongs_to
            if isinstance(has_many, str):
                has_many = [r.strip() for r in has_many.split(",") if r.strip()]
            if isinstance(has_many, list):
                for ref in has_many:
                    if not isinstance(ref, str):
                        errors.append(f"{prefix}: has_many contains non-string value: {ref}")
                    else:
                        sub_refs = [r.strip() for r in ref.split(",") if r.strip()]
                        for sub_ref in sub_refs:
                            if sub_ref not in model_names:
                                errors.append(f"{prefix}: has_many references unknown model '{sub_ref}'")
            else:
                errors.append(f"{prefix}: has_many must be a string or list of strings")

    # Actions
    for index, action in enumerate(spec["actions"]):
        prefix = f"actions[{index}]"
        for field in ["name", "description", "trigger", "role", "steps"]:
            if field not in action:
                errors.append(f"{prefix}: missing field '{field}'")
        if "trigger" in action and action["trigger"] not in VALID_TRIGGERS:
            errors.append(f"{prefix}: invalid trigger '{action['trigger']}' (valid: {', '.join(sorted(VALID_TRIGGERS))})")
        if "role" in action and action["role"] not in role_names:
            errors.append(f"{prefix}: role '{action['role']}' not defined in user_roles")
        if "steps" in action and not action["steps"]:
            errors.append(f"{prefix}: steps must not be empty")

    # Settings
    settings = spec.get("settings", {})
    if "auth" not in settings:
        errors.append("settings: missing 'auth' field")
    if "seed_data" not in settings:
        errors.append("settings: missing 'seed_data' field")

    # Design (optional)
    import re
    _HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
    _VALID_STYLES = {
        "modern light", "modern dark", "clean minimal",
        "warm earthy", "bold vibrant", "corporate professional",
    }
    _STYLE_ALIASES = {
        "modern": "modern light",
        "warm": "warm earthy",
        "bold": "bold vibrant",
        "minimal": "clean minimal",
        "playful": "bold vibrant",
        "corporate": "corporate professional",
        "dark": "modern dark",
        "light": "modern light",
    }
    design = spec.get("design")
    if design is not None:
        if not isinstance(design, dict):
            errors.append("design: must be an object")
        else:
            for color_field in ["primary_color", "secondary_color", "accent_color"]:
                val = design.get(color_field)
                if val is not None and not _HEX_COLOR_RE.match(str(val)):
                    errors.append(f"design.{color_field}: must be a hex color (e.g. #2563eb), got '{val}'")
            style = design.get("style")
            if style is not None and style not in _VALID_STYLES:
                normalized = _STYLE_ALIASES.get(style)
                if normalized:
                    design["style"] = normalized
                else:
                    errors.append(f"design.style: must be one of {sorted(_VALID_STYLES)}, got '{style}'")

            # Warn (not reject) if font_display/font_body uses a banned font
            _BANNED_FONTS = {"Inter", "Roboto", "Open Sans", "Lato", "Arial", "Helvetica"}
            for font_field in ["font_display", "font_body"]:
                font_val = design.get(font_field)
                if font_val and font_val in _BANNED_FONTS:
                    import logging
                    logging.getLogger(__name__).warning(
                        "design.%s uses banned font '%s'; will be substituted at build time",
                        font_field, font_val,
                    )

    return errors


def load_and_validate(filepath):
    """Load a spec JSON file and validate it. Returns (spec, errors)."""
    path = Path(filepath)
    if not path.exists():
        return None, [f"File not found: {filepath}"]
    if not path.suffix == ".json":
        return None, [f"Expected .json file, got: {path.suffix}"]

    try:
        with open(path) as file:
            spec = json.load(file)
    except json.JSONDecodeError as error:
        return None, [f"Invalid JSON: {error}"]

    errors = validate_spec(spec)
    return spec, errors


def main():
    """CLI entry point: validate a spec file."""
    if len(sys.argv) != 2:
        print("Usage: python cli/validate.py <spec.json>")
        sys.exit(1)

    filepath = sys.argv[1]
    spec, errors = load_and_validate(filepath)

    if errors:
        print(f"INVALID - {len(errors)} error(s):\n")
        for error in errors:
            print(f"  - {error}")
        sys.exit(1)

    print(f"VALID - {spec['app_name']} (spec v{spec['spec_version']})")
    print(f"  {len(spec['user_roles'])} roles, {len(spec['pages'])} pages, {len(spec['data_models'])} models, {len(spec['actions'])} actions")


if __name__ == "__main__":
    main()
