# Decisions

These are the calls I made as the director of the AI coding agent that wrote Craft & Ship, roughly in order. Each says what the situation was, what I chose, and what it cost. The technical designs that carry them out (Postgres as the queue, the review condition inside the publishing `UPDATE`, the coordinator's permission table) were the agent's proposals, which I judged by their results rather than by reading the code; [ARCHITECTURE.md](../ARCHITECTURE.md) explains them.

## 1. Agents lose write access to production (9 April 2026)

- **Context.** Production was deleted twice in one day: first by a background agent that had Railway write access, then by an orphan sweep with no guard. It came back from a database backup taken earlier that night ([RAILWAY_DISASTER_RECOVERY.md](RAILWAY_DISASTER_RECOVERY.md)).
- **Decision.** Background agents never get Railway write access, and every Railway mutation runs in the foreground where a person sees it. In code: the sweep does nothing unless explicitly enabled, `destroy_project()` refuses protected names and the platform's own project, and the admin delete route needs `NODE_ENV=production` and `CAS_ALLOW_RAILWAY_WRITES=true`.
- **Cost.** Slower operations, and it set the pattern for every later automation call: autonomy is earned, and production writes stay with a person.

## 2. Take the model out of the build path (20 April 2026)

- **Context.** v1 asked a model to write each customer's app, and 117 post-processors repaired the output. The same six categories of bug kept coming back ([MW_BUG_RECURRENCE_RCA.md](internals/MW_BUG_RECURRENCE_RCA.md)), and the seventh iteration of one customer build surfaced eight regressions.
- **Decision.** Remove AI from the build entirely. The v2 engine composes each site from JSON decision files and unit-tested widgets; the model stays in the conversation and in turning it into a spec ([CAS_V2_STATIC_COMPOSE_PLAN.md](internals/CAS_V2_STATIC_COMPOSE_PLAN.md)).
- **Cost.** Less range per site. In exchange the same spec builds the same files, which also makes refactoring provable: moving the engine's stylesheet into its own file left all 1,733 output files of the ten sample builds byte-identical.

## 3. One audience, one companion, one price (26 to 28 April 2026)

- **Context.** The product had grown a tier system, a payment flow, two companion personas and a seven-day trial.
- **Decision.** Websites for healers, therapists and event producers, made with one companion (Haven), on one subscription sliding from $39 to $19 a month. Two days later the trial clock went too: a site is built when the visitor and Haven agree it is clear, not when a timer runs out ([BRAND_VOICE.md](BRAND_VOICE.md), [CAS_LIVING_PLATFORM_TECHNICAL_ARCHITECTURE.md](CAS_LIVING_PLATFORM_TECHNICAL_ARCHITECTURE.md)).
- **Cost.** A narrower market, chosen on purpose, and a rewrite of the site's copy toward fewer, plainer words. The pre-Harbor tier system and its `/payment` flow were removed from the code.

## 4. A look made for the audience, not by default

- **Context.** The interface the agent produced looked like AI had made it, which is the wrong signal for a spa and wellness community.
- **Decision.** I redesigned the graphics and UI with Claude Design to my own taste for that community, and had real customers give feedback on it.
- **Cost.** Design time spent outside the agent's loop.

## 5. Audit everything, and let the system check itself

- **Context.** One person cannot read every line an agent writes.
- **Decision.** The agent reviewed, tested and audited its own work continuously: code, security, accessibility and UX ([docs/audits/](audits/) holds the accessibility audits of May 2026). Self-healing automation (the coordinator, the monitor and the ops agent) runs behind a permission table with dry run on by default, and the unattended roadmap executor ships disabled. Before publishing, I ran six adversarial review rounds with fresh-context reviewers, each followed by fixes whose tests were shown to fail first ([PRE_RELEASE_REVIEW.md](PRE_RELEASE_REVIEW.md)).
- **Cost.** The pre-release review found what production had been running with, among them a password-reset link shown to whoever asked for it and a companion prompt that claimed encryption and therapist-level confidentiality. Both were in the code while it served customers.

## 6. Automated review (June 2026)

- **Context.** The product promised a human review of every site before it went live.
- **Decision.** I added an automated review mode to get more done: by then I had learned to trust the agent's work. With `CAS_HARBOR_AUTO_REVIEW=true` a finished build waits as `ai_review_queued` for an automated review loop that ran outside this repository ([queue/worker_pg.py](../queue/worker_pg.py), `_initial_review_status`). The worker still never marks a build live.
- **Cost.** It departed from the promise the product made, and the repository does not record which sites took which path. The customer copy in this release was rewritten to claim only what the code enforces: a recorded review verdict before a site is marked live.
