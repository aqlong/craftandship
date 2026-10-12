"""Contract framework for the B+C pipeline rewrite, section 05.

Runs at the tail of the @processor DAG. Asserts invariants about the
generated EJS/HTML/JS files using BeautifulSoup + soupsieve CSS selectors.
Every past MW regression class (snake-case labels, EJS-ternary form actions,
raw file inputs for image fields, YouTube iframes on home, em-dash in brand
nav, hardcoded demo creds) gets one per-view contract so future regressions
trip the same gate instead of shipping silently.

Public surface:
  - ContractViolation        : raised (or collected) when an assertion fails
  - contract(ejs, selector)  : CSS selector assertion
  - contains(ejs, needle)    : literal-substring assertion
  - _parse_ejs(ejs)          : html.parser-backed BS4 parse with EJS fallback

Design notes:
  - html.parser backend (stdlib). No lxml dependency.
  - Selectors flow through soup.select(), which uses soupsieve.
  - Degenerate-parse fallback strips <% ... %> blocks and re-parses so the
    vast majority of contracts (which care about static structure, not EJS
    control flow) keep working even when Claude output has ragged templating.
  - Collector kwarg appends a formatted violation string instead of raising.
    Pytest conftest flips CAS_CONTRACTS_COLLECT=1 via autouse fixture so
    dev-loop tests see every violation at once.
"""

from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup


class ContractViolation(AssertionError):
    """Raised (or collected) when a contract assertion fails.

    Inherits from AssertionError so pytest.raises(ContractViolation) and
    pytest.raises(AssertionError) both work. Message format:

        "<path>: <selector_or_needle> - expected <expected>, got <actual>"

    when path kwarg provided; bare "<selector>: expected ..., got ..." form
    otherwise. No em dash characters appear in messages.
    """


# Regex for stripping EJS control blocks during fallback parse. DOTALL so
# multi-line <% ... %> blocks vanish; non-greedy so consecutive blocks don't
# merge.
_EJS_BLOCK_RE = re.compile(r"<%[\s\S]*?%>")


def _parse_ejs(ejs_src: str) -> BeautifulSoup:
    """Parse EJS-in-HTML via bs4 html.parser with a degenerate fallback.

    Strategy:
      1. Detect an unclosed <% in the source (more <% markers than closing
         %> markers that follow them). Raise ValueError immediately so the
         caller sees a clear signal rather than a silent zero-match parse.
      2. Attempt raw parse. Return if tree is non-degenerate.
      3. Fallback: strip <% ... %> blocks (regex, DOTALL, non-greedy) and
         re-parse. Used for contracts that don't inspect EJS control flow.

    Degeneracy heuristic: if the tree contains zero tag elements while the
    source has more than one element-like open bracket, treat it as
    degenerate. Also triggers when the whole document body collapsed to a
    single bare text node.
    """
    # Eager malformed check: any <% that lacks a subsequent %> is a clear
    # signal the source can't be reasoned about. Count pairs.
    stripped_trial = _EJS_BLOCK_RE.sub("", ejs_src)
    if "<%" in stripped_trial:
        raise ValueError(
            "malformed EJS: unclosed <% block detected; contract parser "
            "cannot reason about partial control flow"
        )

    soup = BeautifulSoup(ejs_src, "html.parser")
    if _looks_parseable(soup, ejs_src):
        return soup
    # Fallback: strip EJS blocks and retry.
    return BeautifulSoup(stripped_trial, "html.parser")


def _looks_parseable(soup: BeautifulSoup, ejs_src: str) -> bool:
    """Return True if the initial parse produced a non-degenerate tree.

    Cheap heuristics sufficient for the parse-vs-fallback choice.
    """
    # Tag count in the parsed tree.
    tag_count = sum(1 for _ in soup.find_all(True))
    # Element-like open brackets in source: rough upper bound on tags if we
    # parsed everything. Counts any `<word` not preceded by `%` (to exclude
    # EJS `<%`).
    open_bracket_count = len(re.findall(r"<(?!%)[A-Za-z!/]", ejs_src))
    if tag_count == 0 and open_bracket_count > 1:
        return False
    # If the tree has some tags, accept it. The fallback is for clearly
    # broken cases where the parser produced almost nothing.
    return True


def _format_violation(
    selector_or_needle: str,
    *,
    path: Optional[str],
    expected: str,
    actual: str,
) -> str:
    """Produce the canonical violation message string.

    Uses ASCII '-' separator, not em dash, per CLAUDE.md code style rules.
    """
    prefix = f"{path}: " if path else ""
    return f"{prefix}{selector_or_needle} - expected {expected}, got {actual}"


def contract(
    ejs_src: str,
    selector: str,
    *,
    count: Optional[int] = None,
    absent: bool = False,
    path: Optional[str] = None,
    collector: Optional[list[str]] = None,
) -> None:
    """Assert a CSS-selector invariant on an EJS source string.

    Semantics:
      - absent=True overrides count: must be exactly zero matches.
      - count=N requires exactly N matches.
      - Default (no count, no absent): at least one match (>=1).

    When collector is provided, append a formatted violation string and
    return (no raise). Otherwise raise ContractViolation on failure.
    """
    soup = _parse_ejs(ejs_src)
    matches = soup.select(selector)
    actual = len(matches)

    if absent:
        if actual == 0:
            return
        msg = _format_violation(
            selector,
            path=path,
            expected="zero matches",
            actual=str(actual),
        )
    elif count is not None:
        if actual == count:
            return
        msg = _format_violation(
            selector,
            path=path,
            expected=f"exactly {count} matches",
            actual=str(actual),
        )
    else:
        if actual >= 1:
            return
        msg = _format_violation(
            selector,
            path=path,
            expected="at least one match",
            actual=str(actual),
        )

    if collector is not None:
        collector.append(msg)
        return
    raise ContractViolation(msg)


def contains(
    ejs_src: str,
    needle: str,
    *,
    absent: bool = False,
    path: Optional[str] = None,
    collector: Optional[list[str]] = None,
) -> None:
    """Literal-substring variant for assertions CSS selectors cannot express.

    Used for: presence of a literal URL prefix (edit.ejs /admin/<slug>/),
    codepoint-in-text checks (U+2014 em-dash detection), and demo-cred
    blacklists.
    """
    present = needle in ejs_src
    if absent:
        if not present:
            return
        msg = _format_violation(
            repr(needle),
            path=path,
            expected="absent from source",
            actual="present",
        )
    else:
        if present:
            return
        msg = _format_violation(
            repr(needle),
            path=path,
            expected="present in source",
            actual="absent",
        )

    if collector is not None:
        collector.append(msg)
        return
    raise ContractViolation(msg)


__all__ = [
    "ContractViolation",
    "contract",
    "contains",
    "_parse_ejs",
]
