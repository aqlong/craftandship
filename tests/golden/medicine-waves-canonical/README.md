# medicine-waves-canonical golden fixture

This directory is the regression baseline for the B+C pipeline rewrite. It
pins the canonical Medicine Waves spec so contract and golden tests can
compare pipeline output to a known-good reference.

Contents:

- `spec.json`: the canonical MW spec, run through `coordinator._finalize_spec`
  and verified against `cli.validate.validate_spec`.
- `artifact.snapshot.json` - `{filename: sha256hex}` map pinning every file
  the build-phase pipeline produces from `spec.json` (contract gate is
  excluded; see `scripts/bless_golden.py` docstring). Regenerated via
  `make bless-golden SPEC=medicine-waves-canonical`.

Field-type caveat: `spec.json` uses only the field types valid in today's
`cli/validate.py::VALID_FIELD_TYPES` (`text`, `integer`, `boolean`,
`timestamp`, `email`, `password`). Once section-02-foundation extends the
validator with richer types (`image_url`, `foreign_key`, etc.) the spec will
be re-blessed to use them directly instead of stringly-typed workarounds.

Re-blessing workflow:

1. `make bless-golden SPEC=medicine-waves-canonical`
2. Review `git diff tests/golden/medicine-waves-canonical/`: every drift
   must be intentional and explained in the commit message.
3. Commit with a message starting `bless:` (e.g.
   `bless: medicine-waves-canonical - add image-crop widget`).

`bless:` prefix rule: any commit that modifies a file in this directory
MUST start with `bless:`. `.githooks/pre-push` enforces this; the only
sanctioned bypass is `CAS_PROMOTE=1` for docs/CI/hooks/tests-only pushes.

See `docs/plans/b-plus-c/claude-plan.md` §8 and
`docs/internals/WIDGETS_AND_CONTRACTS.md` for broader context.
