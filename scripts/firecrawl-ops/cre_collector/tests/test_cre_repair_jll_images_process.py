"""Real local process contracts; fake psql never opens a database or network."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import cre_ingest
import cre_repair_jll_images as repair
import pytest
from cre_checkpoint_refresh import SharedLock

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX ownership contract")
RESULT = {"operation": "delete", "committed": True, "matched": 3, "status": "passed"}
TAG = "JLL_REPAIR_RESULT:" + json.dumps(RESULT)


@pytest.fixture
def client(tmp_path, monkeypatch):
    def configure(body):
        executable = tmp_path / "fake_psql"
        executable.write_text(f"#!{sys.executable}\n" + body)
        executable.chmod(0o700)
        work = tmp_path / "work"
        work.mkdir(mode=0o700)
        monkeypatch.setattr(cre_ingest, "find_psql", lambda: str(executable))
        monkeypatch.setattr(cre_ingest, "psql_connection_args", lambda _url: [])
        monkeypatch.setattr(
            cre_ingest,
            "psql_connection_env",
            lambda _url: {**os.environ, "PGSSLMODE": "verify-full"},
        )
        return work

    return configure


def test_positive_terminal_after_real_eof(client):
    work = client(
        f"import sys\nprint({TAG!r}, flush=True)\nprint('diagnostic', file=sys.stderr)\n"
    )
    assert repair.run_psql("unused", "SELECT 1;", work) == RESULT
    assert (work / "transaction.sql").read_text() == "SELECT 1;"
    assert (work / "stdout.log").read_text().strip() == TAG
    assert (work / "stderr.log").read_text().strip() == "diagnostic"


@pytest.mark.parametrize(
    ("body", "exception"),
    [
        (f"print({TAG!r}, flush=True)\nraise SystemExit(9)\n", ValueError),
        ("print('ordinary stdout')\n", ValueError),
        (f"print({TAG!r})\nprint({TAG!r})\n", ValueError),
        ("print('JLL_REPAIR_RESULT:{broken')\n", json.JSONDecodeError),
        ("print('JLL_REPAIR_RESULT:' + '{}')\n", KeyError),
    ],
)
def test_failed_terminal_never_returns_success(client, body, exception):
    work = client(body)
    with pytest.raises(exception):
        repair.run_psql("unused", "SELECT 1;", work)
    assert (work / "stdout.log").exists()
    assert (work / "stderr.log").exists()


def test_output_flood_is_bounded_and_reaped(client):
    work = client("import os\nwhile True:\n os.write(1, b'x' * 65536)\n")
    started = time.monotonic()
    with pytest.raises(ValueError, match="output bound"):
        repair.run_psql("unused", "SELECT 1;", work, timeout=3)
    assert time.monotonic() - started < 12
    assert 4 * 1024**2 < (work / "stdout.log").stat().st_size <= 4 * 1024**2 + 65536


def test_timeout_settles_descendant_without_touching_foreign_group(client, tmp_path):
    # The owned parent catches INT and reaps its child. Both use the real new
    # session created by run_psql; a separate sentinel must remain untouched.
    pid_file = tmp_path / "owned-pids.json"
    body = f"""import json, os, signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
def stop(*_args):
    child.wait(timeout=4)
    raise SystemExit(0)
signal.signal(signal.SIGINT, stop)
with open({str(pid_file)!r}, 'w') as stream:
    json.dump([os.getpid(), child.pid], stream)
while True:
    time.sleep(0.05)
"""
    work = client(body)
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        with pytest.raises(ValueError, match="deadline"):
            repair.run_psql("unused", "SELECT 1;", work, timeout=0.5)
        assert sentinel.poll() is None
        for pid in json.loads(pid_file.read_text()):
            assert not Path(f"/proc/{pid}").exists()
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=5)


def test_dispatch_failure_does_not_claim_transaction_success(client):
    work = client("raise SystemExit(0)\n")
    (work.parent / "fake_psql").unlink()
    with pytest.raises(FileNotFoundError):
        repair.run_psql("unused", "SELECT 1;", work)
    assert (work / "stdout.log").read_bytes() == b""


def test_pipe_eof_does_not_substitute_for_leader_exit(client, tmp_path):
    pid_file = tmp_path / "pid"
    work = client(
        f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "os.close(1)\nos.close(2)\ntime.sleep(60)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        repair.run_psql("unused", "SELECT 1;", work, timeout=0.5)
    assert not Path(f"/proc/{pid_file.read_text()}").exists()


def test_real_operator_interrupt_settles_client(client, tmp_path):
    pid_file = tmp_path / "pid"
    work = client(
        f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    timer = threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGINT))
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            repair.run_psql("unused", "SELECT 1;", work, timeout=5)
    finally:
        timer.cancel()
        timer.join(timeout=2)
    assert not Path(f"/proc/{pid_file.read_text()}").exists()


def test_failed_client_retains_real_mutation_recovery_authority(client, tmp_path):
    work = client("raise SystemExit(8)\n")
    lock = tmp_path / "private-lock"
    with (
        pytest.raises(ValueError, match="transaction refused"),
        SharedLock(lock, recovery_required=True),
    ):
        repair.run_psql("unused", "SELECT 1;", work)
    assert "recovery-required" in lock.with_name(lock.name + ".authority").read_text()
