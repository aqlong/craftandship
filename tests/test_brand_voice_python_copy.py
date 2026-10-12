"""Regression tests: no em dashes or 'your app' in Python customer copy.

The pre-commit hook runs scripts/check_brand_voice.py on staged Python files
listed in COPY_FILES (queue/mailer.py + coordinator/actuators.py today). This
pytest module acts as a server-side backstop and also covers queue/worker_pg.py
customer-facing status messages that are NOT in the hook's blanket scope
(because that file has many internal logger.info lines that legitimately use
em dashes; we only want to police the customer-visible _append_log strings).

See docs/BRAND_VOICE.md rules 1, 3 for the rules.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

EM_DASH = "\u2014"
HTML_MDASH = "&mdash;"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_mailer_has_no_em_dashes() -> None:
    """queue/mailer.py ships customer build-lifecycle emails; no em dashes."""
    text = _read(REPO_ROOT / "queue" / "mailer.py")
    # Strip # comments so developer pragmas don't trip the check.
    stripped_lines = []
    for line in text.splitlines():
        in_s = in_d = False
        cut = -1
        for i, ch in enumerate(line):
            if ch == "'" and not in_d:
                in_s = not in_s
            elif ch == '"' and not in_s:
                in_d = not in_d
            elif ch == "#" and not in_s and not in_d:
                cut = i
                break
        stripped_lines.append(line if cut == -1 else line[:cut])
    runtime = "\n".join(stripped_lines)
    assert EM_DASH not in runtime, (
        f"queue/mailer.py contains U+2014 em dash in a runtime string. "
        f"Use a colon, semicolon, or new sentence. See docs/BRAND_VOICE.md rule 1."
    )
    assert HTML_MDASH not in runtime, (
        f"queue/mailer.py contains &mdash; HTML entity in a runtime string. "
        f"Use a colon, semicolon, or new sentence. See docs/BRAND_VOICE.md rule 1."
    )


def test_mailer_does_not_say_your_app() -> None:
    """queue/mailer.py customer emails must say 'your website', not 'your app'."""
    text = _read(REPO_ROOT / "queue" / "mailer.py")
    matches = re.findall(r"[Yy]our [Aa]pp\b", text)
    assert not matches, (
        f"queue/mailer.py contains 'your app' in customer copy (found {len(matches)}). "
        f"BRAND_VOICE.md rule 3: use 'website' in customer copy."
    )


def test_customer_copy_does_not_call_the_preview_private() -> None:
    """A preview is a public address with no access control.

    The generated site allows crawling from its first deploy and asks for no
    credentials, so the support corpus the coordinator drafts answers from, and
    the mailer's customer emails, must not describe it as private. The web
    side has the matching checks (views.csp-and-copy.test.ts and the persona,
    email and support-knowledge tests).
    """
    from coordinator.support_knowledge import generate_knowledge

    corpus = generate_knowledge()
    assert "4. Walk around your preview" in corpus
    claims = re.compile(
        r"private\s+preview"
        r"|preview(\s+(url|address|link|site))?\s+(is|stays|remains)\s+private"
        r"|only\s+you\s+can\s+see",
        re.IGNORECASE,
    )
    assert not claims.search(corpus), claims.search(corpus)
    mailer = " ".join(_read(REPO_ROOT / "queue" / "mailer.py").split())
    assert not claims.search(mailer), claims.search(mailer)


def test_coordinator_actuator_email_has_no_em_dash() -> None:
    """coordinator/actuators.py sends the admin permission-request email.

    The footer used to contain '&mdash;' which rendered as an em dash in the
    inbox. Keep this line colon-delimited per BRAND_VOICE.md rule 1.
    """
    text = _read(REPO_ROOT / "coordinator" / "actuators.py")
    # Look specifically at the email-body string literals: any string that
    # wraps an HTML <div>, <p>, or <table> block. Cheap proxy: scan for any
    # em dash that lives inside an f-string triple-quoted block starting with
    # 'html = f"""'. We just check the whole file, but strip # comments and
    # plain triple-quoted docstrings first.
    import scripts.check_brand_voice as bv  # type: ignore
    cleaned = bv._strip_py_comments(text)
    assert EM_DASH not in cleaned, (
        "coordinator/actuators.py has an em dash in a runtime string "
        "(check email HTML bodies). See docs/BRAND_VOICE.md rule 1."
    )
    assert HTML_MDASH not in cleaned, (
        "coordinator/actuators.py has an &mdash; HTML entity in a runtime "
        "string. See docs/BRAND_VOICE.md rule 1."
    )


def test_worker_customer_facing_logs_have_no_em_dashes() -> None:
    """queue/worker_pg.py build-log strings (customer status page) must be clean.

    Only checks _append_log(...) calls since the rest of the file is internal
    logger lines. Uses a regex for single-line calls and a conservative
    multi-line scan for split calls. Em dashes in logger.info/logger.warning
    are allowed for now.
    """
    text = _read(REPO_ROOT / "queue" / "worker_pg.py")
    violations: list[tuple[int, str]] = []
    call_re = re.compile(r"_append_log\([^)]*\)", re.DOTALL)
    for match in call_re.finditer(text):
        chunk = match.group(0)
        if EM_DASH in chunk or HTML_MDASH in chunk:
            line_no = text[: match.start()].count("\n") + 1
            violations.append((line_no, chunk[:160]))
    assert not violations, (
        f"queue/worker_pg.py has em dashes in customer-facing _append_log "
        f"calls: {violations}. See docs/BRAND_VOICE.md rule 1."
    )


def test_worker_customer_facing_logs_say_website_not_app() -> None:
    """queue/worker_pg.py customer status messages must use 'website'."""
    text = _read(REPO_ROOT / "queue" / "worker_pg.py")
    call_re = re.compile(r"_append_log\([^)]*\)", re.DOTALL)
    violations: list[tuple[int, str]] = []
    app_re = re.compile(r"[Yy]our [Aa]pp\b")
    for match in call_re.finditer(text):
        chunk = match.group(0)
        if app_re.search(chunk):
            line_no = text[: match.start()].count("\n") + 1
            violations.append((line_no, chunk[:160]))
    assert not violations, (
        f"queue/worker_pg.py has 'your app' in customer _append_log calls: "
        f"{violations}. BRAND_VOICE.md rule 3: use 'your website'."
    )


def test_check_brand_voice_script_accepts_python_files(tmp_path: Path) -> None:
    """scripts/check_brand_voice.py must scan .py files (added 2026-04-23).

    Regression against a silent coverage hole: before this change the script
    only knew about JS/TS/EJS files, so adding queue/mailer.py to the
    pre-commit COPY_FILES list without teaching the script about Python comment
    syntax would pass files through uninspected. This test writes a .py file
    with an em dash inside a runtime string and one inside a # comment, then
    asserts the script flags the former and ignores the latter.
    """
    fixture = tmp_path / "sample.py"
    fixture.write_text(
        '# a dev comment with an em dash \u2014 should be ignored\n'
        'GREETING = "hi \u2014 world"  # em dash in runtime string\n',
        encoding="utf-8",
    )
    import scripts.check_brand_voice as bv  # type: ignore
    violations = bv.scan_file(fixture)
    # scan_file returns (line_no, line_text, rule) since the Harbor v2
    # multi-rule expansion (2026-04-26). The em-dash check is rule="em-dash".
    em_dash_lines = [ln for ln, _, rule in violations if rule == "em-dash"]
    assert 2 in em_dash_lines, (
        "check_brand_voice.py missed em dash in runtime Python string. "
        f"violations={violations}"
    )
    assert 1 not in em_dash_lines, (
        "check_brand_voice.py flagged em dash in # comment (should be stripped). "
        f"violations={violations}"
    )


def test_worker_v2_compose_log_says_generation_complete() -> None:
    """The v2 build branch's customer-facing _append_log must say 'Generation
    complete', not 'v2 compose complete' or other internal jargon.

    Locks in BRAND_VOICE.md rule 5 (plain English about the build) for the
    specific status-page line a customer sees on every v2 build. Backstop for
    the live-verify deferred check from 2026-04-23: catches drift before it
    reaches a real customer's status page.
    """
    text = _read(REPO_ROOT / "queue" / "worker_pg.py")
    # The v2 branch must use the customer-friendly phrase.
    assert '"Generation complete"' in text, (
        "queue/worker_pg.py v2 branch must _append_log('Generation complete'). "
        "See BRAND_VOICE.md rule 5: plain English, no 'compose' jargon in "
        "customer copy."
    )
    # And must NOT regress to compose-flavored wording in any _append_log call.
    call_re = re.compile(r"_append_log\([^)]*\)", re.DOTALL)
    jargon_re = re.compile(r"\b(?:v2 )?compose (?:complete|done|finished)\b", re.IGNORECASE)
    violations: list[tuple[int, str]] = []
    for match in call_re.finditer(text):
        chunk = match.group(0)
        if jargon_re.search(chunk):
            line_no = text[: match.start()].count("\n") + 1
            violations.append((line_no, chunk[:160]))
    assert not violations, (
        f"queue/worker_pg.py has compose-jargon in customer _append_log: "
        f"{violations}. See BRAND_VOICE.md rule 5."
    )


def test_worker_deploy_failure_log_promises_nobody() -> None:
    """When the failure knowledge base short-circuits deploy_fix, the build
    log line the customer can read says what happens (automatic repair is
    skipped) in plain words. It must not promise that a person will finish
    the deploy: nothing in the worker hands the build to anyone, it is
    retried or marked failed like any other.
    """
    text = _read(REPO_ROOT / "queue" / "worker_pg.py")
    assert '"Automatic repair is skipped for this deploy failure"' in text
    assert "will finish the deploy from here" not in text


def test_coordinator_actuator_footer_uses_amp_entity() -> None:
    """The escalation email footer must render 'Craft & Ship' via the
    HTML-safe `&amp;` entity, never `&mdash;` (which would render as an em
    dash and trip BRAND_VOICE.md rule 1) and never a bare `&` (which is
    invalid HTML inside an attribute-free text node but more importantly
    risks downstream sanitizers misinterpreting it).
    """
    text = _read(REPO_ROOT / "coordinator" / "actuators.py")
    # Footer must use the &amp; entity.
    assert "Craft &amp; Ship Coordinator" in text, (
        "coordinator/actuators.py escalation footer must read "
        "'Craft &amp; Ship Coordinator' (HTML-safe `&amp;`)."
    )
    # And must NOT have regressed to '&mdash;' anywhere.
    assert "&mdash;" not in text, (
        "coordinator/actuators.py contains '&mdash;'. The footer must use "
        "'&amp;' not '&mdash;'. See BRAND_VOICE.md rule 1."
    )


def test_mailer_lifecycle_templates_use_website_wording() -> None:
    """The customer-facing subject of the worker's preview-ready email must
    say 'website'.

    This guards against drift back to 'app' / 'site' / 'platform' wording
    in that subject line.
    """
    text = _read(REPO_ROOT / "queue" / "mailer.py")

    # Subjects: every customer subject mentioning the build must say 'website'.
    # send_preview_ready subject pattern.
    assert "Your website '{app_name}' is ready for review!" in text, (
        "send_preview_ready subject must include \"Your website '{app_name}' "
        "is ready for review!\" (BRAND_VOICE.md rule 3)."
    )
    # send_build_failed subject is neutral ("Build update for ...") which is
    # fine; do not assert wording there.


def test_check_brand_voice_script_ignores_docstrings(tmp_path: Path) -> None:
    """Plain triple-quoted docstrings should not trip the lint (they are
    developer context, not customer copy). f-string triple-quoted blocks
    still DO get scanned because mailer.py puts customer HTML in them.
    """
    fixture = tmp_path / "mod.py"
    fixture.write_text(
        '"""Module docstring \u2014 em dash lives here, should be ignored."""\n'
        'def foo():\n'
        '    """Function docstring \u2014 also ignored."""\n'
        '    body = f"""<p>Hi \u2014 em dash in f-string should be caught.</p>"""\n'
        '    return body\n',
        encoding="utf-8",
    )
    import scripts.check_brand_voice as bv  # type: ignore
    violations = bv.scan_file(fixture)
    # scan_file returns (line_no, line_text, rule) since the Harbor v2
    # multi-rule expansion (2026-04-26). Filter to em-dash rule for this
    # docstring-handling check.
    em_dash_lines = [ln for ln, _, rule in violations if rule == "em-dash"]
    assert 1 not in em_dash_lines, f"module docstring em dash was flagged: {violations}"
    assert 3 not in em_dash_lines, f"function docstring em dash was flagged: {violations}"
    assert 4 in em_dash_lines, (
        f"f-string em dash was NOT flagged (mailer.py uses f-string HTML blocks "
        f"for customer emails): {violations}"
    )


def test_compose_widget_html_comments_have_no_em_dashes() -> None:
    """compose/widgets/**/*.ejs HTML comments must not contain em dashes.

    Every compose widget opens with a <!-- compose:widget X - description -->
    header. Those ship verbatim to the rendered HTML source (even though they
    don't render visually). BRAND_VOICE.md rule 1 (no em dashes anywhere
    customer-facing) covers them. EJS scriptlet comments (<%# %>, <% // %>)
    and intentional `\u2014` glyphs in admin template body output are NOT scanned.
    """
    import re

    widgets_root = REPO_ROOT / "compose" / "widgets"
    assert widgets_root.is_dir(), f"missing {widgets_root}"
    html_comment_re = re.compile(r"<!--[\s\S]*?-->")
    violations: list[tuple[str, int]] = []
    for path in sorted(widgets_root.rglob("*.ejs")):
        text = path.read_text(encoding="utf-8")
        for match in html_comment_re.finditer(text):
            chunk = match.group(0)
            if EM_DASH in chunk or HTML_MDASH in chunk:
                line_no = text.count("\n", 0, match.start()) + 1
                violations.append((str(path.relative_to(REPO_ROOT)), line_no))
    assert not violations, (
        f"compose widget HTML comments contain em dashes (BRAND_VOICE rule 1): "
        f"{violations}. Replace U+2014 with ': ' or ' - '."
    )


def test_check_brand_voice_script_flags_compose_widget_html_comment_em_dash(
    tmp_path: Path,
) -> None:
    """Regression: scripts/check_brand_voice.py must flag em dashes inside
    HTML comments of a compose widget EJS file.

    Driver: a 2026-05-01 customer build session. Five compose widgets
    (utility/stars, utility/icon, form/field, form/contact, offerings/
    reviews-grid) shipped HTML comment headers with U+2014, which then
    rendered into customer-facing page source.
    """
    import scripts.check_brand_voice as bv  # type: ignore

    widget_dir = tmp_path / "compose" / "widgets" / "utility"
    widget_dir.mkdir(parents=True)
    fixture = widget_dir / "fake.ejs"
    fixture.write_text(
        "<!-- compose:widget utility/fake \u2014 a fake widget. -->\n"
        "<%# server-side comment with \u2014 must be ignored %>\n"
        '<span><%= "render literal hyphen-minus" %></span>\n',
        encoding="utf-8",
    )
    violations = bv.scan_file(fixture)
    em_dash_lines = [ln for ln, _, rule in violations if rule == "em-dash"]
    assert 1 in em_dash_lines, (
        "check_brand_voice.py missed em dash in compose widget HTML comment: "
        f"violations={violations}"
    )
    assert 2 not in em_dash_lines, (
        "check_brand_voice.py flagged em dash inside <%# %> EJS scriptlet "
        f"comment (should be ignored on compose widgets): violations={violations}"
    )


def test_check_brand_voice_script_ignores_compose_widget_body_em_dash(
    tmp_path: Path,
) -> None:
    """Compose-widget scan is HTML-comment-only.

    Intentional `\u2014` glyphs in EJS template body (e.g. admin "no value"
    placeholders) and inside EJS scriptlet comments must not trip the lint
    on compose/widgets/**/*.ejs.
    """
    import scripts.check_brand_voice as bv  # type: ignore

    widget_dir = tmp_path / "compose" / "widgets" / "admin"
    widget_dir.mkdir(parents=True)
    fixture = widget_dir / "fake.ejs"
    fixture.write_text(
        "<!-- compose:widget admin/fake - clean header. -->\n"
        '<td><%= value || "\u2014" %></td>\n'
        "<% // server-side note with \u2014 em dash %>\n",
        encoding="utf-8",
    )
    violations = bv.scan_file(fixture)
    em_dash_lines = [ln for ln, _, rule in violations if rule == "em-dash"]
    assert em_dash_lines == [], (
        "check_brand_voice.py flagged em dash outside HTML comment region "
        f"in compose widget: violations={violations}"
    )


def test_check_brand_voice_compose_widget_honors_allow_marker(
    tmp_path: Path,
) -> None:
    """Allow marker (`brand-voice-allow-em-dash:`) must be honored on
    compose-widget HTML comments, same as the main rule."""
    import scripts.check_brand_voice as bv  # type: ignore

    widget_dir = tmp_path / "compose" / "widgets" / "utility"
    widget_dir.mkdir(parents=True)
    fixture = widget_dir / "fake.ejs"
    fixture.write_text(
        "<!-- brand-voice-allow-em-dash: legitimate quoted source -->\n"
        "<!-- compose:widget utility/fake \u2014 still has dash for a reason. -->\n",
        encoding="utf-8",
    )
    violations = bv.scan_file(fixture)
    em_dash_lines = [ln for ln, _, rule in violations if rule == "em-dash"]
    assert em_dash_lines == [], (
        "Allow marker on previous line was not honored on compose widget: "
        f"violations={violations}"
    )
