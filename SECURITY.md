# Security

Craft & Ship ran in production from March to July 2026 and was then wound down. This repository is the archived codebase of that system. Nobody operates it, no instance of it is maintained, and there is no bug bounty and no response-time commitment.

This file has the status of the repository, how to report a problem, and the definitions of three product words that every other security statement depends on. The detail is in two documents:

- [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md): what the code defends against, control by control, with the file and the test for each, and the switches that weaken it.
- [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md): what it does not defend against, grouped by area. It is long.

Where a document and the code disagree, the code is right; please report the difference. [ARCHITECTURE.md](ARCHITECTURE.md) describes how the system fits together, and [docs/PRE_RELEASE_REVIEW.md](docs/PRE_RELEASE_REVIEW.md) describes how the code was reviewed before publication and what that review did not verify.

Two names that can mislead: `/lighthouse` and `lighthouse.ts` are the product's own route and file names for the Harbor companion pages, not Google Lighthouse, and the coordinator's "agents" are mostly rule-based checks (three call a model; see [ARCHITECTURE.md](ARCHITECTURE.md#coordinator-coordinator)). [docs/GLOSSARY.md](docs/GLOSSARY.md) lists the other product words.

## Status and reporting

- To report a vulnerability in this code, use GitHub's private vulnerability reporting: the **Security** tab of the repository, then **Report a vulnerability**. A finding that is not sensitive (a documentation error, a hardening suggestion, a dependency advisory) can be opened as an ordinary issue. [CONTRIBUTING.md](CONTRIBUTING.md) points here.
- The repository is archived. Depending on its settings at the time, GitHub may not accept new reports or issues. If neither route is open there is no other channel, because nobody is operating this software.
- Anyone who deploys this code is responsible for their own review. The limitations in [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md) are real, several of them are deliberate trade-offs for a single-instance deployment with one operator who was also the reviewer, and they do not carry over unchanged to a different deployment.
- The older audit documents under [docs/](docs/README.md) are dated snapshots. This file and the two documents it points to reflect the code as published.

## Preview, review and live

Three product words name less than they suggest. Every control in [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) should be read with these in mind. This section is the one place they are defined; the other documents point here.

- **A preview is not private.** The preview address is a public Railway domain (`generate_domain` in [cli/deploy.py](cli/deploy.py)). The generated site is the same code before and after approval: it serves `/robots.txt` with `Allow: /` and a `/sitemap.xml` (`emit_routes_seo_js` in [compose/build.py](compose/build.py)), marks its public pages `index, follow` ([layout-public.ejs](compose/templates/layout-public.ejs)), asks for no credentials on them and reads no setting that tells a preview from a live site. When a deployment has answered `/health` and the build row is at `preview`, the worker emails the customer the address and a login link for the site's admin, before any review (`_announce_preview` in [worker_pg.py](queue/worker_pg.py), `send_preview_ready` in [mailer.py](queue/mailer.py)).
- **Live is a status.** Publishing is `UPDATE builds SET status = 'live', live_url = preview_url` in the platform's database. Nothing changes on the deployed site, and nothing in the repository takes a site down because it was not approved.
- **Review was a person by design, and not only a person in practice.** The gate is a verdict an admin records at `/admin/reviews`. An automated review mode was added in June 2026 (`CAS_HARBOR_AUTO_REVIEW`, the `ai_review_queued` state). The loop that consumed it ran outside this repository and is not included, and the repository does not record which production builds took which path.
- **Whether a build needs a subscription is a column on the build.** `builds.is_harbor` is set when a Harbor conversation dispatches a build (`insertHarborBuild` in [harbor_dispatch.ts](web/src/services/harbor_dispatch.ts)) and is copied to every build made from it by a trigger (`builds_inherit_is_harbor` in [schema.sql](web/src/db/schema.sql)). The owner's publish and the owner's source download read that column and no other build row, so deleting an ancestor build, which the worker's janitor and the admin and ops routes can do, does not change the answer.
