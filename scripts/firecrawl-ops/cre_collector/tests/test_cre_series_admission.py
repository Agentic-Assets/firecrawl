"""Offline admission contracts; no runtime, provider, credentials or database."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import cre_checkpoint_series as series

SHA = "a" * 40


def config():
    return series.series_config(
        sources=("cbre", "jll"),
        page_cap=400,
        concurrency=1,
        attempts_per_source=1,
        max_resume_age_hours=24,
        max_host_cpu_percent=90,
        cpu_sustain_seconds=30,
        cpu_sample_seconds=2,
        nice=15,
    )


@pytest.mark.parametrize(
    "expected,stamp",
    [
        ("A" * 40, None),
        ("a" * 39, None),
        ("not-a-sha", None),
        (None, 100000),
        (SHA, 84999),
        (SHA, 100001),
        (SHA, -1),
        (SHA, "nan"),
        (SHA, True),
        (SHA, 10**1000),
    ],
)
def test_invalid_startup_admission_refuses_before_credentials_outputs_or_children(
    tmp_path,
    monkeypatch,
    expected,
    stamp,
):
    monkeypatch.setattr(
        series, "verify_reviewed_checkout", lambda *_a: None, raising=False
    )
    monkeypatch.setattr(series.time, "time", lambda: 100)
    monkeypatch.setattr(
        series,
        "database_target_fingerprint",
        lambda *_a: pytest.fail("credentials forbidden"),
    )
    monkeypatch.setattr(series, "git_identity", lambda: (SHA, False))
    output = tmp_path / "must-not-exist"
    args = ["--sources", "cbre", "--out-root", str(output)]
    if expected is not None:
        args += ["--expected-sha", expected]
    if stamp is not None:
        args += ["--windows-admission-unix-ms", str(stamp)]
    with pytest.raises((series.SeriesError, SystemExit)):
        series.main(args)
    assert not output.exists()


@pytest.mark.parametrize(
    "responses",
    [
        ["b" * 40, "", SHA],
        [SHA, " M tracked.py", SHA],
        [SHA, "?? new.py", SHA],
        [SHA, "", "b" * 40],
    ],
)
def test_reviewed_checkout_rejects_wrong_dirty_or_unpushed_tips(monkeypatch, responses):
    outputs = iter(responses)
    monkeypatch.setattr(
        series, "local_git_output", lambda *_a: next(outputs), raising=False
    )
    with pytest.raises(series.SeriesError):
        series.verify_reviewed_checkout(SHA)


def test_valid_startup_is_recorded_separately_and_never_rechecks_windows_per_source(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(series.time, "time", lambda: 100)
    monkeypatch.setattr(
        series, "verify_reviewed_checkout", lambda *_a: None, raising=False
    )
    monkeypatch.setattr(
        series, "database_target_fingerprint", lambda *_a: {"value": "d" * 64}
    )
    monkeypatch.setattr(
        series,
        "git_identity",
        lambda: pytest.fail("legacy Git lookup should be skipped"),
    )
    captured = []
    monkeypatch.setattr(
        series,
        "run_series",
        lambda _path, manifest, **kw: captured.append((manifest.copy(), kw)) or 0,
    )
    monkeypatch.setattr(series, "save_report", lambda *_a: None)
    assert (
        series.main(
            [
                "--sources",
                "cbre",
                "--out-root",
                str(tmp_path),
                "--expected-sha",
                SHA,
                "--windows-admission-unix-ms",
                "100000",
            ]
        )
        == 0
    )
    manifest, kw = captured[0]
    proof = manifest["latest_invocation_admission"]
    assert proof["expected_sha"] == SHA
    assert proof["windows_admission_unix_ms"] == 100000
    assert proof["windows_scope"] == "startup_only"
    assert kw["expected_sha"] == SHA
    assert "expected_sha" not in manifest["config"]
    assert "windows_admission_unix_ms" not in manifest["config"]


def test_local_git_failure_is_sanitized(monkeypatch):
    def fail(*_a, **_k):
        raise OSError("CREDENTIAL_CANARY")

    monkeypatch.setattr(series.subprocess, "Popen", fail)
    with pytest.raises(series.SeriesError) as caught:
        series.local_git_output(["rev-parse", "HEAD"])
    assert "CREDENTIAL_CANARY" not in str(caught.value)


@pytest.mark.parametrize(
    "body,error",
    [
        ("import time; time.sleep(10)", "timeout"),
        ("import os; os.write(1,b'x'*70000)", "output"),
        (
            "import sys; print('CREDENTIAL_CANARY',file=sys.stderr); sys.exit(23)",
            "failed",
        ),
    ],
)
def test_real_synthetic_git_process_bounds_and_local_settlement(
    monkeypatch, body, error
):
    original = series.subprocess.Popen
    children = []

    def spawn(_argv, **kw):
        process = original([sys.executable, "-c", body], **kw)
        children.append(process)
        return process

    monkeypatch.setattr(series.subprocess, "Popen", spawn)
    with pytest.raises(series.SeriesError) as caught:
        series.local_git_output(["rev-parse", "HEAD"], timeout=0.1)
    assert error in str(caught.value)
    assert "CREDENTIAL_CANARY" not in str(caught.value)
    assert children[0].returncode is not None


def test_uncertain_local_git_cleanup_is_not_claimed_settled(monkeypatch):
    original = series.subprocess.Popen
    monkeypatch.setattr(
        series.subprocess,
        "Popen",
        lambda _argv, **kw: original([sys.executable, "-c", "print('ok')"], **kw),
    )
    monkeypatch.setattr(
        series, "settle_owned_groups", lambda *_a, **_k: SimpleNamespace(settled=False)
    )
    with pytest.raises(series.ProcessCleanupUnknown):
        series.local_git_output(["rev-parse", "HEAD"])


def test_identity_drift_prevents_next_child_and_does_not_expire_windows_startup(
    tmp_path,
    monkeypatch,
):
    manifest = series.new_manifest(tmp_path, git_sha=SHA, config=config())
    checks, children = [], []

    def verify(expected):
        checks.append(expected)
        if len(checks) == 2:
            raise series.SeriesError("reviewed checkout changed")

    monkeypatch.setattr(series, "verify_reviewed_checkout", verify, raising=False)

    class FakeProcess:
        def __init__(self, *_a, **_k):
            self.pid = 4321
            children.append(self)

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(series.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(
        series,
        "_load_child_manifest",
        lambda _root, **kw: (
            tmp_path / "runs" / kw["source"] / "manifest.json",
            {"status": series.SUCCESS_STATUS, "error": None},
        ),
    )
    assert (
        series.run_series(
            tmp_path, manifest, env_file=None, retry_failed=False, expected_sha=SHA
        )
        == 1
    )
    assert checks == [SHA, SHA]
    assert len(children) == 1
    assert manifest["sources"]["cbre"]["state"] == "complete"
    assert manifest["sources"]["jll"]["attempts"] == []
    assert manifest["status"] == "failed"


@pytest.mark.parametrize("stamp", ["100000", True, 1.0])
def test_direct_optional_windows_validator_rejects_wrong_types(monkeypatch, stamp):
    monkeypatch.setattr(series, "verify_reviewed_checkout", lambda *_a: None)
    with pytest.raises(series.SeriesError):
        series.admit_series_invocation(SHA, stamp)


def test_windows_freshness_checked_after_git_inspection(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(series.time, "time", lambda: clock[0])
    monkeypatch.setattr(
        series, "verify_reviewed_checkout", lambda *_a: clock.__setitem__(0, 116)
    )
    with pytest.raises(series.SeriesError, match="stale or invalid"):
        series.admit_series_invocation(SHA, 100000)


def test_legacy_resume_removes_old_invocation_observation_without_config_drift(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(series, "git_identity", lambda: (SHA, False))
    monkeypatch.setattr(
        series, "database_target_fingerprint", lambda *_a: {"value": "d" * 64}
    )
    monkeypatch.setattr(series, "save_report", lambda *_a: None)
    captured = []
    monkeypatch.setattr(
        series,
        "run_series",
        lambda _path, manifest, **kw: captured.append((manifest.copy(), kw)) or 0,
    )
    assert series.main(["--sources", "cbre", "--out-root", str(tmp_path)]) == 0
    directory = next(tmp_path.glob("*/manifest.json")).parent
    manifest = json.loads((directory / "manifest.json").read_text())
    original_config = manifest["config"].copy()
    assert "latest_invocation_admission" not in manifest
    manifest["latest_invocation_admission"] = {
        "windows_scope": "startup_only",
        "checked_at": "old",
    }
    series.save_manifest(directory, manifest)
    assert series.main(["--sources", "cbre", "--resume", str(directory)]) == 0
    resumed, kw = captured[-1]
    assert resumed["config"] == original_config
    assert "latest_invocation_admission" not in resumed
    assert "expected_sha" not in kw


def test_git_command_uses_fixed_cwd_local_args_no_shell_and_sanitized_git_env(
    monkeypatch,
):
    original = series.subprocess.Popen
    observed = []
    monkeypatch.setenv("GIT_DIR", "/wrong/repo/.git")

    def spawn(argv, **kw):
        observed.append((argv, kw))
        return original([sys.executable, "-c", "print('local-only')"], **kw)

    monkeypatch.setattr(series.subprocess, "Popen", spawn)
    assert series.local_git_output(["rev-parse", "HEAD"]) == "local-only"
    argv, kw = observed[0]
    assert argv == [
        "git",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "rev-parse",
        "HEAD",
    ]
    assert kw["cwd"] == series.COLLECTOR_DIR
    assert kw["start_new_session"] is True
    assert "shell" not in kw and "GIT_DIR" not in kw["env"]
    assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_windows_startup_stamp_is_not_revalidated_hours_later_during_children(
    tmp_path, monkeypatch
):
    import cre_guarded_tier

    manifest = series.new_manifest(tmp_path, git_sha=SHA, config=config())
    manifest["latest_invocation_admission"] = {
        "windows_admission_unix_ms": 1,
        "windows_scope": "startup_only",
    }
    checks = []
    monkeypatch.setattr(
        series, "verify_reviewed_checkout", lambda sha: checks.append(sha)
    )
    monkeypatch.setattr(
        cre_guarded_tier,
        "validate_windows_admission",
        lambda *_a: pytest.fail("startup only"),
    )

    class FakeProcess:
        def __init__(self, *_a, **_k):
            self.pid = 4321

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(series.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(
        series,
        "_load_child_manifest",
        lambda _root, **kw: (
            tmp_path / "runs" / kw["source"] / "manifest.json",
            {"status": series.SUCCESS_STATUS, "error": None},
        ),
    )
    assert (
        series.run_series(
            tmp_path, manifest, env_file=None, retry_failed=False, expected_sha=SHA
        )
        == 0
    )
    assert checks == [SHA, SHA]
    assert manifest["latest_invocation_admission"]["windows_admission_unix_ms"] == 1


def test_reviewed_identity_checks_all_three_local_conditions(monkeypatch):
    calls = []
    responses = iter([SHA, "", SHA + "\n" + "b" * 40])
    monkeypatch.setattr(
        series, "local_git_output", lambda argv: calls.append(argv) or next(responses)
    )
    series.verify_reviewed_checkout(SHA)
    assert calls == [
        ["rev-parse", "HEAD"],
        ["status", "--porcelain", "--untracked-files=normal"],
        ["for-each-ref", "--format=%(objectname)", "refs/remotes/origin/"],
    ]


def test_local_git_inspection_timeout_refuses_before_credentials_and_output(
    tmp_path, monkeypatch
):
    def timeout(*_a):
        raise series.SeriesError("local Git inspection timeout")

    monkeypatch.setattr(series, "verify_reviewed_checkout", timeout)
    monkeypatch.setattr(
        series,
        "database_target_fingerprint",
        lambda *_a: pytest.fail("must not read credentials"),
    )
    with pytest.raises(series.SeriesError, match="timeout"):
        series.main(
            [
                "--sources",
                "cbre",
                "--expected-sha",
                SHA,
                "--out-root",
                str(tmp_path / "no-output"),
            ]
        )
    assert list(tmp_path.iterdir()) == []


def test_git_cleanup_unknown_before_child_is_terminal_and_not_source_failure(
    tmp_path, monkeypatch
):
    manifest = series.new_manifest(tmp_path, git_sha=SHA, config=config())

    def unknown(*_a):
        raise series.ProcessCleanupUnknown("local Git cleanup_unknown")

    monkeypatch.setattr(series, "verify_reviewed_checkout", unknown)
    monkeypatch.setattr(
        series.subprocess, "Popen", lambda *_a, **_k: pytest.fail("must not dispatch")
    )
    assert (
        series.run_series(
            tmp_path, manifest, env_file=None, retry_failed=False, expected_sha=SHA
        )
        == 1
    )
    assert manifest["sources"]["cbre"]["attempts"] == []
    assert manifest["sources"]["jll"]["state"] == "pending"
    assert manifest["status"] == "failed"
    assert "cleanup_unknown" in manifest["error"]
    assert not list((tmp_path / "logs").iterdir())


def test_exited_git_leader_with_descendant_held_stdout_has_bounded_owned_cleanup(
    monkeypatch,
):
    import time

    original = series.subprocess.Popen
    children = []
    body = "import os,time; child=os.fork(); time.sleep(10) if child==0 else None; os._exit(0)"

    def spawn(_argv, **kw):
        process = original([sys.executable, "-c", body], **kw)
        children.append(process)
        return process

    monkeypatch.setattr(series.subprocess, "Popen", spawn)
    started = time.monotonic()
    with pytest.raises((series.SeriesError, series.ProcessCleanupUnknown)) as caught:
        series.local_git_output(["rev-parse", "HEAD"], timeout=0.1)
    assert time.monotonic() - started < 3
    assert "timeout" in str(caught.value) or "cleanup_unknown" in str(caught.value)
    assert children[0].returncode is not None


def test_reviewed_identity_drift_after_cpu_cooldown_prevents_retry(
    tmp_path, monkeypatch
):
    from test_cre_resource_recovery_review import pair, stop_child

    clock, root, parent, path, child = pair(tmp_path, monkeypatch)
    checks, launches = [], []
    monkeypatch.setattr(series, "allocate_run_id", lambda: path.parent.name)
    monkeypatch.setattr(series.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(series.time, "sleep", clock.sleep)
    monkeypatch.setattr(series, "read_host_cpu_percent", lambda: 55.0)
    monkeypatch.setattr(series, "git_identity", lambda: (SHA, False))
    monkeypatch.setattr(
        series, "database_target_fingerprint", lambda _env: parent["database_target"]
    )

    def verify(expected):
        checks.append(expected)
        if len(checks) == 2:
            raise series.SeriesError("reviewed checkout changed")

    monkeypatch.setattr(series, "verify_reviewed_checkout", verify)

    class ChildProcess:
        pid = 99801

        def __init__(self, argv, **_kw):
            launches.append(list(argv))

        def wait(self, timeout=None):
            clock.sleep(1)
            stop_child(clock, child)
            series.atomic_write_json(path, child)
            return 75

    monkeypatch.setattr(series.subprocess, "Popen", ChildProcess)
    assert (
        series.run_series(
            root, parent, env_file=None, retry_failed=False, expected_sha=SHA
        )
        == 1
    )
    assert len(launches) == 1 and checks == [SHA, SHA]
    assert parent["resource_recovery"]["total_recoveries"] == 1
    assert len(parent["sources"]["cbre"]["attempts"]) == 1
    assert parent["sources"]["cbre"]["state"] == "failed_global"
