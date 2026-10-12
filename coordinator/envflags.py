# coordinator/envflags.py: the one parser for boolean environment switches
#
# Every on/off environment variable read by the worker (queue/), the deploy
# code (cli/deploy.py), the legacy ops agent (ops/) and the coordinator goes
# through one of the two functions below. They differ in which way a value
# they do not recognise falls:
#
#   safety_switch   ON is the safe state (a dry run, analysis only, a gate
#                   that blocks). Only an explicit OFF word turns it off.
#                   Anything else that is set, a typo included, leaves it ON.
#   arming_switch   ON arms something (enable a daemon, allow a sweep, send an
#                   email). Only an explicit ON word turns it on. Anything
#                   else that is set, a typo included, leaves it OFF.
#
# So a mistyped value can never be the thing that starts a live run or a
# delete. Values are compared after trimming whitespace and lower-casing.
# Unrecognised text is logged once per call, by variable name.
#
# The module imports only the standard library, so any component can import it.

import logging
import os

logger = logging.getLogger("envflags")

ON_WORDS = frozenset({"true", "1", "yes", "on", "enabled"})
OFF_WORDS = frozenset({"false", "0", "no", "off", "disabled"})


def safety_switch(name: str, default: bool = True) -> bool:
    """Read a switch whose ON state is the safe one.

    Unset or blank: `default`. An OFF word (false, 0, no, off, disabled):
    False. Every other value: True, with a warning when it is not an ON word.
    """
    raw = os.environ.get(name)
    word = (raw or "").strip().lower()
    if not word:
        return default
    if word in OFF_WORDS:
        return False
    if word not in ON_WORDS:
        logger.warning(
            "%s=%r is not a recognised on/off value; the switch stays ON "
            "(only false, 0, no, off or disabled turns it off)",
            name, raw[:40],
        )
    return True


def arming_switch(name: str, default: bool = False) -> bool:
    """Read a switch whose ON state arms something.

    Unset: `default`. Set to an ON word (true, 1, yes, on, enabled): True.
    Set to anything else, blank included: False, with a warning when the text
    is neither blank nor an OFF word.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    word = raw.strip().lower()
    if word in ON_WORDS:
        return True
    if word and word not in OFF_WORDS:
        logger.warning(
            "%s=%r is not a recognised on/off value; the switch stays OFF "
            "(only true, 1, yes, on or enabled turns it on)",
            name, raw[:40],
        )
    return False
