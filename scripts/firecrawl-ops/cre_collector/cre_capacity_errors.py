"""Shared exception hierarchy for governed CRE capacity operations."""

from __future__ import annotations


class RuntimeAdmissionError(RuntimeError):
    """Runtime state cannot be admitted or changed safely."""


class RuntimeMutationError(RuntimeAdmissionError):
    """A runtime mutation command was issued but did not complete cleanly."""


class RuntimeOverlayCleanupError(RuntimeMutationError):
    """A resource command completed but its private overlay did not clean up."""
