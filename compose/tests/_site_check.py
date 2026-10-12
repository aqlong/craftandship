"""Shared helper: write built sites to disk and run `_site_harness.js` on them.

Not a test module. `check_sites` parses every emitted script and JSON file,
compiles every emitted view, loads the emitted routers into a real Express
app and requests every GET route as a visitor and as the admin, rendering
the views with the real EJS (see the header of `_site_harness.js` for what
is real and what is a stand-in).

Node and web/node_modules (express, ejs, express-ejs-layouts,
express-rate-limit, bcrypt, multer) are required; `AVAILABLE` says whether
they are there.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from compose import build as B

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent.parent
WEB_MODULES = REPO_ROOT / "web" / "node_modules"
HARNESS = TESTS_DIR / "_site_harness.js"
NODE = shutil.which("node")

_NEEDED = ("express", "ejs", "express-ejs-layouts", "express-rate-limit", "bcrypt", "multer")
AVAILABLE = NODE is not None and all((WEB_MODULES / name).exists() for name in _NEEDED)
UNAVAILABLE_REASON = (
    "needs node and web/node_modules with " + ", ".join(_NEEDED) + " (run `cd web && npm ci`)"
)


def tables_for(rs: "B.ResolvedSpec") -> Dict[str, List[Dict[str, str]]]:
    """`{table: [{name, kind}]}` for every model of a resolved spec, from
    the engine's own definitions of a model's table, fields and column
    types. The harness answers SELECTs with one row built from this."""
    tables: Dict[str, List[Dict[str, str]]] = {}
    for model in rs.data_models:
        if not isinstance(model, dict):
            continue
        columns = []
        for field in B._model_fields(model):
            columns.append({"name": str(field.get("name")), "kind": str(B._sql_type_for(field))})
        tables[B._table_name_for_model(model)] = columns
    return tables


def write_files(app_dir: Path, files: Iterable[Tuple[str, object]]) -> None:
    """Write an emitted file list with the engine's own contained writer."""
    app_dir.mkdir(parents=True, exist_ok=True)
    B._write_build_files(app_dir, list(files))


def write_overlay(app_dir: Path, base_dir: Path, base_files: Dict[str, object],
                  files: Dict[str, object]) -> List[str]:
    """Write `files` to `app_dir`, hard-linking every file that is
    byte-identical to the one already on disk under `base_dir`. Returns the
    relative paths that differ from the base (those are the only files a
    change to the spec could have broken)."""
    changed: List[str] = []
    for rel, content in files.items():
        target = app_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if rel in base_files and base_files[rel] == content:
            os.link(base_dir / rel, target)
            continue
        changed.append(rel)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            with open(target, "w", encoding="utf-8", newline="") as fh:
                fh.write(content)
    return changed


def check_sites(apps: List[dict], workdir: Path, timeout: int = 900) -> Dict[str, dict]:
    """Run the harness over `apps` (`[{id, dir, tables, only?, database_url?, extra_paths?}]`)
    in one node process. An app with a `database_url` talks to that
    PostgreSQL database instead of the stand-in (the caller has applied
    the site's schema and seed rows there first: `apply_site_sql`).

    Returns `{id: result}`; a result has `parse` (files that do not parse
    or compile), `load` (why the routers could not be loaded, or None),
    `failures` (requests whose handler or view threw, or that answered 500
    or above), `routes` and `requests`."""
    workdir.mkdir(parents=True, exist_ok=True)
    plan_path = workdir / "plan.json"
    out_path = workdir / "result.json"
    plan = {"out": str(out_path), "apps": [
        {"id": a["id"], "dir": str(a["dir"]), "tables": a.get("tables") or {}, "only": a.get("only"),
         "database_url": a.get("database_url"), "extra_paths": a.get("extra_paths") or []}
        for a in apps
    ]}
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    if out_path.exists():
        out_path.unlink()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "NODE_PATH": str(WEB_MODULES),
        "NODE_ENV": "test",
    }
    if any(a.get("database_url") for a in apps):
        # node-postgres names the role after the OS user when the URL
        # carries none, and reads it from USER.
        import getpass
        env["USER"] = os.environ.get("USER") or getpass.getuser()
    proc = subprocess.run(
        [NODE, str(HARNESS), str(plan_path)],
        capture_output=True, text=True, env=env, timeout=timeout, cwd=str(workdir),
    )
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(
            f"site harness failed (exit {proc.returncode}): {proc.stdout[-600:]} {proc.stderr[-1200:]}"
        )
    results = json.loads(out_path.read_text(encoding="utf-8"))["results"]
    return {r["id"]: r for r in results}


def schema_database_url(database_url: str, schema: str) -> str:
    """`database_url` with a search path that holds only `schema` (both
    node-postgres and libpq read the `options` parameter)."""
    separator = "&" if "?" in database_url else "?"
    return f"{database_url}{separator}options=-c%20search_path%3D{schema}"


def apply_site_sql(app_dir: Path, database_url: str, schema: str = "") -> str:
    """Create the tables and rows a built site's first boot creates: its
    db/schema.sql, then its db/seed.sql, each run as one script (what
    db_migrate.js does on a fresh database). Raises the database's own error
    for a statement it rejects. Returns the URL the site's routers use.

    With no `schema` the `public` schema of the database at `database_url`
    is dropped and created again first, so the URL must name a scratch
    database. With a `schema` that one is created (an existing one of that
    name is dropped) and the returned URL looks nowhere else, so many sites
    can share one database."""
    import psycopg2

    conn = psycopg2.connect(database_url)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            if schema:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE; CREATE SCHEMA "{schema}"; '
                            f'SET search_path TO "{schema}";')
            else:
                cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
            for name in ("schema.sql", "seed.sql"):
                cur.execute((Path(app_dir) / "db" / name).read_text(encoding="utf-8"))
    finally:
        conn.close()
    return schema_database_url(database_url, schema) if schema else database_url


def drop_schema(database_url: str, schema: str) -> None:
    import psycopg2

    conn = psycopg2.connect(database_url)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        conn.close()


def problems_of(result: dict) -> List[str]:
    """Everything wrong with one harness result, as short lines."""
    lines: List[str] = []
    for p in result.get("parse") or []:
        lines.append(f"does not parse: {p['file']}: {p['error']}")
    if result.get("load"):
        lines.append(f"routers did not load: {result['load']}")
    for f in result.get("failures") or []:
        lines.append(f"GET {f['path']} as {f['who']} -> {f['status']}: {f['error']}")
    return lines

