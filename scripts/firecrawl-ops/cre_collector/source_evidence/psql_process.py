"""Bound psql streaming and settle its owned subprocess group.

This establishes local process settlement only, not a database outcome. Native
syscalls and descendants that escape the new session are outside its deadline.
"""

import math
import os
import selectors
import signal
import subprocess
import time

from cre_owned_processes import OwnedProcessGroup, _defer_cleanup_signals, settle_owned_groups
from source_evidence.document_process import DocumentProcessCleanupUnknown

# SQL embeds plan data as hex: up to twice the 32 MiB plan bound, plus
# bounded statement, guard and per-row overhead for at most 10,001 events.
MAX_SQL_BYTES = 2 * (32 * 1024 * 1024) + 8 * 1024 * 1024
MAX_RESULTS_BYTES = 32 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
TIMEOUT_SECONDS = 45
REAP_GRACE_SECONDS = 5
CHUNK_BYTES = 65536


def _close_streams(process):
    errors = []
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except BaseException as error:
                errors.append(error)
    return errors


class _Failure(Exception):
    def __init__(self, code):
        self.code = code


def run(command, sql_bytes, env, *, timeout_seconds=TIMEOUT_SECONDS, max_results_bytes=MAX_RESULTS_BYTES):
    """Return bounded stdout on complete exit zero; all other DB outcomes are unknown."""
    if (os.name != "posix" or any(not callable(getattr(os, name, None))
                                  for name in ("set_blocking", "read", "write", "killpg", "getpgrp"))
            or not hasattr(signal, "SIGKILL")):
        raise RuntimeError("psql process platform unsupported")
    if (not isinstance(command, (list, tuple)) or not command or any(not isinstance(arg, str) for arg in command)
            or not isinstance(sql_bytes, bytes) or len(sql_bytes) > MAX_SQL_BYTES or not isinstance(env, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items())
            or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= TIMEOUT_SECONDS or type(max_results_bytes) is not int
            or not 1 <= max_results_bytes <= MAX_RESULTS_BYTES):
        raise ValueError("invalid psql process bounds")
    deadline = time.monotonic() + timeout_seconds
    output = bytearray()
    stderr_size = 0
    sent = 0
    code = None
    failure = None
    interrupted = None
    settlement = None
    process = None
    group = None
    try:
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=env, start_new_session=True, shell=False, bufsize=0)
            group = OwnedProcessGroup.from_spawn(process)
        except OSError:
            if process is None:
                raise _Failure("launch_failure") from None
            raise DocumentProcessCleanupUnknown("psql ownership unavailable") from None
        except BaseException:
            raise DocumentProcessCleanupUnknown("psql spawn or registration interrupted") from None
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            if sql_bytes:
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                process.stdin.close()
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _Failure("timeout")
                for key, _ in selector.select(remaining):
                    if key.data == "stdin":
                        try:
                            count = os.write(key.fd, memoryview(sql_bytes)[sent:sent + CHUNK_BYTES])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            raise _Failure("incomplete_input") from None
                        if count <= 0:
                            raise _Failure("incomplete_input")
                        sent += count
                        if sent == len(sql_bytes):
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                    else:
                        try:
                            cap = (max_results_bytes - len(output) if key.data == "stdout"
                                   else MAX_STDERR_BYTES - stderr_size)
                            chunk = os.read(key.fd, min(CHUNK_BYTES, cap + 1))
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fileobj)
                        elif key.data == "stdout":
                            if len(output) + len(chunk) > max_results_bytes:
                                raise _Failure("stdout_limit")
                            output.extend(chunk)
                        else:
                            stderr_size += len(chunk)
                            if stderr_size > MAX_STDERR_BYTES:
                                raise _Failure("stderr_limit")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Failure("timeout")
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise _Failure("timeout") from None
        if sent != len(sql_bytes):
            raise _Failure("incomplete_input")
        if code != 0:
            raise _Failure("nonzero_exit")
    except _Failure as error:
        failure = error.code
    except (OSError, ValueError):
        failure = "stream_error"
    except BaseException as error:
        interrupted = error
    finally:
        try:
            with _defer_cleanup_signals():
                close_errors = _close_streams(process) if process is not None else []
                for error in close_errors:
                    if not isinstance(error, Exception) and interrupted is None:
                        interrupted = error
                    elif failure is None:
                        failure = "stream_error"
                if group is not None:
                    settlement = settle_owned_groups([group], grace_seconds=0, initial_signal=signal.SIGKILL,
                                                     kill_seconds=REAP_GRACE_SECONDS)
        except BaseException:
            raise DocumentProcessCleanupUnknown("psql local settlement unavailable") from None
    if group is not None and (settlement is None or not settlement.settled):
        raise DocumentProcessCleanupUnknown("psql local settlement unknown") from interrupted
    if interrupted is not None:
        raise interrupted
    if failure is not None:
        raise RuntimeError("psql outcome unknown: " + failure) from None
    return bytes(output)
