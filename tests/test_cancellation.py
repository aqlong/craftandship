"""Tests for cli/cancellation.py, cooperative cancel-event registry."""

import threading
import time

import pytest

from cli import cancellation


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Snapshot + restore the module-level registry so tests don't leak."""
    snapshot = dict(cancellation._registry)
    try:
        yield
    finally:
        cancellation._registry.clear()
        cancellation._registry.update(snapshot)


class TestCancellationRegistry:
    def test_register_returns_unset_event(self):
        """register(build_id) returns a threading.Event, initially not set."""
        event = cancellation.register("build-1")
        assert isinstance(event, threading.Event)
        assert event.is_set() is False

    def test_register_is_idempotent(self):
        """register called twice with the same build_id returns the SAME
        event object (identity check, not equality)."""
        first = cancellation.register("build-1")
        second = cancellation.register("build-1")
        assert first is second

    def test_get_after_register_returns_same_event(self):
        """get(build_id) after register(build_id) returns the registered event."""
        registered = cancellation.register("build-1")
        fetched = cancellation.get("build-1")
        assert fetched is registered

    def test_get_none_returns_no_op_event(self):
        """get(None) returns cancellation._NO_OP_EVENT, the shared never-set
        sentinel, so callers with no build_id can safely poll .is_set()."""
        fetched = cancellation.get(None)
        assert fetched is cancellation._NO_OP_EVENT
        assert fetched.is_set() is False

    def test_get_unregistered_returns_no_op_event(self):
        """get('never_registered') returns the SAME instance as get(None),
        i.e. the _NO_OP_EVENT sentinel."""
        from_none = cancellation.get(None)
        from_unknown = cancellation.get("never_registered")
        assert from_unknown is from_none
        assert from_unknown is cancellation._NO_OP_EVENT

    def test_fire_sets_the_event(self):
        """fire(build_id) on a registered id transitions its event to set."""
        event = cancellation.register("build-1")
        assert event.is_set() is False
        cancellation.fire("build-1")
        assert event.is_set() is True

    def test_fire_unregistered_is_silent_noop(self):
        """fire('never_registered') does not raise and does not mutate
        the _NO_OP_EVENT sentinel."""
        # Must not raise
        cancellation.fire("never_registered")
        assert cancellation._NO_OP_EVENT.is_set() is False

    def test_clear_removes_from_registry(self):
        """clear(build_id) removes the mapping; a subsequent get(build_id)
        returns the _NO_OP_EVENT sentinel, not the old (possibly-set) event."""
        cancellation.register("build-1")
        cancellation.fire("build-1")
        cancellation.clear("build-1")
        fetched = cancellation.get("build-1")
        assert fetched is cancellation._NO_OP_EVENT
        assert fetched.is_set() is False

    def test_clear_unregistered_is_silent_noop(self):
        """clear('never_registered') does not raise."""
        cancellation.clear("never_registered")  # must not raise

    def test_register_fire_clear_register_yields_fresh_event(self):
        """After register -> fire -> clear -> register, the second register
        returns a brand-new, never-set event (no stuck state from the prior
        lifecycle)."""
        first = cancellation.register("build-1")
        cancellation.fire("build-1")
        assert first.is_set() is True
        cancellation.clear("build-1")
        second = cancellation.register("build-1")
        assert second is not first
        assert second.is_set() is False

    def test_thread_safety_concurrent_register_same_id(self):
        """10 concurrent threads each calling register('same_id') all receive
        the same event object, no race creates a second event. Use a
        threading.Barrier to maximize the contention window."""
        num_threads = 10
        barrier = threading.Barrier(num_threads)
        results: list[threading.Event] = []
        results_lock = threading.Lock()

        def worker():
            barrier.wait()
            event = cancellation.register("same_id")
            with results_lock:
                results.append(event)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == num_threads
        first = results[0]
        for event in results[1:]:
            assert event is first

    def test_thread_safety_fire_visible_across_threads(self):
        """fire() in thread A is visible to get().is_set() in thread B within
        a small bounded delay (use event.wait(timeout=1.0) in the reader
        thread and assert it returns True)."""
        cancellation.register("build-1")
        reader_ready = threading.Event()
        observed = {"set": False}

        def reader():
            event = cancellation.get("build-1")
            reader_ready.set()
            observed["set"] = event.wait(timeout=1.0)

        t = threading.Thread(target=reader)
        t.start()
        reader_ready.wait(timeout=1.0)
        # Tiny sleep to be sure the reader is blocked inside .wait()
        time.sleep(0.05)
        cancellation.fire("build-1")
        t.join(timeout=2.0)
        assert observed["set"] is True

    def test_no_op_event_is_never_set(self):
        """Defence in depth: firing via an unregistered id must NEVER set
        cancellation._NO_OP_EVENT. Assert _NO_OP_EVENT.is_set() is False
        after fire('unregistered')."""
        assert cancellation._NO_OP_EVENT.is_set() is False
        cancellation.fire("unregistered")
        assert cancellation._NO_OP_EVENT.is_set() is False
