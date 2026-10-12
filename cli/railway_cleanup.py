#!/usr/bin/env python3
# cli/railway_cleanup.py - Delete duplicate Railway projects, keeping the newest per name.
#
# Usage:
#   python3 cli/railway_cleanup.py [--dry-run]
#
# Without --dry-run: deletes duplicates immediately (no undo).
# With --dry-run: prints what would be deleted without touching anything.
#
# "Duplicate" = projects sharing the same name. For each group, keeps the one
# with the most recent createdAt and deletes the rest. Projects whose IDs are
# referenced in the builds table (railway_project_id) are never deleted, and if
# that table cannot be read (no DATABASE_URL, query error) nothing is deleted:
# two customers can pick the same app name, so a name match alone does not make
# a project a leftover.

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

# Allow running from project root or cli/
sys.path.insert(0, str(Path(__file__).parent))

from datetime import datetime, timezone

from deploy import DeployConfigError, get_token, gql, list_projects, destroy_project
from dotenv import load_dotenv

load_dotenv()


def _fmt_date(iso_utc: str) -> str:
    """Convert a UTC ISO timestamp to a local date string (YYYY-MM-DD)."""
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return dt.astimezone().strftime("%Y-%m-%d %H:%M local")


def _active_project_ids():
    """Return the set of railway_project_id values recorded in the builds table.

    Returns None (not an empty set) when DATABASE_URL is not set or the query
    fails. The caller must treat None as "unknown": an empty set would mean
    "no project is in use" and every duplicate would look deletable, including
    the live app of a second customer who picked the same app name.
    """
    try:
        import psycopg2
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            print("  DATABASE_URL is not set: cannot tell which projects are in use.")
            return None
        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT railway_project_id FROM builds WHERE railway_project_id IS NOT NULL")
                ids = {row[0] for row in cur.fetchall()}
        finally:
            conn.close()
        return ids
    except Exception as exc:
        print(f"  Could not query the builds table for project ids in use ({type(exc).__name__}).")
        return None


def find_duplicates(projects):
    """Group projects by name; return groups with more than one member.

    Each group is sorted newest-first by createdAt so index 0 is the keeper.

    Returns:
        List of lists, each inner list = [keeper, *to_delete].
    """
    by_name = defaultdict(list)
    for p in projects:
        by_name[p["name"]].append(p)

    duplicates = []
    for name, group in by_name.items():
        if len(group) > 1:
            # Sort newest first
            group.sort(key=lambda p: p["createdAt"], reverse=True)
            duplicates.append(group)
    return duplicates


def main():
    parser = argparse.ArgumentParser(description="Delete duplicate Railway projects.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without deleting")
    args = parser.parse_args()

    dry_run = args.dry_run
    if dry_run:
        print("[DRY RUN] No projects will be deleted.\n")

    try:
        token = get_token()
    except DeployConfigError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    print("Fetching all Railway projects...")
    projects = list_projects(token)
    print(f"  Found {len(projects)} total projects.")

    active_ids = _active_project_ids()
    if active_ids is None:
        if not dry_run:
            # Fail closed: without the builds table there is no way to know
            # which of several same-named projects is somebody's live app.
            print("Refusing to delete anything without the list of project ids in use. "
                  "Set DATABASE_URL (or use --dry-run to see the candidates).")
            sys.exit(1)
        print("  [DRY RUN] Project ids in use are unknown; nothing below has been checked against the DB.")
        active_ids = set()
    elif active_ids:
        print(f"  {len(active_ids)} project IDs are active in the DB (will not be deleted).")

    duplicates = find_duplicates(projects)
    if not duplicates:
        print("No duplicate project names found. Nothing to clean up.")
        return

    total_to_delete = sum(len(group) - 1 for group in duplicates)
    print(f"\nFound {len(duplicates)} duplicate name(s), {total_to_delete} project(s) to delete:\n")

    deleted = 0
    skipped = 0
    for group in duplicates:
        keeper = group[0]
        to_delete = group[1:]
        print(f"  Name: {keeper['name']}")
        print(f"    Keep:   {keeper['id']} (created {_fmt_date(keeper['createdAt'])})")
        for p in to_delete:
            if p["id"] in active_ids:
                print(f"    Skip:   {p['id']} (created {_fmt_date(p['createdAt'])}) -- active in DB")
                skipped += 1
            else:
                print(f"    Delete: {p['id']} (created {_fmt_date(p['createdAt'])})")
                if not dry_run:
                    try:
                        destroy_project(p["id"], token)
                        deleted += 1
                    except RuntimeError as refused:
                        # destroy_project refuses protected or unverifiable projects.
                        print(f"    Not deleted: {refused}")
                        skipped += 1
                else:
                    deleted += 1  # count for dry-run summary

    print(f"\nSummary: {deleted} project(s) {'would be ' if dry_run else ''}deleted, {skipped} skipped (active in DB or refused).")


if __name__ == "__main__":
    main()
