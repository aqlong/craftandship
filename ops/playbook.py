# ops/playbook.py - Known-fix registry for the ops agent
#
# Maps error categories to fix implementations. There are two: set a missing
# environment variable on the worker service (env_config) and redeploy the
# worker service (railway_crash). Both act through ops/fixer.py, which holds
# the limits. A failure in any other category has no playbook: the ops agent
# and the coordinator's infra agent report it to a person.

import importlib
import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("ops.playbook")


@dataclass
class FixResult:
    """Result of applying a playbook fix."""
    success: bool
    description: str
    requires_redeploy: bool = False


@dataclass
class PlaybookEntry:
    """A single playbook entry with metadata and fixer reference."""
    category: str
    description: str
    severity: str  # "infrastructure" or "app_logic"
    auto_fixable: bool
    fixer_module: str  # e.g. "ops.playbooks.railway_redeploy"
    requires_redeploy: bool


# Registry of known fixes
PLAYBOOKS: dict[str, PlaybookEntry] = {
    "env_config": PlaybookEntry(
        category="env_config",
        description="Missing or incorrect Railway environment variable",
        severity="infrastructure",
        auto_fixable=True,
        fixer_module="ops.playbooks.env_config",
        requires_redeploy=False,
    ),
    "railway_crash": PlaybookEntry(
        category="railway_crash",
        description="Service crashed and needs redeployment",
        severity="infrastructure",
        auto_fixable=True,
        fixer_module="ops.playbooks.railway_redeploy",
        requires_redeploy=True,
    ),
}


def match_playbook(category: str) -> Optional[PlaybookEntry]:
    """Return the playbook entry for a category, or None if not fixable."""
    entry = PLAYBOOKS.get(category)
    if entry and entry.auto_fixable:
        return entry
    return None


def load_fixer(entry: PlaybookEntry) -> Optional[Callable]:
    """Dynamically import and return the fix() function from a playbook module."""
    try:
        module = importlib.import_module(entry.fixer_module)
        fixer = getattr(module, "fix", None)
        if fixer is None:
            logger.error("No fix() function in %s", entry.fixer_module)
            return None
        return fixer
    except ImportError as exc:
        logger.error("Failed to import %s: %s", entry.fixer_module, exc)
        return None
