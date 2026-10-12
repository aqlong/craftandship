# tests/test_ops_playbook.py - Tests for playbook matching and loading

import sys
from pathlib import Path

_ROOT = str(Path(__file__).parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ops.playbook import match_playbook, load_fixer, PLAYBOOKS


class TestMatchPlaybook:
    def test_the_categories_that_needed_a_commit_have_no_playbook(self):
        for category in ("version_mismatch", "missing_dep", "missing_tool", "dockerfile"):
            assert match_playbook(category) is None, category

    def test_env_config_returns_entry(self):
        entry = match_playbook("env_config")
        assert entry is not None
        assert entry.category == "env_config"
        assert entry.auto_fixable is True

    def test_railway_crash_returns_entry(self):
        entry = match_playbook("railway_crash")
        assert entry is not None

    def test_app_logic_returns_none(self):
        assert match_playbook("app_logic") is None

    def test_unknown_returns_none(self):
        assert match_playbook("unknown") is None

    def test_nonexistent_category_returns_none(self):
        assert match_playbook("does_not_exist") is None


class TestLoadFixer:
    def test_loads_env_config_fixer(self):
        entry = PLAYBOOKS["env_config"]
        fixer = load_fixer(entry)
        assert fixer is not None
        assert callable(fixer)

    def test_a_registry_entry_whose_module_is_missing_loads_nothing(self):
        from ops.playbook import PlaybookEntry
        gone = PlaybookEntry(category="x", description="", severity="infrastructure", auto_fixable=True,
                             fixer_module="ops.playbooks.missing_deps", requires_redeploy=True)
        assert load_fixer(gone) is None

    def test_loads_railway_redeploy_fixer(self):
        entry = PLAYBOOKS["railway_crash"]
        fixer = load_fixer(entry)
        assert fixer is not None
        assert callable(fixer)
