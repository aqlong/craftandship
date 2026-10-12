#!/usr/bin/env python3
"""Semantic version bumping for CAS.

Usage:
    python3 scripts/bump_version.py --bump patch|minor|major
    python3 scripts/bump_version.py --analyze-commits   # prints recommended bump type
    python3 scripts/bump_version.py --dry-run --bump patch
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

VERSION_FILE = Path(__file__).parent.parent / "version.json"


def read_version() -> str:
    data = json.loads(VERSION_FILE.read_text())
    return data["version"]


def write_version(version: str) -> None:
    data = {
        "version": version,
        "released_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    VERSION_FILE.write_text(json.dumps(data, indent=2) + "\n")


def bump(current: str, bump_type: str) -> str:
    parts = current.split(".")
    major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
    if bump_type == "major":
        return f"{major + 1}.0.0"
    elif bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    else:
        return f"{major}.{minor}.{patch + 1}"


def analyze_commits() -> str:
    """Analyze commits since last tag to recommend bump type."""
    try:
        last_tag = subprocess.check_output(
            ["git", "describe", "--tags", "--abbrev=0", "--match", "v*"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        range_spec = f"{last_tag}..HEAD"
    except subprocess.CalledProcessError:
        range_spec = "HEAD~20..HEAD"

    try:
        log = subprocess.check_output(
            ["git", "log", range_spec, "--pretty=%s"],
            stderr=subprocess.DEVNULL,
        ).decode()
    except subprocess.CalledProcessError:
        return "patch"

    messages = log.strip().split("\n")
    for msg in messages:
        if "BREAKING" in msg.upper():
            return "major"
    for msg in messages:
        lower = msg.lower()
        if lower.startswith("feat:") or lower.startswith("feat("):
            return "minor"
    return "patch"


def main():
    parser = argparse.ArgumentParser(description="Bump CAS version")
    parser.add_argument("--bump", choices=["patch", "minor", "major"])
    parser.add_argument("--analyze-commits", action="store_true",
                        help="Print recommended bump type based on commit messages")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.analyze_commits:
        print(analyze_commits())
        return

    if not args.bump:
        parser.error("--bump is required (or use --analyze-commits)")

    current = read_version()
    new_version = bump(current, args.bump)

    if args.dry_run:
        print(f"Would bump {current} -> {new_version}")
        return

    write_version(new_version)
    print(f"Bumped {current} -> {new_version}")


if __name__ == "__main__":
    main()
