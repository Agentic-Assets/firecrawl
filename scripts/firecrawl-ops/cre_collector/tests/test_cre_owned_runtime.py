"""No Docker/network calls: ownership, fault, cancellation and evidence contracts."""

import copy
import json
import os
import signal
import subprocess

import cre_owned_runtime as owned
import pytest


def container(index, service):
    return {
        "id": str(index) * 64,
        "image": "sha256:" + "a" * 64,
        "project": "firecrawl-cre-test",
        "service": service,
        "restart_policy": {"Name": "always", "MaximumRetryCount": 0},
        "started_at": "2026-10-12T00:00:00Z",
        "restart_count": 0,
        "running": True,
        "restarting": False,
        "dead": False,
        "oom_killed": False,
        "exit_code": 0,
        "finished_at": "0001-01-01T00:00:00Z",
        "ports": {
            "3002/tcp" if service == "api" else "3000/tcp": [
                {
                    "HostIp": "127.0.0.1",
                    "HostPort": "3302" if service == "api" else "3303",
                }
            ]
        },
    }


class FakeDocker:
    def __init__(self):
        self.rows = {
            row["id"]: row
            for row in [
                container(1, "api"),
                container(2, "playwright-service"),
                container(3, "redis"),
            ]
        }
        self.daemon_id = "daemon-one"
        self.mode = None
        self.calls = []
        self.runtime = owned.DockerRuntime("test", self.command)

    def command(self, argv, timeout):
        assert 0 < timeout <= 5
        self.calls.append(list(argv))
        args = argv[2:]
        if args[0] == "context":
            return 0, json.dumps("unix:///tmp/docker.sock")
        if args[0] == "info":
            if self.mode == "daemon_loss":
                raise subprocess.TimeoutExpired(argv, timeout)
            return 0, json.dumps({"id": self.daemon_id, "version": "27.0"})
        if args[0] == "inspect":
            return 0, "\n".join(
                json.dumps(self.rows[item]) for item in args[3:] if item in self.rows
            )
        assert args[0] == "stop"
        for index, item in enumerate(args[3:]):
            if (
                self.mode not in {"stop_timeout", "partial"}
                or index == 0
                and self.mode == "partial"
            ):
                self.rows[item]["running"] = False
                self.rows[item]["ports"] = {}  # Stopped Docker ports may disappear.
            if self.mode == "restart":
                self.rows[item]["restart_count"] += 1
        if self.mode == "stop_timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        return int(self.mode == "partial"), ""

    def pin(self):
        return owned.capture_runtime(
            self.runtime,
            "firecrawl-cre-test",
            {row["service"]: row["id"] for row in self.rows.values()},
        )


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(owned, "LOCK_ROOT", tmp_path / "leases")
    monkeypatch.setattr(owned.time, "sleep", lambda _: None)
    return FakeDocker()


def idle_health(url, _deadline):
    return (
        {"status": "healthy", "activePages": 0}
        if url.endswith("/health")
        else {
            "data": {"jobsInQueue": 0, "activeJobsInQueue": 0, "waitingJobsInQueue": 0}
        }
    )


def test_capture_requires_full_explicit_ids_and_preserves_restart_policy(fake):
    identity = fake.pin()
    assert identity["daemon"]["context"] == "test"
    assert len(identity["containers"]) == 3
    assert identity["containers"][0]["restart_policy"]["Name"] == "always"
    with pytest.raises(owned.RuntimeUnavailable, match="full explicit"):
        owned.capture_runtime(fake.runtime, "firecrawl-cre-test", {"api": "1" * 12})
    with pytest.raises(owned.RuntimeUnavailable):
        owned.capture_runtime(
            fake.runtime, "firecrawl-cre-test", {"wrong-service": "1" * 64}
        )


def test_admission_binds_both_endpoints_and_fresh_idle_health(fake):
    identity = fake.pin()
    owned.admit_runtime(
        fake.runtime,
        identity,
        "http://127.0.0.1:3302",
        "http://127.0.0.1:3303",
        idle_health,
    )
    with pytest.raises(owned.RuntimeUnavailable, match="not bound"):
        owned.admit_runtime(
            fake.runtime,
            identity,
            "http://127.0.0.1:3002",
            "http://127.0.0.1:3303",
            idle_health,
        )


@pytest.mark.parametrize("change", ["daemon", "image", "restart", "policy", "missing"])
def test_admission_rejects_stale_or_changed_runtime(fake, change):
    identity = fake.pin()
    row = fake.rows["1" * 64]
    if change == "daemon":
        fake.daemon_id = "different"
    elif change == "image":
        row["image"] = "sha256:" + "b" * 64
    elif change == "restart":
        row["restart_count"] += 1
    elif change == "policy":
        row["restart_policy"]["Name"] = "no"
    else:
        del fake.rows["1" * 64]
    with pytest.raises(owned.RuntimeUnavailable):
        owned.admit_runtime(
            fake.runtime,
            identity,
            "http://127.0.0.1:3302",
            "http://127.0.0.1:3303",
            idle_health,
        )


@pytest.mark.parametrize(
    "health",
    [
        lambda _url, _deadline: {},
        lambda _url, _deadline: {
            "jobsInQueue": 1,
            "activeJobsInQueue": 0,
            "waitingJobsInQueue": 0,
        },
        lambda url, deadline: (
            {"status": "healthy", "activePages": 1}
            if url.endswith("/health")
            else idle_health(url, deadline)
        ),
    ],
)
def test_admission_rejects_unknown_or_busy_health(fake, health):
    with pytest.raises(owned.RuntimeUnavailable):
        owned.admit_runtime(
            fake.runtime,
            fake.pin(),
            "http://127.0.0.1:3302",
            "http://127.0.0.1:3303",
            health,
        )


def test_stop_freshly_verifies_every_selected_service_without_touching_sentinel(fake):
    identity = fake.pin()
    sentinel = container(4, "sentinel")
    fake.rows[sentinel["id"]] = sentinel
    result = owned.stop_runtime(fake.runtime, identity)
    assert result["ok"]
    assert {row["service"] for row in result["containers"]} == {
        "api",
        "playwright-service",
        "redis",
    }
    assert all(row["state"] == "stopped" for row in result["containers"])
    assert fake.rows[sentinel["id"]]["running"]
    assert not any(sentinel["id"] in argv for argv in fake.calls)


@pytest.mark.parametrize(
    "mode",
    ["daemon_loss", "daemon_change", "stop_timeout", "partial", "restart", "missing"],
)
def test_shutdown_faults_never_certify_unknown_services_stopped(fake, mode):
    identity = fake.pin()
    fake.mode = mode
    if mode == "daemon_change":
        fake.daemon_id = "different"
    if mode == "missing":
        del fake.rows["2" * 64]
    result = owned.stop_runtime(fake.runtime, identity)
    assert len(result["containers"]) == 3
    assert not result["ok"]
    assert any(row["state"] == "unknown" for row in result["containers"])
    if mode in {"daemon_loss", "daemon_change"}:
        assert not any(argv[2] == "stop" for argv in fake.calls)
    if mode == "partial":
        assert [row["state"] for row in result["containers"]] == [
            "stopped",
            "unknown",
            "unknown",
        ]


def test_changed_static_identity_is_never_sent_a_stop(fake):
    identity = fake.pin()
    fake.rows["1" * 64]["image"] = "sha256:" + "c" * 64
    result = owned.stop_runtime(fake.runtime, identity)
    assert result["containers"][0]["state"] == "unknown"
    stop = next(argv for argv in fake.calls if argv[2] == "stop")
    assert "1" * 64 not in stop


def test_lease_excludes_duplicate_identity_owners(fake):
    identity = fake.pin()
    with (
        owned.runtime_lease(identity),
        pytest.raises(BlockingIOError),
        owned.runtime_lease(copy.deepcopy(identity)),
    ):
        pytest.fail("duplicate runtime owner admitted")


@pytest.mark.parametrize(
    "unsafe", ["symlink", "hardlink", "public", "oversize", "fifo"]
)
def test_private_identity_read_rejects_unsafe_files(tmp_path, unsafe):
    path = tmp_path / "identity.json"
    owned.write_private_json(path, {"safe": True})
    if unsafe == "symlink":
        path.rename(tmp_path / "original")
        path.symlink_to(tmp_path / "original")
    elif unsafe == "hardlink":
        os.link(path, tmp_path / "alias")
    elif unsafe == "public":
        path.chmod(0o644)
    elif unsafe == "oversize":
        path.write_bytes(b"x" * (owned.MAX_BYTES + 1))
    else:
        path.unlink()
        os.mkfifo(path)
    with pytest.raises((owned.RuntimeUnavailable, OSError)):
        owned.read_private_json(path)


def test_private_evidence_is_exclusive_and_owner_only(tmp_path):
    path = tmp_path / "identity.json"
    owned.write_private_json(path, {"safe": True})
    assert owned.read_private_json(path) == {"safe": True}
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        owned.write_private_json(path, {"replacement": True})


@pytest.mark.parametrize(
    "collector_rc,cleanup_ok,expected",
    [(75, True, 75), (75, False, 75), (0, False, 1), (0, True, 0)],
)
def test_supervisor_preserves_collector_rc_separately(
    fake, tmp_path, monkeypatch, collector_rc, cleanup_ok, expected
):
    monkeypatch.setattr(owned, "admit_runtime", lambda *_args: None)
    monkeypatch.setattr(
        owned, "stop_runtime", lambda *_args, **_kwargs: {"ok": cleanup_ok}
    )
    captured = {}

    class Child:
        def poll(self):
            return collector_rc

    def spawn(_command, **kwargs):
        captured.update(kwargs)
        return Child()

    monkeypatch.setattr(owned.subprocess, "Popen", spawn)
    out = tmp_path / "evidence"
    assert (
        owned.supervise(
            fake.runtime,
            fake.pin(),
            ["series"],
            out,
            api="http://127.0.0.1:3302",
            browser="http://127.0.0.1:3303",
        )
        == expected
    )
    saved = owned.read_private_json(out / "exit.json")
    assert saved["collector_rc"] == collector_rc
    assert saved["runtime_cleanup"]["ok"] is cleanup_ok
    assert captured["env"]["API_URL"] == "http://127.0.0.1:3302"
    assert captured["env"]["COMPOSE_PROJECT_NAME"] == "firecrawl-cre-test"


def test_sigterm_stops_runtime_before_waiting_for_unresponsive_collector(
    fake, tmp_path, monkeypatch
):
    events, handlers = [], {}
    monkeypatch.setattr(owned, "admit_runtime", lambda *_args: None)
    monkeypatch.setattr(
        owned.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler)
    )
    monkeypatch.setattr(
        owned,
        "stop_runtime",
        lambda *_args, **_kwargs: events.append("runtime_stop") or {"ok": True},
    )
    monkeypatch.setattr(owned.os, "killpg", lambda pid, sig: events.append((pid, sig)))

    class Child:
        pid = 12345

        def poll(self):
            return None

        def send_signal(self, sig):
            events.append(("forward", sig))

        def wait(self, timeout):
            events.append(("wait", timeout))
            if timeout == 1:
                raise subprocess.TimeoutExpired("series", timeout)
            return -signal.SIGKILL

    monkeypatch.setattr(owned.subprocess, "Popen", lambda *_args, **_kwargs: Child())
    monkeypatch.setattr(
        owned.time, "sleep", lambda _: handlers[signal.SIGTERM](signal.SIGTERM, None)
    )
    out = tmp_path / "evidence"
    assert (
        owned.supervise(
            fake.runtime,
            fake.pin(),
            ["series"],
            out,
            api="http://127.0.0.1:3302",
            browser="http://127.0.0.1:3303",
        )
        == 143
    )
    assert events.index("runtime_stop") < events.index(("wait", 1))
    assert (12345, signal.SIGKILL) in events
    saved = owned.read_private_json(out / "exit.json")
    assert saved["collector_rc"] == -signal.SIGKILL
    assert saved["signal"] == signal.SIGTERM


def test_admission_failure_still_attempts_only_bound_runtime_cleanup(
    fake, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        owned,
        "admit_runtime",
        lambda *_args: (_ for _ in ()).throw(owned.RuntimeUnavailable("busy")),
    )
    out = tmp_path / "evidence"
    assert (
        owned.supervise(
            fake.runtime,
            fake.pin(),
            ["series"],
            out,
            api="http://127.0.0.1:3302",
            browser="http://127.0.0.1:3303",
        )
        == 1
    )
    saved = owned.read_private_json(out / "exit.json")
    assert saved["collector_rc"] is None
    assert saved["collector_started"] is False
    assert saved["runtime_cleanup"]["ok"]


def test_health_transport_gets_an_independent_wall_deadline(monkeypatch):
    captured = []

    def stalled(argv, timeout):
        captured.append((argv, timeout))
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(owned, "bounded_command", stalled)
    with pytest.raises(subprocess.TimeoutExpired):
        owned.read_health("http://127.0.0.1:3302/health", owned.time.monotonic() + 10)
    assert captured[0][1] == 2


def test_default_series_has_no_runtime_lifecycle_hook():
    source = (
        owned.Path(owned.__file__).with_name("cre_checkpoint_series.py").read_text()
    )
    assert "cre_owned_runtime" not in source


def test_overlapping_selections_and_context_aliases_share_container_lease(fake):
    identity = fake.pin()
    overlap = copy.deepcopy(identity)
    overlap["containers"] = overlap["containers"][:2]
    overlap["daemon"]["context"] = "alias"
    with (
        owned.runtime_lease(identity),
        pytest.raises(BlockingIOError),
        owned.runtime_lease(overlap),
    ):
        pytest.fail("overlapping runtime owner admitted")


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:3302",
        "https://127.0.0.1:3302",
        "http://user:pass@127.0.0.1:3302",
        "http://127.0.0.1:3302/path",
        "http://example.com:3302",
    ],
)
def test_health_endpoint_rejects_redirectable_or_unbound_origins(endpoint):
    with pytest.raises(owned.RuntimeUnavailable):
        owned.bound_endpoint(endpoint, container(1, "api"), "3002/tcp")


def test_native_health_worker_refuses_non_health_routes():
    with pytest.raises(owned.RuntimeUnavailable):
        owned.health_worker("http://127.0.0.1:3302/v2/scrape")
    with pytest.raises(owned.RuntimeUnavailable, match="redirect"):
        owned.NoRedirect().redirect_request(None, None, None, None, None, None)


def test_daemon_change_during_shutdown_invalidates_all_readback(fake):
    identity = fake.pin()
    original = fake.command

    def change_after_stop(argv, timeout):
        result = original(argv, timeout)
        if argv[2] == "stop":
            fake.daemon_id = "replacement"
        return result

    fake.runtime.runner = change_after_stop
    result = owned.stop_runtime(fake.runtime, identity)
    assert not result["ok"]
    assert all(row["state"] == "unknown" for row in result["containers"])


def test_container_restart_between_fresh_reads_is_unknown(fake):
    identity = fake.pin()
    original = fake.command
    after_stop = False
    reads = 0

    def restart(argv, timeout):
        nonlocal after_stop, reads
        if after_stop and argv[2] == "inspect":
            reads += 1
            if reads == 2:
                fake.rows["1" * 64]["running"] = True
                fake.rows["1" * 64]["restart_count"] += 1
        result = original(argv, timeout)
        after_stop = after_stop or argv[2] == "stop"
        return result

    fake.runtime.runner = restart
    result = owned.stop_runtime(fake.runtime, identity)
    assert not result["ok"]
    assert result["containers"][0]["state"] == "unknown"


def test_shutdown_budget_is_global_not_a_fresh_budget_per_cli(fake, monkeypatch):
    identity = fake.pin()
    elapsed, timeouts = [0.0], []

    def unavailable(argv, timeout):
        timeouts.append(timeout)
        elapsed[0] += timeout
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(owned.time, "monotonic", lambda: elapsed[0])
    fake.runtime.runner = unavailable
    result = owned.stop_runtime(fake.runtime, identity, seconds=15)
    assert not result["ok"]
    assert result["elapsed_seconds"] <= 15
    assert timeouts == [5.0]


def test_exit_evidence_failure_still_stops_runtime_and_restores_handlers(
    fake, tmp_path, monkeypatch
):
    real_write = owned.write_private_json
    stopped, restored = [], []

    def fail_exit(path, value):
        if path.name == "exit.json":
            raise OSError("synthetic evidence unavailable")
        real_write(path, value)

    monkeypatch.setattr(owned, "write_private_json", fail_exit)
    monkeypatch.setattr(
        owned,
        "admit_runtime",
        lambda *_args: (_ for _ in ()).throw(owned.RuntimeUnavailable("busy")),
    )
    monkeypatch.setattr(
        owned,
        "stop_runtime",
        lambda *_args, **_kwargs: stopped.append(True) or {"ok": True},
    )
    monkeypatch.setattr(
        owned.signal, "signal", lambda sig, handler: restored.append((sig, handler))
    )
    with pytest.raises(OSError, match="evidence unavailable"):
        owned.supervise(
            fake.runtime,
            fake.pin(),
            ["series"],
            tmp_path / "evidence",
            api="http://127.0.0.1:3302",
            browser="http://127.0.0.1:3303",
        )
    assert stopped == [True]
    assert len(restored) == 4


def test_cancellation_during_admission_prevents_child_launch(
    fake, tmp_path, monkeypatch
):
    handlers = {}
    monkeypatch.setattr(
        owned.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler)
    )
    monkeypatch.setattr(
        owned,
        "admit_runtime",
        lambda *_args: handlers[signal.SIGTERM](signal.SIGTERM, None),
    )
    monkeypatch.setattr(
        owned.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("cancelled admission launched a child"),
    )
    # Keep Docker mocked: no child launch or actual process signal is needed.
    assert (
        owned.supervise(
            fake.runtime,
            fake.pin(),
            ["series"],
            tmp_path / "evidence",
            api="http://127.0.0.1:3302",
            browser="http://127.0.0.1:3303",
        )
        == 143
    )
    saved = owned.read_private_json(tmp_path / "evidence" / "exit.json")
    assert not saved["collector_started"]
    assert saved["runtime_cleanup"]["ok"]


def test_failed_leader_reap_still_persists_cleanup_and_unknown_ownership(
    fake, tmp_path, monkeypatch
):
    handlers = {}
    monkeypatch.setattr(
        owned.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler)
    )
    monkeypatch.setattr(owned, "admit_runtime", lambda *_args: None)
    monkeypatch.setattr(owned, "stop_runtime", lambda *_args, **_kwargs: {"ok": True})

    class Child:
        def poll(self):
            return None

        def send_signal(self, _sig):
            return None

        def wait(self, timeout):
            raise ChildProcessError("synthetic wait failure")

    monkeypatch.setattr(owned.subprocess, "Popen", lambda *_args, **_kwargs: Child())
    monkeypatch.setattr(
        owned.time, "sleep", lambda _: handlers[signal.SIGTERM](signal.SIGTERM, None)
    )
    out = tmp_path / "evidence"
    assert (
        owned.supervise(
            fake.runtime,
            fake.pin(),
            ["series"],
            out,
            api="http://127.0.0.1:3302",
            browser="http://127.0.0.1:3303",
        )
        == 143
    )
    saved = owned.read_private_json(out / "exit.json")
    assert saved["collector_rc"] is None
    assert saved["collector_leader_reaped"] is False
    assert saved["runtime_cleanup"]["ok"] is True


@pytest.mark.parametrize(
    "field,value",
    [("restarting", True), ("dead", True), ("dead", 0), ("exit_code", None)],
)
def test_stop_rejects_backoff_dead_and_malformed_state(fake, field, value):
    identity = fake.pin()
    original = fake.command

    def command(argv, timeout):
        result = original(argv, timeout)
        if "stop" in argv:
            fake.rows["1" * 64][field] = value
        return result

    fake.runtime.runner = command
    result = owned.stop_runtime(fake.runtime, identity)
    assert not result["ok"]
    row = result["containers"][0]
    assert row["state"] == "unknown"
    assert row["observations"][0][field] == value


@pytest.mark.parametrize(
    "field,value", [("restart_count", 1), ("started_at", "2026-10-12T01:00:00Z")]
)
def test_stop_does_not_signal_changed_epoch(fake, field, value):
    identity = fake.pin()
    fake.rows["1" * 64][field] = value
    result = owned.stop_runtime(fake.runtime, identity)
    assert not result["ok"]
    stops = [call for call in fake.calls if "stop" in call]
    assert stops and all("1" * 64 not in call for call in stops)
    assert result["containers"][0]["state"] == "unknown"
