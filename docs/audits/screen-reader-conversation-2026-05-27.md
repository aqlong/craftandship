# Static review of the `/lighthouse/table` conversation markup for screen readers; 2026-05-27

> **What this is:** a static read of one template (`web/views/lighthouse/table.ejs`) against four contracts. It is not a screen-reader walk: no screen reader or other assistive technology was run, and the live pass described below never ran. `/lighthouse/table` is the product's own Harbor chat route, not Google Lighthouse.

**Parent roadmap item:** `harbor-usability-3-screenreader-aria-live-walk` (Sub-3 of `harbor-usability-live-browser-followups`, decomposed 2026-05-27).

> **Status at public export: findings SR1, SR4, SR5 and SR6 are FIXED in `web/views/lighthouse/table.ejs`** (after the static pass below was written). `#conversation` is now `role="log"` with `aria-live="polite"` and `aria-atomic="false"`, so appended companion turns and the JS-injected email-gate form (appended inside it) are announced (SR1, SR5). The keeper's-table header is an `<h1>` (the companion name) and the `<header>` is bound to its name and eyebrow with `aria-labelledby` (SR4, SR6). SR2 and SR3 passed from the start. The text below is the original static-pass audit and still describes the markup as it was on 2026-05-27; the live Playwright walk (`web/tests/ui/screen-reader-conversation.spec.ts`) was never run green as part of this export.

**Scope:** the conversation surface at `/lighthouse/table`, the only `/lighthouse/*` surface that mutates the DOM in response to the keeper's primary action (Send a turn). Sub-1 (axe sweep) covered static a11y attribute coverage and contrast; Sub-2 (keyboard tab-order walk) covered focus order and Tab-only operability. Sub-3 is the screen-reader-announce-on-mutation contract: when Haven replies, can a non-sighted keeper actually tell?

The four contracts the spec at `web/tests/ui/screen-reader-conversation.spec.ts` asserts:

| Code | Contract | WCAG |
|---|---|---|
| SR1 | When `appendCompanionTurn(...)` mutates `#conversation`, the new turn lives inside an `aria-live` region (or `role="log"` / `role="status"` / `role="alert"`). | 4.1.3 Status Messages |
| SR2 | `#keeper-text` textarea has an associated `<label for>` or `aria-label` or `aria-labelledby`. | 1.3.1 Info and Relationships, 3.3.2 Labels or Instructions |
| SR3 | `#send-btn` has an accessible name (visible text content or explicit `aria-label`). | 4.1.2 Name, Role, Value |
| SR4 | The visible eyebrow text "at the keeper's table" is reachable in the accessibility tree alongside the companion name. | 1.3.1, 2.4.6 Headings and Labels |

## Method

Two passes, identical pattern to Sub-1 and Sub-2.

1. **Static review** (this document) - `web/views/lighthouse/table.ejs` was read in full and the four contracts checked against the markup that ships today. Findings grouped below.
2. **Live Playwright walk**, `web/tests/ui/screen-reader-conversation.spec.ts` drives a real browser at `/lighthouse/table?dev_trial=<UUID>`, submits a turn, waits for Haven's reply to land, and asserts SR1 through SR4 against the post-mutation DOM. Per-route walk JSON is written to `docs/audits/screen-reader-conversation-<date>-table.json`. **Not run in this pass**, see "Live pass: prerequisites" below.

The split mirrors Sub-2 because the live walk needs the same prerequisites (running web server + Postgres + admin-gated trial cookie + `CAS_HARBOR_ENABLED=true`), and the executor cannot stand any of them up. The static pass surfaces the deterministic findings; the live spec is wired so a human (or CI step with the right fixtures) can run it on demand to verify the fixes and pin the contract.

## Static pass: findings

| Code | Severity | Status | Summary |
|---|---|---|---|
| SR1 | issue | ACTIVE | `#conversation` has no `aria-live`; appended companion turns are silent to AT |
| SR2 | - | PASS | `#keeper-text` carries BOTH `<label for>` and `aria-label` |
| SR3 | - | PASS | `#send-btn` has visible text "Send", sufficient accessible name |
| SR4 | advisory | ACTIVE | Eyebrow span is reachable text; no explicit accessible-name binding on the header element itself |
| SR5 | issue | ACTIVE | Email-gate form (JS-injected) is not in an aria-live region; appearing form is silent to AT |
| SR6 | advisory | ACTIVE | Keeper's-table header lacks a heading level (`<h1>` / `<h2>`); AT users can't jump to it via heading navigation |

### SR1: `#conversation` lacks `aria-live`; appended companion turns are silent to AT

**Status:** ACTIVE, fix recommended.

`web/views/lighthouse/table.ejs` line 576 declares:

```html
<section class="conversation" id="conversation">
```

The inline IIFE later mutates this container twice per turn round-trip:

```js
function appendKeeperTurn(text) {
  // ...
  conversation.appendChild(wrap);
  scrollToBottom();
}

function appendCompanionTurn(text, railFlags) {
  // ...
  conversation.appendChild(row);
  scrollToBottom();
}
```

Neither `#conversation` nor any ancestor carries `aria-live`, `role="log"`, `role="status"`, or `role="alert"`. When Haven replies, the DOM gets a new `.turn-row-companion` node, but a screen reader, JAWS, NVDA, VoiceOver, TalkBack, will not announce it. The non-sighted keeper has no way to know Haven has spoken short of repeatedly Shift+Tab walking back into the conversation to manually read the latest turn. This is the central WCAG 4.1.3 (Status Messages) failure on the chat surface.

The contract is satisfied by either:

1. **(preferred)** Adding `aria-live="polite"` and `role="log"` to the conversation container itself:

   ```html
   <section class="conversation" id="conversation" aria-live="polite" aria-atomic="false" role="log" aria-label="Conversation with Haven">
   ```

   `role="log"` is the right ARIA role for an append-only chat-style stream. `aria-live="polite"` waits for the AT to finish the current utterance before announcing; `assertive` would interrupt the keeper mid-thought, which is the wrong UX for chat. `aria-atomic="false"` so only the newly-appended turn is read, not the entire conversation history every time.

2. **(alternative)** Adding `role="status"` + `aria-live="polite"` to each appended `.turn-row-companion` node inside `appendCompanionTurn(...)`. This is more verbose per-turn but lets the keeper-turn append stay silent (since the keeper already knows what they just typed). The Playwright spec accepts either form via `isInLiveRegion(...)` which walks the ancestor chain.

WCAG references: 4.1.3 Status Messages, plus the [APG live regions pattern](https://www.w3.org/WAI/ARIA/apg/practices/live-regions/).

### SR2, Compose textarea labelling, PASS

**Status:** PASS, no action required.

`web/views/lighthouse/table.ejs` lines 737-745:

```html
<label for="keeper-text" class="cas-sr-only">Your message to <%= companionName %></label>
<textarea
  id="keeper-text"
  name="keeperText"
  placeholder="Write to <%= companionName %>..."
  aria-label="Your message to <%= companionName %>"
  rows="4"
  required
></textarea>
```

The textarea is double-bound: a visually-hidden `<label for="keeper-text">` AND an `aria-label` attribute on the textarea itself. Either alone would satisfy WCAG 1.3.1 + 3.3.2; the belt-and-suspenders is fine and intentional (the `cas-sr-only` rule was added in the 2026-05-09 a11y pass, see line 500 comment in the same file). The Playwright assertion `composeTextarea?.hasLabelFor || composeTextarea?.ariaLabel || composeTextarea?.ariaLabelledBy` passes on this markup today.

The dev-only bypass form (line 689) and the JS-injected email-gate form (line 853) carry the same `cas-sr-only` label pattern. Both pass the contract.

### SR3, Send button accessible name, PASS

**Status:** PASS, no action required.

`web/views/lighthouse/table.ejs` line 746:

```html
<button type="submit" id="send-btn">Send</button>
```

Per the [W3C ARIA name-computation algorithm](https://www.w3.org/TR/accname-1.2/), a `<button>` element's accessible name is its text content when no `aria-label` / `aria-labelledby` is present. "Send" is non-empty, unambiguous, and matches the visual label sighted users see; exactly what WCAG 2.5.3 (Label in Name) wants. No `aria-label` is needed. Adding one ("aria-label='Send your message to Haven'") would be acceptable as a more descriptive variant, but the current markup is correct as shipped.

The "Build my website" button (line 647), the "Make changes to my site" button (line 614), and the "[dev] skip Stripe..." details/summary (line 686) all carry non-empty visible text and pass the contract identically.

### SR4, Visible eyebrow text accessible-name'd, ADVISORY

**Status:** ADVISORY, current markup is technically correct but improvable.

`web/views/lighthouse/table.ejs` lines 570-574:

```html
<header class="keepers-table-header">
  <img class="haven-header-avatar" src="/brand-harbor/haven-avatar-80.png" alt="<%= companionName %>" />
  <span class="name"><%= companionName %></span>
  <span class="subtitle">at the keeper's table</span>
</header>
```

The eyebrow is a plain `<span class="subtitle">` containing readable text. A screen reader on landing will speak the elements in document order: "Haven, image" (from the avatar's `alt`), then "Haven" (from `.name`), then "at the keeper's table" (from `.subtitle`). The eyebrow IS in the accessibility tree and IS read; the Playwright assertion `headerEyebrow.textContent.includes("at the keeper's table")` passes today.

What's not great: the `<header>` element itself has no `aria-label` or `aria-labelledby`, so an AT that navigates by landmark will hear "banner" (the implicit role of `<header>` when it's a child of `<body>` and not nested inside `<article>` / `<section>` etc.) with no descriptive name. The avatar's `alt="Haven"` repeats the visible `.name`, so SR users hear "Haven, image. Haven. At the keeper's table."; mildly redundant but not broken.

Two improvement options, both low-cost:

1. **Bind the header explicitly:**

   ```html
   <header class="keepers-table-header" aria-labelledby="keepers-table-name keepers-table-eyebrow">
     <img class="haven-header-avatar" src="/brand-harbor/haven-avatar-80.png" alt="" />
     <span class="name" id="keepers-table-name"><%= companionName %></span>
     <span class="subtitle" id="keepers-table-eyebrow">at the keeper's table</span>
   </header>
   ```

   Drop the avatar `alt` to `""` (it is decorative now that the name binding is explicit), add IDs to the name and eyebrow spans, and `aria-labelledby` so the header landmark announces "Haven, at the keeper's table" as a single label. WCAG 2.4.6.

2. **Use a heading instead of a span pair:** see SR6.

Severity is `advisory` because the current rendering already meets the literal contract (eyebrow text is in the AT tree). The recommendation is to make the surrounding landmark announcement more useful for SR users navigating by landmark, not to fix a present-day failure.

### SR5: JS-injected email-gate form is not in a live region

**Status:** ACTIVE, fix recommended (related to SR1).

`web/views/lighthouse/table.ejs` lines 846-875: when the `POST /lighthouse/table/turn` endpoint returns 402 with `requiresEmail: true`, the inline `appendEmailGate(...)` function injects:

```js
var wrap = document.createElement('div');
wrap.className = 'turn turn-companion';
wrap.id = 'email-gate-affordance';
wrap.innerHTML =
  '<p>' + (message || 'Could you leave me an email so we can stay in touch?') + '</p>' +
  '<form id="email-gate-form" ...>...</form>';
conversation.appendChild(wrap);
```

This is the same problem as SR1; the form appears via DOM mutation inside `#conversation`, which has no `aria-live`. The post-mutation `input.focus()` on `#email-gate-input` partially compensates (SR will announce the focused field), but the prose intro ("Could you leave me an email...") goes unannounced. The non-sighted keeper hears the input field label ("Your email") with no context.

Fixing SR1 fixes SR5 automatically: with `role="log" aria-live="polite"` on `#conversation`, the email-gate `<p>` and the form children both get announced when `appendChild` runs.

### SR6, Header lacks heading semantics, ADVISORY

**Status:** ADVISORY, improves heading navigation.

`web/views/lighthouse/table.ejs` uses `<span class="name">` for the companion name (line 572). No `<h1>` exists on the page. The only heading anywhere in the document is `<h2>What <%= companionName %> remembers</h2>` inside the memory side-panel (line 545). For SR users who navigate by heading (`H` key in JAWS / NVDA / VO), the conversation surface has no top-level landmark to jump to.

Promoting `.name` to `<h1 class="name">` would:

1. Give the document a proper top-level heading (`<h1>`).
2. Keep the visible typography (the existing `.name { font-size: 22px }` rule applies identically to `<h1>` since it targets the class).
3. Mesh with the SR4 fix above (`aria-labelledby="keepers-table-name keepers-table-eyebrow"` still works).

Severity is `advisory` because the surface is operable without it; this is heading-navigation polish, not an SR blocker.

## Live pass: prerequisites

The Playwright spec at `web/tests/ui/screen-reader-conversation.spec.ts` is wired but not run in this audit. Running it requires:

1. `cd web && npm i -D @playwright/test` (already installed; no-op when present).
2. A local Postgres with the CAS Harbor schema applied and `CAS_HARBOR_ENABLED=true`.
3. A web server on `http://localhost:4000` - `cd web && npm run build && CAS_HARBOR_ENABLED=true npm start`.
4. Environment fixtures:
   - `CAS_SR_DEV_TRIAL`: UUID of an admin-gated trial keeper. Use a **fresh** trial whose turn-count is well below the email gate (default 22 turns) so the in-test turn submission does not 402.
   - `CAS_SR_BASE_URL`: optional override (defaults to `http://localhost:4000`).
5. `cd web && npx playwright test tests/ui/screen-reader-conversation.spec.ts`.

The spec writes its per-route walk to `docs/audits/screen-reader-conversation-<date>-table.json`. When run for the first time after a fix lands, append a "Live pass; results" section to this document that names which SR contracts were exercised and pairs each pass/fail with the corresponding static-pass row above.

## Regression contract

The Playwright spec's four `expect(...)` assertions are the regression contract; once SR1 + SR5 are fixed and the live walk runs green, the spec's strictness pins the contract against future drift. Until then, the assertions intentionally fail on the current markup; this is by design and mirrors the Sub-1 / Sub-2 pattern (the contract describes the desired end state, the audit doc records what is missing today).

No static Python pin test ships with this audit (unlike Sub-1's `tests/test_lighthouse_a11y_contrast_scan.py` and Sub-2's `tests/test_keyboard_tab_order_static_scan.py`). The reason: the contracts here are dynamic, `aria-live` only matters once the DOM mutates, so a static EJS scanner would either flag the absent attribute as a false-positive on every visit OR require teaching the scanner about which JS code paths mutate which DOM nodes. The live spec catches the live behavior; the static review in this doc is the deps-free first pass.

## Bug entries

Follow-up items raised by this audit:

- `harbor-a11y-table-conversation-aria-live` - `web/views/lighthouse/table.ejs` `<section id="conversation">` has no `aria-live` region; appended companion turns and the JS-injected email-gate are silent to screen readers. Fix: add `role="log" aria-live="polite" aria-atomic="false" aria-label="Conversation with <%= companionName %>"` to the section opening tag near line 576. Verified by SR1 + SR5 in `web/tests/ui/screen-reader-conversation.spec.ts`.
- `harbor-a11y-table-header-landmark-binding` - `web/views/lighthouse/table.ejs` `.keepers-table-header` is a `<header>` landmark with no `aria-label` / `aria-labelledby`, and the visible eyebrow lives in an unbound `<span>`. Fix: add IDs to `.name` and `.subtitle`, point `aria-labelledby` at both, drop the avatar `alt` to `""`. Optionally promote `.name` to `<h1 class="name">` to give the page a top-level heading. Verified by SR4 + SR6 in the same spec.
