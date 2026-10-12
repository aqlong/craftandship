"""Contract tests for scripts/keyboard_tab_order_static_scan.py.

The scanner is the deps-free, infra-free first pass for the keeper-flow
tab-order audit. These tests pin (a) the four named anti-patterns the
parent roadmap item flagged so the views can't silently regress through
them, (b) the per-view focusable-count contract so a PR can't drop a
primary CTA from the tab path without the test noticing, and (c) the
EJS-stripping + branch-detection primitives so a future refactor of the
scanner keeps line numbers + branch labels intact.

When AP1/AP2/AP3/AP4 are addressed in the lighthouse views, the
matching test will fail with a diagnostic pointing back at
`docs/audits/keyboard-tab-order-2026-05-27.md`, the fix-the-doc-too
contract.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "keyboard_tab_order_static_scan.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("keyboard_tab_order_static_scan", _SCRIPT)
    assert spec and spec.loader, "scanner script not loadable"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["keyboard_tab_order_static_scan"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def scanner():
    return _load_module()


# --- Anti-pattern coverage -------------------------------------------------


class TestActiveAntiPatterns:
    """Each named AP must be present until the corresponding view fix lands.

    When you address the underlying issue, flip the assertion (the test
    will tell you which view was fixed and require updating
    docs/audits/keyboard-tab-order-2026-05-27.md).
    """

    def test_ap1_preview_choose_buttons_share_name(self, scanner):
        reports = scanner.scan_all()
        preview = next(r for r in reports if r.view == "preview.ejs")
        codes = {f.code for f in preview.findings}
        assert "AP1" not in codes, (
            "AP1 (preview.ejs: identical 'Choose this' buttons) regressed. "
            "Per-family aria-label on the Choose-this button was removed. "
            "Restore `aria-label=\"Choose <%= f.name %>\"` near preview.ejs "
            "line 124 and update docs/audits/keyboard-tab-order-2026-05-27.md."
        )

    def test_ap2_table_autofocus_steals_skip_link(self, scanner):
        reports = scanner.scan_all()
        table = next(r for r in reports if r.view == "table.ejs")
        codes = {f.code for f in table.findings}
        assert "AP2" not in codes, (
            "AP2 (table.ejs: textarea.focus() on cold load) regressed. "
            "The IIFE-end cold-load autofocus must stay inside a "
            "`document.referrer` same-origin guard so the skip-to-content "
            "link wins the first focus stop on cold load. Restore the guard "
            "near table.ejs line 1032. See "
            "docs/audits/keyboard-tab-order-2026-05-27.md AP2 (RESOLVED 2026-05-28)."
        )

    def test_ap3_table_memory_drawer_keyboard_close(self, scanner):
        reports = scanner.scan_all()
        table = next(r for r in reports if r.view == "table.ejs")
        codes = {f.code for f in table.findings}
        assert "AP3" not in codes, (
            "AP3 (table.ejs: memory drawer has no Escape handler / does not "
            "move focus into drawer on open) regressed. Restore the Escape "
            "keydown handler that closes #memory-panel + returns focus to "
            "#memory-tab, and the on-open focus shift into the drawer's "
            "first focusable element. See "
            "docs/audits/keyboard-tab-order-2026-05-27.md AP3 (RESOLVED 2026-05-28)."
        )

    def test_ap4_published_meta_refresh_focus_reset(self, scanner):
        reports = scanner.scan_all()
        published = next(r for r in reports if r.view == "published.ejs")
        codes = {f.code for f in published.findings}
        assert "AP4" not in codes, (
            "AP4 (published.ejs: meta-refresh resets focus) regressed. The "
            "almost-there branch must NOT re-introduce a "
            "`<meta http-equiv=\"refresh\">` tag. Use the existing "
            "aria-live polling block (fetch + role=\"status\") near "
            "published.ejs line 80. See "
            "docs/audits/keyboard-tab-order-2026-05-27.md AP4 (RESOLVED 2026-05-28)."
        )


# --- Per-view focusable-count contract -------------------------------------


_EXPECTED_FOCUSABLE_COUNTS = {
    "cancelled.ejs": 1,
    "changes-first.ejs": 2,
    # 11: the nine before, plus the "choose a password" link to
    # /forgot-password shown to the browser that paid and has no account
    # session (after "Cancel subscription" in DOM order), plus the support
    # address link in the subscription notice that is shown when the
    # subscription is not in force (before "Cancel subscription").
    "lamp.ejs": 11,
    # 9: plus the "This is my email address" checkbox, shown to someone paying
    # from a trial. In DOM order: back link, the address field (when the
    # address is asked for; a decided account's address is plain text), that
    # checkbox, the amount slider, the "fits my practice" checkbox, terms,
    # privacy, the submit button, "Not yet". The scanner counts every branch,
    # so the address field and the checkbox are both in the 9.
    "make-public.ejs": 9,
    "preview.ejs": 3,
    # 4: plus "Manage your website and subscription" (to the lamp page) in the
    # settled state, after the "your conversation" link.
    "published.ejs": 4,
    "recall.ejs": 1,
    # 18: plus "Manage your website and subscription" in the live-site keeper
    # banner, after the "Make changes to my site" button.
    "table.ejs": 18,
}


class TestFocusableCounts:
    @pytest.mark.parametrize(
        "view,expected",
        list(_EXPECTED_FOCUSABLE_COUNTS.items()),
    )
    def test_view_focusable_count(self, scanner, view, expected):
        reports = scanner.scan_all()
        match = next((r for r in reports if r.view == view), None)
        assert match is not None, f"missing report for {view}"
        actual = len(match.focusables)
        assert actual == expected, (
            f"{view} focusable count {actual} != expected {expected}. "
            "If you intentionally added or removed a CTA, update the "
            "expected count here AND mirror the change in "
            "docs/audits/keyboard-tab-order-2026-05-27.md."
        )


# --- Skip-link + main-landmark every-view guarantee -----------------------


class TestSkipLinkAndMain:
    def test_every_view_includes_skip_link_partial(self, scanner):
        reports = scanner.scan_all()
        missing = [r.view for r in reports if not r.skip_link_present]
        assert not missing, (
            f"Views missing the _harbor-skip-link include: {missing}. The "
            "skip-link must be the first child of <body> on every keeper "
            "surface. WCAG 2.4.1."
        )

    def test_every_view_has_main_landmark_id_main(self, scanner):
        reports = scanner.scan_all()
        wrong = [r.view for r in reports if r.main_landmark_id != "main"]
        assert not wrong, (
            f"Views without <main id=\"main\">: {wrong}. The skip-link "
            "anchors at #main and breaks when the landmark id changes."
        )


# --- Branch detection sanity ----------------------------------------------


class TestBranchDetection:
    def test_lamp_make_it_public_anchor_lives_in_canmakepublic_branch(self, scanner):
        reports = scanner.scan_all()
        lamp = next(r for r in reports if r.view == "lamp.ejs")
        matches = [f for f in lamp.focusables if "Make it public" in (f.snippet or "")]
        assert matches, "lamp.ejs 'Make it public' anchor not enumerated"
        assert any(
            f.branch and "canMakePublic" in f.branch for f in matches
        ), (
            "lamp.ejs 'Make it public' anchor branch detection regressed; "
            f"got branches: {[f.branch for f in matches]}"
        )

    def test_table_textarea_and_send_btn_are_unconditional(self, scanner):
        reports = scanner.scan_all()
        table = next(r for r in reports if r.view == "table.ejs")
        textarea = next((f for f in table.focusables if f.snippet == "keeper-text"), None)
        send = next((f for f in table.focusables if f.snippet == "send-btn"), None)
        assert textarea is not None, "table.ejs missing keeper-text textarea"
        assert send is not None, "table.ejs missing send-btn"
        assert textarea.branch is None, (
            f"keeper-text textarea must be unconditional; got branch "
            f"{textarea.branch!r}. Regression in branch detector."
        )
        assert send.branch is None, (
            f"send-btn must be unconditional; got branch {send.branch!r}. "
            "Regression in branch detector."
        )


# --- make-public.ejs: the order of the payment form ------------------------


class TestMakePublicFormOrder:
    """The address a payment is tied to, then its confirmation, then the
    amount, then the button. Nothing in the form sets a tabindex, so the
    order in the template is the order the Tab key follows."""

    def _focusables(self, scanner):
        reports = scanner.scan_all()
        view = next(r for r in reports if r.view == "make-public.ejs")
        return view.focusables

    def test_address_then_confirmation_then_amount_then_submit(self, scanner):
        order = [f.snippet for f in self._focusables(scanner)]
        wanted = ["email", "email-confirmed", "amount-slider", "att", "Make it public"]
        missing = [s for s in wanted if s not in order]
        assert not missing, f"make-public.ejs controls not enumerated: {missing}; got {order}"
        positions = [order.index(s) for s in wanted]
        assert positions == sorted(positions), (
            f"make-public.ejs tab order is {order}; expected the address, "
            "its confirmation checkbox, the amount slider, the attestation "
            "checkbox and the submit button in that order."
        )
        # The confirmation directly follows the address it confirms.
        assert order.index("email-confirmed") == order.index("email") + 1

    def test_confirmation_checkbox_is_conditional_and_labelled(self, scanner):
        confirm = next(f for f in self._focusables(scanner) if f.snippet == "email-confirmed")
        assert confirm.branch and "confirmEmail" in confirm.branch, (
            f"the confirmation checkbox must sit in the confirmEmail branch; got {confirm.branch!r}"
        )
        src = (scanner._VIEWS_DIR / "make-public.ejs").read_text()
        assert src.count('<label for="email-confirmed">') == 1, (
            "the confirmation checkbox needs exactly one <label for=...> of its own"
        )
        assert src.count('id="email-confirmed"') == 1
        # When the address is plain text (no Tab stop), the checkbox points at it.
        assert 'aria-describedby="account-email"' in src
        assert src.count('id="account-email"') == 1


# --- EJS-strip primitives --------------------------------------------------


class TestEjsStripping:
    def test_blank_keep_newlines_preserves_newlines(self, scanner):
        s = "foo\nbar\nbaz"
        out = scanner._blank_keep_newlines(s)
        assert out.count("\n") == 2
        assert len(out) == len(s)
        assert "f" not in out and "b" not in out  # alphabet replaced

    def test_strip_scripts_blanks_body(self, scanner):
        src = "<p>before</p><script>var x='<input id=\"fake\">'</script><p>after</p>"
        out = scanner._strip_scripts(src)
        # The script body must NOT carry the fake input markup forward.
        assert "id=\"fake\"" not in out
        # Surrounding text preserved.
        assert "<p>before</p>" in out
        assert "<p>after</p>" in out

    def test_strip_ejs_blanks_expr_tags(self, scanner):
        src = "Hello <%= name %> world"
        out = scanner._strip_ejs(src)
        assert "name" not in out
        # Length is preserved so line counters stay accurate.
        assert len(out) == len(src)


class TestBranchMap:
    def test_else_if_does_not_double_push(self, scanner):
        src = (
            "<% if (a) { %>1<% } else if (b) { %>2<% } else { %>3<% } %>tail"
        )
        lookup = scanner._build_branch_map(src)
        # Walk three offsets that each fall just past one of the openers.
        offsets_and_branches = [
            (src.index("1"), "a"),
            (src.index("2"), "else-if: b"),
            (src.index("3"), "else of: else-if: b"),
            (src.index("tail"), None),
        ]
        for offset, expected in offsets_and_branches:
            actual = lookup(offset)
            if expected is None:
                assert actual is None, f"offset {offset}: expected None, got {actual!r}"
            else:
                assert actual == expected, (
                    f"offset {offset}: expected {expected!r}, got {actual!r}"
                )


# --- Markdown renderer -----------------------------------------------------


class TestMarkdownRender:
    def test_render_includes_summary_counts(self, scanner):
        md = scanner.render_markdown(scanner.scan_all())
        assert "**Views scanned:**" in md
        assert "**Focusable controls enumerated:**" in md
        assert "**Anti-patterns surfaced:**" in md

    def test_render_includes_per_view_section(self, scanner):
        md = scanner.render_markdown(scanner.scan_all())
        for view in _EXPECTED_FOCUSABLE_COUNTS:
            assert f"### `{view}`: tab order" in md
