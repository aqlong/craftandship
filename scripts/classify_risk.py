#!/usr/bin/env python3
"""Risk classification for executor tiered governance.

Reads file paths from stdin (one per line) and prints "high" or "low".

Narrowed to specific high-blast-radius files rather than full directories
to avoid classifying ~90% of executor items as high-risk.

Usage (from shell):
    echo "$changed_files" | python3 scripts/classify_risk.py
"""
import sys

HIGH_RISK_PATHS = [
    # Core auth/session (always high-risk)
    "web/src/middleware/auth.ts",
    "web/src/routes/auth.ts",
    "web/src/db/schema.sql",
    "web/src/app.ts",
    "web/src/server.ts",
    # Pipeline (customer-build-affecting)
    "cli/build.py",
    "cli/deploy.py",
    "cli/fix.py",
    "cli/deploy_fix.py",
    "queue/worker_pg.py",
    # Infrastructure
    "Dockerfile",
    "Dockerfile.worker",
    "railway.toml",
    "web/railway.toml",
    "package.json",
    "web/package.json",
    "requirements.worker.txt",
]


def classify(changed_files: list[str]) -> str:
    """Classify risk level based on changed file paths."""
    if len(changed_files) > 10:
        return "high"
    for f in changed_files:
        for p in HIGH_RISK_PATHS:
            if f == p or f.startswith(p):
                return "high"
    return "low"


def main():
    files = [line.strip() for line in sys.stdin if line.strip()]
    print(classify(files))


if __name__ == "__main__":
    main()
