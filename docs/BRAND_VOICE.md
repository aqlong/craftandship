# Craft & Ship Brand Voice

> Historical document from April 2026; the system was wound down in July 2026. It is kept because the pre-commit lint (`scripts/check_brand_voice.py`) and several tests enforce its hard rules.

This is the authoritative voice guide for every customer-facing surface: landing, pricing, how-it-works, gallery, intake, chat, status, dashboard, payment, contact, auth views, emails, meta tags, legal copy (tone only, not the binding terms), and the support knowledge base. Read this before touching any copy. The pre-commit hook enforces the hard rules; the rest is taste.

**Last reviewed:** 2026-04-26
**Enforcement:** `.githooks/pre-commit` + code review

> **2026-04-26 update: Harbor v2 voice.** The site is reorienting around the Living Platform / Lighthouse-and-Haven offering: one subscription ($39/month sliding to $19), one companion (Haven), one warm home. With that shift come five new hard rules:
> 1. **No competitor framing on customer surfaces.** We are one-of-a-kind. Confidence over comparison.
> 2. **No technical-ownership marketing copy.** No "source code," "GitHub," "self-host" as headlines for healers.
> 3. **Single companion name: Haven.** Marin is retired.
> 4. **Minimalism in word count.** Three sentences beat five. One adjective beats three. The painting carries the emotional register; the copy doesn't have to.
> 5. **Under-promise, over-deliver. Don't catalog Haven's gestures.** Customer copy names what's needed for the first decision and stops. Weekly questions, site blessings, longitudinal markers, end-of-year letters, voice intake, the keeper's-table rituals; these are kept off the marketing surface as named features. They are discovered, not advertised. Haven herself is named; her specific gestures are not.
> 6. **No minimizing modifiers** when describing customers or their work. No *small site*, *small studio*, *small business*, *little practice*, *modest*, *tiny*, *humble*. Name them by their work (practitioner, therapist, facilitator, event producer, studio, practice), not by their scale. The painting and the warm register already signal "made by hand"; we don't need diminutives.
>
> The build narrative now includes Haven explicitly: *Haven, your companion, helps you shape what you want over time. Your website is built from a library of tested building blocks and reviewed before it goes live.* (An earlier draft of this line said that a Senior Web Craftsman designs and builds each website; rule 6 says why copy no longer says so.) The rules below are updated where the shift bites.

---

## Hard rules (enforced by hook)

These are non-negotiable. The pre-commit hook rejects commits that break them. A violation in the web views or email templates will block the commit.

### 1. No em dashes

Never use the em dash character (U+2014, `em dash`) or the HTML named entity for an em dash. Use a colon, a semicolon, a comma, or start a new sentence.

Wrong: `We build it <em dash> every website is reviewed before it goes live <em dash> ready in days.` (`<em dash>` stands for the character itself, which this file does not type)
Right: `We build it. Every website needs a recorded review approval before it goes live.`

En dashes (`–`, U+2013) are allowed in numeric ranges: `$5,000–$15,000`, `4–8 weeks`.

### 2. Affirmative voice only: no competitor framing, no listing what we're not

Focus on what Craft & Ship does. Do not frame value through what we are not, what competitors are, or what the customer should avoid. We are doing something one-of-a-kind. Confidence, not comparison.

**This is the rule that AI assistants drift on most often.** When a model wants to reassure a reader, the easy reach is to list what isn't happening: "no card," "no contracts," "no streaks," "no leaderboards," "won't pretend to be human," "won't optimize for engagement." The discipline is to say what IS happening, instead, with confidence. Minimal is confidence.

Wrong: `Not a template. Not a no-code builder. Not AI checking AI.`
Right: `A custom website, made with you. Every website is reviewed before it goes live.`

Wrong: `We don't lock you in.`
Right: `Your website is yours. If you ever leave, we hand the whole thing over intact.`

Wrong: `Cancel anytime, and your app keeps running.` *(reads as anti-SaaS positioning)*
Right: *(don't say it; the warm offering is the message)*

Wrong: `No tiers. No add-ons. One number, sliding to fit.`
Right: `One number. Sliding to fit.`

Wrong: `A conversation first. No card. No obligation.`
Right: `A conversation first.`

Wrong: `She will not pretend to be human, will not roleplay, will not flatter, will not say yes to bad ideas.`
Right: `She is honest about being AI. When you ask, she tells you. When she doesn't know, she says so.`

Wrong: `There is no clock running. There is no funnel. There is no streak.` *(and the same sentence with "by hand" added: copy does not say a site is made by hand, see rule 6)*
Right: `Slowly, in your voice. The pace is the conversation's.`

Wrong (a "What we won't do" / "What she will not do" bulleted list).
Right: drop the list. Convey what's essential another way, or say less. Say nothing if you can't make it affirmative.

**This rule extended 2026-04-26 (Harbor v2):** competitive framing is no longer allowed in comparison tables, TCO calculators, or "vs \[anyone\]" pages on customer surfaces. Competitor-comparison pages and calculators are removed from the marketing surface. We do not name competitors on customer pages.

**Where the lint draws the line today.** `scripts/check_brand_voice.py` flags named competitors and "vs / switching from" idioms in the staged files it scans. The `/vs` SEO pages (`web/views/vs-index.ejs`, `web/views/comparison.ejs`) keep competitor names to the title, meta description, URL and JSON-LD, and take their data from `web/src/data/comparison-pages.ts`, which sits outside the lint's scope on purpose; their visible body copy stays affirmative. One page is exempt from the competitor rule by name: the citation page `web/views/docs-research.ejs` ("Research & sources"), whose whole purpose is to cite the vendors' public pricing pages behind a claim (`COMPETITOR_RULE_EXEMPT_PATHS` in `scripts/check_brand_voice.py`). Only the competitor rule is waived there; the em-dash and technical-ownership rules still apply. A single line elsewhere can be allowed with a `brand-voice-allow-competitor: <reason>` marker. Never add marketing copy to the exempt list.

**Extended again 2026-04-27 (Harbor v2.1):** explicit "list of refusals" sections are also retired from customer surfaces. The previous draft of `/the-companion` had a "What she will not do" list; the previous draft of `/about` had a "What we won't do" list. Both are gone. The values they expressed remain operationally true; the customer-facing form is what we DO, in a single short paragraph, or nothing.

**Extended again 2026-04-28 (Harbor v2.2: forward-looking only):**

- **No shutdown talk, no scary contingencies, even when the intent is reassurance.** Phrases like *"if we ever close our doors,"* *"never held hostage,"* *"if CAS ever shuts down,"* *"won't lock you in,"* *"no risk of losing your work"* all plant doubt in the keeper's mind. Do not seed disasters that are not happening. The trust is conveyed by the calm of the system itself, not by reassurances about catastrophes.
  - Wrong: *"Your memory is yours, returned as a book if CAS ever closes its doors."*
  - Right: *"Yours, intact, kept across the years."*

- **Competitor names never appear in visible body copy.** A comparison page may carry its comparison intent in metadata (`<title>`, `<meta name="description">`, structured data). The visible body speaks to the practitioner's forward picture, calmly, in the affirmative.

- **Calm, quiet confidence speaks for itself.** When a sentence wants to reassure, ask whether the system already conveys the reassurance through its visible offering (the painting, the typography, the price, the canon of practitioners on `/recent-harbors`). If so, cut the sentence. Minimal is confidence.

### 3. Website, not software / app / platform

In every customer-facing surface, the output is a "website." The word "app" is reserved for technical internals (schemas, logs, code comments, generated-app README files). The word "platform" is reserved for internal engineering discussion.

Wrong: `Own your software.` / `Describe your app.` / `Your app is ready.`
Right: `Own your website.` / `Describe your website.` / `Your website is ready.`

Exception: when the customer's own website is a mobile-style application (an internal tool, a booking portal, a CRM), "web app" is acceptable in technical descriptions of what it contains. The primary framing remains "website."

### 4. No review turnaround in customer copy

Customer-facing copy gives no review time. Do not say "1 to 2 business days," "30 minutes," "60 minutes," "90 minutes," "48 hours," "two days," or any other number of hours or business days for the review. The review is described as a recorded review approval before the website goes live. Harbor sells one subscription, so no tier has its own review time.

Wrong: `Senior engineer review (30+ min)` / `Reviewed within 48 hours` / `Reviewed within 1 to 2 business days`
Right: `A recorded review approval before it goes live`

### 5. Plain English for how we build

Do not use the words "compose," "composed," "reviewed components," "deterministic pipeline," or similar engineering jargon in customer copy. Those terms are internal.

Wrong: `We compose your website from reviewed components.`
Right: `Your website is built from a library of tested building blocks.` or `We build your website from tested parts and tailor them to what you tell Haven.`

Simplest form: `Your website is built from tested building blocks.` That is usually enough. Do not say "proven" or "battle-tested" as a claim about results: the building blocks are covered by tests, and that is the true statement.

### 6. Accuracy about how a site is made: AI in intake (Haven), software in the build, a review before it goes live

Copy says what the code does. As of 2026-04-22, Craft & Ship builds websites with a static engine: software assembles each site from a library of unit-tested building blocks, chosen from the customer's answers, and AI does not write the code that ships. **As of 2026-04-26 (Harbor v2)**, the AI in intake has a name and a role: **Haven**, the companion who talks with the customer, helps them shape what they want over time, and stays present afterward. Haven is honest about being an AI.

What the code supports, and so what copy may say:
- **How the site is made.** It is built from a library of tested building blocks, matched to what the customer told Haven. Copy does not say that a person designs, writes or hand-builds each site, and does not use "by hand", "handcrafted", "handmade" or "honestly human" about the build: no code path has a person do that.
- **That a review happens.** Every website is reviewed before it goes live: the status `live` is reached only through a recorded review verdict (`review_status = 'approved'`) or an admin's own promotion. Copy says that the review happens. It does not say who reviews, and it does not say a named person reads every page, because the repository records neither who gave each verdict nor which builds went through the automated review mode (`CAS_HARBOR_AUTO_REVIEW`). It does not put the review before the preview address: the customer is emailed the preview address when the build is ready, and the review is the step between that preview and the public web.
- **Who Ocean is.** The founder of Craft & Ship, a Senior Web Craftsman, who answers when a customer writes. Copy does not make him the person who designs, builds or reviews each site, and it does not claim years of experience or a number of builds that the repository cannot show.

Wrong: `Our AI writes the full application.` / `AI builds your app in 20 to 30 minutes.` / `Claude writes every route.`
Wrong: `Ocean designs and builds your site by hand and reviews every page.` / `A Senior Web Craftsman reviews every build.` / `Reviewed by Ocean.` / `proven pieces I've refined over years.`
Right: `Haven, your companion, helps you describe what you want and sits with you afterward. Your website is built from a library of tested building blocks. Every website is reviewed before it goes live.`

Haven's role in customer copy:
- Talks with the customer (the cocreation companion)
- Helps shape the spec; remembers across sessions; never pretends to be human
- The relationship surface, ongoing presence, weekly questions, longitudinal markers
- Self-healing after launch (monitors uptime, patches common failures); internal framing
- Support chat on the website: internal framing

Ocean's role in customer copy:
- Founder of Craft & Ship; answers when you write
- Nothing about who builds or reviews a given site (see above)

**Single companion name: Haven.** Marin (the alternate from earlier drafts) is retired. Customer copy uses "Haven" or "your companion." Never "the AI."

**Internal vocabulary stays internal.** "Keeper" (the internal name for a customer) and "the keeper's table" do not appear in what a customer reads; say "your conversation with Haven".

---

## Soft rules (taste and consistency)

These are not hook-enforced. They are what keeps the brand consistent.

### Tone

Confident, warm, and direct. Not defensive. Not salesy. Not precious. Written the way Ocean would explain Craft & Ship to a friend over coffee.

- Prefer short sentences. A three-sentence paragraph beats a seven-sentence one.
- Use "we" for Craft & Ship, "you" for the customer, "Ocean" when naming the founder.
- Name the tradeoff when there is one. Do not pretend the cheap option is always worse.
- No hype words: "revolutionary," "seamless," "game-changing," "cutting-edge," "next-gen." Ship anyway.

### Words we use

- **Own / ownership** (the core promise).
- **Website** (the thing we build).
- **Source code** (what you get).
- **Host anywhere** (the portability promise).
- **Founder** and **Senior Web Craftsman** (for Ocean's title, as in "Ocean Longnion is a Senior Web Craftsman and the founder of Craft & Ship"; not "expert," not "specialist," not "developer," and not as the name of whoever builds or reviews a site).
- **Ocean** (by first name, when personal).
- **Reviewed** (past tense, not "audited" or "validated"; "reviewed before it goes live", with no reviewer named).
- **Tested building blocks** (how a site is made).
- **Launch** (the moment the website goes live).
- **Revision** (a change request).
- **Spec** (the plain-English description of what you want).

### Words we avoid

- **Compose / composed** (except in engineering docs).
- **Generate / generated** (too AI-adjacent; use "build" or "write").
- **Deploy / deployment** (internal; say "launch" or "go live").
- **Stack / tech stack** (internal; only acceptable on the How It Works developer tab).
- **Proprietary** (loaded; just describe what we do).
- **Enterprise-grade** (vague).
- **Production-ready** (means nothing to customers).
- **Seamless / frictionless** (AI-slop giveaways).
- **At scale** (when we do not mean literally large scale).

### Pricing

**Harbor v2 pricing (canonical, aligned with `web/views/pricing.ejs`):** **$39 a month, sliding to $19 on the keeper's word.** Choose any amount from $39 down to $19, whatever fits your practice. Your word is enough. The subscription includes the website built with you, Haven at the keeper's table, hosting and care, unlimited revisions kept across the years, and a review of every website before it goes live.

**The keeper journey** (the order things happen in customer copy):
1. Begin a conversation with Haven. Just talking, your pace.
2. Talk for as long as it feels right, until the shape of your site is clear between you.
3. CAS Harbor builds your site at a preview address, which is emailed to you when it is ready.
4. Walk around in it. Come back to Haven, refine, iterate. The site is reviewed before it goes live.
5. When you're ready to make it public, the subscription begins. $39 a month, sliding to $19 on your word.

**Affirmative voice rule (already enforced in §3 above; restated here because pricing copy is where it drifts).** Describe what IS happening, calmly. Customer copy doesn't put a number of days on the conversation, doesn't describe the relationship as a "trial" with a "period" or an "expiry," and doesn't list what isn't included or what we don't do. The ethos is co-creation: doing things right, calmly, with a review before anything goes live, with our specific customers. Replacement phrasing when drift starts to happen: *"begin a conversation," "talk as long as it feels right," "your word is enough," "when you're ready to make it public, the subscription begins."*

The unified subscription price is the headline. Multi-tier card grids and TCO calculators are removed from customer surfaces.

**Implementation note (post-2026-05-01 tier kill):** the pre-Harbor multi-tier code path (`TIER_DISPLAY_PRICES`, `getEffectiveDisplayPrices()`, `BETA_MODE`, the beta config module, `/payment/:buildId`, `POST /intake/:buildId/confirm`) was removed. Harbor's single-product flow (`/lighthouse/lamp/:buildId/make-public` → Stripe subscription Checkout) is the only path. The canonical pricing literals are `harborSubscriptionPrice` / `harborSlidingScalePrice` in `web/src/app.ts`; the visible source is `web/views/pricing.ejs`.

### Timing

- Review time (customer-facing): **none given** (rule 4); say "a recorded review approval before it goes live"
- Build time (when technical accuracy matters): **"20 to 30 minutes"** (on the How It Works developer tab only)
- End-to-end from order to launch-ready: **none given** (rule 4)

### The Craft Promise

The review before a website goes live is the brand. Every mention of "how we build" includes it, in the words the product uses: "Every website is reviewed before it goes live." Do not describe the build as unreviewed. Do not say that a named person, a "Senior Web Craftsman" or "a human" reads every page: the policy the agents worked under said so, but the code only records a verdict, an automated review mode (`CAS_HARBOR_AUTO_REVIEW`) was added shortly before the wind-down, and the repository does not show which builds a person reviewed (see the Craft Quality Policy note in `CLAUDE.md`). The review time is not stated to customers (rule 4), and nothing in the code enforces a review window.

---

## Examples: before and after

### Hero

Before:
> Own your software. Don't rent it.
> We build it with AI, a Senior Web Craftsman reviews every line, and you get the source code. Ready in days.

After:
> Own your website. Don't rent it.
> Describe your website in plain English. It is built from a library of tested building blocks and needs a recorded review approval before it goes live. You get the full source code.

### Step description

Before:
> Our AI builds your full custom application: every page, every feature, every login screen. Real working software, not a template. Usually ready in 20 to 30 minutes.

After:
> Your website is built from a library of tested building blocks, matched to what you told Haven and tailored to your business. Usually ready in 20 to 30 minutes, with a recorded review approval before it goes live.

### Pricing tier review line

Before:
> Senior engineer review (30+ min)

After:
> A recorded review approval before it goes live

### FAQ

Before:
> Is this a template?
> No. Every app is generated from scratch based on your spec.

After:
> Is this a template?
> You get a custom website, built for your business. We use a library of tested building blocks (login, admin, booking, payment, and others) and tailor them to your spec. Each website is unique.

### Email (build queued)

Before:
> Your build is queued. Our AI generates the full application from your spec, runs automated tests, and stages it. Then a Senior Web Craftsman personally reviews every line before delivery.

After:
> Your build is queued. Your website is built from a library of tested building blocks, tailored to your spec, then tested and staged at a preview address that we email to you. Every website is reviewed before it goes live.

---

## Hook enforcement

`.githooks/pre-commit` runs an em-dash lint on staged files matching these paths:

- `web/views/**/*.ejs`
- `web/src/services/**/*.ts` (expanded 2026-04-27 from `email.ts` only)
- `web/src/cron/**/*.ts` (the scheduled jobs)
- `web/src/routes/**/*.ts`
- `web/src/app.ts`
- `queue/mailer.py`
- `coordinator/actuators.py`
- `compose/widgets/**/*.ejs` (HTML-comment scope only: the comments ship to the rendered page source)

Any file in those paths containing `em dash` (U+2014) or its HTML named entity in a staged diff will block the commit with a message pointing back here.

To bypass for a legitimate edge case (e.g., quoting a customer testimonial that contains an em dash), add `<!-- brand-voice-allow-em-dash: <reason> -->` to the line above. Do not use this to get around the rule. Every use is reviewed.

## Before editing any copy

1. Read this file end to end.
2. Check `web/src/app.ts` (`harborSubscriptionPrice` / `harborSlidingScalePrice`) for current pricing.
3. Make the change. Never hardcode a price or a review window.
4. Run the web unit tests (`scripts/run_web_tests.sh`) if the change touches anything `routes.public-pages.test.ts` asserts.
5. The hook will catch em dashes on commit. Staging or CI will catch the rest.
