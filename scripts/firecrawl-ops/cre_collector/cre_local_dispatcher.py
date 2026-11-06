#!/usr/bin/env python3
"""Bounded WSL dispatcher for explicitly submitted Windows series intents.

This entry point is not installed as a task. It does not reconstruct missed
scheduler triggers or preempt an active series for a later due opportunity.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series
from cre_stop_guarded_tier import _safe_stat
from scheduling import dispatch_queue, dispatch_settlement

HERE = Path(__file__).resolve().parent


class SourcePolicyRefusal(ValueError):
    """This due slot's source or launcher policy is invalid before dispatch."""


def policy_request(policy: dict, slot: str, *, continuation_depth: int = 0) -> dict:
    """Build both launcher arguments and expected policy from one input."""
    keys = {"pipeline", "sources", "admissions", "daum_subset", "daum_subset_policy", "concurrency", "workers", "page_cap",
            "attempts", "deadline_seconds", "expected_sha", "env_file", "windows_launcher",
            "windows_launcher_sha256", "windows_repo", "distro", "linux_user", "alert_file", "notification_mode"}
    if not isinstance(policy, dict) or set(policy) != keys:
        raise SourcePolicyRefusal("dispatch policy fields differ")
    if type(continuation_depth) is not int or continuation_depth not in {0, 1}:
        raise ValueError("unsupported continuation depth")
    pattern = r"(\d{8}T\d{6}Z)_[A-Za-z0-9_-]{1,96}" + (r"\.continuation1" if continuation_depth else "")
    match = re.fullmatch(pattern, slot)
    if match is None:
        raise ValueError("slot must bind a UTC due time and task identity")
    from datetime import datetime, timezone
    due = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
    raw_sources = policy["sources"]
    admissions = policy["admissions"]
    if (not isinstance(raw_sources, list) or not raw_sources or not isinstance(admissions, list)
            or any(not isinstance(value, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]*", value) is None
                   for value in [*raw_sources, *admissions])
            or len(set(raw_sources)) != len(raw_sources) or len(set(admissions)) != len(admissions)):
        raise SourcePolicyRefusal("invalid source policy")
    if type(policy["pipeline"]) is not bool:
        raise SourcePolicyRefusal("invalid pipeline policy")
    if not policy["pipeline"] and (len(raw_sources) != 1 or raw_sources == ["all"]
            or policy["workers"] != 1 or len(admissions) > 1):
        raise SourcePolicyRefusal("serial policy requires one source and worker")
    if type(policy["daum_subset"]) is not bool:
        raise SourcePolicyRefusal("invalid DAUM policy")
    if (not isinstance(policy["daum_subset_policy"], str)
            or policy["daum_subset_policy"] not in {"tenure-only-v1", "tenure-and-empty-title-v2"}
            or (not policy["daum_subset"] and policy["daum_subset_policy"] != "tenure-only-v1")):
        raise SourcePolicyRefusal("invalid DAUM subset version")
    for key, low, high in (("concurrency", 1, 6), ("workers", 1, 4), ("page_cap", 0, 2147483647),
                           ("attempts", 1, 2147483647), ("deadline_seconds", 30, 86400)):
        if type(policy[key]) is not int or not low <= policy[key] <= high:
            raise SourcePolicyRefusal("invalid bounded series policy")
    if not isinstance(policy["expected_sha"], str) or re.fullmatch(r"[a-f0-9]{40}", policy["expected_sha"]) is None:
        raise SourcePolicyRefusal("invalid expected revision")
    if not isinstance(policy["windows_launcher_sha256"], str) or re.fullmatch(r"[a-f0-9]{64}", policy["windows_launcher_sha256"]) is None:
        raise SourcePolicyRefusal("invalid launcher hash")
    if (not isinstance(policy["env_file"], str) or not policy["env_file"].startswith("/")
            or policy["windows_repo"] != str(refresh.REPO_ROOT)
            or not isinstance(policy["windows_launcher"], str)
            or not re.fullmatch(r"[A-Za-z]:\\[^\r\n]+\\Invoke-CreGuardedTier\.ps1", policy["windows_launcher"])
            or not isinstance(policy["distro"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", policy["distro"])
            or not isinstance(policy["linux_user"], str) or not re.fullmatch(r"[a-z_][a-z0-9_-]*", policy["linux_user"])
            or not isinstance(policy["notification_mode"], str)
            or policy["notification_mode"] not in {"Failure", "StartFinish"}
            or not (policy["alert_file"] is None or isinstance(policy["alert_file"], str))):
        raise SourcePolicyRefusal("invalid launcher custody")
    # Resolve and validate the same source and admission rules as the real series.
    try:
        sources = series.parse_sources(",".join(raw_sources))
        recovery = series.RecoveryConfig(max_recoveries_per_source=0)
        config = series.series_config(sources=sources, page_cap=policy["page_cap"], concurrency=policy["concurrency"],
            attempts_per_source=policy["attempts"], max_resume_age_hours=series.DEFAULT_MAX_RESUME_AGE_HOURS,
            max_host_cpu_percent=90, cpu_sustain_seconds=30, cpu_sample_seconds=2,
            memory_sustain_seconds=20, memory_sample_seconds=2, nice=15, recovery=recovery,
            admit_current_inventory_source=admissions, admit_daum_verified_subset=policy["daum_subset"],
            daum_subset_policy=policy["daum_subset_policy"],
            pipeline=policy["pipeline"], source_workers=policy["workers"])
    except (ValueError, TypeError, KeyError) as exc:
        raise SourcePolicyRefusal("source or admission policy refused") from exc
    target = refresh.database_target_fingerprint(policy["env_file"])
    series_id = due.strftime("%Y-%m-%dT%H%M%SZ") + "-" + hashlib.sha256(slot.encode()).hexdigest()[:12]
    return {"slot": slot, "policy": policy, "config": config, "target": target, "series_id": series_id,
            "due_unix_ms": int(due.replace(tzinfo=timezone.utc).timestamp() * 1000),
            "continuation_depth": continuation_depth, "predecessor_series_id": None,
            "predecessor_no_work": False}


def validate_request(request: dict) -> None:
    """Rederive policy, due locator and predecessor before any real dispatch."""
    if not isinstance(request, dict):
        raise ValueError("dispatch intent differs")
    depth = request.get("continuation_depth")
    slot = request.get("slot")
    expected = policy_request(request.get("policy"), slot, continuation_depth=depth)
    if depth == 1:
        origin = policy_request(request["policy"], slot.removesuffix(".continuation1"))
        expected["predecessor_series_id"] = origin["series_id"]
        if type(request.get("predecessor_no_work")) is not bool:
            raise ValueError("dispatch predecessor outcome differs")
        expected["predecessor_no_work"] = request["predecessor_no_work"]
    if json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False) != json.dumps(
            expected, sort_keys=True, separators=(",", ":"), allow_nan=False):
        raise ValueError("dispatch intent differs")


def launcher_arguments(request: dict) -> list[str]:
    policy = request["policy"]
    selection = (["-Source", policy["sources"][0]] if not policy["pipeline"] or policy["sources"] == ["all"] else
                 ["-Sources", ",".join(policy["sources"])])
    argv = ["/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", "-NoProfile", "-NonInteractive", "-File", policy["windows_launcher"],
        "-Tier", "series", *(["-Pipeline"] if policy["pipeline"] else []), *selection,
        "-Concurrency", str(policy["concurrency"]), *(["-SourceWorkers", str(policy["workers"])] if policy["pipeline"] else []),
        "-PageCap", str(policy["page_cap"]), "-AttemptsPerSource", str(policy["attempts"]),
        "-ExpectedSha", policy["expected_sha"], "-FreshSeriesId", request["series_id"],
        "-ExpectedTargetSha256", request["target"]["value"],
        "-WorkDeadlineSeconds", str(policy["deadline_seconds"]), "-AcknowledgeDatabaseWrites",
        "-LinuxRepo", policy["windows_repo"], "-LinuxEnvFile", policy["env_file"],
        "-Distro", policy["distro"], "-LinuxUser", policy["linux_user"], "-NotificationMode", policy["notification_mode"]]
    if policy["admissions"]:
        argv += (["-AdmitCurrentInventorySources", ",".join(policy["admissions"])] if policy["pipeline"]
                 else ["-AdmitCurrentInventorySource", policy["admissions"][0]])
    if policy["daum_subset"]:
        argv += ["-AdmitDaumVerifiedSubset", "-DaumSubsetPolicy", policy["daum_subset_policy"]]
    if policy["alert_file"] is not None:
        argv += ["-AlertWebhookFile", policy["alert_file"]]
    if request["predecessor_series_id"] is not None and not request["predecessor_no_work"]:
        argv += ["-PriorityFromSeriesId", request["predecessor_series_id"]]
    return argv


def prepare_launch(request: dict) -> list[str]:
    """Perform only read-only checks before any native process is started."""
    policy = request["policy"]
    if refresh.database_target_fingerprint(policy["env_file"]) != request["target"]:
        raise ValueError("database target changed before dispatch")

    def hold_local(exc):
        # A target change remains a shared stop, even when discovered while
        # rederiving a source-specific policy or launcher path.
        if refresh.database_target_fingerprint(policy["env_file"]) != request["target"]:
            raise ValueError("database target changed before dispatch") from exc
        raise dispatch_queue.LocalPreflightRefused("source-specific preflight refused") from exc

    try:
        validate_request(request)
    except (ValueError, TypeError, KeyError) as exc:
        hold_local(exc)
    verify_no_work_predecessor(request)
    # The OS executes the exact pinned launcher; never evaluate shell text.
    try:
        local = subprocess.run(["wslpath", "-u", policy["windows_launcher"]], check=True,
                               capture_output=True, text=True, timeout=10).stdout.strip()
    except subprocess.CalledProcessError as exc:
        hold_local(exc)
    launcher = Path(local)
    windows = launcher.parent
    files = {
        "windows/Invoke-CreGuardedTier.ps1": launcher,
        "windows/CreNativeCapture.cs": windows / "CreNativeCapture.cs",
        "windows/CreWindowsNotification.cs": windows / "CreWindowsNotification.cs",
        "windows/Test-CreNotificationConfig.ps1": windows / "Test-CreNotificationConfig.ps1",
        "data/cre-source-policy.json": windows.parent / "data/cre-source-policy.json",
        "data/cre-buildout-sources.json": windows.parent / "data/cre-buildout-sources.json",
    }
    for relative, observed in files.items():
        try:
            expected = subprocess.run(["git", "show", f"{policy['expected_sha']}:scripts/firecrawl-ops/cre_collector/{relative}"],
                cwd=refresh.REPO_ROOT, check=True, capture_output=True, timeout=10).stdout
        except subprocess.CalledProcessError as exc:
            hold_local(exc)
        try:
            raw = observed.read_bytes()
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError) as exc:
            hold_local(exc)
        if raw != expected or (observed == launcher and hashlib.sha256(raw).hexdigest() != policy["windows_launcher_sha256"]):
            hold_local(ValueError("Windows launcher custody changed before dispatch"))
    return launcher_arguments(request)


def launch(request: dict, argv: list[str]) -> dict | None:
    """Invoke the existing native launcher; it performs fresh Windows admission."""
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        # With shell=False and no pre-exec hook, an exec error means the native
        # launcher image did not start. All later failures remain uncertain.
        return {"outcome": "settled_no_work", "status": "native_start_failed", "scope": "shared"}
    stdout, _stderr = process.communicate()
    return native_no_work_result(process.returncode, stdout, request)


def native_no_work_result(returncode: int, stdout: str, request: dict) -> dict | None:
    """Accept only a final marker bound to this completed native process."""
    policy = request["policy"]
    markers = [line.removeprefix("CRE_DISPATCH_NO_WORK:") for line in stdout.splitlines()
               if line.startswith("CRE_DISPATCH_NO_WORK:")]
    if returncode != 0 and len(markers) == 1:
        try:
            marker = json.loads(markers[0])
        except json.JSONDecodeError:
            marker = None
        expected = {"schema": "cre-dispatch-no-work/v1", "seriesId": request["series_id"],
                    "expectedSha": policy["expected_sha"], "targetSha256": request["target"]["value"],
                    "source": ",".join(policy["sources"])}
        if (isinstance(marker, dict) and set(marker) == set(expected) | {"reason"}
                and all(marker.get(key) == value for key, value in expected.items())
                and marker.get("reason") in {"local_api_not_ready_no_dispatch",
                    "startup_admission_deadline_no_dispatch", "notification_config_refused_no_dispatch",
                    "admission_refused"}):
            return {"outcome": "settled_no_work", "status": "native_refused", "reason": marker["reason"],
                    "scope": ("source" if marker["reason"] == "notification_config_refused_no_dispatch" else "shared")}


def qualify(request: dict) -> dict:
    """Require an exact controlled receipt and existing write-fence checks."""
    policy = request["policy"]
    if refresh.database_target_fingerprint(policy["env_file"]) != request["target"]:
        raise ValueError("database target changed during dispatch")
    validate_request(request)
    root = HERE / "out" / "checkpoint-series"
    manifest = json.loads((root / request["series_id"] / "manifest.json").read_text())
    invocation = manifest["latest_invocation_control"]["invocation_id"]
    with refresh.SharedLock(refresh.canonical_shared_lock_dir()) as owner:
        return dispatch_settlement.qualify(root, request["series_id"], invocation, owner=owner,
            expected_sha=policy["expected_sha"], expected_target=request["target"],
            expected_config=request["config"], expected_deadline_seconds=policy["deadline_seconds"])


def verify_no_work_predecessor(request: dict) -> None:
    """Only a requalified deadline with no child may omit predecessor ordering."""
    if not request.get("predecessor_no_work"):
        return
    origin = policy_request(request["policy"], request["slot"].removesuffix(".continuation1"))
    if origin["series_id"] != request["predecessor_series_id"]:
        raise ValueError("no-work predecessor identity differs")
    previous = qualify(origin)
    if previous.get("status") != "deadline_interrupted" or previous.get("outcome") != "settled_no_work":
        raise ValueError("no-work predecessor did not requalify")


def successor(slot: str, request: dict, result: dict):
    """One bounded follow-on; pending scheduled peers precede it."""
    if (result.get("status") == "deadline_interrupted" and result.get("unfinished_sources")
            and request["continuation_depth"] == 1):
        result["continuation_limit_reached"] = True
        return None
    if (result.get("status") != "deadline_interrupted"
            or not result.get("unfinished_sources") or request["continuation_depth"] != 0):
        return None
    next_slot = slot + ".continuation1"
    next_request = policy_request(request["policy"], next_slot, continuation_depth=1)
    if next_request["target"] != request["target"] or next_request["config"] != request["config"]:
        raise ValueError("continuation target or source policy changed")
    next_request["predecessor_series_id"] = request["series_id"]
    next_request["predecessor_no_work"] = result.get("outcome") == "settled_no_work"
    return next_slot, next_request


def main(argv=None) -> int:
    if not sys.platform.startswith("linux"):
        raise ValueError("this phase requires the WSL/Linux series control contract")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot")
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--wake", action="store_true", help="drain already recorded due intents")
    args = parser.parse_args(argv)
    if args.wake and (args.slot is not None or args.policy is not None):
        parser.error("wake cannot submit a new intent")
    if not args.wake and (args.slot is None or args.policy is None):
        parser.error("submission requires slot and policy")
    if not args.wake and re.fullmatch(r"\d{8}T\d{6}Z_[A-Za-z0-9_-]{1,96}", args.slot) is None:
        raise ValueError("slot must bind a UTC due time and task identity")
    from datetime import datetime
    if not args.wake:
        datetime.strptime(args.slot[:16], "%Y%m%dT%H%M%SZ")
    parent = HERE / "out"
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _safe_stat(parent_fd, directory=True)
        try:
            os.mkdir("local-dispatch", 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError:
            pass
    finally:
        os.close(parent_fd)
    path = parent / "local-dispatch"
    if args.wake:
        dispatch_queue.drain(path, prepare=prepare_launch, launch=launch, qualify=qualify, successor=successor)
        return 0
    try:
        raw_policy = args.policy.read_bytes()
    except OSError:
        dispatch_queue.hold_invalid_policy(path, args.slot, None, reason="source_policy_unreadable")
    else:
        digest = hashlib.sha256(raw_policy).hexdigest()
        try:
            request = policy_request(json.loads(raw_policy), args.slot)
        except (json.JSONDecodeError, UnicodeDecodeError, SourcePolicyRefusal):
            dispatch_queue.hold_invalid_policy(path, args.slot, digest)
        except Exception as exc:
            dispatch_queue.hold_invalid_policy(path, args.slot, digest, reason="shared_preflight_unavailable")
            raise dispatch_queue.DispatchRefused("shared target preflight unavailable") from exc
        else:
            dispatch_queue.enqueue(path, args.slot, request)
    dispatch_queue.drain(path, prepare=prepare_launch, launch=launch, qualify=qualify, successor=successor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
