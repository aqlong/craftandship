# Keeper-flow keyboard tab order (static enumeration of the markup): 2026-05-27

> **What this is:** a static review of the markup. A script lists the focusable elements in the order the `web/views/lighthouse/*.ejs` files declare them; no one pressed Tab in a browser, and the live Playwright walk described below was never run. The `/lighthouse/*` routes named here are the product's own Harbor companion surface, not Google Lighthouse.

**Parent roadmap item:** `harbor-usability-2-keyboard-tab-order-walk` (Sub-2 of `harbor-usability-live-browser-followups`).

**Scope:** the full keeper flow as named in the parent task, `/lighthouse` (chat) → `/lighthouse/table` (8 turns of conversation) → email gate → `/lighthouse/lamp/<id>` (build state) → `/lighthouse/lamp/<id>/make-public` (subscription form). Plus the surrounding lighthouse surfaces a keyboard-only keeper will encounter when navigating between those steps: `/lighthouse/table/recall`, `/lighthouse/preview`, `/lighthouse/lamp/<id>/published`, `/lighthouse/lamp/<id>/cancelled`, and the changes-first 409 detour.

**Method:** two passes, identical pattern to Sub-1 (the `/lighthouse/*` axe + contrast audit).

1. **Static tab-order enumeration** (this document) - `scripts/keyboard_tab_order_static_scan.py` walks every `web/views/lighthouse/*.ejs` file, strips EJS + script bodies (preserving line numbers), enumerates focusable elements in document order, annotates each with the conditional branch it lives in, and detects four named anti-patterns the parent item explicitly asked the live walk to flag. Runs without infra (no DB, no server, no browser).
2. **Live Playwright walk**, `web/tests/ui/keyboard-tab-order.spec.ts` drives every interactive surface in the keeper flow with **only `Tab` / `Shift-Tab` / `Enter`** (per the parent task), captures the active element after each keystroke, writes a JSON walk to `docs/audits/keyboard-tab-order-<date>-<route>.json`, and asserts (a) the primary CTA on each page is reachable via forward Tab, and (b) the 5 `/lighthouse/preview` family-card buttons have unique accessible names. **Not run in this pass**, see "Live pass: prerequisites" below.

The split mirrors Sub-1's static-then-live approach because the live walk needs the same prerequisites the live axe sweep does (running web server + Postgres + an admin-gated trial cookie + a build in `preview/approved` state), and the executor cannot stand any of them up. The static pass surfaces the deterministic findings; the live spec is wired so a human (or CI step with the right fixtures) can run it on demand.

## Static pass: tab-order enumeration

Run via `python3 scripts/keyboard_tab_order_static_scan.py` (deterministic: re-run anytime to refresh). The current snapshot:

- **Views scanned:** 8
- **Focusable controls enumerated:** 41 across all branches and JS-injected forms
- **Anti-patterns surfaced:** 0 (AP1 on `preview.ejs`, AP2 + AP3 on `table.ejs`, and AP4 on `published.ejs` all resolved 2026-05-28)

### View-level summary

Every keeper-flow view ships the shared `_harbor-skip-link.ejs` partial as the first element inside `<body>`, and every view wraps its primary content in `<main id="main">` so the skip-link's `href="#main"` resolves to a sensible landing zone. That foundation is correct on every surface and is the only thing the audit confirms unconditionally.

| View | Routes | Skip-link first? | `<main id>` | Focusables | Findings |
|---|---|---|---|---|---|
| `cancelled.ejs` | `/lighthouse/lamp/<id>/cancelled` | YES | `main` | 1 | 0 |
| `changes-first.ejs` | `/lighthouse/lamp/<id>/make-public` (review_status=`changes_requested`) | YES | `main` | 2 | 0 |
| `lamp.ejs` | `/lighthouse/lamp/<id>` | YES | `main` | 9 (state-dependent) | 0 |
| `make-public.ejs` | `/lighthouse/lamp/<id>/make-public` | YES | `main` | 9 | 0 |
| `preview.ejs` | `/lighthouse/preview` | YES | `main` | 3 (per family-card iteration) | 0 (AP1 resolved 2026-05-28) |
| `published.ejs` | `/lighthouse/lamp/<id>/published` | YES | `main` | 3 (state-dependent) | 0 (AP4 resolved 2026-05-28) |
| `recall.ejs` | `/lighthouse/table/recall` | YES | `main` | 1 | 0 |
| `table.ejs` | `/lighthouse/table` | YES | `main` | 17 (state-dependent + JS-injected; +3 on 2026-06-07 for the self-serve "Build my free preview" CTA: `ss-email` input, `ss-build-btn` button, "see it come to life" link) | 0 (AP2 + AP3 resolved 2026-05-28) |

### Findings: grouped

#### AP1: `/lighthouse/preview` template-family buttons share an accessible name (RESOLVED 2026-05-28)

Every card in the family grid emitted the same button:

```html
<button type="submit">Choose this</button>
```

The EJS `families.forEach(...)` loop generates one card per template family (today: `classic-business`, `editorial-statement`, `directory-rich`, `simple-statement`, `event-led`). A sighted user disambiguates by reading the `<h2>` and the `.specs` list above each button. A screen-reader user tabbing through the grid would hear five identical "Choose this, button" announcements with no contextual clue which family their next Enter would commit. WCAG 2.4.6 (Headings and Labels) and 4.1.2 (Name, Role, Value) both require each interactive control to have a discernible accessible name.

**Fix landed 2026-05-28** in `harbor-a11y-preview-family-buttons-aria-label`: `web/views/lighthouse/preview.ejs` line 124 now reads:

```html
<button type="submit" aria-label="Choose <%= f.name %>">Choose this</button>
```

so each family-card button gets a per-family accessible name ("Choose Classic Business", "Choose Editorial Statement", ...). The visible button text remains "Choose this" so the sighted UI is unchanged. The Playwright spec's `/lighthouse/preview` test asserts the contract:

```ts
expect(unique.size).toBe(accessibleNames.length); // every "Choose this" → unique
```

and the static scanner's AP1 detector no longer flags `preview.ejs`. The pin in `tests/test_keyboard_tab_order_static_scan.py::test_ap1_preview_choose_buttons_share_name` was flipped to assert `"AP1" not in codes` in the same commit.

#### AP2: `/lighthouse/table` autofocus on compose textarea defeats the skip-link (RESOLVED 2026-05-28)

`web/views/lighthouse/table.ejs` ran an inline `<script>` at the end of `<body>` that unconditionally called:

```js
textarea.focus();
```

at the end of the main IIFE. Browsers honored that call before the user could press Tab, so the first focusable element a keyboard-only or screen-reader keeper actually encountered was the compose textarea, **not** the `cas-skip-link` anchor sitting first inside `<body>`. The skip-link's whole purpose (WCAG 2.4.1 Bypass Blocks) is to be the first focus stop on cold load so AT users don't have to traverse the brand bar / header / memory drawer / quiet-links every visit. Shift-Tab from the textarea did walk backwards to the skip-link, but it was reachable LAST, not first, the opposite of the intended ergonomics.

**Fix landed 2026-05-28** in `harbor-a11y-table-autofocus-defeats-skip-link`: the IIFE-end autofocus is now gated by a same-origin `document.referrer` check near `web/views/lighthouse/table.ejs` line 1032:

```js
scrollToBottom();
try {
  if (document.referrer) {
    var refUrl = new URL(document.referrer);
    if (refUrl.origin === window.location.origin) {
      textarea.focus();
    }
  }
} catch (e) { /* malformed referrer; leave focus on skip-link */ }
```

Cold-load visits (typed URL, bookmark, cross-origin entry from search results or social) leave `document.referrer` empty or cross-origin, so autofocus is skipped and the skip-link wins the first focus stop. Returning visits from another same-origin lighthouse surface (`/recall`, `/preview`, `/lamp`) keep the "jump straight to typing" affordance. The post-turn `textarea.focus()` inside `sendTurn()`'s `finally` block is unchanged (it runs after the keeper has already interacted with the page, not on cold load).

The static scanner's AP2 detector was tightened to look specifically for `scrollToBottom();` immediately followed by an unguarded `textarea.focus();` (the cold-load anti-pattern), so the post-turn focus call does not false-positive. The pin in `tests/test_keyboard_tab_order_static_scan.py::test_ap2_table_autofocus_steals_skip_link` was flipped to assert `"AP2" not in codes` in the same commit.

WCAG references: 2.4.1 Bypass Blocks, 2.4.3 Focus Order, 3.2.1 On Focus.

#### AP3: `/lighthouse/table` memory drawer has no keyboard close affordance and does not move focus into the drawer on open (RESOLVED 2026-05-28)

The memory side-panel toggle:

```html
<button id="memory-tab" type="button" aria-expanded="false" aria-controls="memory-panel">
```

flipped `aria-expanded` and toggled `.is-open` on `#memory-panel`. The click-outside listener closed the drawer when the user **clicked** anywhere outside it. There was no `keydown` handler for `Escape`. There was also no `panel.querySelector('a, button').focus()` on open, so when a keyboard-only keeper hit Enter on the toggle, the visual drawer opened but focus stayed on the toggle button. Tabbing from there walked into the brand bar / quiet-links / textarea, not the drawer's contents. To close it from the keyboard the keeper either had to Tab cycle back to the toggle and Enter again, or perform a mouse-only action.

**Fix landed 2026-05-28** in `harbor-a11y-table-memory-drawer-keyboard-close`: the inline IIFE in `web/views/lighthouse/table.ejs` now:

1. On open (`memoryPanel.classList.toggle('is-open')` true branch), calls `memoryPanel.querySelector('a, button, [tabindex]:not([tabindex="-1"])')?.focus()` so AT users land inside the drawer.
2. Adds a document-level `keydown` listener that recognises `Escape`, removes `.is-open`, resets `aria-expanded="false"`, and calls `memoryTab.focus()` so the keeper returns to the toggle that opened the drawer.
3. Factors the close path into a shared `closeMemoryPanel(returnFocus)` helper so the click-outside listener and the Escape handler stay in sync.

The static scanner's AP3 detector no longer flags `table.ejs`, and the pin in `tests/test_keyboard_tab_order_static_scan.py::test_ap3_table_memory_drawer_keyboard_close` was flipped to assert `"AP3" not in codes` in the same commit.

WCAG references: 2.1.1 Keyboard, 2.1.2 No Keyboard Trap, 2.4.3 Focus Order, plus the [APG disclosure pattern](https://www.w3.org/WAI/ARIA/apg/patterns/disclosure/) which calls for Escape-to-close on disclosure widgets.

#### AP4: `/lighthouse/lamp/<id>/published` meta refresh resets focus every 3 seconds (RESOLVED 2026-05-28)

When the Stripe webhook hadn't settled yet, `published.ejs` emitted:

```html
<meta http-equiv="refresh" content="3">
```

The page reloaded every 3 seconds until the build flipped to `live`. Each reload reset `document.activeElement` to `<body>`. A keeper who tabbed onto the "Skip the wait and go to your website" link lost focus every 3 seconds, which was both disorienting and a WCAG 2.2.4 (Interruptions) hazard.

**Fix landed 2026-05-28** in `harbor-a11y-published-meta-refresh-focus-reset`: the `<meta http-equiv="refresh">` tag was removed. The almost-there branch now wraps the status copy in a `<div role="status" aria-live="polite" id="publish-status">` block and runs an inline polling script (carrying the per-request `cspNonce`) that calls `fetch(window.location.href)` every 5 seconds with `Accept: text/html` + `cache: no-store`. When the fetched HTML contains the settled marker (`It is real now`), the script calls `window.location.reload()` once to swap into the settled state. Until that point, focus on the "Skip the wait" link is never reset and the SR-polite status region announces the wait copy once on first paint without re-announcing on every poll.

```html
<div role="status" aria-live="polite" id="publish-status">
  <p>Your website is being published right now. This takes a moment.</p>
  <p class="small">This page will update on its own as soon as your website goes live.</p>
</div>
```

```js
fetch(window.location.href, { credentials: 'same-origin', headers: { 'Accept': 'text/html' }, cache: 'no-store' })
  .then(r => r.ok ? r.text() : null)
  .then(html => {
    if (html && html.indexOf('It is real now') !== -1) window.location.reload();
    else setTimeout(poll, 5000);
  });
```

The static scanner's AP4 detector no longer flags `published.ejs`, and the pin in `tests/test_keyboard_tab_order_static_scan.py::test_ap4_published_meta_refresh_focus_reset` was flipped to assert `"AP4" not in codes` in the same commit.

WCAG references: 2.2.1 Timing Adjustable, 2.2.4 Interruptions, 2.4.3 Focus Order.

## Live pass: prerequisites

The Playwright spec at `web/tests/ui/keyboard-tab-order.spec.ts` is wired but not run in this audit. Running it requires:

1. `cd web && npm i -D @playwright/test` (already installed; no-op when present).
2. A local Postgres with the CAS schema applied and `CAS_HARBOR_ENABLED=true`.
3. A web server on `http://localhost:4000` - `cd web && npm run build && CAS_HARBOR_ENABLED=true npm start`.
4. Environment fixtures:
   - `CAS_KB_DEV_TRIAL`: UUID of an admin-gated trial keeper. Covers `/lighthouse/table`, `/lighthouse/preview`. Use a **fresh** trial (turn count well below the email gate) so the 8-turn walk doesn't 402 before the test asserts.
   - `CAS_KB_LAMP_BUILD_ID`: UUID of a build in `status='preview'` with `review_status='approved'`. Covers `/lighthouse/lamp/<id>` and `/lighthouse/lamp/<id>/make-public`. The `make-public` form gates on `canMakePublic`, so a `preview/approved` build is required (a `preview/pending` build will not render the form).
   - `CAS_KB_BASE_URL`: optional override (defaults to `http://localhost:4000`).
5. `cd web && npx playwright test tests/ui/keyboard-tab-order.spec.ts`.

The spec writes per-route walks to `docs/audits/keyboard-tab-order-<date>-<route>.json`. When run, append a "Live pass; results" section to this document that names every focus stop the walk recorded and pairs it with the corresponding static-pass row.

## Regression contract

`tests/test_keyboard_tab_order_static_scan.py` pins the four anti-patterns as expected-active and the per-view focusable counts as expected-stable. When AP1 / AP2 / AP3 / AP4 are addressed in the views, the corresponding test will fail with a diagnostic pointing back at this document; the fix-the-doc-too contract. Per-view focusable counts protect against silent removal of CTAs from the tab path (e.g. dropping the "Make it public" anchor in `lamp.ejs` would drop the count from 9 → 8 and trip the guard).

## Bug entries

Follow-up items raised by this audit (all four anti-patterns above were resolved on 2026-05-28):

- `harbor-a11y-preview-family-buttons-aria-label`: every template-family card in `web/views/lighthouse/preview.ejs` ships a `<button>Choose this</button>` with no per-family aria-label, so SR users tabbing the grid hear five identical "Choose this" announcements. Fix: add `aria-label="Choose <%= f.name %>"` near line 124. Covered by AP1 in `scripts/keyboard_tab_order_static_scan.py` and the `/lighthouse/preview` test in `web/tests/ui/keyboard-tab-order.spec.ts`.
- `harbor-a11y-table-autofocus-defeats-skip-link` - `web/views/lighthouse/table.ejs` calls `textarea.focus()` at the end of its initial-state IIFE, which steals focus from the skip-to-content link on cold load. Fix: defer the autofocus until first keeper interaction OR only apply it when `document.referrer` is non-empty.
- `harbor-a11y-table-memory-drawer-keyboard-close` - `web/views/lighthouse/table.ejs` memory-panel disclosure widget does not move focus into the drawer on open and has no Escape keydown handler. Fix: add an Escape handler + on-open `panel.querySelector('a, button')?.focus()` + on-close `memoryTab.focus()` return.
- `harbor-a11y-published-meta-refresh-resets-focus`: RESOLVED 2026-05-28 (shipped as `harbor-a11y-published-meta-refresh-focus-reset`). `web/views/lighthouse/published.ejs` previously used a 3-second `<meta http-equiv="refresh">` while the Stripe webhook was in flight, resetting focus every cycle. Now ships a `role="status" aria-live="polite"` block + inline polling script (`fetch(window.location.href)` every 5s, reloads once the settled marker appears).

## Addendum, 2026-10-10: focusable counts after the paying-browser links and the subscription notices

Three Harbor views gained plain anchors when the browser that paid for a build was given a way back to its lamp page (`req.harborPaidKeeper`, `web/src/middleware/trial-cookie.ts`), and `lamp.ejs` gained one more when it began to show a notice for each state a subscription can be in. The findings above (AP1 to AP4, all resolved 2026-05-28) are unchanged; only the per-view counts moved, and `tests/test_keyboard_tab_order_static_scan.py` (`_EXPECTED_FOCUSABLE_COUNTS`) pins the new numbers.

| View | Focusables before | Now | The added controls (plain `<a>` in DOM order, no `tabindex`) |
|---|---|---|---|
| `lamp.ejs` | 9 | 11 | "choose a password", a link to `/forgot-password`, rendered only for the paying browser that has no account session (`ownerKind === 'keeper' && locals.signedInToAccount === false`); it follows "Cancel subscription". And the support address link (`mailto:`) inside the subscription notice (`subscriptionNotice`), shown when the subscription is not in force (unpaid, paused, incomplete, incomplete_expired, canceled, and past_due); it comes before "Cancel subscription" |
| `published.ejs` | 3 | 4 | "Manage your website and subscription", a link to the lamp page, rendered in the settled state (`webhookSettled`); it follows the "your conversation" link |
| `table.ejs` | 17 | 18 | the same "Manage your website and subscription" link, in the live-site keeper banner (`keeperHandoffBuildId && hasLiveSite`); it follows the "Make changes to my site" button |

`make-public.ejs` went from 8 to 9 on 2026-10-11, when the form began to show the address a payment is tied to (`docs/THREAT_MODEL.md`, "The address a payment is tied to"). The address now comes first, the "This is my email address" checkbox (shown to someone paying from a trial) follows it, and the amount comes after both. When the account is already decided the address is plain text, which the Tab key skips; the checkbox then names it with `aria-describedby`. The scanner counts every branch, so the address field and the checkbox are both in the 9, and the order in the template, which is the Tab order, is: back link, address field (only when the address is asked for), the checkbox, the amount slider, "this amount fits my practice", terms, privacy, the submit button, "Not yet".

The other four views are unchanged (`cancelled.ejs` 1, `changes-first.ejs` 2, `preview.ejs` 3, `recall.ejs` 1). The total the scanner reports is now 49 controls across the 8 views; the "41" in the static-pass summary above was already out of date before these changes (the per-view counts in its table summed to 44, after the three self-serve controls added to `table.ejs` on 2026-06-07). Some link texts in these views were reworded afterwards (for example "back to your conversation with Haven" in place of a phrase that named the keeper's table); that changes labels, not counts. Re-run `python3 scripts/keyboard_tab_order_static_scan.py` for the current enumeration. None of the new links is conditional on JavaScript, and none changes the skip-link or `<main id="main">` structure confirmed above. The live Playwright walk has still not been run, so the order is established from the templates, not from a browser, with one exception: the fixed-address branch of `make-public.ejs` (the address shown as text) was walked with the Tab key in a browser on 2026-10-11 and went skip link, back link, "This is my email address", the amount slider, terms, privacy, the submit button, "Not yet". The typed-address branch is covered by the rendered-view test and the scanner only.
