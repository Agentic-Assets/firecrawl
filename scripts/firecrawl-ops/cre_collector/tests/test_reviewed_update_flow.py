"""Isolated foreground orchestration with real Git and mocked native surfaces."""

import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "failure",
    [
        "",
        "already_current",
        "real_logged_native",
        "native_current",
        "runtime_current",
        "hold_noop",
        "hold_partial",
        "hold_timeout",
        "hold_unsettled_exit",
        "hold_signaled",
        "hold_real_proxy_kill",
        "advance_partial",
        "restore_partial",
        "repin_settled_failure",
        "repin_unsettled",
        "repin_reconcile_failure",
        "restore_settled_failure",
        "restore_reconcile_failure",
        "image_success",
        "image_noop",
        "image_mixed",
        "image_lock_success",
        "image_completed_failure",
        "image_unready",
        "image_unready_rollback_degraded",
        "image_degraded_after_advance",
        "image_rollback_unknown",
        "image_unknown",
        "image_build_failure",
        "image_idle_after_hold",
        "image_drift_prehold",
        "nuq_change",
        "compose_change",
        "dependency_change",
        "collector_dependency_change",
        "ignored_collision",
        "selected_incompatible",
        "health_before",
        "health_after_advance",
        "api_before",
        "browser_after_advance",
        "docker_unavailable",
    ],
)
def test_foreground_source_flow(tmp_path, monkeypatch, capsys, failure):
    if (
        failure == "hold_real_proxy_kill"
        and os.environ.get("CRE_TEST_NATIVE_INTEROP") != "1"
    ):
        pytest.skip("explicit disposable native interop test required")
    import cre_checkpoint_refresh as checkpoint
    import cre_code_handoff as handoff
    import cre_reviewed_update as update
    import cre_reviewed_update_images as direct
    import cre_runtime_maintenance_images as image_helpers

    development, native, runtime = (
        tmp_path / name for name in ("development", "native", "runtime")
    )
    development.mkdir()

    def git(root, *args):
        return subprocess.check_output(
            ["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL
        ).strip()

    git(development, "init", "--initial-branch=main")
    git(development, "config", "user.name", "Synthetic")
    git(development, "config", "user.email", "fixture@example.invalid")
    (development / ".gitignore").write_text(
        ".scratch/\nscripts/firecrawl-ops/cre_collector/out/\nblocked\n"
    )
    (development / "value").write_text("before")
    git(development, "add", ".")
    git(development, "commit", "-m", "before")
    old = git(development, "rev-parse", "HEAD")
    for root in (native, runtime):
        subprocess.run(
            ["git", "clone", "--no-hardlinks", str(development), str(root)],
            check=True,
            capture_output=True,
        )
    (development / "value").write_text("after")
    if failure.startswith("image_"):
        (development / "apps/api").mkdir(parents=True)
        name = "package.json" if failure == "image_lock_success" else "input.txt"
        (development / "apps/api" / name).write_text("image input")
        if failure == "image_mixed":
            (development / "apps/playwright-service-ts").mkdir(parents=True)
            (development / "apps/playwright-service-ts/input.txt").write_text("image input")
    if failure == "nuq_change":
        (development / "apps/nuq-postgres").mkdir(parents=True)
        (development / "apps/nuq-postgres/nuq.sql").write_text("fixture")
    if failure == "compose_change":
        (development / "docker-compose.yaml").write_text("fixture")
    if failure == "dependency_change":
        (development / "package-lock.json").write_text("{}")
    if failure == "collector_dependency_change":
        (development / "scripts/firecrawl-ops/cre_collector").mkdir(parents=True)
        (development / "scripts/firecrawl-ops/cre_collector/package-lock.json").write_text("{}")
    git(development, "add", ".")
    if failure == "ignored_collision":
        (development / "blocked").write_text("selected")
        git(development, "add", "-f", "blocked")
        (native / "blocked").write_text("preserved local work")
    git(development, "commit", "-m", "after")
    selected = git(development, "rev-parse", "HEAD")
    git(development, "update-ref", "refs/remotes/origin/main", selected)
    if failure in {"already_current", "real_logged_native", "native_current", "runtime_current"}:
        advanced = (
            (native, runtime)
            if failure in {"already_current", "real_logged_native"}
            else ((native,) if failure == "native_current" else (runtime,))
        )
        for root in advanced:
            git(root, "fetch", str(development), "refs/remotes/origin/main")
            git(root, "update-ref", "refs/remotes/origin/main", selected, old)
            git(root, "merge", "--ff-only", selected)

    state = {
        "held": False,
        "pin": selected if failure in {"already_current", "real_logged_native", "runtime_current"} else old,
        "successor": False,
        "calls": [],
        "held_checks": 0,
        "fleet_reads": 0,
        "api_reads": 0,
        "browser_reads": 0,
    }
    services = ["api", "nuq-postgres", "playwright-service", "redis", "rabbitmq"]
    docker = Path("/usr/bin/true")
    original_run = subprocess.run

    def fake_subprocess(argv, *args, **kwargs):
        if str(argv[0]) == str(docker) and failure == "docker_unavailable":
            raise OSError("synthetic Docker unavailable")
        if str(argv[0]) == str(docker) and "inspect" in argv:
            state["fleet_reads"] += 1
            unhealthy = failure == "health_before" or (
                failure == "health_after_advance" and state["fleet_reads"] == 2
            )
            rows = [
                {
                    "service": name,
                    "status": "running",
                    "Config": {"Labels": {"com.docker.compose.service": name}},
                    "State": {
                        "Status": "running",
                        "Health": {
                            "Status": "unhealthy"
                            if unhealthy and name == "nuq-postgres"
                            else "healthy"
                        },
                    },
                }
                for name in services
            ]
            return subprocess.CompletedProcess(argv, 0, json.dumps(rows).encode(), b"")
        if str(argv[0]) == str(docker) and "exec" in argv:
            state["browser_reads"] += 1
            status = (
                "unhealthy"
                if failure == "browser_after_advance" and state["browser_reads"] == 2
                else "healthy"
            )
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"status": status}).encode(), b""
            )
        return original_run(argv, *args, **kwargs)

    def fake_native(argv, log_path, *, env=None):
        args = {
            argv[i]: argv[i + 1]
            for i in range(len(argv) - 1)
            if argv[i].startswith("-")
        }
        stage = args["-Stage"]
        state["calls"].append(stage)

        def completed_failure(code=1):
            receipt = {
                "stage": stage,
                "plan_sha256": args["-PlanSha"],
                "owner_pid": os.getpid(),
                "generation_sha256": hashlib.sha256(
                    args["-Generation"].encode()
                ).hexdigest(),
                "completed": True,
            }
            Path(args["-FailureOutput"]).write_text(json.dumps(receipt))
            return code

        data = json.loads(Path(args["-Plan"]).read_text())
        if stage == "Inspect":
            if failure == "selected_incompatible":
                return 1
            result = {
                "qualified_sources": "interra-realty,newmark",
                "task_baseline": data["release"]["task_baseline"]
                + [
                    {
                        "key": "enrich:newmark",
                        "task_path": "\\Agentic Assets\\CRE\\",
                        "task_name": "Enrichment - newmark",
                    }
                ],
                "snapshot": [],
                "digest": "a" * 64,
            }
        else:
            assert data["release"]["reviewed_fleet"] is True
            assert data["release"]["task_baseline"][-1] == {
                "key": "enrich:newmark",
                "task_path": "\\Agentic Assets\\CRE\\",
                "task_name": "Enrichment - newmark",
            }
        if stage == "Inspect":
            pass
        elif stage == "Hold":
            if failure == "hold_noop":
                return completed_failure()
            state["held"] = True
            if failure == "hold_unsettled_exit":
                return 1
            if failure == "hold_signaled":
                return completed_failure(-15)
            if failure == "hold_real_proxy_kill":
                folder = Path(
                    tempfile.mkdtemp(
                        prefix="cre5228-proxy-",
                        dir="/mnt/c/Users/CSEAGRAVES2018/AppData/Local/Temp",
                    )
                )
                ready, marker, parent = (
                    folder / "started",
                    folder / "finished",
                    folder / "parent.ps1",
                )

                def windows(path):
                    return subprocess.check_output(
                        ["wslpath", "-w", str(path)], text=True
                    ).strip()

                child = (
                    "Start-Sleep -Seconds 4; [IO.File]::WriteAllText('"
                    + windows(marker)
                    + "','done')"
                )
                encoded = base64.b64encode(child.encode("utf-16le")).decode()
                parent.write_text(
                    "$p=Start-Process -FilePath 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe' "
                    "-ArgumentList @('-NoProfile','-NonInteractive','-EncodedCommand','"
                    + encoded
                    + "') -WindowStyle Hidden -PassThru\n"
                    "[IO.File]::WriteAllText('"
                    + windows(ready)
                    + "',[string]$p.Id)\nStart-Sleep -Seconds 20\n"
                )
                proc = subprocess.Popen(
                    [
                        "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-File",
                        windows(parent),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                deadline = time.monotonic() + 15
                while (
                    not ready.exists()
                    and proc.poll() is None
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.1)
                assert ready.exists()
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)
                deadline = time.monotonic() + 12
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.1)
                assert marker.exists(), (
                    "synthetic native descendant did not survive proxy termination"
                )
                return proc.returncode
            if failure == "hold_timeout":
                raise TimeoutError("native completion unknown")
            if failure == "hold_partial":
                return completed_failure()
            result = {"stage": stage, "complete": True}
        elif stage == "VerifyHeld":
            state["held_checks"] += 1
            if failure == "advance_partial" and state["held_checks"] == 4:
                return 1
            assert state["held"]
            result = {"stage": stage, "complete": True}
        elif stage == "Repin":
            assert state["held"]
            state["pin"] = data["new_sha"]
            if failure in {"repin_settled_failure", "repin_reconcile_failure"}:
                return completed_failure()
            if failure == "repin_unsettled":
                return 1
            result = {"stage": stage, "complete": True}
        elif stage == "CompleteRepin":
            assert state["held"]
            state["pin"] = data["new_sha"]
            if failure == "repin_reconcile_failure":
                return 1
            result = {"stage": stage, "complete": True}
        elif stage == "CompleteRestore":
            assert state["pin"] == data["new_sha"]
            state["held"] = False
            state["successor"] = True
            if failure == "restore_reconcile_failure":
                return 1
            result = {"stage": stage, "complete": True}
        elif stage == "Restore":
            state["held"] = False
            state["successor"] = True
            if failure == "restore_partial":
                return 1
            if failure in {"restore_settled_failure", "restore_reconcile_failure"}:
                return completed_failure()
            result = {"stage": stage, "complete": True}
        elif stage == "VerifyRestored":
            assert not state["held"] and state["pin"] == data["new_sha"]
            result = {"stage": stage, "complete": True}
        elif stage == "ReconcileHold":
            assert state["pin"] == old
            state["held"] = False
            result = {"stage": stage, "complete": True}
        elif stage == "VerifyOriginal":
            assert not state["held"] and state["pin"] == old
            result = {"stage": stage, "complete": True}
        else:
            raise AssertionError(stage)
        Path(args["-Output"]).write_text(json.dumps(result))
        return 0

    fake_probe = types.ModuleType("firecrawl_operator_handoff")

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def get_json(self, path):
            assert path == "/"
            state["api_reads"] += 1
            return {"message": "other" if failure == "api_before" else "Firecrawl API"}

    fake_probe.ReadOnlyLoopbackClient = Client
    fake_probe.idle_snapshot = lambda client: {}
    monkeypatch.setitem(sys.modules, "firecrawl_operator_handoff", fake_probe)
    monkeypatch.setattr(subprocess, "run", fake_subprocess)
    if failure != "real_logged_native":
        monkeypatch.setattr(checkpoint, "_run_logged_command", fake_native)
    monkeypatch.setattr(handoff, "visible_linux_frontends", list)
    monkeypatch.setattr(update, "installed_dependencies", lambda root: {})
    monkeypatch.setattr(update, "windows_path", lambda value: str(value))
    monkeypatch.setattr(image_helpers, "snapshot_container", lambda row: row)
    if failure.startswith("image_"):
        import cre_reviewed_update_images as direct
        import cre_runtime_maintenance_steps as steps

        state["image_calls"] = []
        state["idle_calls"] = 0

        def prepare(git_object, source, identity, affected, docker_path, native_path, override, output):
            state["image_calls"].append("build")
            if failure == "image_build_failure":
                raise RuntimeError("synthetic image build failure")
            assert affected == ({"api", "playwright-service"} if failure == "image_mixed" else {"api"})
            images = {"api": {"image": "fixture", "tag": "fixture-tag"}}
            if failure == "image_mixed":
                images["playwright-service"] = {"image": "unchanged", "tag": "other-tag"}
            return ({"selected": identity, "apply_services": [] if failure == "image_noop" else ["api"],
                     "images": images, "previous_images": {"api": {"ref": "prior", "image": "old-image"}},
                     "compose_stack": {"files": ["C:/base.yaml"], "sha256": ["c" * 64]},
                     "override_path": "C:/stable.json", "override_sha256": hashlib.sha256(b"override").hexdigest(),
                     "docker_sha256": "d" * 64}, b"override")

        def recheck(*args):
            state["image_calls"].append("recheck")
            if failure == "image_drift_prehold":
                raise RuntimeError("synthetic image drift")

        def idle(*args):
            state["idle_calls"] += 1
            if failure == "image_idle_after_hold" and state["idle_calls"] == 2:
                raise RuntimeError("synthetic queue activity")

        def install(*args):
            state["image_calls"].append("install")
            assert state["held"] and git(runtime, "rev-parse", "HEAD") == old

        def compose(*args):
            state["image_calls"].append("compose")
            if failure in {"image_completed_failure", "image_rollback_unknown"} and state["image_calls"].count("compose") == 1:
                raise direct.ComposeCompletedFailure("synthetic completed failure")
            if failure == "image_rollback_unknown" and state["image_calls"].count("compose") == 2:
                raise RuntimeError("synthetic unknown rollback outcome")
            if failure == "image_unknown":
                raise RuntimeError("synthetic unknown outcome")

        def verify(*args):
            state["image_calls"].append("verify")
            return {"images": {"unchanged": ["api"] if failure == "image_noop" else [],
                               "replaced": [] if failure == "image_noop" else ["api"]},
                    "containers": {"api": "running"}}

        monkeypatch.setattr(direct, "prepare_image_plan", prepare)
        monkeypatch.setattr(direct, "recheck_image_plan", recheck)
        monkeypatch.setattr(direct, "assert_image_idle", idle)
        monkeypatch.setattr(direct, "install_image_override", install)
        monkeypatch.setattr(direct, "run_current_compose", compose)
        monkeypatch.setattr(direct, "verify_image_result", verify)
        def ready(*args):
            if failure in {"image_unready", "image_unready_rollback_degraded"}:
                raise direct.SelectedImageUnready("synthetic readiness timeout")
            return verify(*args)
        monkeypatch.setattr(direct, "wait_for_selected_images", ready)
        monkeypatch.setattr(direct, "restore_image_override", lambda *args: state["image_calls"].append("restore"))
        monkeypatch.setattr(direct, "qualify_settled_partial_fleet", lambda *args: state["image_calls"].append("qualify-partial"))
        def original(*args):
            state["image_calls"].append("verify-original")
            return {"containers": {"api": "running"}}
        monkeypatch.setattr(direct, "verify_original_images", original)
        def readiness(plan, docker, native, client, owner, observe):
            result = observe()
            if failure in {"image_unready_rollback_degraded", "image_degraded_after_advance"}:
                return None
            return result
        monkeypatch.setattr(direct, "wait_for_service_readiness", readiness)
        def compose_argv(*args, **kwargs):
            assert args[4] == {"api"}
            return ["docker", "fixture"]
        monkeypatch.setattr(steps, "current_compose_argv", compose_argv)
    lock = checkpoint.canonical_shared_lock_dir(runtime)
    args = {
        "development": development,
        "native": native,
        "runtime": runtime,
        "native_git": Path("/usr/bin/git"),
        "powershell": Path("/usr/bin/true"),
        "docker": docker,
    }
    if failure == "real_logged_native":
        executable = tmp_path / "synthetic-native"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv\n"
            "stage = args[args.index('-Stage') + 1]\n"
            "plan = json.loads(Path(args[args.index('-Plan') + 1]).read_text())\n"
            "fleet = plan['release']['task_baseline']\n"
            "extra = {'key': 'enrich:newmark', 'task_path': chr(92) + 'Agentic Assets' + chr(92) + 'CRE' + chr(92), 'task_name': 'Enrichment - newmark'}\n"
            "if stage != 'Inspect': assert fleet[-1] == extra and plan['release']['reviewed_fleet'] is True\n"
            "result = {'qualified_sources': 'interra-realty,newmark', 'task_baseline': fleet + [extra], 'snapshot': [], 'digest': 'a' * 64} if stage == 'Inspect' else {'stage': stage, 'complete': True}\n"
            "Path(args[args.index('-Output') + 1]).write_text(json.dumps(result))\n"
            "print('synthetic native ' + stage)\n"
        )
        executable.chmod(0o755)
        args["powershell"] = executable
    if failure in {"already_current", "real_logged_native"}:
        result = update.run_source_update(selected, **args)
        assert result["selected"] == selected
        if failure == "real_logged_native":
            logs = list((development / ".scratch").glob("reviewed-update-*/*.log"))
            assert len(logs) == 2
            assert all("synthetic native" in log.read_text() and "rc=0" in log.read_text() for log in logs)
            assert not list((development / ".scratch").rglob("*.performance.jsonl"))
            assert "CRE performance telemetry degraded" not in capsys.readouterr().err
        else:
            assert state["calls"] == ["Inspect", "VerifyRestored"]
        assert not state["held"] and not lock.exists()
    elif failure in {"native_current", "runtime_current"}:
        result = update.run_source_update(selected, **args)
        assert result["selected"] == selected
        assert git(native, "rev-parse", "HEAD") == selected
        assert git(runtime, "rev-parse", "HEAD") == selected
        assert state["successor"] and not lock.exists()
        assert ("Repin" in state["calls"]) == (failure == "native_current")
    elif failure == "image_noop":
        result = update.run_source_update(selected, **args)
        assert result["image_changes_applied"] is False
        assert state["idle_calls"] == 0 and "install" not in state["image_calls"] and "compose" not in state["image_calls"]
        assert state["successor"] and not lock.exists()
    elif failure in {"image_success", "image_mixed", "image_lock_success"}:
        result = update.run_source_update(selected, **args)
        assert result["image_changes_applied"] is True
        assert state["successor"] and not lock.exists()
        assert state["image_calls"].index("build") < state["image_calls"].index("install")
        assert state["image_calls"].index("install") < state["image_calls"].index("compose")
        assert state["image_calls"].count("verify") >= 2
    elif failure == "image_rollback_unknown":
        with pytest.raises(RuntimeError, match="unknown rollback outcome"):
            update.run_source_update(selected, **args)
        assert lock.exists() and state["held"]
        assert git(native, "rev-parse", "HEAD") == old and git(runtime, "rev-parse", "HEAD") == old
        assert "ReconcileHold" not in state["calls"] and "verify-original" not in state["image_calls"]
    elif failure == "image_degraded_after_advance":
        result = update.run_source_update(selected, **args)
        assert result["status"] == "completed_with_warnings" and result["image_readiness"] == "degraded"
        assert state["successor"] and not lock.exists()
    elif failure in {"image_completed_failure", "image_unready", "image_unready_rollback_degraded"}:
        with pytest.raises(RuntimeError):
            update.run_source_update(selected, **args)
        assert not lock.exists() and not state["held"]
        assert git(native, "rev-parse", "HEAD") == old and git(runtime, "rev-parse", "HEAD") == old
        assert state["image_calls"].count("compose") == 2
        assert "restore" in state["image_calls"] and "verify-original" in state["image_calls"]
        assert "ReconcileHold" in state["calls"]
    elif failure in {"image_build_failure", "image_drift_prehold", "image_idle_after_hold"}:
        with pytest.raises(RuntimeError):
            update.run_source_update(selected, **args)
        assert not lock.exists()
        assert git(native, "rev-parse", "HEAD") == old
        assert git(runtime, "rev-parse", "HEAD") == old
        if failure == "image_idle_after_hold":
            assert "ReconcileHold" in state["calls"] and not state["held"]
        else:
            assert state["calls"] == []
    elif failure in {"nuq_change", "compose_change"}:
        with pytest.raises(ValueError):
            update.run_source_update(selected, **args)
        assert not lock.exists() and state["calls"] == []
        assert git(native, "rev-parse", "HEAD") == old
        assert git(runtime, "rev-parse", "HEAD") == old
    elif failure in {"dependency_change", "collector_dependency_change"}:
        with pytest.raises(ValueError, match="not yet supported"):
            update.run_source_update(selected, **args)
        assert not lock.exists()
        assert state["calls"] == []
        assert git(native, "rev-parse", "HEAD") == old
        assert git(runtime, "rev-parse", "HEAD") == old
    elif failure in {"repin_settled_failure", "restore_settled_failure"}:
        result = update.run_source_update(selected, **args)
        assert result["selected"] == selected
        assert result["status"] == "completed"
        assert result["reconciled_after"] == (
            "repin" if failure == "repin_settled_failure" else "restore"
        )
    elif failure == "ignored_collision":
        with pytest.raises(RuntimeError):
            update.run_source_update(selected, **args)
        assert not lock.exists() and state["calls"] == []
        assert (native / "blocked").read_text() == "preserved local work"
        assert git(native, "rev-parse", "HEAD") == old
        assert git(runtime, "rev-parse", "HEAD") == old
    elif failure == "hold_timeout":
        with pytest.raises(TimeoutError):
            update.run_source_update(selected, **args)
    elif failure in {
        "health_before",
        "health_after_advance",
        "api_before",
        "browser_after_advance",
        "docker_unavailable",
    }:
        result = update.run_source_update(selected, **args)
        assert state["successor"] and not lock.exists()
        if failure == "health_before":
            assert (
                result["service_health_before"]["containers"]["nuq-postgres"]
                == "unhealthy"
            )
        if failure == "health_after_advance":
            assert (
                result["service_health_after_advance"]["containers"]["nuq-postgres"]
                == "unhealthy"
            )
            assert (
                result["service_health_after"]["containers"]["nuq-postgres"]
                == "healthy"
            )
        if failure == "api_before":
            assert result["service_health_before"]["api"] == "unexpected-response"
        if failure == "browser_after_advance":
            assert result["service_health_after_advance"]["browser"] == "unhealthy"
            assert result["service_health_after"]["browser"] == "healthy"
        if failure == "docker_unavailable":
            assert result["service_health_before"]["containers"] == "unknown"
            assert result["service_health_after"]["browser"] == "unknown"
    elif failure:
        with pytest.raises(RuntimeError):
            update.run_source_update(selected, **args)
    else:
        result = update.run_source_update(selected, **args)
        assert result["selected"] == selected
        assert result["existing_image_provenance"] == "not established"
        assert git(native, "rev-parse", "HEAD") == selected
        assert git(runtime, "rev-parse", "HEAD") == selected
        assert state["successor"] and not lock.exists()
    if failure == "selected_incompatible":
        assert not lock.exists() and state["calls"] == ["Inspect"]
        assert git(native, "rev-parse", "HEAD") == old
        assert git(runtime, "rev-parse", "HEAD") == old
    if failure in {
        "hold_timeout",
        "hold_unsettled_exit",
        "hold_signaled",
        "hold_real_proxy_kill",
    }:
        assert lock.exists() and state["held"]
        assert "ReconcileHold" not in state["calls"]
    if failure in {"hold_noop", "hold_partial"}:
        assert not state["held"] and not lock.exists()
        assert state["calls"][-2:] == ["ReconcileHold", "VerifyOriginal"]
    if failure == "image_unknown":
        assert lock.exists() and state["held"]
        assert "Repin" not in state["calls"] and "Restore" not in state["calls"]
    if failure == "advance_partial":
        assert lock.exists() and git(native, "rev-parse", "HEAD") == selected
        assert git(runtime, "rev-parse", "HEAD") == old
    if failure == "restore_partial":
        assert lock.exists() and state["successor"]
        assert state["calls"][-1] == "Restore"
    if failure == "repin_unsettled":
        assert lock.exists() and "CompleteRepin" not in state["calls"]
    if failure in {"repin_reconcile_failure", "restore_reconcile_failure"}:
        assert lock.exists()
        if failure == "restore_reconcile_failure":
            assert state["successor"]
    if failure in {"repin_settled_failure", "restore_settled_failure"}:
        assert not lock.exists() and state["successor"]
        assert state["calls"][-2:] == ["CompleteRestore", "VerifyRestored"]
        if failure == "repin_settled_failure":
            assert "CompleteRepin" in state["calls"]
