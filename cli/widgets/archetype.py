"""Archetype gate for the widget registry."""

# WIDGETS_ENABLED (env var) - three-state control for widget emission:
#   unset        -> universal (every spec emits widget admin surfaces).
#                   Previously MW-archetype-gated, but non-MW customer
#                   builds repeatedly tripped admin_settings + admin_crud
#                   contracts ("file missing") because Claude does not
#                   reliably produce views/admin/<slug>/new.ejs +
#                   views/admin/settings.ejs. Per the
#                   standard-widget doctrine, after the 2nd
#                   recurrence we ship a canonical @processor instead of
#                   tweaking prompts. Universal widget emission is that
#                   canonical path. The widget emitter is intentionally
#                   spec-agnostic (pure model-driven CRUD, SECTION_LAYOUT-
#                   driven settings with _has() gates so unused keys do
#                   not render).
#   "true"       -> same as unset (explicit on; kept for the opt-in
#                   ramp period in case an operator wants the explicit
#                   setting for audit clarity)
#   "false"      -> emergency kill-switch: restore the pre-universalization
#                   legacy path (no widget emission, Claude admin output
#                   lives or dies on its own). Use when a specific build
#                   needs a rollback without redeploying the worker.
#   any other    -> ValueError at build start ("WIDGETS_ENABLED must be
#                   unset, 'true', or 'false'; got <value>")
#
# Unambiguous tri-state avoids the common "WIDGETS_ENABLED=1 / yes / on / True"
# drift trap where truthy-looking strings silently flip to the wrong branch.

from __future__ import annotations

import os


def _widgets_enabled_for(spec: dict) -> bool:
    """Return True when the widget pipeline should be used for this spec.

    Reads the `WIDGETS_ENABLED` env var on every call (no caching) so a
    worker process picking up a build right after a variableUpsert sees
    the new value without a restart. The `spec` argument is accepted for
    API continuity (so the call site and future spec-conditional kill
    switches stay clean) but unused in the default path; widget emission
    is universal when the env var is unset.

    History:
      Originally this gate returned True only for Medicine Waves
      specs (description contains "medicine waves" or app_name starts
      with "medicine-waves"). Non-MW builds then failed at
      contracts/admin_crud.py and contracts/admin_settings.py with
      "file missing" because Claude did not produce
      views/admin/<slug>/new.ejs + edit.ejs + views/admin/settings.ejs.
      The widget emitter is purely model-driven (CRUD) and
      SECTION_LAYOUT-driven (settings, with _has() gates so absent
      settings keys do not render), so it is safe to run on every spec.
      WIDGETS_ENABLED=false remains the kill switch if an emergency
      rollback is needed for a specific build.
    """
    _ = spec  # accepted for API symmetry; not read in the default path
    env_value = os.environ.get("WIDGETS_ENABLED")
    if env_value is not None:
        if env_value == "true":
            return True
        if env_value == "false":
            return False
        raise ValueError(
            "WIDGETS_ENABLED expected: unset, 'true', or 'false'; "
            f"got '{env_value}'"
        )
    return True
