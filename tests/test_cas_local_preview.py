"""Tests for scripts/cas-local-preview.

Pure-unit coverage of the helpers + the launch.json upsert behavior + the
end-to-end argument parsing. The DB / npm / node steps are NOT exercised
here (they run in tests/test_v2_local_e2e.py under the v2_integration
marker); this file stays fast and never touches the filesystem outside
of tmp_path.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "cas-local-preview"


@pytest.fixture(scope="module")
def cas_local_preview():
    """Import the dash-named, extensionless script as a module."""
    loader = SourceFileLoader("cas_local_preview", str(SCRIPT_PATH))
    spec = importlib.util.spec_from_loader("cas_local_preview", loader)
    assert spec is not None, "could not build module spec for cas-local-preview"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cas_local_preview"] = mod
    loader.exec_module(mod)
    return mod


def test_derive_db_name_replaces_dashes_with_underscores(cas_local_preview):
    assert cas_local_preview.derive_db_name("sample-irrigation") == "sample_irrigation_local"


def test_derive_db_name_collapses_runs_and_strips_edges(cas_local_preview):
    assert cas_local_preview.derive_db_name("--foo--bar--") == "foo_bar_local"


def test_derive_db_name_lowercases_and_strips_specials(cas_local_preview):
    # Nothing about Postgres demands strict ASCII, but the convention in
    # launch.json is lowercase + underscores; punctuation is dropped.
    assert cas_local_preview.derive_db_name("Sample.Reiki Studio!") == "sample_reiki_studio_local"


def test_derive_session_secret_is_stable_per_app(cas_local_preview):
    a = cas_local_preview.derive_session_secret("sample-healer")
    b = cas_local_preview.derive_session_secret("sample-healer")
    assert a == b
    assert a != cas_local_preview.derive_session_secret("ridgeline-plumbing")


def test_build_launch_command_carries_db_session_port_and_dev_mode(cas_local_preview):
    cmd = cas_local_preview.build_launch_command(
        build_dir=Path("/tmp/foo-local"),
        db_url="postgresql://u@localhost:5432/foo_local",
        session_secret="local_foo_secret",
        port=4710,
        path_dirs=["/somewhere/node/bin", "/somewhere/pg/bin"],
    )
    assert cmd[0] == "/bin/bash"
    assert cmd[1] == "-c"
    inner = cmd[2]
    assert "cd /tmp/foo-local" in inner
    assert "DATABASE_URL=postgresql://u@localhost:5432/foo_local" in inner
    assert "SESSION_SECRET=local_foo_secret" in inner
    assert "PORT=4710" in inner
    assert "NODE_ENV=development" in inner
    assert "node server.js" in inner
    # the directories it was given go first on PATH, and nothing else does
    assert inner.startswith("export PATH=/somewhere/node/bin:/somewhere/pg/bin:$PATH ")


def _fake_program(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    program = directory / name
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    return program


def test_launch_command_uses_the_programs_found_on_path_at_run_time(cas_local_preview, tmp_path, monkeypatch):
    # node in one directory, the PostgreSQL programs in another, neither a
    # place the script could have known in advance
    node_dir, pg_dir = tmp_path / "node-here", tmp_path / "pg 16" / "bin"
    _fake_program(node_dir, "node")
    _fake_program(pg_dir, "psql")
    monkeypatch.setenv("PATH", f"{node_dir}:{pg_dir}")
    assert cas_local_preview.postgres_bin_dir() == pg_dir
    assert cas_local_preview.launch_path_dirs() == [str(node_dir), str(pg_dir)]
    inner = cas_local_preview.build_launch_command(Path("/tmp/x"), "postgresql://u@localhost/x", "s", 4711)[2]
    # each directory quoted for the shell (one has a space in it)
    assert inner.startswith(f"export PATH={node_dir}:'{pg_dir}':$PATH ")
    assert "Cellar" not in inner and "postgresql@" not in inner


def test_a_keg_only_postgres_is_found_through_brew_prefix(cas_local_preview, tmp_path, monkeypatch):
    # No psql on PATH. `brew --prefix <formula>` prints a prefix whether or
    # not the formula is installed; the one that has a psql in it is taken.
    keg = tmp_path / "opt" / "postgresql@16"
    _fake_program(keg / "bin", "psql")
    brew_dir = tmp_path / "brew-bin"
    brew = _fake_program(brew_dir, "brew")
    brew.write_text(f'#!/bin/sh\n[ "$1" = "--prefix" ] || exit 1\necho "{tmp_path}/opt/$2"\n')
    monkeypatch.setenv("PATH", str(brew_dir))
    assert cas_local_preview.postgres_bin_dir() == keg / "bin"


def test_no_postgres_anywhere_is_none(cas_local_preview, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert cas_local_preview.postgres_bin_dir() is None
    assert cas_local_preview.launch_path_dirs() == []


def test_launch_command_quotes_what_it_is_given(cas_local_preview):
    inner = cas_local_preview.build_launch_command(
        Path("/tmp/my builds/x; touch pwned"), "postgresql://u@localhost/x", "it's", 4712, path_dirs=[])[2]
    import shlex
    words = shlex.split(inner)
    assert words[words.index("cd") + 1] == "/tmp/my builds/x; touch pwned"
    assert "SESSION_SECRET=it's" in words
    assert "touch" not in words


def test_upsert_launch_entry_creates_file_when_missing(cas_local_preview, tmp_path):
    target = tmp_path / "fresh" / ".claude" / "launch.json"
    cas_local_preview.upsert_launch_entry(
        launch_json=target,
        name="foo-local",
        build_dir=tmp_path / "build",
        db_url="postgresql://u@localhost:5432/foo_local",
        session_secret="local_foo_secret",
        port=4711,
        quiet=True,
    )
    assert target.is_file()
    doc = json.loads(target.read_text())
    assert doc["version"] == "0.0.1"
    assert len(doc["configurations"]) == 1
    entry = doc["configurations"][0]
    assert entry["name"] == "foo-local"
    assert entry["port"] == 4711
    assert entry["autoPort"] is True
    assert entry["runtimeArgs"][0] == "-c"
    assert "node server.js" in entry["runtimeArgs"][1]


def test_upsert_launch_entry_appends_when_present(cas_local_preview, tmp_path):
    target = tmp_path / "launch.json"
    target.write_text(json.dumps({
        "version": "0.0.1",
        "configurations": [
            {"name": "existing-one", "port": 4000},
            {"name": "existing-two", "port": 4001},
        ],
    }))

    cas_local_preview.upsert_launch_entry(
        launch_json=target,
        name="brand-new",
        build_dir=tmp_path / "build",
        db_url="postgresql://u@localhost:5432/brand_new_local",
        session_secret="local_brand_new_secret",
        port=4712,
        quiet=True,
    )

    doc = json.loads(target.read_text())
    names = [c["name"] for c in doc["configurations"]]
    assert names == ["existing-one", "existing-two", "brand-new"]
    # Pre-existing entries must be preserved verbatim.
    assert doc["configurations"][0] == {"name": "existing-one", "port": 4000}
    assert doc["configurations"][1] == {"name": "existing-two", "port": 4001}


def test_upsert_launch_entry_replaces_in_place_when_name_matches(cas_local_preview, tmp_path):
    target = tmp_path / "launch.json"
    target.write_text(json.dumps({
        "version": "0.0.1",
        "configurations": [
            {"name": "alpha", "port": 4000},
            {"name": "sample-irrigation-local", "port": 4606, "stale": True},
            {"name": "omega", "port": 4999},
        ],
    }))

    cas_local_preview.upsert_launch_entry(
        launch_json=target,
        name="sample-irrigation-local",
        build_dir=tmp_path / "sample-irrigation",
        db_url="postgresql://u@localhost:5432/sample_irrigation_local",
        session_secret="local_fw_secret",
        port=4710,
        quiet=True,
    )

    doc = json.loads(target.read_text())
    # Order preserved + replaced in place; siblings untouched.
    assert [c["name"] for c in doc["configurations"]] == [
        "alpha",
        "sample-irrigation-local",
        "omega",
    ]
    assert doc["configurations"][0] == {"name": "alpha", "port": 4000}
    assert doc["configurations"][2] == {"name": "omega", "port": 4999}
    replaced = doc["configurations"][1]
    assert replaced["port"] == 4710
    assert "stale" not in replaced  # stale fields gone, full replacement
    assert replaced["runtimeExecutable"] == "/bin/bash"


def test_upsert_launch_entry_rejects_malformed_doc(cas_local_preview, tmp_path):
    target = tmp_path / "launch.json"
    target.write_text(json.dumps({"version": "0.0.1"}))  # no configurations list

    with pytest.raises(RuntimeError, match="malformed"):
        cas_local_preview.upsert_launch_entry(
            launch_json=target,
            name="foo",
            build_dir=tmp_path,
            db_url="postgresql://u@localhost:5432/foo",
            session_secret="s",
            port=4713,
            quiet=True,
        )


def test_parse_args_defaults(cas_local_preview):
    args = cas_local_preview.parse_args(["specs/foo.json"])
    assert args.spec == "specs/foo.json"
    assert args.port == 4606
    assert args.no_install is False
    assert args.no_migrate is False
    assert args.no_launch_json is False
    assert args.recreate_db is False
    assert args.quiet is False
    assert args.db_name is None
    assert args.launch_name is None
    assert args.output_dir is None


def test_parse_args_overrides(cas_local_preview):
    args = cas_local_preview.parse_args([
        "specs/foo.json",
        "--port", "4710",
        "--db-name", "custom_db",
        "--launch-name", "custom-name",
        "--output-dir", "/tmp/custom",
        "--launch-json", "/tmp/launch.json",
        "--no-install",
        "--no-migrate",
        "--no-launch-json",
        "--recreate-db",
        "--quiet",
    ])
    assert args.port == 4710
    assert args.db_name == "custom_db"
    assert args.launch_name == "custom-name"
    assert args.output_dir == "/tmp/custom"
    assert args.launch_json == "/tmp/launch.json"
    assert args.no_install is True
    assert args.no_migrate is True
    assert args.no_launch_json is True
    assert args.recreate_db is True
    assert args.quiet is True


def test_run_returns_1_on_missing_spec(cas_local_preview, tmp_path, capsys):
    rc = cas_local_preview.run(cas_local_preview.parse_args([
        str(tmp_path / "missing.json"),
        "--no-install",
        "--no-migrate",
        "--no-launch-json",
    ]))
    assert rc == 1
    err = capsys.readouterr().err
    assert "spec not found" in err


def test_run_returns_1_when_app_name_missing(cas_local_preview, tmp_path, capsys, monkeypatch):
    bad_spec = tmp_path / "bad.json"
    bad_spec.write_text(json.dumps({"archetype": "tradesperson-contractor"}))

    # Skip the prereq check so this test passes on hosts without psql/node.
    monkeypatch.setattr(cas_local_preview, "check_prereqs", lambda: [])

    rc = cas_local_preview.run(cas_local_preview.parse_args([
        str(bad_spec),
        "--no-install",
        "--no-migrate",
        "--no-launch-json",
    ]))
    assert rc == 1
    err = capsys.readouterr().err
    assert "app_name" in err


def test_find_default_launch_json_under_repo_root(cas_local_preview):
    p = cas_local_preview.find_default_launch_json()
    # The script lives at <root>/scripts/cas-local-preview, so the default
    # must resolve to a sibling .claude directory, never anywhere else.
    assert p.name == "launch.json"
    assert p.parent.name == ".claude"


def test_check_prereqs_returns_list(cas_local_preview):
    """Just exercise the function shape; do not assert on host PATH."""
    missing = cas_local_preview.check_prereqs()
    assert isinstance(missing, list)
    for cmd in missing:
        assert cmd in cas_local_preview.REQUIRED_CMDS
