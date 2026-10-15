# Documentation index

Start with [ARCHITECTURE.md](../ARCHITECTURE.md), which describes the code as it stands, and [SECURITY.md](../SECURITY.md), which says what "preview", "review" and "live" mean in this system. Almost everything in this folder is a dated snapshot written while the system was running (March to July 2026) and not kept current afterwards. Where a file here disagrees with the code, the code is right.

The numeric prefixes on a few files (`02_`, `06_`, `08_`, `15_`) come from an earlier numbered series; the gaps are documents that are not part of this repository.

## Review

- [DECISIONS.md](DECISIONS.md): the calls the author made as the agent's director, with what each cost.
- [WHAT_RAN_AND_WHAT_CHANGED.md](WHAT_RAN_AND_WHAT_CHANGED.md): the engineering highlights, split into what ran in production and what the pre-release review added, and what the guarantees do not cover.
- [PRE_RELEASE_REVIEW.md](PRE_RELEASE_REVIEW.md): how the code was reviewed before this publication, what was found and changed, and what was not verified.
- [THREAT_MODEL.md](THREAT_MODEL.md): what the code defends against, control by control, with the file and the test for each, and the switches that weaken it.
- [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md): what it does not defend against, grouped by area.
- [GLOSSARY.md](GLOSSARY.md): the product's vocabulary (Harbor, Haven, keeper, lamp, spec, archetype).

## Architecture and engines

- [02_TECH_ARCHITECTURE.md](02_TECH_ARCHITECTURE.md): the original system design (pipeline stages, APIs, data flow) of the pre-Harbor multi-tier product; written in March 2026 and bannered as historical.
- [internals/pipeline.md](internals/pipeline.md): per-module notes on `cli/`, the v1 engine (April 2026); [cli/README.md](../cli/README.md) holds the current measured facts.
- [internals/post-processors.md](internals/post-processors.md): catalog of the v1 post-processors; states the registry size (117) and how to count it.
- [internals/POST_PROCESSOR_ORDERING.md](internals/POST_PROCESSOR_ORDERING.md): the ordering invariants of the v1 post-processor DAG.
- [internals/WIDGETS_AND_CONTRACTS.md](internals/WIDGETS_AND_CONTRACTS.md): the v1 admin-form widgets, output contracts and golden fixtures.
- [internals/CAS_V2_STATIC_COMPOSE_PLAN.md](internals/CAS_V2_STATIC_COMPOSE_PLAN.md): the plan and decision of 2026-04-20 to remove the model from the build path; the layout it proposes was not built as written.
- [internals/CAS_V2_OPTION_A_PLAN.md](internals/CAS_V2_OPTION_A_PLAN.md): the 2026-04-22 decision that v2 emits what the customer's spec declares; a planning document.
- [internals/CAS_V2_TEMPLATE_FAMILIES_PLAN.md](internals/CAS_V2_TEMPLATE_FAMILIES_PLAN.md): the 2026-04-28 plan for template families.
- [internals/V2_ENGINE_BEHAVIOR.md](internals/V2_ENGINE_BEHAVIOR.md): worker environment variables and how the worker integrates the v2 engine, migrated out of `CLAUDE.md` in July 2026.
- [internals/SERVICE_PREFILL_PATTERN.md](internals/SERVICE_PREFILL_PATTERN.md): the `?service=` contact-form prefill contract in v2 output.
- [internals/worker.md](internals/worker.md): notes on the build queue worker (April 2026, with later corrections).
- [internals/coordinator.md](internals/coordinator.md): notes on the coordinator agents, scoring and budget (April 2026, with later corrections).
- [MASTER_COORDINATOR_SPEC.md](MASTER_COORDINATOR_SPEC.md): the coordinator specification, written before it was implemented; its banner lists what was never built.
- [internals/monitor-ops-scripts.md](internals/monitor-ops-scripts.md): notes on the monitor, the legacy ops agent, the scripts and the roadmap executor.
- [internals/design-decisions.md](internals/design-decisions.md): secondary design decisions extracted from `CLAUDE.md` in July 2026.

## Harbor (companion and subscription product)

- [CAS_HARBOR_PHASE_1_TECH_PLAN.md](CAS_HARBOR_PHASE_1_TECH_PLAN.md): the Phase 1 work order (Haven runtime, memory, rails), first drafted 2026-04-26 with corrections at the top.
- [CAS_HARBOR_PHASE_2_TECH_PLAN.md](CAS_HARBOR_PHASE_2_TECH_PLAN.md): the Phase 2 plan (promotion, billing, build handoff); a pivot note of 2026-04-28 supersedes parts of the body.
- [CAS_LIVING_PLATFORM_TECHNICAL_ARCHITECTURE.md](CAS_LIVING_PLATFORM_TECHNICAL_ARCHITECTURE.md): the April 2026 design draft behind Harbor; its banner lists what was built differently or never built.
- [internals/HARBOR_IMPLEMENTATION_NOTES.md](internals/HARBOR_IMPLEMENTATION_NOTES.md): implementation and changelog narratives migrated out of `CLAUDE.md` on 2026-07-03; facts are accurate as of their inline dates.
- [plans/se-review-gate-spec.md](plans/se-review-gate-spec.md): the brief for the senior-engineer review gate; the shipped design is in [ARCHITECTURE.md](../ARCHITECTURE.md) and `CLAUDE.md`.
- [08_EMAIL_SYSTEM.md](08_EMAIL_SYSTEM.md): the transactional email design, which sender fires from which route, and the exceptions that do not use `sendEmail()`.
- [BRAND_VOICE.md](BRAND_VOICE.md): the customer-facing voice rules; the pre-commit lint and several tests enforce the hard rules.

## Operations

- [RAILWAY_DISASTER_RECOVERY.md](RAILWAY_DISASTER_RECOVERY.md): the recovery playbook written on 2026-04-09 after production was deleted twice in one day, and the guardrails added afterwards.
- [FAILOVER_RUNBOOK.md](FAILOVER_RUNBOOK.md): procedures for infrastructure outages; the repository holds no record of the Render and Fly.io options being used.
- [LOCAL_EXECUTOR.md](LOCAL_EXECUTOR.md): the local roadmap executor (April to May 2026); the script ships disabled.
- [15_PROD_BUILD_PATTERNS.md](15_PROD_BUILD_PATTERNS.md): a pattern library of recurring production build failures (April 2026).

## Testing and quality

- [06_TESTING.md](06_TESTING.md): the web test strategy, the CI job list and the prerequisites of the Python tests.
- [APP_QUALITY_CHECKLIST.md](APP_QUALITY_CHECKLIST.md): the quality bar for generated apps, derived from a reference build of 2026-04-07.
- [MW_TEST_CHECKLIST.md](MW_TEST_CHECKLIST.md): the regression checklist for the "MW" reference event-site build, last major update 2026-04-17.
- [internals/MW_BUG_RECURRENCE_RCA.md](internals/MW_BUG_RECURRENCE_RCA.md): root-cause analysis of the bugs that recurred across v1 builds of the MW site.
- [IMPECCABLE_DESIGN_SYSTEM.md](IMPECCABLE_DESIGN_SYSTEM.md): design rules for generated apps, with an attribution note; see [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Static reviews and one audit

Dated reports. Where later work changed a result, the report says so in a status note or inline. The four accessibility reports are static reviews of markup: a script or a reader went through the templates, and no live browser or assistive-technology pass (screen reader, keyboard walk in a browser, axe in a running page) was run for any of them. "Lighthouse" in the first file's name is the product's own `/lighthouse` route (the Harbor companion surface), not Google Lighthouse.

- [audits/lighthouse-a11y-2026-05-27.md](audits/lighthouse-a11y-2026-05-27.md): static contrast review of the markup of `/lighthouse/*` (a scanner that computes ratios from the colors in the views); groups A and D were fixed afterwards, groups B and C were not re-verified and should be treated as open.
- [audits/keyboard-tab-order-2026-05-27.md](audits/keyboard-tab-order-2026-05-27.md): static enumeration of the tab order of the keeper flow's views; all four anti-patterns it names are marked resolved on 2026-05-28, and the live Playwright walk was not run.
- [audits/screen-reader-conversation-2026-05-27.md](audits/screen-reader-conversation-2026-05-27.md): static read of the chat template's markup against four screen-reader contracts (not a screen-reader walk); findings SR1, SR4, SR5 and SR6 were fixed afterwards.
- [audits/compose-archetype-a11y-2026-05-27.md](audits/compose-archetype-a11y-2026-05-27.md): static scan of v2 archetype output; the static findings are closed and the live browser pass was never run.
- [audits/prompt-caching-2026-08-06.md](audits/prompt-caching-2026-08-06.md): a measured audit (token counts per stage and a query of stored build records) of the prompt-caching sites in the build pipeline; findings only, all open.

## Planning examples

- [plans/b-plus-c/claude-plan.md](plans/b-plus-c/claude-plan.md): the plan for the v1 post-processor DAG, widget and contract rewrite; cli/ code and tests cite its sections (see "Plan section labels" in [cli/README.md](../cli/README.md)). Its per-section files are not included.

## Diagrams

- [internals/CAS_V1_PIPELINE_VISUAL.md](internals/CAS_V1_PIPELINE_VISUAL.md): visual reference for the v1 pipeline; the rendered diagram is [cas_v1_pipeline.html](internals/cas_v1_pipeline.html) ([png](internals/cas_v1_pipeline.png)).
- [internals/CAS_V2_ARCHITECTURE_VISUAL.md](internals/CAS_V2_ARCHITECTURE_VISUAL.md): the initial proposal for the v2 architecture, with Mermaid diagrams; the rendered diagram is [cas_v2_architecture.html](internals/cas_v2_architecture.html) ([png](internals/cas_v2_architecture.png)), titled as a May 2026 snapshot.
- [images/](images/): screenshots of the web app running locally. The root [README.md](../README.md) embeds the home page and the admin build list; [companion.png](images/companion.png) is the companion page.
