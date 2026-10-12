#!/usr/bin/env python3
"""Roadmap helper for the executor loop (scripts/cas-roadmap-loop.sh).

Reads coordinator/roadmap.json and scripts/cas-executor-config.json. No network.

  --next / --next-n N      print the highest-priority eligible item(s) as JSON
  --list                   human-readable list of pending items with markers
  --build-prompt ITEM_ID   print the executor prompt for one item
  --set-status ID STATUS [--note KEY VALUE]
                           atomically set one item's status (and optionally one
                           allowed note field); exit 1 if the item is not found
  --verify-transition BEFORE_FILE ID STATUS [--note KEY VALUE]
                           exit 0 only when roadmap.json is BEFORE_FILE (an
                           earlier copy) plus exactly that one --set-status
                           change; otherwise print the difference and exit 1
  --enabled                exit 0 when the config enables the executor, 1 when
                           it does not (or cannot be read). The loop and the
                           watchdog both ask here; only a JSON true enables
  --loop-config            print the config as the loop reads it, one checked
                           value per line (LOOP_CONFIG_LINES); exit 2 with the
                           reason when the config is not a JSON object or a
                           value is of the wrong kind

An item is eligible when it is pending, unblocked, not manual, not over its
failure caps, not "complex" (unless the config allows it) and not opted out
with executor_eligible: false. Items whose id is not a plain slug are never
eligible, because the id ends up in a git branch name and a worktree path.

Exit codes: 0 ok, 1 item not found (or, for --verify-transition, a difference),
2 unreadable roadmap or bad arguments.
"""

import json, math, os, re, sys, argparse, tempfile
from datetime import date, datetime, timezone
from pathlib import Path

ROADMAP = Path(__file__).parent.parent / "coordinator" / "roadmap.json"
PROMPT_TEMPLATE = Path(__file__).parent / "cas-executor-prompt.md"
# Path contracted with scripts/cas-roadmap-loop.sh: both use CAS_EXECUTOR_STATE_DIR,
# else the user's temp dir ($TMPDIR, falling back to /tmp). Local-only tracker.
FAIL_TRACKER = Path(os.environ.get("CAS_EXECUTOR_STATE_DIR") or tempfile.gettempdir()) / "cas-executor-failures"
MAX_FAILURES_PER_DAY = 3

# Item ids become git branch names (auto/<id>) and worktree paths, and are
# passed through shell code in the loop, so only plain slugs are accepted.
ITEM_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Items that need manual action (not code changes)
MANUAL_AGENTS = {"documentation"}  # social media, graphics, etc.
SKIP_STATUSES = {"completed", "deferred", "waiting_for_answer", "in_review"}
# Statuses the loop may write via --set-status, and the only extra fields it may set.
SETTABLE_STATUSES = SKIP_STATUSES | {"pending"}
SETTABLE_NOTE_KEYS = {"deferred_reason", "status_drift_note"}

# Keywords that signal items too broad for a single Claude session
COMPLEX_KEYWORDS = {"redesign", "overhaul", "rewrite", "comprehensive", "migration"}
MAX_TOTAL_FAILURES = 4  # fallback if config not readable (matches the shipped config)

CONFIG_PATH = Path(__file__).parent / "cas-executor-config.json"


def load_roadmap():
    with open(ROADMAP) as f:
        return json.load(f)


def load_config():
    """Load executor config for skip_complex_items and max_total_failures."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


class ConfigError(ValueError):
    """The executor config cannot be used as it is."""


def read_config(path=None):
    """The executor config as a dict; ConfigError when it is not a JSON object.

    NaN and Infinity are not JSON; Python's parser would accept them, and a
    NaN limit compares false against everything, so they are refused here.
    """
    path = Path(path or CONFIG_PATH)

    def not_json(name):
        raise ConfigError(f"{path.name}: {name} is not JSON")

    try:
        with open(path) as f:
            config = json.load(f, parse_constant=not_json)
    except ConfigError:
        raise
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path.name}: {exc}") from exc
    if not isinstance(config, dict):
        raise ConfigError(f"{path.name}: the config is not a JSON object")
    return config


def executor_enabled(config):
    """The one reading of "enabled", for the loop and the watchdog: only a
    JSON true enables. A missing key, null, 1 and the strings "true" and
    "True" all leave the executor off."""
    return config.get("enabled") is True


def _whole(config, key, default, minimum):
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f'"{key}" must be a whole number of at least {minimum}, not {value!r}')
    return value


def _amount(config, key, default, allow_zero):
    value = config.get(key, default)
    usable = (isinstance(value, (int, float)) and not isinstance(value, bool)
              and math.isfinite(value) and (value >= 0 if allow_zero else value > 0))
    if not usable:
        raise ConfigError(f'"{key}" must be a number {"of at least 0" if allow_zero else "above 0"}, not {value!r}')
    return value


def pause_state(value, now=None):
    """"active" when no pause is in force, otherwise the reason the loop is
    paused. A pause_until that cannot be read as a time is a pause: the key is
    there to stop the loop, so a typo must not let it run. A time written
    without a UTC offset is taken as local time."""
    if value is None or value == "":
        return "active"
    try:
        until = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return f"paused: pause_until is not an ISO-8601 time ({str(value)[:40]!r})"
    if until.tzinfo is None:
        until = until.astimezone()
    now = now or datetime.now(timezone.utc)
    return f"paused until {value}" if now < until else "active"


# A branch name the loop can put into refs/heads/<name> and a refspec.
TARGET_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")

# What --loop-config prints, in this order, one per line. The loop reads the
# lines by position (preflight_check in scripts/cas-roadmap-loop.sh). A key
# missing from the file gets the shipped config's conservative value; a
# missing "enabled" means disabled.
LOOP_CONFIG_LINES = (
    "enabled", "pause_state", "max_items_per_day", "daily_spend_ceiling_usd", "interval_minutes",
    "max_parallel", "max_budget_per_item_usd", "max_total_failures_before_defer",
    "skip_complex_items", "target_branch", "tiered_governance",
)


def loop_config(config, now=None):
    """The checked values behind LOOP_CONFIG_LINES, as a dict.

    The two safety switches are on unless the file says a JSON false:
    skip_complex_items (also on when the key is missing) and
    tiered_governance (off when the key is missing, which is the shipped
    value; any other value that is not false turns the review path on).
    """
    branch = config.get("target_branch", "master")
    if (not isinstance(branch, str) or not TARGET_BRANCH_RE.fullmatch(branch)
            or ".." in branch or "//" in branch or branch.endswith(("/", ".", ".lock"))):
        raise ConfigError(f'"target_branch" must be a plain branch name, not {branch!r}')
    return {
        "enabled": executor_enabled(config),
        "pause_state": pause_state(config.get("pause_until"), now),
        "max_items_per_day": _whole(config, "max_items_per_day", 3, 0),
        "daily_spend_ceiling_usd": _amount(config, "daily_spend_ceiling_usd", 10, True),
        "interval_minutes": _whole(config, "interval_minutes", 120, 1),
        "max_parallel": _whole(config, "max_parallel", 1, 1),
        "max_budget_per_item_usd": _amount(config, "max_budget_per_item_usd", 3, False),
        "max_total_failures_before_defer": _whole(config, "max_total_failures_before_defer", 4, 1),
        "skip_complex_items": config.get("skip_complex_items", True) is not False,
        "target_branch": branch,
        "tiered_governance": config.get("tiered_governance", False) is not False,
    }


def estimate_complexity(item):
    """Return 'simple', 'moderate', or 'complex'.

    Round 28 (2026-04-23) fix: honor the item's explicit `complexity`
    field first (set by humans or the coordinator when they already
    know the work is complex). Previously this function only scanned
    title + description length, ignoring explicit tagging. Result:
    security-hardening-top-to-bottom (tagged `complexity: complex`
    AND starting with "Deep plan + ... bulletproof ...") passed the
    auto-skip because "bulletproof" was not a COMPLEX_KEYWORDS match
    and title length was under the moderate threshold. Executor burned
    Claude spend on a 6-8 hour security overhaul that cannot be
    autonomously executed in one go."""
    # 2026-05-18: accept common-language aliases so a human who writes
    # `complexity: "small"` (or `easy` / `medium` / `standard`) doesn't
    # accidentally hit the title-keyword scan and get auto-skipped as
    # complex. Drove bug-ops-api-costs-endpoint-underreports (HIGH)
    # silently bouncing off the picker for a day because "small" wasn't
    # in the recognized set.
    explicit_raw = (item.get("complexity") or "").strip().lower()
    COMPLEXITY_ALIASES = {
        "complex": "complex",
        "moderate": "moderate",
        "medium": "moderate",
        "standard": "moderate",
        "simple": "simple",
        "small": "simple",
        "easy": "simple",
        "trivial": "simple",
    }
    if explicit_raw in COMPLEXITY_ALIASES:
        return COMPLEXITY_ALIASES[explicit_raw]

    title = item.get("title", "").lower()
    desc = item.get("description", "")
    # Description signals (auto-match) in ADDITION to title COMPLEX_KEYWORDS.
    # "comprehensive" / "deep plan" / "top to bottom" / "bulletproof"
    # all flag overhauls that a single autonomous run cannot complete.
    desc_lower = desc.lower()
    description_complex_markers = (
        "comprehensive", "deep plan", "top to bottom", "bulletproof",
        "threat model", "pen-test", "owasp top 10", "end-to-end audit",
    )

    if any(kw in title for kw in COMPLEX_KEYWORDS):
        return "complex"
    if any(marker in desc_lower for marker in description_complex_markers):
        return "complex"

    # Long descriptions with many numbered steps
    numbered = sum(1 for i, c in enumerate(desc) if c.isdigit() and i > 0 and desc[i - 1] in ".(")
    if len(desc) > 800 and numbered >= 5:
        return "complex"

    if len(desc) > 500:
        return "moderate"

    return "simple"


def get_total_failures(item_id):
    """Count total failures across all days in the tracker."""
    if not FAIL_TRACKER.exists():
        return 0
    count = 0
    for line in FAIL_TRACKER.read_text().splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[1] == item_id:
            count += 1
    return count


def get_failed_today():
    """Return set of item IDs that have failed MAX_FAILURES_PER_DAY+ times today."""
    today = date.today().isoformat()
    counts: dict[str, int] = {}
    if FAIL_TRACKER.exists():
        for line in FAIL_TRACKER.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0] == today:
                counts[parts[1]] = counts.get(parts[1], 0) + 1
    return {item_id for item_id, count in counts.items() if count >= MAX_FAILURES_PER_DAY}


def get_eligible(data):
    """Return all eligible items sorted by priority."""
    completed_ids = {i.get("id") for i in data["items"] if i.get("status") == "completed"}
    failed_ids = get_failed_today()
    config = load_config()
    skip_complex = config.get("skip_complex_items", True) is not False
    max_total = config.get("max_total_failures_before_defer", MAX_TOTAL_FAILURES)

    eligible = []
    for item in data["items"]:
        if not ITEM_ID_RE.fullmatch(str(item.get("id", ""))) or "title" not in item:
            continue
        if item.get("status") in SKIP_STATUSES:
            continue
        if item.get("status") != "pending":
            continue
        blocked = [b for b in item.get("blocked_by", []) if b not in completed_ids]
        if blocked:
            continue
        if item.get("agent") in MANUAL_AGENTS:
            continue
        if item.get("id", "") in failed_ids:
            continue
        # Skip items that have exceeded total failure threshold
        if get_total_failures(item.get("id", "")) >= max_total:
            continue
        # Skip complex items if configured
        if skip_complex and estimate_complexity(item) == "complex":
            continue
        # 2026-05-17: honor explicit `executor_eligible: false` flag for
        # operator-only tasks (DB pokes, screenshot capture, decisions)
        # and items that need human framing before autonomous attempt.
        # See coordinator/roadmap.json `executor_skip_reason` field for
        # why a given item is opted out.
        if item.get("executor_eligible") is False:
            continue
        eligible.append(item)

    eligible.sort(key=lambda x: PRIORITY_ORDER.get(x.get("priority", "low"), 3))
    return eligible


def pick_next(data):
    """Return the highest-priority pending automatable item."""
    eligible = get_eligible(data)
    return eligible[0] if eligible else None


def pick_next_n(data, n):
    """Return up to n highest-priority items. Skips items that share
    blocked_by dependencies with already-selected items (avoids parallel
    items that would produce the same completion signal)."""
    eligible = get_eligible(data)
    if not eligible:
        return []

    selected = []
    selected_ids = set()
    for item in eligible:
        if len(selected) >= n:
            break
        # Don't pick two items that block each other
        item_blocks = set(item.get("blocked_by", []))
        if item_blocks & selected_ids:
            continue
        selected.append(item)
        selected_ids.add(item["id"])

    return selected


def set_item_status(item_id, status, note=None, path=None):
    """Set one item's status, optionally with one note field; atomic write.

    note is a (key, value) pair; key must be in SETTABLE_NOTE_KEYS. Returns True
    when the item was found and written, False when it does not exist (the file
    is then left untouched). The new content goes to <path>.tmp and is moved
    over the original with os.replace, so a crash cannot leave a half-written
    roadmap.json behind. path defaults to ROADMAP (looked up at call time).
    """
    if status not in SETTABLE_STATUSES:
        raise ValueError(f"status {status!r} is not settable (allowed: {sorted(SETTABLE_STATUSES)})")
    if note is not None and note[0] not in SETTABLE_NOTE_KEYS:
        raise ValueError(f"note key {note[0]!r} is not allowed (allowed: {sorted(SETTABLE_NOTE_KEYS)})")
    path = Path(path or ROADMAP)
    with open(path) as f:
        data = json.load(f)
    for item in data["items"]:
        if item.get("id") == item_id:
            item["status"] = status
            if note is not None:
                item[note[0]] = note[1]
            break
    else:
        return False
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return True


def transition_problem(before, after, item_id, status, note=None):
    """Say why `after` is not `before` plus one status transition, or None.

    before and after are two parsed roadmap documents. The loop commits a
    roadmap.json only when the difference between HEAD's copy and the file is
    exactly what set_item_status(item_id, status, note) writes: that item's
    "status", and the one note field when a note is given. Every other item,
    the order of the items, every other field of that item and every
    top-level key must be equal. Values are compared after parsing, so the
    re-indentation set_item_status does is not a difference.
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        return "the roadmap is not a JSON object"
    if set(before) != set(after):
        return f"top-level keys differ: {sorted(set(before) ^ set(after))}"
    for key in before:
        if key != "items" and before[key] != after[key]:
            return f"top-level field {key!r} changed"
    old_items, new_items = before.get("items"), after.get("items")
    if not isinstance(old_items, list) or not isinstance(new_items, list):
        return "\"items\" is not a list"
    if len(old_items) != len(new_items):
        return f"the number of items changed from {len(old_items)} to {len(new_items)}"
    allowed = {"status"} | ({note[0]} if note is not None else set())
    seen = 0
    for position, (old, new) in enumerate(zip(old_items, new_items)):
        if not isinstance(old, dict) or not isinstance(new, dict):
            if old != new:
                return f"item {position} changed"
            continue
        if old.get("id") != new.get("id"):
            return f"item {position} is {new.get('id')!r}, was {old.get('id')!r}"
        if old.get("id") != item_id:
            if old != new:
                return f"item {old.get('id')!r} changed, and it is not the item being marked"
            continue
        seen += 1
        changed = {k for k in set(old) | set(new) if old.get(k, _MISSING) != new.get(k, _MISSING)}
        if changed - allowed:
            return f"item {item_id!r} changed in {sorted(changed - allowed)}, not only in {sorted(allowed)}"
        if new.get("status") != status:
            return f"item {item_id!r} has status {new.get('status')!r}, expected {status!r}"
        if note is not None and new.get(note[0], _MISSING) != note[1]:
            return f"item {item_id!r} does not carry the expected {note[0]}"
    if seen != 1:
        return f"item {item_id!r} appears {seen} times"
    return None


_MISSING = object()


def build_prompt(item):
    """Build the full Claude Code prompt for executing an item."""
    template = PROMPT_TEMPLATE.read_text() if PROMPT_TEMPLATE.exists() else ""
    item_block = json.dumps(item, indent=2)
    return template.replace("{{ITEM_JSON}}", item_block).replace("{{ITEM_ID}}", item["id"]).replace("{{ITEM_TITLE}}", item["title"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--next", action="store_true", help="Print next item as JSON")
    parser.add_argument("--next-n", metavar="N", type=int, help="Print next N items as JSON array")
    parser.add_argument("--build-prompt", metavar="ITEM_ID", help="Build prompt for item")
    parser.add_argument("--list", action="store_true", help="List all eligible items")
    parser.add_argument("--set-status", nargs=2, metavar=("ITEM_ID", "STATUS"),
                        help="Set an item's status in roadmap.json (atomic write)")
    parser.add_argument("--verify-transition", nargs=3, metavar=("BEFORE_FILE", "ITEM_ID", "STATUS"),
                        help="Check that roadmap.json is BEFORE_FILE plus exactly one --set-status change")
    parser.add_argument("--note", nargs=2, metavar=("KEY", "VALUE"),
                        help="With --set-status or --verify-transition: one note field "
                             f"({', '.join(sorted(SETTABLE_NOTE_KEYS))})")
    parser.add_argument("--enabled", action="store_true",
                        help="Exit 0 when the config enables the executor (a JSON true), 1 otherwise")
    parser.add_argument("--loop-config", action="store_true",
                        help="Print the checked config values the loop reads, one per line")
    args = parser.parse_args()

    if args.enabled or args.loop_config:
        try:
            config = read_config()
            if args.enabled:
                sys.exit(0 if executor_enabled(config) else 1)
            values = loop_config(config)
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1 if args.enabled else 2)
        for name in LOOP_CONFIG_LINES:
            print(str(values[name]).replace("\n", " "))
        return

    if args.verify_transition:
        before_file, item_id, status = args.verify_transition
        try:
            with open(before_file) as f:
                before = json.load(f)
            after = load_roadmap()
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(2)
        problem = transition_problem(before, after, item_id, status,
                                     tuple(args.note) if args.note else None)
        if problem:
            print(f"roadmap.json is not the expected single change: {problem}", file=sys.stderr)
            sys.exit(1)
        return

    if args.set_status:
        item_id, status = args.set_status
        try:
            found = set_item_status(item_id, status, tuple(args.note) if args.note else None)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(2)
        if not found:
            print(f"Item {item_id} not found", file=sys.stderr)
            sys.exit(1)
        return

    try:
        data = load_roadmap()
    except (OSError, ValueError) as exc:
        print(f"error: cannot read {ROADMAP}: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.next:
        item = pick_next(data)
        if item:
            print(json.dumps(item))
        else:
            print("null")
    elif args.next_n:
        items = pick_next_n(data, args.next_n)
        print(json.dumps(items))
    elif args.build_prompt:
        for item in data["items"]:
            if item.get("id") == args.build_prompt and ITEM_ID_RE.fullmatch(args.build_prompt):
                print(build_prompt(item))
                return
        print(f"Item {args.build_prompt} not found", file=sys.stderr)
        sys.exit(1)
    elif args.list:
        completed_ids = {i.get("id") for i in data["items"] if i.get("status") == "completed"}
        for item in data["items"]:
            if not item.get("id"):
                continue
            if item.get("status") in SKIP_STATUSES:
                continue
            if item.get("status") != "pending":
                continue
            blocked = [b for b in item.get("blocked_by", []) if b not in completed_ids]
            markers = []
            if blocked:
                markers.append("BLOCKED")
            complexity = estimate_complexity(item)
            if complexity == "complex":
                markers.append("COMPLEX")
            elif complexity == "moderate":
                markers.append("MOD")
            total_fails = get_total_failures(item.get("id", ""))
            if total_fails > 0:
                markers.append(f"FAILS:{total_fails}")
            tag = f" [{', '.join(markers)}]" if markers else ""
            print(f"  {item.get('priority', 'low'):8s} {item['id']}: {item.get('title', '?')[:55]}{tag}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
