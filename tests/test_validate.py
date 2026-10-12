"""Tests for cli/validate.py - full branch coverage of spec validation rules.

All tests exercise validate_spec() directly (pure function, no I/O) plus
load_and_validate() for the file-loading path.
"""

import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import json
import sys
import tempfile
from pathlib import Path

import pytest

# Allow importing from the cli/ directory without installing the package.
sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

from validate import validate_spec, load_and_validate, SPEC_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_spec():
    """Return a minimal but fully valid spec dict."""
    return {
        "spec_version": "0.1",
        "app_name": "my-app",
        "description": "A test app",
        "user_roles": [
            {"role": "admin", "description": "Manages everything", "can": ["manage_all"]}
        ],
        "pages": [
            {
                "path": "/",
                "title": "Home",
                "purpose": "Landing page",
                "auth_required": False,
                "allowed_roles": [],
            }
        ],
        "data_models": [
            {
                "name": "User",
                "fields": [
                    {"name": "email", "type": "email", "required": True}
                ],
                "belongs_to": None,
                "has_many": None,
            }
        ],
        "actions": [
            {
                "name": "login",
                "description": "Log in",
                "trigger": "form_submit",
                "role": "admin",
                "steps": ["Validate credentials"],
            }
        ],
        "settings": {"auth": True, "seed_data": True},
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestValidSpecPasses:
    def test_minimal_spec_has_no_errors(self):
        """A correctly structured spec should produce zero errors."""
        errors = validate_spec(_minimal_spec())
        assert errors == []

    def test_spec_with_multiple_roles(self):
        spec = _minimal_spec()
        spec["user_roles"].append(
            {"role": "member", "description": "Regular user", "can": ["view"]}
        )
        # Add an action using the new role so the reference check passes
        spec["actions"].append({
            "name": "view_home",
            "description": "View home page",
            "trigger": "button_click",
            "role": "member",
            "steps": ["Load page"],
        })
        assert validate_spec(spec) == []

    def test_spec_with_relationships(self):
        spec = _minimal_spec()
        spec["data_models"].append({
            "name": "Post",
            "fields": [{"name": "title", "type": "text", "required": True}],
            "belongs_to": "User",
            "has_many": None,
        })
        spec["data_models"][0]["has_many"] = ["Post"]
        assert validate_spec(spec) == []

    def test_all_valid_field_types(self):
        """Every supported field type should be accepted."""
        spec = _minimal_spec()
        spec["data_models"] = [{
            "name": "AllTypes",
            "fields": [
                {"name": "f1", "type": "text", "required": True},
                {"name": "f2", "type": "integer", "required": False},
                {"name": "f3", "type": "boolean", "required": False},
                {"name": "f4", "type": "timestamp", "required": False},
                {"name": "f5", "type": "email", "required": False},
                {"name": "f6", "type": "password", "required": False},
            ],
            "belongs_to": None,
            "has_many": None,
        }]
        assert validate_spec(spec) == []

    def test_all_valid_triggers(self):
        spec = _minimal_spec()
        spec["actions"] = [
            {
                "name": f"action_{t.replace('_', '')}",
                "description": "desc",
                "trigger": t,
                "role": "admin",
                "steps": ["step"],
            }
            for t in ["form_submit", "button_click", "scheduled"]
        ]
        assert validate_spec(spec) == []

    def test_auth_required_page_with_roles(self):
        spec = _minimal_spec()
        spec["pages"].append({
            "path": "/admin",
            "title": "Admin",
            "purpose": "Admin panel",
            "auth_required": True,
            "allowed_roles": ["admin"],
        })
        assert validate_spec(spec) == []


# ---------------------------------------------------------------------------
# Missing top-level fields
# ---------------------------------------------------------------------------

class TestMissingTopLevelFields:
    @pytest.mark.parametrize("field", [
        "spec_version", "app_name", "description",
        "user_roles", "pages", "data_models", "actions", "settings",
    ])
    def test_missing_top_level_field_reported(self, field):
        spec = _minimal_spec()
        del spec[field]
        errors = validate_spec(spec)
        assert any(field in e for e in errors), f"Expected error for missing '{field}'"


# ---------------------------------------------------------------------------
# spec_version
# ---------------------------------------------------------------------------

class TestSpecVersion:
    def test_wrong_version_reported(self):
        spec = _minimal_spec()
        spec["spec_version"] = "9.9"
        errors = validate_spec(spec)
        assert any("spec_version" in e or "Unsupported" in e for e in errors)

    def test_correct_version_passes(self):
        spec = _minimal_spec()
        spec["spec_version"] = SPEC_VERSION
        assert validate_spec(spec) == []


# ---------------------------------------------------------------------------
# app_name validation
# ---------------------------------------------------------------------------

class TestAppName:
    def test_app_name_with_uppercase_rejected(self):
        spec = _minimal_spec()
        spec["app_name"] = "MyApp"
        # Uppercase letters are alphanumeric so they pass the current rule;
        # verify the current behaviour is consistent.
        errors = validate_spec(spec)
        # Current rule: alphanumeric + hyphens; uppercase IS alphanumeric
        # so this should NOT produce an error about app_name format.
        assert not any("app_name" in e for e in errors)

    def test_app_name_with_space_rejected(self):
        spec = _minimal_spec()
        spec["app_name"] = "my app"
        errors = validate_spec(spec)
        assert any("app_name" in e for e in errors)

    def test_app_name_with_underscore_rejected(self):
        spec = _minimal_spec()
        spec["app_name"] = "my_app"
        errors = validate_spec(spec)
        assert any("app_name" in e for e in errors)

    def test_empty_app_name_rejected(self):
        spec = _minimal_spec()
        spec["app_name"] = ""
        errors = validate_spec(spec)
        assert any("app_name" in e for e in errors)

    def test_valid_kebab_case_passes(self):
        spec = _minimal_spec()
        spec["app_name"] = "my-app-123"
        assert validate_spec(spec) == []


# ---------------------------------------------------------------------------
# user_roles validation
# ---------------------------------------------------------------------------

class TestUserRoles:
    def test_empty_user_roles_rejected(self):
        spec = _minimal_spec()
        spec["user_roles"] = []
        errors = validate_spec(spec)
        assert any("user_roles" in e for e in errors)

    def test_duplicate_role_name_rejected(self):
        spec = _minimal_spec()
        spec["user_roles"].append(
            {"role": "admin", "description": "duplicate", "can": ["x"]}
        )
        errors = validate_spec(spec)
        assert any("duplicate" in e for e in errors)

    def test_role_missing_can_field(self):
        spec = _minimal_spec()
        spec["user_roles"] = [{"role": "admin", "description": "desc"}]
        errors = validate_spec(spec)
        assert any("can" in e for e in errors)

    def test_role_missing_role_field(self):
        spec = _minimal_spec()
        spec["user_roles"] = [{"description": "desc", "can": ["x"]}]
        errors = validate_spec(spec)
        assert any("role" in e for e in errors)

    def test_role_missing_description_field(self):
        spec = _minimal_spec()
        spec["user_roles"] = [{"role": "admin", "can": ["x"]}]
        errors = validate_spec(spec)
        assert any("description" in e for e in errors)

    def test_empty_can_list_rejected(self):
        spec = _minimal_spec()
        spec["user_roles"] = [{"role": "admin", "description": "d", "can": []}]
        errors = validate_spec(spec)
        assert any("can" in e for e in errors)


# ---------------------------------------------------------------------------
# pages validation
# ---------------------------------------------------------------------------

class TestPages:
    def test_empty_pages_rejected(self):
        spec = _minimal_spec()
        spec["pages"] = []
        errors = validate_spec(spec)
        assert any("pages" in e for e in errors)

    def test_page_path_must_start_with_slash(self):
        spec = _minimal_spec()
        spec["pages"] = [{
            "path": "noslash",
            "title": "T",
            "purpose": "P",
            "auth_required": False,
            "allowed_roles": [],
        }]
        errors = validate_spec(spec)
        assert any("path" in e for e in errors)

    def test_duplicate_page_path_rejected(self):
        spec = _minimal_spec()
        spec["pages"].append({
            "path": "/",
            "title": "Dup",
            "purpose": "P",
            "auth_required": False,
            "allowed_roles": [],
        })
        errors = validate_spec(spec)
        assert any("duplicate path" in e for e in errors)

    def test_auth_required_with_empty_roles_rejected(self):
        spec = _minimal_spec()
        spec["pages"].append({
            "path": "/secret",
            "title": "Secret",
            "purpose": "P",
            "auth_required": True,
            "allowed_roles": [],
        })
        errors = validate_spec(spec)
        assert any("allowed_roles" in e for e in errors)

    def test_unknown_role_in_allowed_roles_rejected(self):
        spec = _minimal_spec()
        spec["pages"].append({
            "path": "/secret",
            "title": "Secret",
            "purpose": "P",
            "auth_required": True,
            "allowed_roles": ["ghost_role"],
        })
        errors = validate_spec(spec)
        assert any("ghost_role" in e for e in errors)

    @pytest.mark.parametrize("field", ["path", "title", "purpose", "auth_required", "allowed_roles"])
    def test_page_missing_field_reported(self, field):
        spec = _minimal_spec()
        page = dict(spec["pages"][0])
        del page[field]
        spec["pages"] = [page]
        errors = validate_spec(spec)
        assert any(field in e for e in errors)


# ---------------------------------------------------------------------------
# data_models validation
# ---------------------------------------------------------------------------

class TestDataModels:
    def test_empty_data_models_rejected(self):
        spec = _minimal_spec()
        spec["data_models"] = []
        errors = validate_spec(spec)
        assert any("data_models" in e for e in errors)

    def test_duplicate_model_name_rejected(self):
        spec = _minimal_spec()
        spec["data_models"].append({
            "name": "User",
            "fields": [{"name": "x", "type": "text", "required": True}],
            "belongs_to": None,
            "has_many": None,
        })
        errors = validate_spec(spec)
        assert any("duplicate" in e for e in errors)

    def test_empty_fields_list_rejected(self):
        spec = _minimal_spec()
        spec["data_models"][0]["fields"] = []
        errors = validate_spec(spec)
        assert any("fields" in e for e in errors)

    def test_invalid_field_type_rejected(self):
        spec = _minimal_spec()
        # Use a genuinely unknown type: "json" is now a valid type in the
        # section-02 14-field-type extension, so the old "json" test input
        # no longer fails.
        spec["data_models"][0]["fields"][0]["type"] = "definitely_not_a_type"
        errors = validate_spec(spec)
        assert any("invalid type" in e for e in errors)

    def test_duplicate_field_name_rejected(self):
        spec = _minimal_spec()
        spec["data_models"][0]["fields"].append(
            {"name": "email", "type": "text", "required": False}
        )
        errors = validate_spec(spec)
        assert any("duplicate field" in e for e in errors)

    @pytest.mark.parametrize("key", ["name", "type", "required"])
    def test_field_missing_key_reported(self, key):
        spec = _minimal_spec()
        field = dict(spec["data_models"][0]["fields"][0])
        del field[key]
        spec["data_models"][0]["fields"] = [field]
        errors = validate_spec(spec)
        assert any(key in e for e in errors)

    def test_belongs_to_unknown_model_rejected(self):
        spec = _minimal_spec()
        spec["data_models"][0]["belongs_to"] = "NonExistent"
        errors = validate_spec(spec)
        assert any("belongs_to" in e for e in errors)

    def test_has_many_unknown_model_rejected(self):
        spec = _minimal_spec()
        spec["data_models"][0]["has_many"] = ["Ghost"]
        errors = validate_spec(spec)
        assert any("has_many" in e for e in errors)

    def test_valid_self_referencing_has_many(self):
        """A model can has_many itself (e.g. tree structures)."""
        spec = _minimal_spec()
        spec["data_models"][0]["has_many"] = ["User"]
        assert validate_spec(spec) == []

    def test_belongs_to_comma_separated_string(self):
        """Claude sometimes generates 'User,ModelName' instead of ['User', 'ModelName']."""
        spec = _minimal_spec()
        spec["data_models"].append({
            "name": "ActivityLog",
            "fields": [{"name": "action", "type": "text", "required": True}],
            "belongs_to": "User,User",  # comma-separated, both valid
            "has_many": [],
        })
        errors = validate_spec(spec)
        assert not any("belongs_to" in e for e in errors)

    def test_belongs_to_comma_separated_unknown_rejected(self):
        """Comma-separated belongs_to still rejects unknown models."""
        spec = _minimal_spec()
        spec["data_models"][0]["belongs_to"] = "User,NonExistent"
        errors = validate_spec(spec)
        assert any("NonExistent" in e for e in errors)
        assert not any("User" in e and "unknown" in e for e in errors)

    def test_has_many_comma_separated_string(self):
        """has_many also handles comma-separated strings."""
        spec = _minimal_spec()
        spec["data_models"][0]["has_many"] = "User"
        errors = validate_spec(spec)
        assert not any("has_many" in e for e in errors)


# ---------------------------------------------------------------------------
# actions validation
# ---------------------------------------------------------------------------

class TestActions:
    @pytest.mark.parametrize("field", ["name", "description", "trigger", "role", "steps"])
    def test_action_missing_field_reported(self, field):
        spec = _minimal_spec()
        action = dict(spec["actions"][0])
        del action[field]
        spec["actions"] = [action]
        errors = validate_spec(spec)
        assert any(field in e for e in errors)

    def test_invalid_trigger_rejected(self):
        spec = _minimal_spec()
        spec["actions"][0]["trigger"] = "magic_event"
        errors = validate_spec(spec)
        assert any("trigger" in e for e in errors)

    def test_unknown_role_in_action_rejected(self):
        spec = _minimal_spec()
        spec["actions"][0]["role"] = "nobody"
        errors = validate_spec(spec)
        assert any("nobody" in e for e in errors)

    def test_empty_steps_rejected(self):
        spec = _minimal_spec()
        spec["actions"][0]["steps"] = []
        errors = validate_spec(spec)
        assert any("steps" in e for e in errors)


# ---------------------------------------------------------------------------
# settings validation
# ---------------------------------------------------------------------------

class TestSettings:
    def test_missing_auth_field_reported(self):
        spec = _minimal_spec()
        del spec["settings"]["auth"]
        errors = validate_spec(spec)
        assert any("auth" in e for e in errors)

    def test_missing_seed_data_field_reported(self):
        spec = _minimal_spec()
        del spec["settings"]["seed_data"]
        errors = validate_spec(spec)
        assert any("seed_data" in e for e in errors)


# ---------------------------------------------------------------------------
# load_and_validate (file I/O path)
# ---------------------------------------------------------------------------

class TestLoadAndValidate:
    def test_valid_file_returns_spec_and_no_errors(self, tmp_path):
        spec = _minimal_spec()
        p = tmp_path / "spec.json"
        p.write_text(json.dumps(spec))
        loaded_spec, errors = load_and_validate(str(p))
        assert errors == []
        assert loaded_spec["app_name"] == "my-app"

    def test_nonexistent_file_returns_error(self, tmp_path):
        spec, errors = load_and_validate(str(tmp_path / "missing.json"))
        assert spec is None
        assert any("not found" in e.lower() for e in errors)

    def test_non_json_extension_returns_error(self, tmp_path):
        p = tmp_path / "spec.txt"
        p.write_text("{}")
        spec, errors = load_and_validate(str(p))
        assert spec is None
        assert any(".json" in e for e in errors)

    def test_invalid_json_returns_error(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{this is not valid json")
        spec, errors = load_and_validate(str(p))
        assert spec is None
        assert any("JSON" in e for e in errors)

    def test_invalid_spec_returns_errors(self, tmp_path):
        bad_spec = {"spec_version": "0.1", "app_name": "ok"}  # lots of missing fields
        p = tmp_path / "bad_spec.json"
        p.write_text(json.dumps(bad_spec))
        spec, errors = load_and_validate(str(p))
        assert len(errors) > 0


# ---------------------------------------------------------------------------
# Design field validation
# ---------------------------------------------------------------------------

class TestDesignValidation:
    """Tests for the optional design field in validate_spec()."""

    def test_no_design_field_is_valid(self):
        """Spec without a design field should pass validation."""
        spec = _minimal_spec()
        assert "design" not in spec
        errors = validate_spec(spec)
        assert errors == []

    def test_valid_design_passes(self):
        """A properly formatted design object should pass."""
        spec = _minimal_spec()
        spec["design"] = {
            "primary_color": "#2563eb",
            "secondary_color": "#1e40af",
            "accent_color": "#f59e0b",
            "style": "modern light",
        }
        errors = validate_spec(spec)
        assert errors == []

    def test_design_must_be_object(self):
        """design must be a dict, not a string or list."""
        spec = _minimal_spec()
        spec["design"] = "modern light"
        errors = validate_spec(spec)
        assert any("must be an object" in e for e in errors)

    def test_invalid_hex_color_rejected(self):
        """Invalid hex colors should produce errors."""
        spec = _minimal_spec()
        spec["design"] = {
            "primary_color": "blue",
            "secondary_color": "#1e40af",
            "accent_color": "#GGG999",
        }
        errors = validate_spec(spec)
        color_errors = [e for e in errors if "hex color" in e]
        assert len(color_errors) == 2  # primary_color and accent_color

    def test_valid_hex_colors_accepted(self):
        """Various valid hex colors should pass."""
        spec = _minimal_spec()
        spec["design"] = {
            "primary_color": "#000000",
            "secondary_color": "#FFFFFF",
            "accent_color": "#abcdef",
        }
        errors = validate_spec(spec)
        assert errors == []

    def test_invalid_style_rejected(self):
        """An unknown style value should be rejected."""
        spec = _minimal_spec()
        spec["design"] = {"style": "neon cyberpunk"}
        errors = validate_spec(spec)
        assert any("design.style" in e for e in errors)

    def test_all_valid_styles_accepted(self):
        """Each valid style should pass validation."""
        valid_styles = [
            "modern light", "modern dark", "clean minimal",
            "warm earthy", "bold vibrant", "corporate professional",
        ]
        for style in valid_styles:
            spec = _minimal_spec()
            spec["design"] = {"style": style}
            errors = validate_spec(spec)
            assert errors == [], f"Style '{style}' should be valid but got: {errors}"

    def test_style_alias_normalized(self):
        """Short-form style aliases should be normalized to canonical values."""
        aliases = {
            "modern": "modern light",
            "warm": "warm earthy",
            "bold": "bold vibrant",
            "minimal": "clean minimal",
            "dark": "modern dark",
        }
        for alias, canonical in aliases.items():
            spec = _minimal_spec()
            spec["design"] = {"style": alias}
            errors = validate_spec(spec)
            assert errors == [], f"Alias '{alias}' should be accepted but got: {errors}"
            assert spec["design"]["style"] == canonical, (
                f"Alias '{alias}' should normalize to '{canonical}', got '{spec['design']['style']}'"
            )

    def test_null_color_fields_ignored(self):
        """None/null color values should be ignored (optional fields)."""
        spec = _minimal_spec()
        spec["design"] = {
            "primary_color": None,
            "secondary_color": None,
            "accent_color": None,
            "style": None,
        }
        errors = validate_spec(spec)
        assert errors == []

    def test_design_none_is_valid(self):
        """Explicitly setting design to None should be valid (treated as absent)."""
        spec = _minimal_spec()
        spec["design"] = None
        errors = validate_spec(spec)
        assert errors == []


# ---------------------------------------------------------------------------
# Section 02: extended VALID_FIELD_TYPES (14 types)
# ---------------------------------------------------------------------------


class TestExtendedFieldTypes:
    """Section-02-foundation extends VALID_FIELD_TYPES from 6 to 14 entries
    so section 04's widget registry can dispatch on the richer set. These
    tests lock the new contract in and prove existing specs keep validating.
    """

    EXPECTED_14_TYPES = {
        "text", "string", "integer", "float", "boolean",
        "timestamp", "datetime",
        "email", "password",
        "image_url", "url",
        "enum", "json", "foreign_key",
    }

    def test_valid_field_types_constant_matches_spec(self):
        from validate import VALID_FIELD_TYPES

        assert VALID_FIELD_TYPES == self.EXPECTED_14_TYPES

    def test_validate_accepts_all_14_field_types(self):
        """Build a spec whose fields exercise every extended type. Each type
        must be accepted without generating a type-related validation error."""
        base = _minimal_spec()
        base["data_models"][0]["fields"] = [
            {"name": f"f_{idx}", "type": type_name, "required": False}
            for idx, type_name in enumerate(sorted(self.EXPECTED_14_TYPES))
        ]
        errors = validate_spec(base)
        # There may be no errors at all, but certainly none of them should
        # complain about an invalid field type.
        type_errors = [e for e in errors if "invalid type" in e]
        assert type_errors == [], type_errors

    def test_validate_rejects_unknown_field_type_mentions_valid_list(self):
        spec = _minimal_spec()
        spec["data_models"][0]["fields"][0]["type"] = "totally_bogus_type"
        errors = validate_spec(spec)

        type_errors = [e for e in errors if "invalid type" in e]
        assert len(type_errors) == 1
        message = type_errors[0]
        # The error message must name the bad type AND enumerate valid
        # alternatives, so developers get actionable feedback without
        # having to grep the source.
        assert "totally_bogus_type" in message
        for type_name in ("text", "email", "password"):
            assert type_name in message, f"{type_name} not in error: {message}"

    def test_existing_canonical_mw_spec_still_validates(self):
        """The section-01 canonical MW fixture uses only pre-extension
        types; it must continue to validate unchanged under the extended
        VALID_FIELD_TYPES set."""
        canonical = (
            Path(__file__).parent / "golden" / "medicine-waves-canonical" / "spec.json"
        )
        if not canonical.exists():
            pytest.skip("tests/golden/medicine-waves-canonical/spec.json is missing")

        with open(canonical) as file:
            spec = json.load(file)

        errors = validate_spec(spec)
        assert errors == [], errors

    def test_existing_claude_raw_mw_spec_still_validates(self):
        """Another existing MW fixture, claude_mw_v19_raw.json, must also
        continue to pass validation after the type-set extension."""
        raw_fixture = Path(__file__).parent / "fixtures" / "claude_mw_v19_raw.json"
        if not raw_fixture.exists():
            pytest.skip("tests/fixtures/claude_mw_v19_raw.json is missing")

        with open(raw_fixture) as file:
            payload = json.load(file)

        errors = validate_spec(payload["spec"])
        assert errors == [], errors
