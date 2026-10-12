#!/usr/bin/env python3
"""Stop-hook: a phrase tripwire for the honest-completion protocol.

What it does: when the assistant's last message contains one of a short list
of overconfident phrases (CLAIMS_RE: "production-ready", "all green", "all
tests pass", "works perfectly", ...), and the message has no not-verified
enumeration, the hook refuses the stop once and feeds the reason back so the
message is revised.

What counts as the enumeration:
  - one of the gap phrases in GAPS_RE anywhere in the message ("Not verified:",
    "I have NOT tested ...", "Deferred:", "relied on memory", ...). Used as a
    label ("Not verified: ..."), it must have something after it: a label
    followed by nothing, "none", "n/a" or "nothing" is an empty enumeration and
    does not count, and neither does a phrase that is being denied ("No gaps",
    "nothing left unverified");
  - or the explicit no-gaps form, which has to say what was observed:
    "Gaps: none, observed end-to-end: <list>" (EXPLICIT_NO_GAPS_RE).

What it does not do. It is a tripwire, not a judge:
  - a plain "done", "fixed", "verified" or "shipped" is not in CLAIMS_RE and
    passes untouched;
  - it does not check that a listed gap is real or relevant. A gap phrase used
    in passing ("I did not rename the file") satisfies it;
  - it blocks at most once per stop. Claude Code sets stop_hook_active on the
    stop that follows a blocked one, and the hook then allows it; without that
    a message the model cannot or will not fix would loop forever;
  - it fails open: unreadable input, a missing transcript or any error in the
    hook allows the stop. A broken hook must not trap a session.
Both of the last two are deliberate.

Of the hooks in .claude/settings.example.json this is the only one that can
block anything; the PreToolUse and PostToolUse hooks there only add reminder
text to the model's context.

Rationale: see CLAUDE.md "Honest-completion protocol". Wire it as a Claude Code
Stop hook (command: python3 scripts/honest_completion_audit.py) in your
.claude/settings.json.

Contract (stdin JSON):
    {"session_id": "...", "transcript_path": "...", "stop_hook_active": bool}

Exits 0 = allow stop, 2 = block with stderr re-injected as context.
"""
from __future__ import annotations

import json
import os
import re
import sys


# Phrases that constitute a claim of completion/success.
# Keep these specific: overly broad matches produce false blocks.
CLAIMS_RE = re.compile(
    r"\b("
    r"production[- ]?ready|all[- ]?green|"
    r"everything (?:works|is working|checks out|looks (?:good|great))|"
    r"all (?:tests?|checks?) (?:pass|passing|green)|"
    r"fully (?:verified|tested|working)|works perfectly|"
    r"complete(?:ly)? (?:verified|working)|"
    r"ready (?:to ship|for (?:prod|production))|"
    r"totally worked|deploy(?:ment)? successful|"
    r"shipped (?:and|&) verified|verified end[- ]to[- ]end"
    r")\b",
    re.IGNORECASE,
)

# Phrases that satisfy the "what I did NOT do" enumeration requirement.
GAPS_RE = re.compile(
    r"("
    r"not (?:verified|tested|checked|executed|exercised)|"
    r"haven'?t (?:verified|tested|checked|run|executed)|"
    r"did\s*NOT|NOT verified|NOT tested|NOT checked|"
    r"skipped (?:verification|testing|the check)|"
    r"assumed (?:without|from memory|but didn'?t)|"
    r"unverified|unchecked|un-?tested|"
    r"gaps?\s*:|unchecked\s*:|deferred\s*:|"
    r"to re-?check|follow[- ]?up check|needs (?:verification|re-?check)|"
    r"trust[- ]but[- ]verify|edge cases? not|"
    r"did not (?:run|execute|click|exercise)|"
    r"relied on memory|recalled from memory"
    r")",
    re.IGNORECASE,
)

# The explicit no-gaps declaration. It satisfies the rule only in the form that
# says what was observed: "Gaps: none, observed end-to-end: <list>" (also
# "No gaps: observed ...: <list>"). The list after the last colon must not be
# empty; _has_enumeration checks that.
EXPLICIT_NO_GAPS_RE = re.compile(
    r"(?:gaps?\s*:\s*none|no\s+gaps)\s*[,;:.-]?\s*"
    r"(?:all\s+)?observed(?:\s+end[- ]to[- ]end)?\s*:",
    re.IGNORECASE,
)

# A label value that means "there is nothing here": blank or punctuation only
# (see _is_empty_value), or none / n/a / nothing / nil followed by the end of
# the line or by punctuation. ("none of the mobile flows were run" names a gap
# and is not empty.)
_EMPTY_VALUE_RE = re.compile(r"(?:none|n/?a|nothing|nil)\b\s*(?:[^\w\s].*)?$", re.IGNORECASE)
_LIST_MARKER_RE = re.compile(r"^(?:[-*+>]|\d+[.)])\s*(?:\[[ xX]?\]\s*)?")


def _label_value(text: str, start: int) -> str | None:
    """The value of an enumeration label, or None when the phrase is not a label.

    `start` is where the matched phrase ends. The phrase is a label when a
    colon follows it (or ended it), Markdown emphasis marks aside. The value is
    the rest of that line; if the line ends at the colon, it is the next line,
    without its list marker (a bulleted list under the label). A blank next
    line means no value.
    """
    if text[start - 1:start] != ":":
        after = text[start:]
        stripped = after.lstrip(" \t*_`")
        if not stripped.startswith(":"):
            return None
        start += len(after) - len(stripped) + 1
    line, _, rest = text[start:].partition("\n")
    value = line.strip(" \t*_`")
    if not value:
        next_line = rest.partition("\n")[0].strip()
        value = _LIST_MARKER_RE.sub("", next_line).strip(" \t*_`")
    return value


def _is_empty_value(value: str) -> bool:
    if not re.search(r"\w", value):
        return True
    return _EMPTY_VALUE_RE.match(value) is not None


# "No gaps: ...", "nothing left unverified", "no untested paths": the gap phrase
# is being denied, not used. Checked against the text just before the phrase,
# on the same line.
_DENIAL_BEFORE_RE = re.compile(
    r"\b(?:no|nothing(?:[ \t]+(?:is|was|left|remains|remaining))*)[ \t]+$", re.IGNORECASE)


def _has_enumeration(text: str) -> bool:
    """True when the message carries a not-verified enumeration that is not empty."""
    for match in GAPS_RE.finditer(text):
        if _DENIAL_BEFORE_RE.search(text[:match.start()]):
            continue
        value = _label_value(text, match.end())
        if value is None or not _is_empty_value(value):
            return True
    for match in EXPLICIT_NO_GAPS_RE.finditer(text):
        observed = _label_value(text, match.end())
        if observed is not None and not _is_empty_value(observed):
            return True
    return False


def read_last_assistant_text(transcript_path: str) -> str:
    """Return the concatenated text blocks of the most recent assistant message."""
    last_text = ""
    try:
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                msg = entry.get("message") or {}
                content = msg.get("content") or []
                blocks = [
                    c.get("text", "")
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
                if blocks:
                    last_text = "\n".join(blocks)
    except (OSError, ValueError):
        return ""
    return last_text


def audit(text: str) -> str | None:
    """Return a violation message, or None if the text passes."""
    if not text or not text.strip():
        return None

    has_claim = bool(CLAIMS_RE.search(text))
    if not has_claim:
        return None

    if _has_enumeration(text):
        return None

    return (
        "HONEST-COMPLETION PROTOCOL VIOLATION: your response claims "
        "completion/success (e.g. 'production-ready', 'all green', 'totally worked', "
        "'deploy successful') without the required enumeration of what you did NOT "
        "verify. Per CLAUDE.md 'Honest-completion protocol', every success claim "
        "MUST be paired with one of:\n"
        "  (a) an explicit 'Not verified:' / 'Did NOT:' / 'Unchecked:' / 'Gaps:' / "
        "'Deferred:' section listing untested edge cases, branches not executed "
        "end-to-end, assumptions taken from memory without re-checking current code, "
        "or items the user should re-check later. A label followed by 'none', 'n/a' "
        "or nothing does not count; OR\n"
        "  (b) an explicit 'Gaps: none, observed end-to-end: <list>' declaration "
        "when you genuinely executed every branch and observed every outcome. The "
        "list of observations is required.\n"
        "Revise your response before returning control to the user. Add the section; "
        "do not remove the claim. If a gap was deferred for a later session, also "
        "record it in your project's follow-up notes so it surfaces next session."
    )


def main() -> int:
    # Fail open: any problem reading the hook input allows the stop.
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0

    # Block at most once: this is the stop that follows a blocked one.
    if payload.get("stop_hook_active"):
        return 0

    transcript = payload.get("transcript_path") or ""
    if not isinstance(transcript, str) or not transcript or not os.path.exists(transcript):
        return 0

    try:
        violation = audit(read_last_assistant_text(transcript))
    except Exception:  # noqa: BLE001 - fail open, see the module docstring
        return 0
    if violation is None:
        return 0

    sys.stderr.write(violation + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
