"""Pure C10 session helpers.

This module deliberately contains no runtime, lock, subprocess, browser, or
callback authority. Production execution is owned exclusively by
``capacity_c10.production`` and its host session. Keeping the one-use ledger
helper here makes state-machine tests possible without leaving an injectable
production runner behind.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import C10Error, new_session, require_no_write


def initial_session(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable-plan-bound, empty C10 arm ledger."""
    return new_session(plan)


def validate_test_terminal_result(result: Mapping[str, Any]) -> None:
    """Assert a completed no-write fixture without executing any callback.

    This exists only to make state-machine tests explicit. It accepts data, not
    callables, and cannot acquire a lock, start a process, or issue provider
    work.
    """
    if result.get("terminal") is not True:
        raise C10Error("C10 test terminal result is not terminal")
    require_no_write(result)
