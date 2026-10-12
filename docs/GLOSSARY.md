# Glossary

| Term | Meaning |
|---|---|
| Harbor | The subscription product surface: conversation, preview, review, publish. Enabled by `CAS_HARBOR_ENABLED`. |
| Haven | The companion persona a visitor talks to. |
| keeper | A customer. A `trial_keeper` is an anonymous visitor identified by a cookie. |
| lighthouse, table, lamp, recall | Harbor's routes: `/lighthouse/table` is the conversation, `/lighthouse/lamp/:buildId` is a build's status and publish page, `/lighthouse/table/recall` shows what the companion remembers. |
| spec | The structured JSON description of a site that the engines build from. |
| archetype | A kind of business (healer, restaurant, author and so on) with default pages, models and layouts in the v2 engine. |
| v1, v2 | The two build engines: [cli/](../cli/README.md) (model-written, legacy) and [compose/](../compose/build.py) (deterministic, default). `CAS_BUILD_ENGINE` selects. |
| SE review | The review a build passes before it can be marked live: a recorded verdict on the build row. A person by design; an automated mode (`CAS_HARBOR_AUTO_REVIEW`) was added in June 2026. |
| Senior Web Craftsman | The title the product's pages give its reviewer, who was the founder. It is the product's marketing voice, not a credential. |
| MW | Medicine Waves, the author's event brand, used as the canonical test site. |
| coordinator, executor | Budget-gated operational automation ([coordinator/](../coordinator/tick.py)) and the autonomous roadmap loop ([scripts/cas-roadmap-loop.sh](../scripts/cas-roadmap-loop.sh)). |
