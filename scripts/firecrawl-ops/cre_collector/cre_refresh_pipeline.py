"""Bounded scheduling helpers for the existing foreground checkpoint owner."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_CALLBACK: ContextVar[Callable[[], None] | None] = ContextVar(
    "cre_pipeline_poll_callback", default=None
)
_IN_CALLBACK: ContextVar[bool] = ContextVar("cre_pipeline_in_callback", default=False)


@contextmanager
def command_poll_hook(callback: Callable[[], None]) -> Iterator[None]:
    """Keep one owner's acquisitions moving during DB/compute command waits.

    Only the main coordinator may install this hook. Its callback must perform
    bounded collection settlement/publication/dispatch, never database work.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("pipeline polling requires the main owner thread")
    if _CALLBACK.get() is not None:
        raise RuntimeError("nested pipeline polling contexts are forbidden")
    if not callable(callback):
        raise TypeError("pipeline polling requires a callable")

    def invoke() -> None:
        if _IN_CALLBACK.get():
            return
        token = _IN_CALLBACK.set(True)
        try:
            callback()
        finally:
            _IN_CALLBACK.reset(token)

    token = _CALLBACK.set(invoke)
    try:
        yield
    finally:
        _CALLBACK.reset(token)


def poll_hook() -> Callable[[], None] | None:
    """Return the current scoped, non-reentrant collection callback."""
    return _CALLBACK.get()


def wait_with_poll_hook(process: Any) -> int:
    """Preserve the original wait unless overlap was explicitly installed."""
    callback = poll_hook()
    if callback is None:
        return process.wait()
    while True:
        rc = process.poll()
        if rc is not None:
            return rc
        callback()
        time.sleep(0.05)
