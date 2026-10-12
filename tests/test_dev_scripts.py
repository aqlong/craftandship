"""Behavior tests for the developer-facing shell scripts, hooks and CI config.

Covers (each is a bug that was found by running the script, not by reading it):
  - scripts/run_worker_local.sh: loads web/.env without evaluating it, refuses a
    non-local DATABASE_URL, falls back to the docker-compose database.
  - scripts/cas-executor-watchdog.sh: no `eval` (a worktree directory name with
    shell syntax must not execute), --dry-run changes nothing, paid or noisy
    checks stay off while the executor is disabled, and an unmerged path is
    reported and left alone.
  - scripts/com.cas.executor-watchdog.plist.template: renders to a launchd job
    that runs the watchdog every 30 minutes; `make test` runs tests/ and
    compose/tests/.
  - scripts/cas-exec: rejects a non-numeric `parallel` argument.
  - scripts/verify_hooks.sh and `make hooks`: .githooks/ holds the two gates
    (pre-commit, pre-push) and nothing else, and a hook added beside them is
    reported.
  - scripts/promote_staging.sh: rollback restores the tagged tree and its push
    carries CAS_PROMOTE=1.
  - CI: the `quickstart` job and the single web-test run; Dockerfile.worker and
    .dockerignore invariants; every script path referenced by hooks, workflows
    and the Makefile exists.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
BASH = shutil.which("bash") or "/bin/bash"


def _clean_env(**extra) -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp")}
    env.update(extra)
    return env


# ---------------------------------------------------------------------------
# Syntax of every shell script and hook
# ---------------------------------------------------------------------------

SHELL_FILES = sorted(
    [p for p in SCRIPTS.glob("*.sh")]
    + [SCRIPTS / "cas-exec"]
    + [p for p in (REPO_ROOT / ".githooks").iterdir() if p.is_file()]
)


@pytest.mark.parametrize("path", SHELL_FILES, ids=lambda p: p.name)
def test_shell_file_parses_and_is_executable(path):
    interpreter = "sh" if path.read_text().startswith("#!/bin/sh") else "bash"
    result = subprocess.run([interpreter, "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert os.access(path, os.X_OK), f"{path.name} is not executable"


# ---------------------------------------------------------------------------
# run_worker_local.sh
# ---------------------------------------------------------------------------

@pytest.fixture
def worker_layout(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "web").mkdir()
    (tmp_path / "queue").mkdir()
    shutil.copy(SCRIPTS / "run_worker_local.sh", tmp_path / "scripts" / "run_worker_local.sh")
    (tmp_path / "queue" / "worker_pg.py").write_text(
        "import os\n"
        "for k in ('DATABASE_URL','FOO','BAR','SPACED','PRESET','MAX_CONCURRENT_BUILDS','OPS_DRY_RUN'):\n"
        "    print(k + '=' + str(os.environ.get(k)))\n"
    )
    return tmp_path


def _run_worker(layout: Path, **env):
    return subprocess.run([BASH, str(layout / "scripts" / "run_worker_local.sh")], cwd=layout,
                          env=_clean_env(**env), capture_output=True, text=True, timeout=60)


def _printed(out: str) -> dict:
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line and line.split("=", 1)[0].isupper())


def test_worker_local_loads_env_file_without_evaluating_it(worker_layout):
    marker = worker_layout / "pwned"
    (worker_layout / "web" / ".env").write_text(
        "# comment\n\nFOO=bar\nBAR=\"quoted val\"\n"
        f"SPACED=has $(touch {marker}) spaces\nPRESET=fromfile\n"
        "DATABASE_URL=postgresql://u:p@127.0.0.1:5432/x\nbad-key=1\n"
    )
    result = _run_worker(worker_layout, PRESET="fromenv")
    assert result.returncode == 0, result.stderr
    got = _printed(result.stdout)
    assert got["FOO"] == "bar"
    assert got["BAR"] == "quoted val"
    assert got["SPACED"] == f"has $(touch {marker}) spaces"
    assert got["PRESET"] == "fromenv", "variables already in the environment must win"
    assert got["MAX_CONCURRENT_BUILDS"] == "1" and got["OPS_DRY_RUN"] == "true"
    assert not marker.exists(), "web/.env content was executed as shell"
    banner = result.stdout.split("\n\n")[0]  # the script's own output, before the worker's
    assert "p@" not in banner, "the DATABASE_URL (with its password) must not be printed"


def test_worker_local_refuses_a_remote_database(worker_layout):
    (worker_layout / "web" / ".env").write_text("DATABASE_URL=postgresql://u:p@db.example.com:5432/x\n")
    refused = _run_worker(worker_layout)
    assert refused.returncode == 1
    assert "not localhost" in refused.stderr
    allowed = _run_worker(worker_layout, CAS_ALLOW_REMOTE_DB="1")
    assert allowed.returncode == 0


def test_worker_local_defaults_to_the_compose_database(worker_layout):
    result = _run_worker(worker_layout)
    assert result.returncode == 0
    assert _printed(result.stdout)["DATABASE_URL"] == "postgresql://cas:cas@127.0.0.1:5432/craftandship_web"
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    env = compose["services"]["postgres"]["environment"]
    assert (env["POSTGRES_USER"], env["POSTGRES_PASSWORD"], env["POSTGRES_DB"]) == ("cas", "cas", "craftandship_web")


# ---------------------------------------------------------------------------
# cas-executor-watchdog.sh
# ---------------------------------------------------------------------------

@pytest.fixture
def watchdog_project(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("requires git on PATH")
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPTS / "cas-executor-watchdog.sh", tmp_path / "scripts" / "cas-executor-watchdog.sh")
    (tmp_path / "scripts" / "cas-executor-config.json").write_text('{"enabled": false}\n')
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=tmp_path, check=True)
    (tmp_path / "state").mkdir()
    return tmp_path


def _run_watchdog(project: Path, *args):
    env = _clean_env(CAS_EXECUTOR_STATE_DIR=str(project / "state"))
    return subprocess.run([BASH, str(project / "scripts" / "cas-executor-watchdog.sh"), *args],
                          cwd=project, env=env, capture_output=True, text=True, timeout=90)


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_watchdog_has_no_eval():
    code = [ln for ln in (SCRIPTS / "cas-executor-watchdog.sh").read_text().splitlines()
            if not ln.strip().startswith("#")]
    assert not [ln for ln in code if re.search(r"\beval\b", ln)]


def test_watchdog_dry_run_changes_nothing(watchdog_project):
    lock = watchdog_project / "state" / "cas-executor-merge.lock"
    lock.write_text(f"{_dead_pid()}\n")
    result = _run_watchdog(watchdog_project, "--dry-run")
    assert result.returncode == 0
    assert "DRY-RUN would: rm -f" in result.stdout
    assert lock.exists()


def test_watchdog_removes_stale_lock_and_exits_zero(watchdog_project):
    lock = watchdog_project / "state" / "cas-executor-merge.lock"
    lock.write_text(f"{_dead_pid()}\n")
    result = _run_watchdog(watchdog_project)
    assert result.returncode == 0
    assert "removed stale merge_lock" in result.stdout
    assert not lock.exists()


def test_watchdog_prunes_orphan_worktree_without_executing_its_name(watchdog_project):
    marker = watchdog_project / "PWNED"
    worktrees = watchdog_project / ".worktrees"
    worktrees.mkdir()
    # the watchdog runs from the project root, so `touch PWNED` would land there
    hostile = worktrees / "auto-a'$(touch PWNED)'b"
    hostile.mkdir()
    # A fixed, very old mtime: the watchdog is a separate process that reads the
    # real clock, so the age must not be computed from this process's clock.
    long_ago = 1_000_000_000
    os.utime(hostile, (long_ago, long_ago))
    result = _run_watchdog(watchdog_project)
    assert result.returncode == 0
    assert not marker.exists(), "a directory name was executed as shell"
    assert not hostile.exists(), "the orphan worktree should have been pruned"
    assert "pruned orphan worktree" in result.stdout


def test_watchdog_prunes_an_orphan_gate_checkout_and_nothing_else(watchdog_project):
    # The executor tests each commit in .worktrees/gate-<item>; a loop that was
    # killed mid-test leaves that checkout behind.
    worktrees = watchdog_project / ".worktrees"
    worktrees.mkdir()
    gate = worktrees / "gate-item-a"
    unrelated = worktrees / "notes"
    long_ago = 1_000_000_000
    for directory in (gate, unrelated):
        directory.mkdir()
        os.utime(directory, (long_ago, long_ago))
    result = _run_watchdog(watchdog_project)
    assert result.returncode == 0
    assert not gate.exists(), "the orphan gate checkout should have been pruned"
    assert unrelated.exists(), "only auto-* and gate-* directories are the watchdog's to remove"


def _watchdog_probe(tmp_path, fake_cli_body, limit):
    """Run the watchdog's own _claude_auth_probe, cut out of the script, on a
    stand-in CLI. (The whole watchdog would go looking for the real one.)"""
    text = (SCRIPTS / "cas-executor-watchdog.sh").read_text()
    start = text.index("_claude_auth_probe() {")
    function = text[start:text.index("\n}\n", start) + 3]
    fake = tmp_path / "stand-in-cli"
    fake.write_text(fake_cli_body)
    fake.chmod(0o755)
    probe = tmp_path / "probe.sh"
    probe.write_text(f'{function}\n_claude_auth_probe "{limit}" "{fake}" some-model\n')
    return fake, subprocess.run([BASH, str(probe)], capture_output=True, text=True, env=_clean_env(), timeout=120)


def test_watchdog_claude_probe_is_cut_off_at_its_time_limit(tmp_path):
    # A CLI that takes its input and then never answers, with a child of its
    # own. launchd does not start the next watchdog run while one is waiting.
    import time
    body = ('#!/bin/bash\necho "$@" > "$(dirname "$0")/args"\ncat > "$(dirname "$0")/stdin"\n'
            '/bin/sleep 300 &\nwait\n')
    started = time.time()
    fake, result = _watchdog_probe(tmp_path, body, limit=2)
    assert time.time() - started < 30
    assert result.returncode == 0 and result.stdout == ""
    assert (tmp_path / "args").read_text().split() == ["--print", "--model", "some-model"]
    assert (tmp_path / "stdin").read_text() == "ping\n"
    left = subprocess.run(["pgrep", "-fl", str(fake)], capture_output=True, text=True).stdout
    assert left == "", f"the probe's CLI is still running: {left}"


def test_watchdog_claude_probe_returns_the_start_of_what_the_cli_says(tmp_path):
    body = ('#!/bin/bash\ncat > /dev/null\necho "API Error: 401 invalid authentication"\n'
            'head -c 100000 /dev/zero | tr "\\0" x\n')
    _, result = _watchdog_probe(tmp_path, body, limit=20)
    assert result.stdout.startswith("API Error: 401 invalid authentication\n")
    assert len(result.stdout) == 4096, "the answer is cut at 4 KiB"


def test_watchdog_probes_the_cli_the_loop_uses():
    text = (SCRIPTS / "cas-executor-watchdog.sh").read_text()
    code = "\n".join(ln for ln in text.split("\n") if not ln.strip().startswith("#"))
    assert 'CLAUDE_CLI="${CLAUDE_BIN:-$(command -v claude 2>/dev/null || true)}"' in code
    assert '_claude_auth_probe "$PROBE_TIMEOUT_SEC" "$CLAUDE_CLI"' in code
    # nothing else in the script starts the CLI
    assert len([ln for ln in code.split("\n") if "--print" in ln]) == 1


def test_watchdog_stays_quiet_while_the_executor_is_disabled(watchdog_project):
    result = _run_watchdog(watchdog_project)
    assert result.returncode == 0
    assert "executor was DOWN" not in result.stdout and "executor is DOWN" not in result.stdout
    assert "AUTH FAILURE" not in result.stdout


def test_watchdog_alerts_on_an_unmerged_path_and_resolves_nothing(watchdog_project):
    # A stash pop that conflicted: the file is unmerged (git ls-files -u lists
    # it) and holds conflict markers. The watchdog reports it and changes
    # neither, in a live run or a dry one.
    env = _clean_env(GIT_AUTHOR_NAME="sim", GIT_AUTHOR_EMAIL="sim@example.invalid",
                     GIT_COMMITTER_NAME="sim", GIT_COMMITTER_EMAIL="sim@example.invalid",
                     GIT_CONFIG_NOSYSTEM="1")

    def git(*args):
        return subprocess.run(["git", *args], cwd=watchdog_project, env=env,
                              capture_output=True, text=True)

    kb = watchdog_project / "cli" / "failure_kb.json"
    kb.parent.mkdir()
    kb.write_text('{}\n')
    git("add", "-A")
    git("commit", "-qm", "base")
    git("checkout", "-q", "-b", "side")
    kb.write_text('{"side": 1}\n')
    git("commit", "-qam", "side")
    git("checkout", "-q", "master")
    kb.write_text('{"master": 1}\n')
    git("commit", "-qam", "master")
    assert git("merge", "--no-commit", "--no-ff", "side").returncode != 0
    listing = git("ls-files", "-u").stdout
    assert "cli/failure_kb.json" in listing

    for args in ((), ("--dry-run",)):
        result = _run_watchdog(watchdog_project, *args)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ALERT: unmerged paths in PROJECT_DIR (cli/failure_kb.json)" in result.stdout, result.stdout
        assert git("ls-files", "-u").stdout == listing
        assert "<<<<<<<" in kb.read_text()


# ---------------------------------------------------------------------------
# cas-exec, doc_update_check.sh
# ---------------------------------------------------------------------------

def test_cas_exec_parallel_rejects_non_numeric_argument():
    result = subprocess.run([BASH, str(SCRIPTS / "cas-exec"), "parallel", "2; import os"],
                            capture_output=True, text=True, env=_clean_env())
    assert result.returncode == 2
    assert "whole number" in result.stderr


def test_cas_exec_pause_until_requires_a_timestamp():
    result = subprocess.run([BASH, str(SCRIPTS / "cas-exec"), "pause-until"],
                            capture_output=True, text=True, env=_clean_env())
    assert result.returncode == 2


def test_doc_update_check_is_silent_without_jq(tmp_path):
    # PATH with no jq; bash is invoked by absolute path
    result = subprocess.run([BASH, str(SCRIPTS / "doc_update_check.sh")], input='{"tool_input":{}}',
                            capture_output=True, text=True, env={"PATH": str(tmp_path), "HOME": str(tmp_path)})
    assert result.returncode == 0
    assert result.stdout == "" and result.stderr == ""


@pytest.fixture
def promote_checkout(tmp_path):
    """A throwaway repository holding only promote_staging.sh, with one tracked file."""
    if shutil.which("git") is None:
        pytest.skip("requires git on PATH")
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPTS / "promote_staging.sh", tmp_path / "scripts" / "promote_staging.sh")
    (tmp_path / "tracked.txt").write_text("committed\n")
    env = _clean_env(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                     GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    for args in (["init", "-q", "-b", "master"], ["add", "-A"], ["commit", "-qm", "start"]):
        subprocess.run(["git", *args], cwd=tmp_path, env=env, check=True, capture_output=True)
    return tmp_path


def _promote(checkout, *args):
    return subprocess.run([BASH, "scripts/promote_staging.sh", *args], cwd=checkout, env=_clean_env(),
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("staged", [False, True], ids=["an unstaged edit", "a staged edit"])
def test_promote_staging_refuses_to_start_with_uncommitted_changes_because_its_rollback_would_discard_them(
        promote_checkout, staged):
    (promote_checkout / "tracked.txt").write_text("edited, not committed\n")
    if staged:
        subprocess.run(["git", "add", "tracked.txt"], cwd=promote_checkout, check=True)
    result = _promote(promote_checkout, "--dry-run")
    assert result.returncode == 1
    assert "ABORT: uncommitted changes to tracked files" in result.stdout, result.stdout
    assert "Pre-flight" not in result.stdout, "it stopped before its first step"
    assert (promote_checkout / "tracked.txt").read_text() == "edited, not committed\n"


def test_promote_staging_goes_on_from_a_clean_tree(promote_checkout):
    # the stand-in tree has no scripts/smoke_prod.py, so the first step is where it stops
    result = _promote(promote_checkout, "--dry-run")
    assert result.returncode == 1
    assert "uncommitted" not in result.stdout
    assert "ABORT: staging smoke test failed" in result.stdout, result.stdout


def test_promote_staging_rollback_restores_the_tagged_tree_and_pushes_with_promote_flag():
    text = (SCRIPTS / "promote_staging.sh").read_text()
    assert 'git read-tree -u --reset "$PREV_GOOD_TAG"' in text
    assert re.search(r"CAS_PROMOTE=1 git push origin master\n\s+python3 scripts/wait_for_deploy.py", text), (
        "the rollback push must carry CAS_PROMOTE=1 or the staging gate blocks it"
    )
    assert "git revert --no-commit HEAD" not in text


# ---------------------------------------------------------------------------
# CI workflows
# ---------------------------------------------------------------------------

def _workflow(name: str) -> dict:
    return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / name).read_text())


def _run_text(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_workflows_parse_and_define_expected_jobs():
    tests = _workflow("test.yml")
    assert set(tests["jobs"]) == {"unit", "python", "quickstart", "worker-image", "secrets", "audit", "ui"}
    assert set(_workflow("hook-integrity.yml")["jobs"]) == {"hooks"}


def test_worker_image_job_compares_the_images_pg_dump_with_the_server_major():
    """The job builds Dockerfile.worker and reads the server major from the one
    place cli/deploy.py states it; a job that named its own number would pass
    while the two drifted apart."""
    job = _workflow("test.yml")["jobs"]["worker-image"]
    text = _run_text(job)
    assert "docker build -f Dockerfile.worker" in text
    assert "POSTGRES_SERVER_MAJOR" in text and "cli/deploy.py" in text
    assert 'test "$client_major" -ge "$server_major"' in text
    assert "--schema-only" in text
    assert re.search(r'postgres:\$server_major', text), "the server image must follow POSTGRES_SERVER_MAJOR"
    dump_step = next(step for step in job["steps"] if "can dump a server" in step.get("name", ""))
    assert "set -o pipefail" in dump_step["run"], "pg_dump is piped to tail: without pipefail its failure would pass"
    server_line = re.search(r"^POSTGRES_SERVER_MAJOR = (\d+)", (REPO_ROOT / "cli" / "deploy.py").read_text(), re.M)
    assert server_line, "cli/deploy.py no longer states POSTGRES_SERVER_MAJOR as `POSTGRES_SERVER_MAJOR = <n>`"


def test_secret_scan_job_verifies_the_gitleaks_download_and_scans_history():
    job = _workflow("test.yml")["jobs"]["secrets"]
    checkout = next(s for s in job["steps"] if "checkout" in s.get("uses", ""))
    assert checkout["with"]["fetch-depth"] == 0, "history scan needs the full clone"
    text = _run_text(job)
    assert "sha256sum -c" in text, "the gitleaks tarball must be checksum-verified"
    assert len(job["env"]["GITLEAKS_SHA256"]) == 64
    assert "--config .gitleaks.toml" in text
    assert '--log-opts="--all"' in text


def test_quickstart_job_proves_the_readme_quickstart_with_compose():
    job = _workflow("test.yml")["jobs"]["quickstart"]
    assert "services" not in job, "compose provides Postgres; no service container"
    text = _run_text(job)
    for command in ("docker compose up -d --wait", "cp web/.env.example web/.env",
                    "cd web && npm ci && npm run build", "npm start",
                    "http://localhost:4000/health", "http://localhost:4000/", "<title"):
        assert command in text, f"quickstart job is missing: {command}"
    assert any(step.get("if") == "failure()" and "docker compose logs" in step.get("run", "")
               and "server.log" in step.get("run", "") for step in job["steps"]), \
        "quickstart must dump the server log and compose logs on failure"
    # order matters: database up, env file, build, start, health, home page
    order = [text.index(c) for c in ("docker compose up", "cp web/.env.example", "npm ci",
                                     "npm start", "/health", "<title")]
    assert order == sorted(order)


def test_unit_job_runs_web_tests_once():
    job = _workflow("test.yml")["jobs"]["unit"]
    assert _run_text(job).count("npm test") == 1


def test_workflows_need_no_secrets_or_variables():
    for name in ("test.yml", "hook-integrity.yml"):
        text = (REPO_ROOT / ".github" / "workflows" / name).read_text()
        assert "secrets." not in text.replace("no repository secrets", "")
        assert "vars." not in text


def test_workflow_triggers_cover_master():
    for name in ("test.yml", "hook-integrity.yml"):
        on = _workflow(name).get("on") or _workflow(name).get(True)
        assert "master" in on["push"]["branches"]
        assert "pull_request" in on


# ---------------------------------------------------------------------------
# Dockerfile.worker, .dockerignore, compose
# ---------------------------------------------------------------------------

def test_dockerfile_worker_invariants():
    dockerfile = (REPO_ROOT / "Dockerfile.worker").read_text()
    assert re.search(r"npm install -g @railway/cli@\d+\.\d+\.\d+", dockerfile), "Railway CLI must be pinned"
    image_tag = re.search(r"playwright/python:v(\d+\.\d+\.\d+)", dockerfile).group(1)
    pinned = re.search(r"^playwright==(\S+)", (REPO_ROOT / "requirements.worker.txt").read_text(), re.M).group(1)
    assert image_tag == pinned, "base image Playwright version must match requirements.worker.txt"
    assert "Node.js 20 reached end-of-life" in dockerfile, "the Node 20 EOL limitation must stay documented"
    cmd = re.search(r'CMD \["python", "([^"]+)"\]', dockerfile).group(1)
    assert (REPO_ROOT / cmd).is_file()


def test_dockerignore_keeps_secrets_out_and_runtime_inputs_in():
    lines = [ln.strip() for ln in (REPO_ROOT / ".dockerignore").read_text().splitlines()
             if ln.strip() and not ln.startswith("#")]
    for must_exclude in (".git", ".env", "**/.env", "**/node_modules", "**/*.pem", "**/*.key"):
        assert must_exclude in lines
    assert lines.index("!**/.env.example") > lines.index("**/.env.*"), "re-include must come after the exclude"
    # things the worker/coordinator read at runtime must not be excluded
    for needed in ("web", "web/src", "docs", "tests", "cli", "compose", "queue", "coordinator"):
        assert needed not in lines
    assert (REPO_ROOT / "web" / "src" / "db" / "schema.sql").is_file()


def test_compose_file_matches_the_quickstart_env_template():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    pg = compose["services"]["postgres"]
    assert pg["ports"] == ["127.0.0.1:5432:5432"]
    assert "healthcheck" in pg, "`docker compose up --wait` needs a healthcheck"
    template = (REPO_ROOT / "web" / ".env.example").read_text()
    assert "DATABASE_URL=postgresql://cas:cas@localhost:5432/craftandship_web" in template
    assert "PORT=4000" in template


# ---------------------------------------------------------------------------
# Every script path that hooks, workflows, the Makefile and scripts reference exists
# ---------------------------------------------------------------------------

def test_script_paths_referenced_by_hooks_workflows_and_makefile_exist():
    sources = [p for p in (REPO_ROOT / ".githooks").iterdir() if p.is_file()]
    sources += list((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    sources += [REPO_ROOT / "Makefile", REPO_ROOT / ".github" / "pull_request_template.md"]
    sources += list(SCRIPTS.glob("*.sh")) + [SCRIPTS / "cas-exec"]
    pattern = re.compile(
        r"(?<![\w/.-])(scripts/[A-Za-z0-9_-]+(?:[A-Za-z0-9_.-]*\.(?:sh|py|mjs|json|md|plist|template))?)(?![A-Za-z0-9_\\-])")
    missing = {}
    for source in sources:
        for ref in pattern.findall(source.read_text()):
            ref = ref.rstrip(".")
            # `scripts/cas-` style prefixes in regex text and dir-only mentions
            if ref.endswith("-") or ref in ("scripts/cas", "scripts/classify_risk"):
                continue
            if not (REPO_ROOT / ref).exists():
                missing.setdefault(source.name, set()).add(ref)
    assert not missing, f"referenced script paths that do not exist: {missing}"


def test_verify_hooks_script_passes_on_this_tree():
    result = subprocess.run([BASH, str(SCRIPTS / "verify_hooks.sh")], cwd=REPO_ROOT,
                            env=_clean_env(CI="true"), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_githooks_holds_the_two_gates_and_nothing_else():
    # `make hooks` points core.hooksPath at .githooks, so every file there that
    # is named after a git hook runs on that git operation for whoever enabled
    # the hooks. The directory once also held four post-* hooks that rebuilt a
    # personal dashboard after every commit, checkout, merge and rewrite.
    assert sorted(p.name for p in (REPO_ROOT / ".githooks").iterdir()) == ["pre-commit", "pre-push"]
    makefile = (REPO_ROOT / "Makefile").read_text()
    assert "git config core.hooksPath .githooks" in makefile
    workflow = (REPO_ROOT / ".github" / "workflows" / "hook-integrity.yml").read_text()
    assert "bash scripts/verify_hooks.sh" in workflow, "CI must run the same check"


@pytest.fixture
def hooks_checkout(tmp_path):
    """A throwaway git repository holding this tree's hooks, verify_hooks.sh
    and the two scripts the pre-commit hook's own checks are run with."""
    if shutil.which("git") is None:
        pytest.skip("requires git on PATH")
    (tmp_path / "scripts").mkdir()
    for name in ("verify_hooks.sh", "check_destructive_ddl.py", "check_brand_voice.py"):
        shutil.copy(SCRIPTS / name, tmp_path / "scripts" / name)
    shutil.copytree(REPO_ROOT / ".githooks", tmp_path / ".githooks")
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=tmp_path, check=True)
    return tmp_path


def _verify_hooks(checkout: Path, **env):
    return subprocess.run([BASH, "scripts/verify_hooks.sh"], cwd=checkout, env=_clean_env(**env),
                          capture_output=True, text=True, timeout=60)


def test_verify_hooks_requires_the_hooks_to_be_enabled_outside_ci(hooks_checkout):
    off = _verify_hooks(hooks_checkout)
    assert off.returncode == 1 and "core.hooksPath is ''" in off.stdout
    # what `make hooks` runs
    subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=hooks_checkout, check=True)
    on = _verify_hooks(hooks_checkout)
    assert on.returncode == 0, on.stdout
    assert "verify_hooks: PASSED" in on.stdout


@pytest.mark.parametrize("name", ["post-commit", "post-checkout", "commit-msg", ".hidden-hook"])
def test_verify_hooks_reports_a_hook_added_beside_the_two_gates(hooks_checkout, name):
    extra = hooks_checkout / ".githooks" / name
    extra.write_text("#!/bin/sh\nexit 0\n")
    extra.chmod(0o755)
    result = _verify_hooks(hooks_checkout, CI="true")
    assert result.returncode == 1, result.stdout
    assert f".githooks/{name}" in result.stdout


@pytest.mark.parametrize("hook,script", [
    ("pre-commit", "check_destructive_ddl.py"),
    ("pre-push", "run_web_tests.sh"),
    ("pre-push", "run_dep_audit.sh"),
    ("pre-push", "run_python_security_audit.sh"),
])
def test_verify_hooks_reports_a_gate_that_no_longer_runs_its_script(hooks_checkout, hook, script):
    path = hooks_checkout / ".githooks" / hook
    path.write_text(path.read_text().replace(script, "removed"))
    result = _verify_hooks(hooks_checkout, CI="true")
    assert result.returncode == 1, result.stdout
    assert script in result.stdout


# A hook that names every script it used to run and runs none of them. The
# check used to grep the hook for those names, so this passed.
STUB_WITH_THE_NAMES = (
    "#!/bin/sh\n"
    "# check_destructive_ddl.py check_brand_voice.py check_seo_consistency.py\n"
    "# npm test run_web_tests.sh run_dep_audit.sh run_python_security_audit.sh\n"
    "exit 0\n"
)


@pytest.mark.parametrize("hook,refusal_it_no_longer_makes", [
    ("pre-commit", "a staged schema.sql that drops a table"),
    ("pre-commit", "a staged schema.sql with a row change that has no reason"),
    ("pre-commit", "a staged schema.sql with a tagged DO block that sets a build live"),
    ("pre-push", "a commit that changes tests/golden/ without a bless: message"),
])
def test_verify_hooks_fails_a_hook_that_is_an_empty_stub(hooks_checkout, hook, refusal_it_no_longer_makes):
    path = hooks_checkout / ".githooks" / hook
    path.write_text(STUB_WITH_THE_NAMES)
    path.chmod(0o755)
    result = _verify_hooks(hooks_checkout, CI="true")
    assert result.returncode == 1, result.stdout
    assert "verify_hooks: FAILED" in result.stdout
    assert f"FAIL {hook}: {refusal_it_no_longer_makes}" in result.stdout, result.stdout


# One gate switched off at a time, by the smallest edit that does it. Each is
# a hook that still looks whole; each must be reported with the refusal it no
# longer makes.
GATES_SWITCHED_OFF = [
    ("pre-commit", 'python3 scripts/check_destructive_ddl.py - "$SCHEMA_FILE (as staged)" < "$STAGED_DIR/$SCHEMA_FILE"',
     "true", "a staged schema.sql that drops a table"),
    ("pre-commit", 'python3 -c "import json, sys; json.load(open(sys.argv[1]))" "$STAGED_DIR/$FAILURE_KB_FILE" 2>&1',
     "true", "a staged cli/failure_kb.json that is not JSON"),
    ("pre-commit", 'xargs -0 python3 "$ROOT/scripts/check_brand_voice.py"', "xargs -0 true",
     "a staged view with an em dash"),
    ("pre-commit", 'python3 scripts/check_seo_consistency.py --root "$STAGED_DIR"', "true",
     "a staged web/src/app.ts while scripts/check_seo_consistency.py fails"),
    ("pre-push", '"$ROOT/scripts/run_dep_audit.sh"', "true", "a push while scripts/run_dep_audit.sh fails"),
    ("pre-push", '"$ROOT/scripts/run_python_security_audit.sh"', "true",
     "a push while scripts/run_python_security_audit.sh fails"),
    ("pre-push", '"$ROOT/scripts/run_web_tests.sh"', "true", "a push while scripts/run_web_tests.sh fails"),
    ("pre-push", 'if [ -s "$BLESS_GATE_TMP" ]; then', "if false; then",
     "a commit that changes tests/golden/ without a bless: message"),
    ("pre-push", 'if [ "$STAGING_ENABLED" = "true" ] &&', 'if [ "$STAGING_ENABLED" = "never" ] &&',
     "a push to master while use_staging_workflow is true"),
]


# The destructive-DDL gate makes three refusals that verify_hooks.sh checks
# (a dropped table, a row change with no reason, a tagged block that sets a
# build live); switching it off loses all three.
OTHER_REFUSALS_OF_THE_SAME_GATE = {
    "a staged schema.sql that drops a table": [
        "a staged schema.sql with a row change that has no reason",
        "a staged schema.sql with a tagged DO block that sets a build live",
    ],
}


@pytest.mark.parametrize("hook,line,replacement,refusal", GATES_SWITCHED_OFF,
                         ids=[refusal for _, _, _, refusal in GATES_SWITCHED_OFF])
def test_verify_hooks_reports_each_gate_that_was_switched_off(hooks_checkout, hook, line, replacement, refusal):
    path = hooks_checkout / ".githooks" / hook
    text = path.read_text()
    assert text.count(line) == 1, f"{hook} no longer has the line this test edits: {line}"
    path.write_text(text.replace(line, replacement))
    result = _verify_hooks(hooks_checkout, CI="true")
    assert result.returncode == 1, result.stdout
    expected = [refusal, *OTHER_REFUSALS_OF_THE_SAME_GATE.get(refusal, [])]
    for each in expected:
        assert f"FAIL {hook}: {each}" in result.stdout, result.stdout
    failures = [ln for ln in result.stdout.splitlines() if ln.strip().startswith("FAIL")]
    assert len(failures) == len(expected), failures


def test_verify_hooks_reports_a_pre_push_that_waits_for_the_worker_again(hooks_checkout):
    # The hook once ran scripts/wait_for_worker.py before a push; that wait
    # was taken out on purpose. A hook that runs it again is reported.
    path = hooks_checkout / ".githooks" / "pre-push"
    text = path.read_text()
    marker = 'ROOT="$(git rev-parse --show-toplevel)"\n'
    assert text.count(marker) == 1
    path.write_text(text.replace(marker, marker + '"$ROOT/scripts/wait_for_worker.py" || exit 1\n'))
    result = _verify_hooks(hooks_checkout, CI="true")
    assert result.returncode == 1, result.stdout
    assert "FAIL pre-push: the push ran scripts/wait_for_worker.py" in result.stdout


def test_verify_hooks_says_so_when_a_script_the_hook_needs_is_gone(hooks_checkout):
    (hooks_checkout / "scripts" / "check_destructive_ddl.py").unlink()
    result = _verify_hooks(hooks_checkout, CI="true")
    assert result.returncode == 1
    assert "scripts/check_destructive_ddl.py is missing" in result.stdout


def test_verify_hooks_is_not_fooled_by_bypass_variables_in_its_environment(hooks_checkout):
    # CAS_PROMOTE=1 lets a push through two of the pre-push gates. Set for the
    # checker itself it must change nothing: the gates are run without it.
    result = _verify_hooks(hooks_checkout, CI="true", CAS_PROMOTE="1", CAS_EXECUTOR="1", CAS_SKIP_AUDIT="1")
    assert result.returncode == 0, result.stdout
    assert "ok   pre-push: a commit that changes tests/golden/ without a bless: message: refused" in result.stdout


def test_verify_hooks_leaves_nothing_behind(hooks_checkout, tmp_path_factory):
    scratch = tmp_path_factory.mktemp("verify-hooks-tmp")
    result = _verify_hooks(hooks_checkout, CI="true", TMPDIR=str(scratch))
    assert result.returncode == 0, result.stdout
    assert list(scratch.iterdir()) == []


def test_verify_hooks_reports_a_missing_or_non_executable_hook(hooks_checkout):
    (hooks_checkout / ".githooks" / "pre-push").chmod(0o644)
    assert "is not executable" in _verify_hooks(hooks_checkout, CI="true").stdout
    (hooks_checkout / ".githooks" / "pre-push").unlink()
    result = _verify_hooks(hooks_checkout, CI="true")
    assert result.returncode == 1 and ".githooks/pre-push is missing" in result.stdout


@pytest.mark.requires_tools("git")
@pytest.mark.parametrize("ignore_files", [
    (".gitignore", "web/.gitignore"),  # the repository as it is
    (".gitignore",),                   # each file on its own
    ("web/.gitignore",),
])
def test_a_symlinked_node_modules_is_ignored(tmp_path, ignore_files):
    # The executor links web/node_modules into its worktrees. A `node_modules/`
    # pattern (trailing slash) matches directories only: the link is then not
    # ignored, `git add -A` stages it, and a committed link breaks the web build.
    (tmp_path / "web").mkdir()
    (tmp_path / "shared" / "node_modules" / "pkg").mkdir(parents=True)
    for rel in ignore_files:
        shutil.copy(REPO_ROOT / rel, tmp_path / rel)
    os.symlink(tmp_path / "shared" / "node_modules", tmp_path / "web" / "node_modules")
    env = _clean_env(GIT_CONFIG_NOSYSTEM="1")
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=tmp_path, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, env=env)
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=tmp_path, env=env,
                            capture_output=True, text=True, check=True).stdout.split()
    assert "web/node_modules" not in staged, staged


def test_makefile_targets_exist_and_reference_real_files():
    makefile = (REPO_ROOT / "Makefile").read_text()
    for target in ("hooks", "verify-hooks", "test", "test-web", "bless-golden"):
        assert re.search(rf"^{re.escape(target)}:", makefile, re.M), target
    assert (SCRIPTS / "bless_golden.py").is_file() and (SCRIPTS / "verify_hooks.sh").is_file()


def test_make_test_runs_the_tests_and_the_compose_tests():
    # The default Python run is tests/ plus compose/tests/ (CONTRIBUTING.md and
    # tests/README.md say so); the Makefile target has to run both.
    makefile = (REPO_ROOT / "Makefile").read_text()
    recipe = re.search(r"^test:\n\t(.+)$", makefile, re.M)
    assert recipe, "the test target has a recipe"
    assert re.search(r"-m pytest tests/ compose/tests(\s|$)", recipe.group(1)), recipe.group(1)


def test_watchdog_plist_is_a_template_that_renders_to_a_valid_launchd_job():
    # The plist carries placeholders, so it is a template and is named as one,
    # like com.cas.roadmap-executor.plist.template. The header's install command
    # names it, and the file renders to a launchd job for the watchdog.
    import plistlib

    template = SCRIPTS / "com.cas.executor-watchdog.plist.template"
    assert template.is_file()
    assert not (SCRIPTS / "com.cas.executor-watchdog.plist").exists()
    text = template.read_text()
    assert "scripts/com.cas.executor-watchdog.plist.template \\" in text, "the install command names the template"
    rendered = text.replace("__REPO_ROOT__", "/srv/example-repo").replace("__HOME__", "/srv/example-home")
    assert not re.search(r"__[A-Z_]+__", rendered), "every placeholder is filled in at install"
    job = plistlib.loads(rendered.encode())
    assert job["Label"] == "com.cas.executor-watchdog"
    assert job["ProgramArguments"] == ["/bin/bash", "/srv/example-repo/scripts/cas-executor-watchdog.sh"]
    assert job["StartInterval"] == 1800 and job["KeepAlive"] is False
    assert job["EnvironmentVariables"]["HOME"] == "/srv/example-home"
