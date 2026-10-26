# Prompt caching audit, 2026-08-06

> Historical document from August 2026; the system is no longer developed and the site was taken offline in October 2026. "Production" below means the stored build records of the platform database that the audit read.

Status: findings only. No code changes accompanied this audit; treat all four findings below as **open**.

Scope: every `cache_control` site in the CAS build pipeline, plus the cost/cache
telemetry that would let us verify hit rates.

## Summary

Only 1 of the 8 `cache_control` markers in `cli/` actually caches. The other 7
sit on system prompts that are below the minimum cacheable prefix for the model
that stage runs on, so the API silently declines to cache them. There is no
error and no warning; `cache_creation_input_tokens` just comes back 0.

Separately, the pipeline's cost aggregation is recording zero metrics, so the
cache hit rate cannot be measured from the stored build records. That is why
seven inert breakpoints went unnoticed.

## Finding 1: seven inert `cache_control` markers

The minimum cacheable prefix is per-model and is **not monotonic** across
generations: Opus 4.6 requires 4096 tokens, while the newer Opus 4.8 requires
1024 and Opus 5 requires 512. A prefix under the minimum caches nothing.

Measured with `client.messages.count_tokens()` against the model each stage
actually uses (not estimated from character counts):

| File | Line | Model | System tokens | Model minimum | Caches |
|---|---:|---|---:|---:|---|
| `cli/build.py` | 1229, 11219 | opus-4-6 | 16,436 | 4096 | yes |
| `cli/intake.py` | 245 | opus-4-6 | 2,454 | 4096 | no |
| `cli/qa.py` | 146 | haiku-4-5 | 1,745 | 4096 | no |
| `cli/design_review.py` | 292 | opus-4-6 | 667 | 4096 | no |
| `cli/qa_fix.py` | 208 | opus-4-6 | 530 | 4096 | no |
| `cli/design_fix.py` | 285 | opus-4-6 | 485 | 4096 | no |
| `cli/fix.py` | 203 | opus-4-6 | 477 | 4096 | no |
| `cli/review.py` | 224 | sonnet-4-6 | 216 | 1024 | no |

Note `cli/qa.py` runs on haiku-4-5, which has no newer revision with a lower
minimum. At 1,745 tokens it cannot be made cacheable by a model upgrade.

`cli/review.py` at 216 tokens is below every current model's minimum, including
Opus 5's 512. Its marker can never do anything.

## Finding 2: the breakpoint is on the wrong content in the fix loops

This is the larger miss, and it would still be a miss even if Finding 1 were
resolved.

In `cli/fix.py:attempt_fix()` the repeated payload is the **user** message, not
the system prompt. Each iteration rebuilds:

- the full source-file dump (`_format_source_files`, `cli/fix.py:330`)
- the spec JSON, truncated to 5000 chars
- the route manifest

and appends the test-failure text plus KB hints. Only the trailing failure
block changes between iterations. The source files, which dominate the token
count, are identical and are re-sent uncached every time.

`cli/qa_fix.py` and `cli/design_fix.py` follow the same shape.

The correct placement is a breakpoint at the end of the stable prefix, after the
source-files block and before the volatile failure text, so successive fix
iterations read the prefix instead of rewriting it. Ordering matters: caching is
a prefix match, so anything volatile must come after the last breakpoint.

## Finding 3: TTL is 5 minutes everywhere

Every marker uses the implicit default (`{"type": "ephemeral"}` with no `ttl`).
Build stages separated by more than five minutes pay a cold cache write each
time. Worth evaluating `{"type": "ephemeral", "ttl": "1h"}` on the build system
prompt specifically, though note the write premium rises from 1.25x to 2x, so
it needs at least three reads to pay off rather than two.

## Finding 4: cache and cost telemetry records nothing

`cli/api_client.py` computes `cache_read_tokens` and `cache_write_tokens`
correctly and `estimate_cost()` prices them at the right multipliers (0.1x read,
1.25x write). The problem is downstream.

At `queue/worker_pg.py:3686` the aggregation block runs but `all_metrics` is
empty, so:

- `total_api_cost_usd` is written as `0.0000`
- `if all_metrics:` is false, so no `api_costs` row is ever appended

Verified against production: every build created after the cost-aggregation
change (2026-03-30) has `total_api_cost_usd = 0.0000`, including
builds that reached `live` status. Those same builds have 29 to 235 `build_logs`
rows each, so logging in general is working; there are zero rows with
`stage = 'api_costs'`.

Consequences beyond caching: the build cost figures on `/admin/costs` are
reporting `$0` for tracked builds, and per-tier and per-build cost averages are
meaningless.

This should be fixed first. Without it, no change to the caching setup can be
verified, and the same class of silent regression can recur.

## Suggested order of work

Not filed to the autonomous executor's roadmap deliberately: the executor ships
roadmap items without further review, and these are findings pending review, not
approved work.

1. Repair the metrics collector (`queue/worker_pg.py:3686` and the
   `_pipeline_metrics` handoff). Prerequisite for verifying anything else, and
   independently fixes the $0 cost dashboard.
2. Move the fix / qa_fix / design_fix breakpoint onto the source-files block in
   the user message. Largest real token win.
3. Upgrade the opus-4-6 stages. At the 1024 minimum (Opus 4.8) `intake` becomes
   cacheable; at 512 (Opus 5) `qa_fix` and `design_review` also clear it, with
   no prompt changes required.
4. Remove the markers from `cli/review.py` and `cli/fix.py`, or accept they are
   decorative. Both are below every current model's minimum.
5. Add a regression guard: assert `cache_read_input_tokens > 0` on the second
   call of a repeated-prefix stage, so an inert breakpoint fails a test instead
   of silently costing money.

## Verification notes

- Token counts came from `messages.count_tokens()` per stage against that
  stage's own model, not from character-count estimates.
- File and line references are to the files as they were when the audit was
  written; the files have been edited since, so the same markers and the
  aggregation block now sit on other lines.
- Production figures came from a read-only query of the stored build records
  (`builds` and `build_logs`). The audit reports what those records contain and
  makes no claim about when the pipeline was or was not running.
