#!/usr/bin/env python3
"""Generate changelog entries from git commits since last tag.

Reads existing CHANGELOG.md (if any) and prepends new release section at top.
The repository does not ship a CHANGELOG.md; the first non-dry run creates it.
Groups commits by type: features, fixes, executor/auto items, other.

Usage:
    python3 scripts/generate_changelog.py
    python3 scripts/generate_changelog.py --dry-run
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CHANGELOG_FILE = Path(__file__).parent.parent / "CHANGELOG.md"
VERSION_FILE = Path(__file__).parent.parent / "version.json"


def get_version() -> str:
    try:
        data = json.loads(VERSION_FILE.read_text())
        return data["version"]
    except Exception:
        return "unreleased"


def get_commits_since_last_tag() -> list[dict]:
    """Get commits since last version tag."""
    try:
        last_tag = subprocess.check_output(
            ["git", "describe", "--tags", "--abbrev=0", "--match", "v*"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        range_spec = f"{last_tag}..HEAD"
    except subprocess.CalledProcessError:
        range_spec = "HEAD~50..HEAD"

    try:
        log = subprocess.check_output(
            ["git", "log", range_spec, "--pretty=%H|%s|%an"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except subprocess.CalledProcessError:
        return []

    commits = []
    for line in log.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|", 2)
        if len(parts) >= 2:
            commits.append({
                "hash": parts[0][:7],
                "message": parts[1],
                "author": parts[2] if len(parts) > 2 else "",
            })
    return commits


def classify_commit(message: str) -> str:
    lower = message.lower()
    if lower.startswith("feat:") or lower.startswith("feat("):
        return "features"
    if lower.startswith("fix:") or lower.startswith("fix("):
        return "fixes"
    if "[auto]" in lower or "merge cas-" in lower or "mark " in lower and "completed" in lower:
        return "auto"
    return "other"


def generate_section(version: str, commits: list[dict]) -> str:
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    groups = {"features": [], "fixes": [], "auto": [], "other": []}

    for c in commits:
        category = classify_commit(c["message"])
        groups[category].append(c)

    lines = [f"## [{version}] - {date}", ""]

    if groups["features"]:
        lines.append("### Features")
        for c in groups["features"]:
            lines.append(f"- {c['message']} ({c['hash']})")
        lines.append("")

    if groups["fixes"]:
        lines.append("### Fixes")
        for c in groups["fixes"]:
            lines.append(f"- {c['message']} ({c['hash']})")
        lines.append("")

    if groups["auto"]:
        lines.append("### Automated")
        for c in groups["auto"]:
            lines.append(f"- {c['message']} ({c['hash']})")
        lines.append("")

    if groups["other"]:
        lines.append("### Other")
        for c in groups["other"]:
            lines.append(f"- {c['message']} ({c['hash']})")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Generate CHANGELOG.md entries")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    version = get_version()
    commits = get_commits_since_last_tag()

    if not commits:
        print("No commits found since last tag")
        return

    new_section = generate_section(version, commits)

    if args.dry_run:
        print(new_section)
        return

    # Prepend to existing changelog (newest first)
    existing = ""
    if CHANGELOG_FILE.exists():
        existing = CHANGELOG_FILE.read_text()

    header = "# Changelog\n\n"
    if existing.startswith("# Changelog"):
        # Strip existing header to avoid duplication
        existing = existing[existing.index("\n") + 1:].lstrip("\n")

    CHANGELOG_FILE.write_text(header + new_section + "\n" + existing)
    print(f"Updated CHANGELOG.md with v{version} ({len(commits)} commits)")


if __name__ == "__main__":
    main()
