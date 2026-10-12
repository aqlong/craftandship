"""scripts/honest_completion_audit.py: the Stop hook behind the honest-completion protocol.

The hook is a phrase tripwire, and these tests pin exactly that, including what
it does not catch:

  * the red-flag phrases CLAUDE.md lists each trip it, and a plain "done" or
    "fixed" does not;
  * every form of the enumeration CLAUDE.md documents satisfies it;
  * an empty enumeration ("Gaps: none", "Not verified: n/a", a label with
    nothing under it) does not, and the explicit no-gaps form counts only when
    it lists what was observed;
  * run as a process with a transcript on disk, it exits 2 once and then lets
    the stop through (block once), and exits 0 on any input it cannot read
    (fail open);
  * in .claude/settings.example.json it is the only hook that can block.
"""

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "honest_completion_audit.py"

_spec = importlib.util.spec_from_file_location("honest_completion_audit", SCRIPT)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

CLAIM = "All tests pass."


def blocked(text: str) -> bool:
    return hook.audit(text) is not None


# ---------------------------------------------------------------------------
# What trips it
# ---------------------------------------------------------------------------

def _documented(lead_in: str) -> str:
    """The paragraph of CLAUDE.md's Honest-completion protocol that starts with
    this bold lead-in. These two tests tie the working agreement's wording to
    the hook: if the paragraph is renamed or removed they fail, on purpose."""
    text = (ROOT / "CLAUDE.md").read_text()
    protocol = text[text.index("## Honest-completion protocol"):]
    paragraph = protocol[protocol.index("**" + lead_in):]
    return paragraph.split("\n\n", 1)[0]


def test_every_red_flag_phrase_the_agreement_lists_trips_the_hook():
    listed = _documented("Red-flag phrases").split(":**", 1)[1]
    phrases = [p.strip(" .") for p in listed.split(",")]
    assert len(phrases) >= 12, phrases
    for phrase in phrases:
        assert blocked(f"Summary: {phrase}."), f"{phrase!r} is documented as a red flag but passes"


@pytest.mark.parametrize("text", [
    "Done. The fix is complete and deployed.",
    "Fixed, verified, and shipped. It works.",
    "Implemented the change and ran the tests.",
    "",
    "   \n",
])
def test_plain_completion_words_are_not_red_flags(text):
    """The limit the docstring states: only the listed phrases are claims."""
    assert not blocked(text)


# ---------------------------------------------------------------------------
# What satisfies it
# ---------------------------------------------------------------------------

def test_every_documented_form_of_the_enumeration_is_accepted():
    paragraph = _documented("Accepted forms")
    # the form templates are the backticked strings with a placeholder in them
    forms = [f for f in re.findall(r"`([^`]+)`", paragraph) if "..." in f or "<" in f]
    assert len(forms) >= 9, forms
    assert any(f.startswith("Gaps: none, observed end-to-end:") for f in forms)
    for form in forms:
        filled = form.replace("...", "the mobile layout and the concurrent-delete race")
        filled = filled.replace("<complete list of observations>", "signup, login and checkout, each clicked through")
        assert not blocked(f"{CLAIM}\n{filled}"), f"documented form is refused: {filled!r}"


@pytest.mark.parametrize("enumeration", [
    "Not verified: the mobile layout.",
    "I have NOT tested the concurrent delete path.",
    "I haven't run the Playwright suite.",
    "Not verified:\n- mobile layout\n- dark mode",
    "**Not verified:** the staging deploy",
    "Not verified: none of the mobile flows were run.",
    "Not verified: nothing beyond the unit tests was executed.",
    "Deferred: re-check the cron job tomorrow.",
    "Gaps: none, observed end-to-end: signup, login, checkout in a browser.",
    "No gaps: observed end-to-end: every route returned the expected page.",
    "Gaps: none, observed end-to-end:\n- signup\n- login",
])
def test_a_real_enumeration_lets_the_claim_through(enumeration):
    assert not blocked(f"{CLAIM}\n\n{enumeration}")


# ---------------------------------------------------------------------------
# An empty enumeration is not an enumeration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("enumeration", [
    # the two the review ran
    "Gaps: none",
    "Not verified: n/a",
    # the same thing in other clothes
    "Gaps: none.", "Gaps: None!", "gaps : none", "**Gaps:** none", "**Gaps**: none",
    "Not verified: N/A", "Not verified: nothing.", "Not verified: nothing", "Not verified: nil",
    "Not verified:", "Not verified: -", "Not verified:\n- none", "Not verified:\n\nNext steps follow.",
    "Unchecked: none", "Deferred: n/a", "Did NOT: nothing",
    "Gaps: none, everything was perfect",
    "Gaps: none (all verified)",
    # the explicit form without the observations it exists to carry
    "Gaps: none, observed end-to-end:", "Gaps: none, observed end-to-end: n/a",
    "Gaps: none, observed end-to-end: nothing",
    # declarations the old pattern accepted with no list at all
    "No gaps: all good", "No gaps - shipped", "Nothing left unverified.",
    "There is nothing unchecked.", "No untested paths remain.",
])
def test_an_empty_enumeration_does_not_satisfy_the_hook(enumeration):
    assert blocked(f"{CLAIM}\n\n{enumeration}"), f"{enumeration!r} was accepted as an enumeration"


def test_an_empty_label_next_to_a_real_one_still_passes():
    assert not blocked(f"{CLAIM}\nDeferred: none\nNot verified: the mobile layout")
    # a line that ends in "no" does not deny the label on the next line
    assert not blocked(f"{CLAIM} Regressions: no\nNot verified: the mobile layout")


def test_known_limit_a_gap_phrase_in_passing_satisfies_it():
    """Stated in the docstring: the hook cannot tell a real gap from the phrase."""
    assert not blocked("All tests pass. I did NOT rename the file.")


def test_the_explicit_no_gaps_pattern_has_no_repeated_branch():
    pattern = hook.EXPLICIT_NO_GAPS_RE.pattern
    for group in re.findall(r"\(\?:([^()]*)\)", pattern):
        branches = group.split("|")
        assert len(branches) == len(set(branches)), f"repeated alternative in (?:{group})"


# ---------------------------------------------------------------------------
# The hook as a process: block once, fail open
# ---------------------------------------------------------------------------

def _transcript(tmp_path, *assistant_messages):
    path = tmp_path / "transcript.jsonl"
    lines = [json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}})]
    for text in assistant_messages:
        lines.append(json.dumps({"type": "assistant",
                                 "message": {"content": [{"type": "text", "text": text}]}}))
    lines.append("this line is not JSON")
    path.write_text("\n".join(lines) + "\n")
    return path


def _run_hook(stdin: str):
    return subprocess.run([sys.executable, str(SCRIPT)], input=stdin, capture_output=True,
                          text=True, timeout=30)


def test_process_blocks_a_bare_claim_with_exit_2_and_explains(tmp_path):
    transcript = _transcript(tmp_path, "Working on it.", "All green. Gaps: none")
    result = _run_hook(json.dumps({"transcript_path": str(transcript), "stop_hook_active": False}))
    assert result.returncode == 2
    assert "HONEST-COMPLETION PROTOCOL VIOLATION" in result.stderr
    assert "does not count" in result.stderr


def test_process_reads_only_the_last_assistant_message(tmp_path):
    transcript = _transcript(tmp_path, "All green.", "Looking at the next file now.")
    result = _run_hook(json.dumps({"transcript_path": str(transcript)}))
    assert result.returncode == 0


def test_process_allows_a_claim_with_a_real_enumeration(tmp_path):
    transcript = _transcript(tmp_path, "All green.\nNot verified: the mobile layout.")
    assert _run_hook(json.dumps({"transcript_path": str(transcript)})).returncode == 0


def test_process_blocks_at_most_once(tmp_path):
    """stop_hook_active marks the stop that follows a blocked one; it is allowed
    even though the message still has the bare claim, so a stop cannot loop."""
    transcript = _transcript(tmp_path, "Production-ready.")
    first = _run_hook(json.dumps({"transcript_path": str(transcript), "stop_hook_active": False}))
    second = _run_hook(json.dumps({"transcript_path": str(transcript), "stop_hook_active": True}))
    assert (first.returncode, second.returncode) == (2, 0)


@pytest.mark.parametrize("stdin", [
    "", "not json", "[]", "null", "{}", '{"transcript_path": 7}',
    '{"transcript_path": "/nonexistent/transcript.jsonl"}',
])
def test_process_fails_open_on_input_it_cannot_use(stdin):
    result = _run_hook(stdin)
    assert result.returncode == 0 and result.stderr == ""


def test_process_fails_open_on_an_unreadable_transcript(tmp_path):
    binary = tmp_path / "transcript.jsonl"
    binary.write_bytes(b"\xff\xfe\x00 not text \x80\n")
    assert _run_hook(json.dumps({"transcript_path": str(binary)})).returncode == 0
    assert _run_hook(json.dumps({"transcript_path": str(tmp_path)})).returncode == 0  # a directory


# ---------------------------------------------------------------------------
# The example settings: which hooks can block
# ---------------------------------------------------------------------------

def test_the_audit_is_the_only_hook_in_the_example_settings_that_can_block():
    settings = json.loads((ROOT / ".claude" / "settings.example.json").read_text())
    commands = {
        event: [hook_def["command"] for group in groups for hook_def in group["hooks"]]
        for event, groups in settings["hooks"].items()
    }
    assert {event: len(cmds) for event, cmds in commands.items()} == {
        "PreToolUse": 1, "PostToolUse": 4, "Stop": 1}

    # The Stop hook is this script, whose exit status 2 refuses the stop.
    assert commands["Stop"] == ['python3 "$CLAUDE_PROJECT_DIR/scripts/honest_completion_audit.py"']

    # The other five can only add text: none exits 2 or returns a decision, and
    # each one's only output is additionalContext.
    doc_check = (ROOT / "scripts" / "doc_update_check.sh").read_text()
    for command in commands["PreToolUse"] + commands["PostToolUse"]:
        source = doc_check if command.endswith("doc_update_check.sh\"") else command
        assert "additionalContext" in source
        assert not re.search(r"exit\s+2\b", source)
        assert "permissionDecision" not in source and '"decision"' not in source
        assert '\\"decision\\"' not in source
