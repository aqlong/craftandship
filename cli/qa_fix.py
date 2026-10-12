# cli/qa_fix.py - Self-correction for generated apps when QA checks fail.
#
# Same pattern as fix.py, but targets QA failures rather than smoke test
# failures. The key difference: smoke tests pass when this runs, so the app
# is fundamentally working -- we are fixing feature-level issues (missing pages,
# broken CRUD forms, empty seed data) rather than startup/auth crashes.
#
# Called by worker_pg.py after qa.py finds failures. One fix attempt is made.
# If the fix is applied, the caller re-runs smoke tests to ensure no regression,
# then re-runs QA to measure improvement.

"""LEGACY v1 build engine (frozen): QA fix loop. Superseded by the v2 engine in
compose/, where no model repairs build output; kept for reference only and run
only when CAS_BUILD_ENGINE=v1. Do not extend it. See cli/README.md.
"""

import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv

from api_client import call_api as _shared_call_api, make_client, BuildCancelled
from cli import cancellation
from fix import _build_advisor_prompt

logger = logging.getLogger(__name__)

# Module-level reference to the most recently launched QA subprocess.
# The worker reads this on FixLoopBudgetExceeded to kill any orphan Chromium
# or Node children before degrading gracefully to deploy.
_last_qa_proc: Optional[subprocess.Popen] = None

load_dotenv()


# ---------------------------------------------------------------------------
# Process-tree management (QA_FIX_HANG1 mitigation)
# ---------------------------------------------------------------------------

def _run_node_syntax_check(build_path: Path) -> Optional[subprocess.Popen]:
    """Run `node --check server.js` in a new process group after applying fixes.

    Uses start_new_session=True so the Node child and any grandchildren share
    a distinct process group. If the worker later catches FixLoopBudgetExceeded
    it can call kill_proc_group(_last_qa_proc) to SIGKILL the whole tree,
    preventing orphan Chromium processes from QA re-runs in prior iterations.

    Non-blocking: the process is started and returned; the caller waits with a
    short timeout. Errors are silently swallowed so this never blocks a build.
    """
    global _last_qa_proc
    server_js = build_path / "server.js"
    if not server_js.exists():
        return None
    try:
        # `node --check` only parses the file, but it is still a child process
        # pointed at model-written code: it gets the scrubbed environment like
        # every other one (see deploy.env_without_secrets).
        from deploy import env_without_secrets
        proc = subprocess.Popen(
            ["node", "--check", str(server_js)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=env_without_secrets(),
        )
        _last_qa_proc = proc
        return proc
    except Exception:
        return None


def kill_proc_group(proc: Optional[subprocess.Popen]) -> None:
    """Send SIGKILL to the process group of *proc*.

    Called by the worker's FixLoopBudgetExceeded handler to clean up any orphan
    Chromium or Node processes left over from a QA run that timed out.

    Safe to call even if the process has already exited:
    - ProcessLookupError (no such process) is silently swallowed.
    - PermissionError is logged as a warning and not re-raised.
    - proc=None is a no-op.
    """
    if proc is None:
        return
    if proc.poll() is not None:
        # Process already exited cleanly -- nothing to kill.
        return
    # NOTE (thread-safety): _last_qa_proc is a module-level global. With
    # MAX_CONCURRENT_BUILDS > 1, two concurrent builds could overwrite each
    # other's _last_qa_proc. Production uses MAX_CONCURRENT_BUILDS=1, which
    # makes this safe. If concurrent builds are ever enabled, switch to a
    # per-build dict keyed on build_id.
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return  # Process exited between poll() and getpgid(); nothing to kill.
    except OSError:
        return  # Unexpected OS error looking up PGID; leave the process alone.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # Process group already gone.
    except PermissionError as exc:
        logger.warning(
            "qa_fix: kill_proc_group could not SIGKILL pgid for pid %s: %s",
            getattr(proc, "pid", "?"),
            exc,
        )


def _raise_if_cancelled(build_id, *, stage):
    """Raise BuildCancelled if the supervisor has fired this attempt's cancel event.

    `build_id` is the attempt's cancel key (see the caller). With no key the
    check is a no-op: a run outside the worker has no supervisor.

    Module-local helper (intentionally duplicated across fix loops) so that
    a refactor or rename of one fix module cannot silently remove the
    cooperative cancel check from another.
    """
    if not build_id:
        return
    event = cancellation.get(build_id)
    if event.is_set():
        raise BuildCancelled(build_id)

MODEL = "claude-opus-4-6"
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 2


def _get_qa_fix_advisor_config():
    """Return advisor parameters if USE_ADVISOR_QA_FIX is enabled, else empty dict."""
    val = os.environ.get("USE_ADVISOR_QA_FIX", "").lower()
    if val and val not in ("false", "0", "no"):
        return {"advisor_model": "claude-opus-4-6", "advisor_max_uses": 2}
    return {}


# Files to always include in fix context (if present)
_KEY_FILES = [
    "server.js",
    "package.json",
    "middleware/auth.js",
    "db/schema.sql",
    "db/seed.sql",
    "db/migrate.js",
]

_SOURCE_DIRS = ["routes", "views", "public"]

_MAX_CONTEXT_CHARS = 80000

QA_FIX_SYSTEM_PROMPT = """You are a code fixer for CraftAndShip, a platform that generates Express.js + PostgreSQL web apps.

A generated app's smoke tests PASS (auth, login, logout, role enforcement all work) but deeper spec-aware QA checks have found issues. You are given:
1. The QA test results showing which checks passed and which failed with error details
2. The app's original spec (JSON) describing the intended pages, data models, and actions
3. The relevant source files from the build directory

Your job is to fix the app code so the QA issues are resolved WITHOUT breaking the smoke tests.

Typical QA failures and their fixes:
- Page returns non-200 or shows error text: fix the route handler and/or view template
- Expected UI elements missing (table, form, buttons): update the EJS view to include them
- CRUD form submission fails or record does not appear: fix the route POST handler and redirect
- Seed data not visible (empty list page): fix db/seed.sql to insert realistic records
- Navigation links broken or missing: update the nav partial or layout template

## Output rules

For each file you need to fix, use this exact format:

===FILE: path/to/file.ext===
complete file contents here
===END FILE===

IMPORTANT:
- Output ONLY files that need changes. Do not output unchanged files.
- Output the COMPLETE file content, not just the diff.
- The path must match the original file's path exactly (e.g. routes/tasks.js, views/tasks.ejs).
- Focus on fixing the QA findings. Do not refactor, add features, or change auth logic.
- NEVER modify these files: server.js, middleware/auth.js, routes/auth.js, db/migrate.js. These are critical for smoke tests.
- NEVER change login, logout, register, or session handling logic in ANY file.
- NEVER change bcrypt imports, password hashing, or session.save() patterns.
- NEVER remove CSRF token hidden inputs from forms.
- NEVER change the layout.ejs nav structure or footer.
- If seed.sql needs more data, generate 10-15 realistic records per table.
- If a view has an error, fix only the specific broken part; do not rewrite the entire template.
"""


def _create_client():
    """Create an Anthropic client."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return make_client(api_key=api_key)


def _call_api(client, messages, system_prompt=None, model=None,
              advisor_model=None, advisor_max_uses=2, build_id=None):
    """Thin wrapper delegating to api_client.call_api. Always realtime.

    Forwards ``build_id`` so api_client can poll the cancellation event
    between retries on its side.
    """
    effective_model = model or MODEL
    prompt_text = system_prompt or QA_FIX_SYSTEM_PROMPT
    system = [{
        "type": "text",
        "text": prompt_text,
        "cache_control": {"type": "ephemeral"},
    }]
    extra_kwargs = {}
    if advisor_model:
        extra_kwargs["advisor_model"] = advisor_model
        extra_kwargs["advisor_max_uses"] = advisor_max_uses
    if build_id is not None:
        extra_kwargs["build_id"] = build_id
    response, _metrics = _shared_call_api(
        client=client,
        messages=messages,
        model=effective_model,
        system=system,
        max_tokens=16384,
        use_batch=False,
        module="qa_fix",
        handle_continuation=False,
        **extra_kwargs,
    )
    return response


def _parse_fix_files(text: str) -> list:
    """Extract ===FILE: path=== ... ===END FILE=== blocks from response."""
    import re
    pattern = r"===FILE:\s*(.+?)\s*===\n(.*?)===END FILE==="
    matches = re.findall(pattern, text, re.DOTALL)
    files = []
    for filepath, content in matches:
        filepath = filepath.strip()
        # Security: prevent path traversal
        if ".." in filepath or filepath.startswith("/"):
            continue
        files.append((filepath, content))
    return files


def _collect_source_files(build_dir: Path) -> list:
    """Collect relevant source files from the build directory.

    Returns list of (relative_path, content) tuples, respecting _MAX_CONTEXT_CHARS.
    """
    files = []
    total_chars = 0

    for rel_path in _KEY_FILES:
        full_path = build_dir / rel_path
        if full_path.exists() and full_path.is_file():
            content = full_path.read_text(errors="replace")
            files.append((rel_path, content))
            total_chars += len(content)

    for dir_name in _SOURCE_DIRS:
        dir_path = build_dir / dir_name
        if not dir_path.is_dir():
            continue
        for file_path in sorted(dir_path.rglob("*")):
            if not file_path.is_file():
                continue
            if file_path.suffix not in (".js", ".ejs", ".css", ".sql", ".json", ".html"):
                continue
            rel = str(file_path.relative_to(build_dir))
            if any(rel == existing[0] for existing in files):
                continue
            content = file_path.read_text(errors="replace")
            if total_chars + len(content) > _MAX_CONTEXT_CHARS:
                break
            files.append((rel, content))
            total_chars += len(content)

    return files


def _format_qa_results(qa_results: list) -> str:
    """Format QA results list for the prompt.

    Accepts list of {name, passed, error} dicts.
    """
    lines = []
    for item in qa_results:
        if isinstance(item, dict):
            status = "PASS" if item.get("passed") else "FAIL"
            name = item.get("name", "unnamed")
            error = item.get("error")
        else:
            # Fallback: (name, passed, error) tuple
            name, passed, error = item
            status = "PASS" if passed else "FAIL"
        lines.append(f"[{status}] {name}")
        if error and status == "FAIL":
            lines.append(f"  Error: {error}")
    return "\n".join(lines)


def _format_source_files(source_files: list) -> str:
    """Format source files for the prompt."""
    parts = []
    for rel_path, content in source_files:
        parts.append(f"--- {rel_path} ---\n{content}\n")
    return "\n".join(parts)


def attempt_qa_fix(
    build_dir: str,
    qa_results: list,
    spec: Optional[dict] = None,
    *,
    cancel_key: Optional[str] = None,
) -> dict:
    """Attempt to fix a generated app based on QA failures.

    Args:
        build_dir: Path to the build directory.
        qa_results: List of {name, passed, error} dicts from qa.py.
        spec: The original app spec (optional, for context).
        cancel_key: Key of the attempt's cancel event. When the worker's
            supervisor fires it, this call raises BuildCancelled at its next
            checkpoint.

    Returns:
        Dict with:
          - fixed: bool (True if files were written)
          - files_changed: list of file paths that were updated
          - description: str summary of what was fixed
    """
    # Key of the attempt's cancel event, handed over by the worker
    # (queue/worker_pg.py, _new_cancel_key; it begins with the build's id).
    # The model client is given the same key as its `build_id`: it uses that
    # argument to find the event and as the label on its log lines.
    build_id = cancel_key

    # Cancel guard: iteration_top. Bail before any expensive work if the
    # watchdog supervisor has already fired for this build.
    _raise_if_cancelled(build_id, stage="qa_fix:iteration_top")

    build_path = Path(build_dir).resolve()

    source_files = _collect_source_files(build_path)
    if not source_files:
        return {"fixed": False, "files_changed": [], "description": "No source files found"}

    failed_checks = [
        item for item in qa_results
        if (isinstance(item, dict) and not item.get("passed"))
        or (isinstance(item, tuple) and not item[1])
    ]
    if not failed_checks:
        return {"fixed": False, "files_changed": [], "description": "No failed QA checks to fix"}

    # Also include the generated qa_tests.py if present (helps Claude understand what was tested)
    qa_test_file = build_path / "qa_tests.py"
    qa_tests_content = ""
    if qa_test_file.exists():
        qa_tests_content = qa_test_file.read_text(errors="replace")[:3000]

    prompt_parts = []
    prompt_parts.append("## QA Results\n")
    prompt_parts.append(_format_qa_results(qa_results))

    if qa_tests_content:
        prompt_parts.append("\n\n## QA Test Code (what was tested)\n")
        prompt_parts.append(qa_tests_content)

    if spec:
        prompt_parts.append("\n\n## App Spec\n")
        prompt_parts.append(json.dumps(spec, indent=2)[:5000])

    prompt_parts.append("\n\n## Source Files\n")
    prompt_parts.append(_format_source_files(source_files))

    prompt_parts.append(
        "\n\nFix the failing QA checks. Output only the files that need changes. "
        "Do NOT modify auth logic or login/logout routes."
    )

    prompt = "\n".join(prompt_parts)

    client = _create_client()
    advisor_config = _get_qa_fix_advisor_config()
    effective_model = "claude-sonnet-4-6" if advisor_config else None
    effective_prompt = _build_advisor_prompt(QA_FIX_SYSTEM_PROMPT) if advisor_config else None
    response = _call_api(
        client, [{"role": "user", "content": prompt}],
        system_prompt=effective_prompt,
        model=effective_model,
        build_id=build_id,
        **advisor_config,
    )

    # Cancel guard: post_api. The API call is the longest blocking step in
    # this loop, so re-check immediately after it returns.
    _raise_if_cancelled(build_id, stage="qa_fix:post_api")

    response_text = response.content[0].text if response.content else ""

    # Build error summary from failed checks for KB recording
    failed_names = [
        item.get("name", str(item)) if isinstance(item, dict) else str(item)
        for item in failed_checks
    ]
    error_summary = "QA failed: " + "; ".join(failed_names[:5])

    fixed_files = _parse_fix_files(response_text)

    # Cancel guard: pre_subprocess. Last chance to bail before we touch the
    # filesystem with Claude's suggested fixes.
    _raise_if_cancelled(build_id, stage="qa_fix:pre_subprocess")
    if not fixed_files:
        # Record failed fix attempt in KB
        try:
            from failure_kb import record_fix
            record_fix(error_summary, "Claude returned no QA fixes", [],
                       fix_category="qa", succeeded=False)
        except Exception:
            pass
        return {"fixed": False, "files_changed": [], "description": "Claude returned no file fixes"}

    files_changed = []
    for rel_path, content in fixed_files:
        target = build_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        files_changed.append(rel_path)

    # Syntax-check server.js in a new process group so that, if the worker's
    # FixLoopBudgetExceeded fires before this check finishes, kill_proc_group()
    # can SIGKILL the entire Node/Chromium tree without leaving orphan processes.
    node_check = _run_node_syntax_check(build_path)
    if node_check is not None:
        try:
            node_check.wait(timeout=30)
        except Exception:
            pass  # Non-fatal; orphan will be killed by kill_proc_group if budget fires.

    description = f"Fixed {len(files_changed)} file(s): {', '.join(files_changed)}"

    return {
        "fixed": True,
        "files_changed": files_changed,
        "description": description,
        "error_summary": error_summary,  # passed back so worker can record true outcome
        "fix_category": "qa",
    }
