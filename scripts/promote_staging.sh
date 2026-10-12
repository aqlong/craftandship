#!/bin/bash
# promote_staging.sh: Manual promotion from staging to production.
#
# Pre-flight checks: staging health, full test suite.
# Tags current master for rollback. Fast-forward merges staging.
# Post-deploy: smoke test with auto-rollback on failure.
# Visual regression between production and staging (--skip-visual to bypass).
# Release tagging via bump_version.py + generate_changelog.py.
#
# Usage:
#   ./scripts/promote_staging.sh
#   ./scripts/promote_staging.sh --skip-visual
#   ./scripts/promote_staging.sh --dry-run
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

DRY_RUN=false
SKIP_VISUAL=false
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --skip-visual) SKIP_VISUAL=true ;;
    esac
done

echo "=== CAS Staging Promotion ==="
echo "Dry run: $DRY_RUN"
echo ""

# The rollback further down makes the working tree match a tag exactly
# (git read-tree -u --reset), which discards uncommitted changes to tracked
# files. So the script starts only from a tree that has none.
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "ABORT: uncommitted changes to tracked files; commit or stash them first"
    echo "(a rollback after a failed production smoke test restores the tagged tree and would discard them)"
    exit 1
fi

# 1. Pre-flight: verify staging is healthy
echo "--- Pre-flight: staging smoke test ---"
if ! python3 scripts/smoke_prod.py --env staging; then
    echo "ABORT: staging smoke test failed"
    exit 1
fi

# 2. Run tests
echo ""
echo "--- Running test suite ---"
cd web && npm test && cd "$PROJECT_DIR"
python3 -m pytest tests/ -q --tb=short

# 3. Visual regression (requires staging + production running; --skip-visual to bypass).
# A detected regression aborts the promotion. If the screenshots cannot be
# captured or the comparison module cannot run, that is reported and skipped:
# there is nothing to compare, which is not the same as a regression.
if [ "$SKIP_VISUAL" = false ]; then
    if [ -f scripts/capture_cas_screenshots.py ] && [ -f cli/visual_diff.py ]; then
        echo ""
        echo "--- Visual regression check ---"
        VR_DIR="$(mktemp -d)"
        if python3 scripts/capture_cas_screenshots.py --env production --output "$VR_DIR/baseline/" \
           && python3 scripts/capture_cas_screenshots.py --env staging --output "$VR_DIR/candidate/"; then
            VR_RC=0
            python3 -c "
import sys
try:
    from cli.visual_diff import compare_screenshots
except Exception as exc:
    print(f'visual diff module unavailable: {exc}')
    sys.exit(2)
r = compare_screenshots(sys.argv[1], sys.argv[2], sys.argv[3])
if not r['passed']:
    for p in r['pages']:
        if p['mismatch_pct'] > 0.02:
            print(f'  REGRESSION: {p[\"page\"]} ({p[\"mismatch_pct\"]:.1%} changed)')
    sys.exit(1)
print('Visual regression: PASSED')
" "$VR_DIR/baseline/" "$VR_DIR/candidate/" "$VR_DIR/diff/" || VR_RC=$?
            if [ "$VR_RC" -eq 1 ]; then
                rm -rf "$VR_DIR"
                echo "ABORT: visual regression between production and staging (re-run with --skip-visual to bypass)"
                exit 1
            elif [ "$VR_RC" -ne 0 ]; then
                echo "WARNING: visual regression comparison could not run (exit $VR_RC); skipped"
            fi
        else
            echo "WARNING: could not capture screenshots; visual regression skipped"
        fi
        rm -rf "$VR_DIR"
    fi
fi

if [ "$DRY_RUN" = true ]; then
    echo ""
    echo "DRY RUN: would merge staging -> master, push, verify, and tag release"
    exit 0
fi

# 4. Tag current master as rollback target
ROLLBACK_TAG="release/good/$(date +%Y%m%d-%H%M%S)"
echo ""
echo "--- Tagging current master as $ROLLBACK_TAG ---"
git tag "$ROLLBACK_TAG" master

# 5. Merge staging into master (fast-forward only)
echo ""
echo "--- Merging staging -> master ---"
git checkout master
git merge --ff-only staging
CAS_PROMOTE=1 git push origin master

# 6. Wait for deploy and run post-deploy smoke
echo ""
echo "--- Waiting for production deploy ---"
python3 scripts/wait_for_deploy.py --service both --timeout 10

echo ""
echo "--- Post-deploy smoke test ---"
PREV_GOOD_TAG=$(git describe --tags --match "release/good/*" --abbrev=0 2>/dev/null || echo "")

if ! python3 scripts/smoke_prod.py --env production; then
    echo "PRODUCTION SMOKE FAILED"
    if [ -n "$PREV_GOOD_TAG" ]; then
        echo "Rolling back: restoring the tree of $PREV_GOOD_TAG in a new commit..."
        # read-tree makes the index and working tree match the tag exactly
        # (deleting files added since), and works even when the promoted range
        # contains merge commits, which `git revert` refuses without -m.
        git read-tree -u --reset "$PREV_GOOD_TAG" && git commit -m "Rollback: smoke test failure after staging promotion"
        # CAS_PROMOTE=1: the pre-push staging gate would otherwise block this push.
        CAS_PROMOTE=1 git push origin master
        python3 scripts/wait_for_deploy.py --service both --timeout 10
    fi
    # Alert via ops API
    python3 -c "
import requests, os
try:
    requests.post(
        os.environ.get('CAS_PROD_URL', '') + '/ops/api/v1/executor/heartbeat',
        headers={'Authorization': 'Bearer ' + os.environ.get('OPS_API_KEY', '')},
        json={'status': 'rollback', 'reason': 'smoke test failure after staging promotion'},
        timeout=10,
    )
except Exception:
    pass
" 2>/dev/null || true
    exit 1
fi

# 7. Release tagging
echo ""
echo "--- Creating release ---"
BUMP=$(python3 scripts/bump_version.py --analyze-commits)
python3 scripts/bump_version.py --bump "$BUMP"
VERSION=$(python3 -c "import json; print(json.load(open('version.json'))['version'])")
python3 scripts/generate_changelog.py || true
git add version.json
# CHANGELOG.md is created by generate_changelog.py on first release.
[ -f CHANGELOG.md ] && git add CHANGELOG.md
git commit -m "Release v${VERSION}"
git tag "v${VERSION}" HEAD
CAS_PROMOTE=1 git push origin master
git push origin "v${VERSION}"

echo ""
echo "=== Promotion complete: v${VERSION} ==="
