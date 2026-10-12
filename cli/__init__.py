# cli/: v1 (legacy) Claude-per-build pipeline. Frozen; see cli/README.md.
#
# DEPRECATION NOTE (2026-04-27):
# v1 is the legacy build engine. v2 (compose/) is the production default
# as of 2026-04-22 and the assumed pipeline as of 2026-04-27. This
# directory is preserved as a reference + opt-in fallback for forensics
# of pre-2026-04-22 production builds.
#
# To run a build through v1 (e.g. to repro a v1-era failure):
#     CAS_BUILD_ENGINE=v1 python3 -m queue.worker_pg
#
# Active surface (imported by live v2 / worker / coordinator code, NOT just
# the v1 fix loops -- do NOT treat these as v1-only deletable):
# - cli/deploy.py       : deploy(), verify_health(), verify_render(),
#                         wait_for_deployment() and the Railway GraphQL
#                         helpers, used by the worker for both engines;
#                         monitor/backup.py imports redact_url
# - cli/api_client.py / cli/cancellation.py : worker-side helpers
# - cli/failure_kb.py   : imported by the coordinator (actuators,
#                         build_failure_categorizer, work_detection,
#                         failure_kb_sync, backend_engineer) and by
#                         scripts/prune_failure_kb*.py
# - cli/build_trace.py  : imported by the coordinator reconciliation agent
# - cli/stock_photos/   : data; read by compose/stock_photos.py
#
# Imported by dev/ops scripts (not the build path, but still NOT deletable):
# - cli/pipeline.py     : scripts/bless_golden.py (golden-file blessing)
# - cli/visual_diff.py  : scripts/promote_staging.sh
#
# Deprecated v1-only modules (do NOT import from new code):
# - cli/build.py        : Claude-per-build code emitter
# - cli/test.py / ui_test.py : Playwright smoke and UI tests of the generated
#                         app (queue/worker_pg.py imports test.py only on
#                         the v1 branch)
# - cli/fix.py / qa_fix.py / deploy_fix.py / design_fix.py : Claude fix loops
# - cli/qa.py / review.py / design_review.py : Claude QA and review loops
# - cli/intake.py / validate.py / design_data.py : v1 intake, spec schema
#                         and design enrichment
# - cli/contracts/      : v1 BS4 contract assertions
# - cli/widgets/        : v1 widget emitters (superseded by compose/widgets/)
#
# Tests for these modules carry `pytestmark = pytest.mark.v1_only` and
# are skipped by default in pytest collection (see the root conftest.py).
# Set CAS_BUILD_ENGINE=v1 to run them explicitly.
