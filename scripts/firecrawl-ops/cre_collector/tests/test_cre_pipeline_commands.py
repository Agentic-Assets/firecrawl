"""Offline process probes for responsive single-owner pipeline waits."""

import os
import sys
import time

import pytest

import cre_checkpoint_refresh as refresh


@pytest.fixture(autouse=True)
def isolated_resource_trip_state():
    """Existing fault suites may leave a synthetic sticky watchdog trip."""
    refresh._clear_cpu_guard_trip()
    try:
        yield
    finally:
        refresh._clear_cpu_guard_trip()


def test_pipeline_default_wait_remains_blocking(tmp_path, monkeypatch):
    class Process:
        returncode = 0
        pid = 999999991

        def wait(self):
            return 7

        def poll(self):
            pytest.fail("serial command unexpectedly polled")

    monkeypatch.setattr(refresh.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(
        refresh, "_require_completed_group_settlement", lambda group: None
    )
    assert refresh._run_logged_command(["offline"], tmp_path / "serial.log") == 7


def test_pipeline_callback_advances_while_command_blocked(tmp_path):
    from cre_refresh_pipeline import command_poll_hook

    ready = tmp_path / "ready"
    released = tmp_path / "released"
    ticks = []

    def refill():
        ticks.append(time.monotonic())
        if ready.exists():
            released.write_text("refilled")

    code = (
        "from pathlib import Path; import sys,time; "
        "Path(sys.argv[1]).touch(); "
        "\nwhile not Path(sys.argv[2]).exists(): time.sleep(0.01)\n"
    )
    with command_poll_hook(refill):
        assert (
            refresh.run_command(
                [sys.executable, "-c", code, str(ready), str(released)],
                tmp_path / "blocked.log",
            )
            == 0
        )
    assert ticks and released.read_text() == "refilled"


def test_pipeline_poll_callback_cannot_reenter(tmp_path):
    from cre_refresh_pipeline import command_poll_hook, poll_hook

    calls = []

    def callback():
        calls.append(1)
        assert refresh.run_command(
            [sys.executable, "-c", "import time; time.sleep(0.15)"],
            tmp_path / "nested-command.log",
        ) == 0
        poll_hook()()

    assert poll_hook() is None
    with command_poll_hook(callback) as poll:
        poll()
        assert calls == [1]
        with pytest.raises(RuntimeError, match="nested"):
            with command_poll_hook(callback):
                pass
    assert poll_hook() is None


@pytest.mark.parametrize("error", [ValueError("tick failed"), KeyboardInterrupt()])
def test_pipeline_callback_failure_settles_owned_command(tmp_path, monkeypatch, error):
    from cre_refresh_pipeline import command_poll_hook

    monkeypatch.setattr(refresh, "COMMAND_INTERRUPT_GRACE_SECONDS", 0.05)
    ready = tmp_path / "ready"
    code = (
        "from pathlib import Path; import sys,time; "
        "Path(sys.argv[1]).write_text(str(__import__('os').getpid())); time.sleep(15)"
    )

    def fail():
        if ready.exists():
            raise error

    with command_poll_hook(fail), pytest.raises(type(error)) as caught:
        refresh._run_logged_command(
            [sys.executable, "-c", code, str(ready)], tmp_path / "fault.log"
        )
    assert caught.value is error
    with pytest.raises(ProcessLookupError):
        os.kill(int(ready.read_text()), 0)
    assert error.cre_local_settlement["state"] == "confirmed"


def test_pipeline_callback_fault_unknown_cleanup_keeps_fence(tmp_path, monkeypatch):
    from cre_owned_processes import Settlement
    from cre_refresh_pipeline import command_poll_hook

    class Process:
        pid = 999999991
        returncode = None

        def poll(self):
            return None

    monkeypatch.setattr(refresh.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(
        refresh,
        "settle_owned_groups",
        lambda *a, **k: Settlement(False, ("denied",), ()),
    )

    def fail():
        raise ValueError("tick failed")

    with command_poll_hook(fail), pytest.raises(refresh.ProcessCleanupUnknown):
        refresh._run_logged_command(["offline"], tmp_path / "unknown.log")


def test_pipeline_spawn_gap_retains_unknown_fence(tmp_path, monkeypatch):
    from cre_refresh_pipeline import command_poll_hook

    def interrupted_spawn(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(refresh.subprocess, "Popen", interrupted_spawn)
    with command_poll_hook(lambda: None), pytest.raises(KeyboardInterrupt) as caught:
        refresh._run_logged_command(["offline"], tmp_path / "spawn.log")
    assert refresh._cleanup_unknown_in_chain(caught.value)


def test_pipeline_completed_leader_requires_descendant_settlement(
    tmp_path, monkeypatch
):
    from cre_refresh_pipeline import command_poll_hook

    class Process:
        pid = 999999991
        returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(refresh.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(refresh, "_group_alive", lambda pgid: True)
    with command_poll_hook(lambda: None), pytest.raises(refresh.ProcessCleanupUnknown):
        refresh._run_logged_command(["offline"], tmp_path / "descendant.log")


def test_pipeline_log_close_failure_is_not_success(tmp_path, monkeypatch):
    from pathlib import Path
    from cre_refresh_pipeline import command_poll_hook

    target = tmp_path / "close.log"
    real_open = Path.open

    class BrokenClose:
        def __init__(self):
            self.inner = real_open(target, "a", encoding="utf-8")

        def fileno(self):
            return self.inner.fileno()

        def write(self, value):
            return self.inner.write(value)

        def flush(self):
            return self.inner.flush()

        def close(self):
            self.inner.close()
            raise OSError("evidence close failed")

    def opened(path, *args, **kwargs):
        return BrokenClose() if path == target else real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opened)
    with (
        command_poll_hook(lambda: None),
        pytest.raises(OSError, match="evidence close"),
    ):
        refresh._run_logged_command([sys.executable, "-c", "pass"], target)
