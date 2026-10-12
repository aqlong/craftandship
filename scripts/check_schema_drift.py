#!/usr/bin/env python3
"""scripts/check_schema_drift.py: Detect schema drift between PostgreSQL environments.

Connects to each configured environment, introspects table/column definitions,
and reports differences. Meant to run in CI, as a coordinator periodic check,
or manually.

Usage:
    python3 scripts/check_schema_drift.py                    # compare local vs prod
    python3 scripts/check_schema_drift.py --envs local,prod  # explicit pair
    python3 scripts/check_schema_drift.py --json             # machine-readable output

Exit codes:
    0: no drift detected
    1: drift detected
    2: connection error
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Allow importing from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import psycopg2
except ImportError:
    psycopg2 = None

INTROSPECT_QUERY = """
SELECT
    c.table_name,
    c.column_name,
    c.data_type,
    c.is_nullable,
    c.column_default,
    c.character_maximum_length,
    c.numeric_precision
FROM information_schema.columns c
JOIN information_schema.tables t
    ON c.table_name = t.table_name AND c.table_schema = t.table_schema
WHERE c.table_schema = 'public'
  AND t.table_type = 'BASE TABLE'
ORDER BY c.table_name, c.ordinal_position
"""

ENV_CONFIGS = {
    "local": {
        "url_env": "DATABASE_URL",
        "default": "postgresql://localhost:5432/craftandship_web",
        "env_file": "web/.env",
    },
    "staging": {
        "url_env": "STAGING_DATABASE_URL",
        "default": None,
    },
    "prod": {
        "url_env": "PROD_DATABASE_URL",
        "default": None,
    },
}


def _load_env_file(env_file: str) -> None:
    """Load key=value pairs from an env file into os.environ (no overwrite)."""
    project_root = Path(__file__).resolve().parent.parent
    filepath = project_root / env_file
    if not filepath.exists():
        return
    for line in filepath.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def get_connection_url(env_name: str) -> str:
    """Resolve the DATABASE_URL for a named environment."""
    config = ENV_CONFIGS.get(env_name)
    if not config:
        raise ValueError(f"Unknown environment: {env_name}")

    if "env_file" in config:
        _load_env_file(config["env_file"])

    url = os.environ.get(config["url_env"]) or config.get("default")
    if not url:
        raise ValueError(
            f"No connection URL for '{env_name}'. "
            f"Set {config['url_env']} environment variable."
        )
    return url


def introspect_schema(connection_url: str) -> dict:
    """Connect to a database and return a dict of {table: {column: info}}."""
    if psycopg2 is None:
        raise RuntimeError("psycopg2 not installed. pip install psycopg2-binary")

    conn = psycopg2.connect(connection_url, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute(INTROSPECT_QUERY)
            rows = cur.fetchall()
    finally:
        conn.close()

    schema = {}
    for table, column, dtype, nullable, default, max_len, precision in rows:
        if table not in schema:
            schema[table] = {}
        schema[table][column] = {
            "type": dtype,
            "nullable": nullable,
            "default": default,
            "max_length": max_len,
            "precision": precision,
        }
    return schema


def compare_schemas(schema_a: dict, schema_b: dict, name_a: str, name_b: str) -> list:
    """Compare two schema dicts and return a list of drift descriptions."""
    drifts = []
    all_tables = sorted(set(schema_a.keys()) | set(schema_b.keys()))

    for table in all_tables:
        cols_a = schema_a.get(table, {})
        cols_b = schema_b.get(table, {})

        if table not in schema_a:
            drifts.append({
                "table": table,
                "type": "table_missing",
                "detail": f"Table '{table}' exists in {name_b} but not in {name_a}",
            })
            continue
        if table not in schema_b:
            drifts.append({
                "table": table,
                "type": "table_missing",
                "detail": f"Table '{table}' exists in {name_a} but not in {name_b}",
            })
            continue

        all_cols = sorted(set(cols_a.keys()) | set(cols_b.keys()))
        for col in all_cols:
            if col not in cols_a:
                drifts.append({
                    "table": table,
                    "column": col,
                    "type": "column_missing",
                    "detail": f"{table}.{col} exists in {name_b} but not in {name_a}",
                })
            elif col not in cols_b:
                drifts.append({
                    "table": table,
                    "column": col,
                    "type": "column_missing",
                    "detail": f"{table}.{col} exists in {name_a} but not in {name_b}",
                })
            else:
                info_a = cols_a[col]
                info_b = cols_b[col]
                if info_a["type"] != info_b["type"]:
                    drifts.append({
                        "table": table,
                        "column": col,
                        "type": "type_mismatch",
                        "detail": (
                            f"{table}.{col} type: "
                            f"{name_a}={info_a['type']}, {name_b}={info_b['type']}"
                        ),
                    })
                if info_a["nullable"] != info_b["nullable"]:
                    drifts.append({
                        "table": table,
                        "column": col,
                        "type": "nullable_mismatch",
                        "detail": (
                            f"{table}.{col} nullable: "
                            f"{name_a}={info_a['nullable']}, {name_b}={info_b['nullable']}"
                        ),
                    })

    return drifts


def main():
    parser = argparse.ArgumentParser(description="Detect schema drift between environments")
    parser.add_argument(
        "--envs",
        default="local,prod",
        help="Comma-separated pair of environments to compare (default: local,prod)",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    env_names = [e.strip() for e in args.envs.split(",")]
    if len(env_names) != 2:
        print("Error: --envs must specify exactly two environments (e.g., local,prod)")
        sys.exit(2)

    name_a, name_b = env_names

    try:
        url_a = get_connection_url(name_a)
        url_b = get_connection_url(name_b)
    except ValueError as e:
        print(f"Config error: {e}")
        sys.exit(2)

    try:
        print(f"Introspecting {name_a}...")
        schema_a = introspect_schema(url_a)
        print(f"  Found {len(schema_a)} tables")

        print(f"Introspecting {name_b}...")
        schema_b = introspect_schema(url_b)
        print(f"  Found {len(schema_b)} tables")
    except Exception as e:
        print(f"Connection error: {e}")
        sys.exit(2)

    drifts = compare_schemas(schema_a, schema_b, name_a, name_b)

    if args.json:
        print(json.dumps({"drifts": drifts, "count": len(drifts)}, indent=2))
    elif drifts:
        print(f"\n{'='*60}")
        print(f"SCHEMA DRIFT DETECTED: {len(drifts)} difference(s)")
        print(f"{'='*60}")
        for d in drifts:
            print(f"  [{d['type']}] {d['detail']}")
        print()
    else:
        print(f"\nNo drift detected between {name_a} and {name_b}.")

    sys.exit(1 if drifts else 0)


if __name__ == "__main__":
    main()
