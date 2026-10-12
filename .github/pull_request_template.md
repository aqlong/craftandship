# Pull request

## Summary

<!-- What does this change and why? Link to the roadmap item or issue if applicable. -->

## Test plan

<!-- Bulleted markdown checklist of how this was verified. Include test suite(s) run. -->

- [ ] `pytest tests/ -q` passes locally
- [ ] `cd web && npm test` passes locally
- [ ] End-to-end smoke verified where applicable

## Golden-file changes

<!-- Required section per docs/internals/WIDGETS_AND_CONTRACTS.md. -->

- [ ] This PR does NOT touch `tests/golden/`.
- [ ] This PR touches `tests/golden/` and the design change was approved.
      The commit message starts with `bless:` so `.githooks/pre-push` accepts
      the push. Updated snapshots were produced by
      `make bless-golden SPEC=<name>`.

## Follow-ups

<!-- Optional. Out-of-scope improvements surfaced by this change. -->
