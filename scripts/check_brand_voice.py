#!/usr/bin/env python3
"""
Brand-voice lint: reject customer-copy violations on the staged files.

Hard rules enforced (see docs/BRAND_VOICE.md):

1. Em dashes (U+2014) and &mdash;.
2. Competitor framing on customer surfaces (Harbor v2, 2026-04-26): named
   competitor mentions and "vs / switching from" idioms. We are one-of-a-kind
   and do not compare on customer surfaces.
3. Technical-ownership marketing copy (Harbor v2, 2026-04-26): "source code,"
   "GitHub," "self-host," "Dockerfile" do not lead in EJS body copy. Soft
   ownership reassurance is fine in FAQ wording; this rule fires anywhere
   those terms appear in customer-scope files unless allow-listed.

Invoked from .githooks/pre-commit against the staged copy files. Exits 0 on
clean, 1 on violation. Prints every violating line with a file:line pointer.

Allow-list: a line immediately preceded by a comment containing one of
    brand-voice-allow-em-dash: <reason>
    brand-voice-allow-competitor: <reason>
    brand-voice-allow-tech-ownership: <reason>
is skipped. This is for rare cases (e.g. quoting a testimonial that names a
competitor; legal copy that mentions a payment processor). Each allowance is
reviewed.

Two scoped waivers are built in, because the rules target marketing copy and
these are not marketing copy:
  - COMPETITOR_RULE_EXEMPT_PATHS: pages whose purpose is to cite the public
    pricing sources behind a claim (today only web/views/docs-research.ejs,
    the "Research & sources" index). They must name the vendors they cite.
    Only the competitor rule is waived there; the em-dash and
    technical-ownership rules still apply.
  - Avoidance instructions (technical-ownership rule only): a line such as
    'Avoid jargon like "self-host"' tells a reader or an LLM prompt NOT to use
    the term, so it is not ownership marketing. See _is_avoidance_instruction.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

EM_DASH = "\u2014"
HTML_MDASH = "&mdash;"
ALLOW_MARKER = "brand-voice-allow-em-dash"
ALLOW_COMPETITOR_MARKER = "brand-voice-allow-competitor"
ALLOW_TECH_OWNERSHIP_MARKER = "brand-voice-allow-tech-ownership"

# Named competitors that must not appear on customer surfaces (Harbor v2 rule).
# These are case-insensitive whole-word matches (\b), so "Squarespace" trips but
# the variable name "square_space_id" wouldn't (no boundary on `_`). Pi is too
# short and ambiguous (math constant, file path); we only catch unmistakable
# brand mentions. Deliberately conservative to avoid false positives.
_COMPETITOR_NAMES = (
    "Mindbody",
    "Acuity",
    "Vagaro",
    "Booker",
    "Squarespace",
    "Webflow",
    "Practice Better",
    "Practice.do",
    "Kajabi",
    "Calendly",
    "Thinkific",
    "Teachable",
    "WellnessLiving",
    "Glofox",
    "Pike13",
    "Simplero",
    "Podia",
    "Headspace",
    "Replika",
    "Character.AI",
)
_COMPETITOR_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(n) for n in _COMPETITOR_NAMES) + r")\b",
    re.IGNORECASE,
)

# Anti-subscription / comparison idioms that shouldn't appear on customer
# surfaces. These are full phrase matches; we use word-boundaries on either
# side to keep them precise.
_COMPARE_IDIOMS = (
    r"\bvs\.?\b",
    r"\bversus\b",
    r"\bswitching from\b",
    r"\bcancel anytime\b",
    r"\bno contracts\b",
    r"\bno setup fees\b",
    r"\blifetime access\b",
    r"\bown your data\b",
    r"\bown your audience\b",
)
_COMPARE_RE = re.compile("|".join(_COMPARE_IDIOMS), re.IGNORECASE)

# Technical-ownership marketing phrases. Healers don't shop for these; the
# soft reassurance in FAQ uses different wording ("If you ever leave, we hand
# the whole site over intact"). Allow-listed where legitimately needed in
# legal copy or technical email subjects.
_TECH_OWNERSHIP_RE = re.compile(
    r"\b(?:source code|github|self[- ]host|dockerfile|own your code)\b",
    re.IGNORECASE,
)

# Paths skipped because the em-dash check does not apply:
#   - admin-*.ejs and _admin-*.ejs: admin-only views; em dash is used as a
#     typographic "no value" glyph in data tables, not customer copy.
#   - 404.ejs / 500.ejs: error pages; revisit if they ever grow long copy.
SKIP_FRAGMENTS = (
    "web/views/admin-",
    "web/views/_admin-",
)

# Files exempt from the competitor rule (rule 2) only. docs-research.ejs is the
# citation index behind the pricing claims: every row is a link to a named
# vendor's public pricing or terms page, so naming the vendors is the page's
# whole purpose (app.ts carries the matching allow marker for its meta
# description). Keep this list to citation pages; never add marketing copy.
COMPETITOR_RULE_EXEMPT_PATHS = (
    "web/views/docs-research.ejs",
)

# Cues that a line is telling the reader NOT to use a term. Matched only against
# the text that precedes the flagged term on the same line.
_AVOIDANCE_CUE_RE = re.compile(
    r"\b(?:avoid|never|do not|don't|must not|should not|shouldn't|not use|not say)\b",
    re.IGNORECASE,
)

# Extensions whose JS/TS code comments (`//` and `/* ... */`) are developer
# context, not customer copy. We strip them before scanning so a pragma like
# `// POST /builds/:buildId, blah` does not trip the lint. Customer-facing
# strings in these files (template literals, JSX text, subject/body of emails)
# are still scanned.
# Compose widget EJS files have a different scan model. They are server-side
# templates whose <!-- ... --> HTML comments ship to the rendered page source
# even though they don't render visually. EJS scriptlet comments (<%# ... %>,
# <% // ... %>) are server-side only and not customer copy. Intentional `em dash`
# glyphs in template body content (e.g. admin table "no value" placeholders)
# are not customer copy either. So for compose/widgets/**/*.ejs we restrict
# the em-dash check to HTML-comment regions only.
COMPOSE_WIDGET_PATH_FRAG = "compose/widgets/"

JS_TS_COMMENT_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

# Python files whose `#` line comments and triple-quoted docstrings are
# developer context. Strip both before scanning so notes in comments and
# module-level docstrings do not trip the lint, while customer-facing string
# literals (email HTML, subjects, _append_log messages) still get checked.
PY_COMMENT_EXTS = (".py",)

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_PY_TRIPLE_STRING_RE = re.compile(r'("""|\'\'\').*?\1', re.DOTALL)


def _strip_js_comments(text: str) -> str:
    """Remove JS/TS code comments so scanner focuses on runtime strings.

    Naive but sufficient: strips /* ... */ blocks, then strips //... to end
    of line. Does NOT try to preserve em dashes inside string literals that
    happen to contain "//" that's fine; the lint would have flagged them
    anyway, and string-literal content is the thing we DO want to check.

    EXCEPT: lines containing a `brand-voice-allow-*` marker must be preserved
    as-is so the allow-list logic can find them in the line directly above
    a violation. Otherwise the marker line becomes empty whitespace and the
    `_allowed` check (which inspects lines[idx-1]) misses it.
    """
    text = _BLOCK_COMMENT_RE.sub("", text)
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        # Preserve allow-marker comment lines verbatim.
        if "brand-voice-allow-" in line:
            cleaned_lines.append(line)
            continue
        idx = line.find("//")
        if idx != -1:
            # Preserve the line's leading bytes (string literals, code) and
            # drop the tail so the line numbering stays stable when we rebuild.
            cleaned_lines.append(line[:idx])
        else:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


_PY_PLAIN_TRIPLE_RE = re.compile(
    r'(?<![fFbBrR])(?<![fFbBrR][rRbB])(?<![rRbB][fF])'
    r'("""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\')'
)


def _strip_py_comments(text: str) -> str:
    # Strip plain triple-quoted docstrings while preserving line numbers.
    # f-string triple-quotes are NOT stripped because customer-facing email
    # HTML in mailer.py lives inside those blocks. Then strip `#` line comments
    # that appear outside of single/double-quoted string literals.
    def _blank_out(match: "re.Match[str]") -> str:
        return "\n" * match.group(0).count("\n")

    text = _PY_PLAIN_TRIPLE_RE.sub(_blank_out, text)

    cleaned_lines: list[str] = []
    for line in text.splitlines():
        # Preserve allow-marker comment lines verbatim (same rationale as
        # _strip_js_comments: the line directly above a violation must keep
        # the marker text for _allowed() to find it).
        if "brand-voice-allow-" in line:
            cleaned_lines.append(line)
            continue
        in_single = False
        in_double = False
        cut = -1
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == "#" and not in_single and not in_double:
                cut = i
                break
            i += 1
        cleaned_lines.append(line if cut == -1 else line[:cut])
    return "\n".join(cleaned_lines)


def _compose_widget_em_dash_violations(text: str) -> list[tuple[int, str, str]]:
    """Em-dash check restricted to HTML comments of a compose widget EJS file.

    Compose widget files use <!-- compose:widget X - description --> headers
    (and occasionally multi-line HTML comments). Those ship to the rendered
    HTML source even though they aren't visible to users. Per BRAND_VOICE.md
    rule 1 they must not contain em dashes (U+2014 or &mdash;).

    Internal EJS scriptlet comments (<%# %>, <% // %>) and intentional `em dash`
    glyphs in template body output are NOT scanned by this path.

    Allow-list: a line with a `brand-voice-allow-em-dash:` marker above (or
    inline before the violation) is honored, same as the main rule.
    """
    lines = text.splitlines()
    violations: list[tuple[int, str, str]] = []
    in_html_comment = False
    flagged_lines: set[int] = set()
    for idx, line in enumerate(lines):
        pos = 0
        while pos <= len(line):
            if not in_html_comment:
                start = line.find("<!--", pos)
                if start == -1:
                    break
                end = line.find("-->", start)
                if end == -1:
                    segment = line[start:]
                    in_html_comment = True
                    pos = len(line) + 1
                else:
                    segment = line[start:end + 3]
                    pos = end + 3
            else:
                end = line.find("-->", pos)
                if end == -1:
                    segment = line[pos:]
                    pos = len(line) + 1
                else:
                    segment = line[pos:end + 3]
                    in_html_comment = False
                    pos = end + 3
            if EM_DASH in segment or HTML_MDASH in segment:
                flagged_lines.add(idx)
    for idx in sorted(flagged_lines):
        line = lines[idx]
        first_em_idx = min(
            (line.index(EM_DASH) if EM_DASH in line else len(line)),
            (line.index(HTML_MDASH) if HTML_MDASH in line else len(line)),
        )
        if _allowed(idx, lines, line, ALLOW_MARKER, first_em_idx):
            continue
        violations.append((idx + 1, line.rstrip(), "em-dash"))
    return violations


def _allowed(idx: int, lines: list[str], line: str, marker: str, first_hit_idx: int) -> bool:
    """Return True if the violation at lines[idx] is allow-listed by `marker`.

    Allow forms:
      - Previous line contains the marker (in a comment or anywhere).
      - Same line has the marker BEFORE the violation site (inline allow).
    """
    if idx > 0 and marker in lines[idx - 1]:
        return True
    if marker in line and line.index(marker) < first_hit_idx:
        return True
    return False


def _is_avoidance_instruction(line: str, hit_start: int) -> bool:
    """True when the text before `hit_start` tells the reader to avoid the term.

    Example: an LLM system-prompt bullet 'Avoid jargon like "self-host."' names
    the term in order to forbid it. Used for the technical-ownership rule only.
    """
    return _AVOIDANCE_CUE_RE.search(line[:hit_start]) is not None


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return [(line_no, line_text, rule)] for each violation in path.

    `rule` is one of: 'em-dash', 'competitor', 'tech-ownership'.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"WARN: could not read {path}: {exc}", file=sys.stderr)
        return []

    # Compose widget EJS files: only HTML comments are customer-shipped copy.
    # Restrict scanning to em dashes inside those comment regions; skip the
    # competitor / tech-ownership rules (compose widgets don't carry that
    # kind of customer prose).
    path_str = str(path).replace("\\", "/")
    if COMPOSE_WIDGET_PATH_FRAG in path_str and path.suffix.lower() == ".ejs":
        return _compose_widget_em_dash_violations(text)

    # For JS/TS source files, strip code comments before scanning so that
    # internal `// ...` pragmas don't register as customer-copy violations.
    # Em dashes inside customer-facing string literals are still caught.
    if path.suffix.lower() in JS_TS_COMMENT_EXTS:
        text = _strip_js_comments(text)
    elif path.suffix.lower() in PY_COMMENT_EXTS:
        text = _strip_py_comments(text)

    competitor_exempt = any(path_str.endswith(frag) for frag in COMPETITOR_RULE_EXEMPT_PATHS)

    violations: list[tuple[int, str, str]] = []
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        # Em-dash check (rule 1)
        if EM_DASH in line or HTML_MDASH in line:
            first_em_idx = min(
                (line.index(EM_DASH) if EM_DASH in line else len(line)),
                (line.index(HTML_MDASH) if HTML_MDASH in line else len(line)),
            )
            if not _allowed(idx, lines, line, ALLOW_MARKER, first_em_idx):
                violations.append((idx + 1, line.rstrip(), "em-dash"))

        # Competitor framing (rule 2): named competitors + comparison idioms.
        # Citation pages (COMPETITOR_RULE_EXEMPT_PATHS) are exempt from this
        # rule only.
        comp_match = None if competitor_exempt else _COMPETITOR_RE.search(line)
        compare_match = None if competitor_exempt else _COMPARE_RE.search(line)
        if comp_match or compare_match:
            first_hit = min(
                (comp_match.start() if comp_match else len(line)),
                (compare_match.start() if compare_match else len(line)),
            )
            if not _allowed(idx, lines, line, ALLOW_COMPETITOR_MARKER, first_hit):
                violations.append((idx + 1, line.rstrip(), "competitor"))

        # Technical-ownership marketing copy (rule 3).
        tech_match = _TECH_OWNERSHIP_RE.search(line)
        if tech_match and not _is_avoidance_instruction(line, tech_match.start()):
            if not _allowed(idx, lines, line, ALLOW_TECH_OWNERSHIP_MARKER, tech_match.start()):
                violations.append((idx + 1, line.rstrip(), "tech-ownership"))

    return violations


_RULE_LABEL = {
    "em-dash":         "em dash in customer copy",
    "competitor":      "competitor framing on customer surface",
    "tech-ownership":  "technical-ownership marketing copy (Harbor v2)",
}

_RULE_HELP = {
    "em-dash": (
        "Use a colon, semicolon, comma, or start a new sentence.",
        "<!-- brand-voice-allow-em-dash: quoted testimonial -->",
    ),
    "competitor": (
        "We are one-of-a-kind on customer surfaces; do not name competitors or "
        "compare. See docs/BRAND_VOICE.md rule 2 (Harbor v2 update).",
        "<!-- brand-voice-allow-competitor: legitimate (e.g. payment processor name in legal copy) -->",
    ),
    "tech-ownership": (
        "Healers don't shop for source code. Soft ownership reassurance lives "
        "in FAQ wording (e.g. 'If you ever leave, we hand the whole site over "
        "intact'). See docs/BRAND_VOICE.md (Harbor v2 rule 3).",
        "<!-- brand-voice-allow-tech-ownership: technical email subject line -->",
    ),
}


def main(argv: list[str]) -> int:
    files = [Path(p) for p in argv[1:] if p]
    if not files:
        return 0

    counts: dict[str, int] = {}
    for path in files:
        if not path.exists():
            continue
        path_str = str(path)
        if any(frag in path_str for frag in SKIP_FRAGMENTS):
            continue
        violations = scan_file(path)
        if not violations:
            continue
        for line_no, line_text, rule in violations:
            counts[rule] = counts.get(rule, 0) + 1
            display = line_text if len(line_text) <= 140 else line_text[:137] + "..."
            print(f"{path}:{line_no}: {_RULE_LABEL[rule]}")
            print(f"  {display}")

    total = sum(counts.values())
    if total > 0:
        print("")
        per_rule = ", ".join(f"{n} {rule}" for rule, n in counts.items())
        print(f"Brand-voice lint failed: {total} violation(s) ({per_rule}).")
        print("See: docs/BRAND_VOICE.md")
        print("")
        rules_seen = set(counts.keys())
        if "em-dash" in rules_seen:
            print("Rule 1: no em dashes (U+2014 or &mdash;) in customer copy.")
            help_msg, allow_form = _RULE_HELP["em-dash"]
            print(f"  {help_msg}")
            print(f"  Allow: {allow_form}")
            print("")
        if "competitor" in rules_seen:
            print("Rule 2 (Harbor v2): no competitor framing on customer surfaces.")
            help_msg, allow_form = _RULE_HELP["competitor"]
            print(f"  {help_msg}")
            print(f"  Allow: {allow_form}")
            print("")
        if "tech-ownership" in rules_seen:
            print("Rule 3 (Harbor v2): no technical-ownership marketing copy.")
            help_msg, allow_form = _RULE_HELP["tech-ownership"]
            print(f"  {help_msg}")
            print(f"  Allow: {allow_form}")
            print("")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
