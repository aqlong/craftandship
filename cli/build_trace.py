"""Lightweight JSONL trace writer for the build pipeline.

Emits structured events to `{build_dir}/.build-trace.jsonl` so we can
answer questions like:
  - Which post-processors actually changed files in this build?
  - What did design review think, and how confident was it?
  - Why did a post-processor bail out (early return)?

Design goals:
  - Fail-safe: never raises, never blocks the build.
  - Zero behavior change: additive only, no reads by the pipeline itself.
  - Minimal surface: one public function, `emit_build_trace`.

Events are append-only JSON lines. Each line is self-contained (no
cross-line references) so a partial file is still parseable.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping, Optional

_TRACE_FILENAME = ".build-trace.jsonl"
_DISABLE_ENV = "BUILD_TRACE_DISABLED"


def _resolve_trace_path(build_dir: Any) -> Optional[Path]:
    """Resolve the trace file path, tolerating None / missing dirs."""
    if build_dir is None:
        return None
    try:
        path = Path(build_dir)
    except (TypeError, ValueError):
        return None
    # Don't create the build dir ourselves, if the pipeline hasn't made it
    # yet, silently drop the event. Trace is observability, not the source
    # of truth.
    if not path.exists() or not path.is_dir():
        return None
    return path / _TRACE_FILENAME


def emit_build_trace(
    build_dir: Any,
    event_type: str,
    payload: Optional[Mapping[str, Any]] = None,
) -> None:
    """Append a single JSONL trace event. Fail-safe; never raises.

    Args:
        build_dir: Path (str or Path) to the current build directory.
                   None or missing dir → drop event silently.
        event_type: Short dotted event name (e.g. "admin_mount.start",
                    "design_review.completed").
        payload: Optional dict of structured fields. Values must be
                 JSON-serialisable; non-serialisable values are coerced to
                 their repr() so the writer never raises.
    """
    # Global kill switch (tests or debugging)
    if os.environ.get(_DISABLE_ENV, "").lower() in ("1", "true", "yes"):
        return

    trace_path = _resolve_trace_path(build_dir)
    if trace_path is None:
        return

    record: dict[str, Any] = {
        "ts": time.time(),
        "event": event_type,
    }
    if payload:
        # Merge payload, shallow: let emit_build_trace callers control keys.
        for key, value in payload.items():
            record[key] = value

    try:
        line = json.dumps(record, default=repr, ensure_ascii=False)
    except (TypeError, ValueError):
        # Absolute last-resort: drop the event rather than crash.
        return

    try:
        with trace_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        # Disk full, permission denied, race with cleanup; swallow.
        return
