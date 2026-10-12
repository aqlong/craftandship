# Craft & Ship Makefile
#
# Thin shim over developer-facing tooling. Venv-aware: prefers
# .venv/bin/python when it exists and falls back to system python3.
# Extend cautiously: the canonical developer surface stays the scripts in
# scripts/ so CI parity is trivial.

PYTHON := $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)

.PHONY: help hooks verify-hooks test test-web bless-golden

help:
	@echo "make hooks                       enable the repo's two git hooks, pre-commit and pre-push (core.hooksPath = .githooks)"
	@echo "make verify-hooks                check the two hooks are wired, executable, the only ones, and still refuse what they should (runs them in a throwaway repository)"
	@echo "make test                        Python tests in tests/ and compose/tests/ (needs: pip install -r requirements-dev.txt)"
	@echo "make test-web                    web unit tests (needs: cd web && npm ci)"
	@echo "make bless-golden SPEC=<name>    regenerate a golden fixture snapshot"

hooks:
	git config core.hooksPath .githooks
	@echo "core.hooksPath = $$(git config --get core.hooksPath)"

verify-hooks:
	bash scripts/verify_hooks.sh

test:
	$(PYTHON) -m pytest tests/ compose/tests -q

test-web:
	cd web && npm test

# Regenerate the pinned sha256 map for a golden fixture.
# Usage:  make bless-golden SPEC=medicine-waves-canonical
# See docs/internals/WIDGETS_AND_CONTRACTS.md for when to re-bless. The
# resulting commit MUST start with `bless:` so .githooks/pre-push allows the
# push.
bless-golden:
	@if [ -z "$(SPEC)" ]; then \
		echo "usage: make bless-golden SPEC=medicine-waves-canonical"; \
		exit 1; \
	fi
	@$(PYTHON) scripts/bless_golden.py $(SPEC)
	@echo ""
	@echo "Golden files regenerated for $(SPEC)."
	@echo "Review:  git diff tests/golden/$(SPEC)/"
	@echo "Commit:  git commit -m 'bless: $(SPEC) - <reason>'"
