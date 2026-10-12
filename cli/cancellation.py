"""Cooperative cancellation registry for in-flight builds.

The worker supervisor registers a build_id at the start of each job,
fires the event when it decides the build has hung past its tier
deadline, and clears the event in its finally block.

Cooperative consumers (cli.api_client.call_api and each fix loop) look
up the event via ``get(build_id)`` and poll ``event.is_set()`` at safe
checkpoints. When set, they raise BuildCancelled (defined in
cli/api_client.py in section-02) and propagate out.

Pure stdlib. No network. No disk. All mutation of the registry dict is
guarded by a module-level lock. The threading.Event objects themselves
are intrinsically thread-safe for read/write, so consumers can poll
without taking the lock.

The module-level ``_NO_OP_EVENT`` is a shared sentinel that is NEVER
set. ``get()`` returns it when the caller has no build_id or the
build_id is not registered, so consumers can always poll ``.is_set()``
on the returned event without a None check. ``fire()`` carefully
refuses to fall through to this sentinel so it stays unset forever.

Public API:

    register(build_id) -> threading.Event
    get(build_id)      -> threading.Event   # _NO_OP_EVENT if absent
    fire(build_id)     -> None              # silent no-op if absent
    clear(build_id)    -> None              # silent no-op if absent
"""

import threading
from typing import Optional

# Module-level state. Tests snapshot and restore _registry around each
# test via an autouse fixture to prevent cross-test leakage.
_registry: dict[str, threading.Event] = {}
_registry_lock: threading.Lock = threading.Lock()

# Shared sentinel returned by get() when the caller has no build_id or
# is not registered. This event is NEVER set; fire() must never touch
# it. Consumers polling is_set() on _NO_OP_EVENT always see False, so
# they behave as if cancellation is unavailable.
_NO_OP_EVENT: threading.Event = threading.Event()


def register(build_id: str) -> threading.Event:
    """Create (or return the existing) cancel event for a build.

    Idempotent: calling twice with the same build_id returns the SAME
    event object. Called by queue/worker_pg.py _process_job at the
    start of each job. Does NOT reset state on re-register, if the
    event was already set by a prior fire(), it stays set.
    """
    with _registry_lock:
        event = _registry.get(build_id)
        if event is None:
            event = threading.Event()
            _registry[build_id] = event
        return event


def get(build_id: Optional[str]) -> threading.Event:
    """Look up the cancel event for a build_id.

    Returns ``_NO_OP_EVENT`` (a shared never-set sentinel) when
    ``build_id`` is None or not present in the registry. Callers can
    always poll ``.is_set()`` on the returned event without a None
    check.
    """
    if build_id is None:
        return _NO_OP_EVENT
    with _registry_lock:
        return _registry.get(build_id, _NO_OP_EVENT)


def fire(build_id: str) -> None:
    """Signal cancellation for a build.

    Called by queue/worker_pg.py _supervisor_force_fail BEFORE any DB
    write, so any in-flight fix loop or call_api retry sees the event
    on its next check and raises BuildCancelled.

    Silently no-ops if ``build_id`` is not registered. Never touches
    the ``_NO_OP_EVENT`` sentinel: the fetched local variable is used
    directly instead of falling back to the sentinel.
    """
    with _registry_lock:
        event = _registry.get(build_id)
    if event is not None:
        event.set()


def clear(build_id: str) -> None:
    """Remove the cancel event from the registry.

    Called from queue/worker_pg.py _process_job's finally block to
    prevent the registry from leaking events across job claims.

    Silently no-ops if ``build_id`` is not registered. Does NOT call
    ``.set()`` or ``.clear()`` on the removed event; any remaining
    polling consumers holding a reference to the old event will just
    see its final state, which is correct.
    """
    with _registry_lock:
        _registry.pop(build_id, None)
