#!/usr/bin/env python3
"""WSL dispatcher for current-policy Windows series intents.

This WIP is not installed. Snapshot ingestion records unknown prior policy
history, and never establishes that a historical trigger was executed.
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
            "attempts", "deadline_seconds", "expected_sha", "env_file", "windows_launcher", "task_path", "task_name", "task_xml_sha256",
            "windows_launcher_sha256", "windows_repo", "distro", "linux_user", "alert_file", "notification_mode"}
    if not isinstance(policy, dict) or set(policy) != keys:
        raise SourcePolicyRefusal("dispatch policy fields differ")
    if type(continuation_depth) is not int or continuation_depth < 0:
        raise ValueError("unsupported continuation depth")
    pattern = r"(\d{8}T\d{6}Z)_[A-Za-z0-9_-]{1,96}" + (rf"\.continuation{continuation_depth}" if continuation_depth else "")
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
    task_binding = (policy["task_path"], policy["task_name"], policy["task_xml_sha256"])
    if task_binding != (None, None, None) and (
            policy["task_path"] != "\\Agentic Assets\\CRE\\"
            or not isinstance(policy["task_name"], str) or not policy["task_name"]
            or any(c in policy["task_name"] for c in "\\/\r\n")
            or not isinstance(policy["task_xml_sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", policy["task_xml_sha256"]) is None):
        raise SourcePolicyRefusal("invalid scheduled task binding")
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
            "continuation_depth": continuation_depth, "eligible_unix_ms": int(due.replace(tzinfo=timezone.utc).timestamp() * 1000),
            "predecessor_series_id": None, "predecessor_slot": None, "predecessor_policy": None,
            "predecessor_no_priority": False}


def source_policy(policy: dict, sources: list[str]) -> dict:
    """Project one current policy to a source subset without inventing options."""
    selected = series.parse_sources(",".join(policy["sources"]))
    if not sources or len(set(sources)) != len(sources) or not set(sources) <= set(selected):
        raise ValueError("continuation source scope differs")
    result = dict(policy)
    result["sources"] = [source for source in selected if source in sources]
    result["admissions"] = [source for source in policy["admissions"] if source in sources]
    result["daum_subset"] = policy["daum_subset"] and "daum-commercial" in sources
    if not result["daum_subset"]:
        result["daum_subset_policy"] = "tenure-only-v1"
    return result


def continuation_slot(previous_slot: str, sources: list[str], depth: int) -> str:
    prefix = previous_slot[:16]
    key = hashlib.sha256((previous_slot + "\0" + ",".join(sources)).encode()).hexdigest()[:24]
    return f"{prefix}_c{key}.continuation{depth}"


def validate_request(request: dict) -> None:
    """Rederive policy, due locator and predecessor before any real dispatch."""
    if not isinstance(request, dict):
        raise ValueError("dispatch intent differs")
    depth = request.get("continuation_depth")
    slot = request.get("slot")
    expected = policy_request(request.get("policy"), slot, continuation_depth=depth)
    if depth:
        prior = request.get("predecessor_slot")
        prior_policy = request.get("predecessor_policy")
        origin = policy_request(prior_policy, prior, continuation_depth=depth - 1)
        if source_policy(prior_policy, expected["config"]["sources"]) != request["policy"]:
            raise ValueError("continuation policy projection differs")
        if slot != continuation_slot(prior, expected["config"]["sources"], depth):
            raise ValueError("continuation slot differs")
        expected["predecessor_series_id"] = origin["series_id"]
        expected["predecessor_slot"] = prior
        expected["predecessor_policy"] = prior_policy
        if type(request.get("predecessor_no_priority")) is not bool:
            raise ValueError("dispatch predecessor outcome differs")
        expected["predecessor_no_priority"] = request["predecessor_no_priority"]
        eligible = request.get("eligible_unix_ms")
        if type(eligible) is not int or eligible < expected["due_unix_ms"]:
            raise ValueError("continuation eligibility differs")
        expected["eligible_unix_ms"] = eligible
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
    if request["predecessor_series_id"] is not None and not request["predecessor_no_priority"] and policy["pipeline"]:
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
    verify_predecessor(request)
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
    if policy["task_path"] is not None:
        for name in ("Invoke-CreDispatcherWake.ps1", "CreReviewedTaskUpdate.ps1", "CreCodeHandoff.ps1",
                     "CreTaskContract.ps1", "CreRestorationExecution.ps1", "CreRuntimeMaintenance.ps1"):
            files["windows/" + name] = windows / name
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
    if policy["task_path"] is not None:
        from pathlib import PureWindowsPath
        helper = str(PureWindowsPath(policy["windows_launcher"]).with_name("Invoke-CreDispatcherWake.ps1"))
        try:
            subprocess.run(["/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", "-NoProfile",
                "-NonInteractive", "-File", helper, "-VerifyTaskPath", policy["task_path"],
                "-VerifyTaskName", policy["task_name"], "-ExpectedTaskSha256", policy["task_xml_sha256"]],
                check=True, capture_output=True, timeout=30)
        except subprocess.CalledProcessError as exc:
            hold_local(exc)
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


def qualify(request: dict, *, validate=True) -> dict:
    """Require an exact controlled receipt and existing write-fence checks."""
    policy = request["policy"]
    if refresh.database_target_fingerprint(policy["env_file"]) != request["target"]:
        raise ValueError("database target changed during dispatch")
    if validate:
        validate_request(request)
    root = HERE / "out" / "checkpoint-series"
    manifest = json.loads((root / request["series_id"] / "manifest.json").read_text())
    invocation = manifest["latest_invocation_control"]["invocation_id"]
    with refresh.SharedLock(refresh.canonical_shared_lock_dir()) as owner:
        return dispatch_settlement.qualify(root, request["series_id"], invocation, owner=owner,
            expected_sha=policy["expected_sha"], expected_target=request["target"],
            expected_config=request["config"], expected_deadline_seconds=policy["deadline_seconds"])


def verify_predecessor(request: dict) -> None:
    """Requalify the exact prior generation before any projected retry."""
    if request["continuation_depth"] == 0:
        return
    depth = request["continuation_depth"]
    origin = policy_request(request["predecessor_policy"], request["predecessor_slot"], continuation_depth=depth - 1)
    if origin["series_id"] != request["predecessor_series_id"]:
        raise ValueError("predecessor identity differs")
    previous = qualify(origin, validate=False)
    if (previous.get("outcome") not in {"qualified", "settled_no_work"}
            or not set(request["config"]["sources"]) <= set(previous.get("unfinished_sources") or [])
            or request["predecessor_no_priority"] != (
                previous.get("status") != "deadline_interrupted" or previous.get("outcome") == "settled_no_work")):
        raise ValueError("predecessor did not requalify")


def successor(slot: str, request: dict, result: dict):
    """Queue immediate peers and separately deferred sources from settled work."""
    unfinished = result.get("unfinished_sources")
    if not unfinished:
        return None
    deferred = result.get("deferred_sources") or []
    if (not isinstance(unfinished, list) or len(set(unfinished)) != len(unfinished)
            or not set(unfinished) <= set(request["config"]["sources"])
            or not isinstance(deferred, list) or len(set(deferred)) != len(deferred)
            or not set(deferred) <= set(unfinished)
            or type(result.get("finished_unix_ms")) is not int):
        raise ValueError("continuation settlement differs")
    depth = request["continuation_depth"]
    active = [source for source in unfinished if source not in deferred]
    groups = ([active] if active else []) + [[source] for source in deferred]
    pending = []
    for sources in groups:
        policy = source_policy(request["policy"], sources)
        next_slot = continuation_slot(slot, sources, depth + 1)
        next_request = policy_request(policy, next_slot, continuation_depth=depth + 1)
        if next_request["target"] != request["target"]:
            raise ValueError("continuation target changed")
        next_request["predecessor_series_id"] = request["series_id"]
        next_request["predecessor_slot"] = slot
        next_request["predecessor_policy"] = request["policy"]
        next_request["predecessor_no_priority"] = (
            result.get("status") != "deadline_interrupted" or result.get("outcome") == "settled_no_work")
        delay = 300_000 if sources[0] in deferred else 0
        next_request["eligible_unix_ms"] = max(next_request["due_unix_ms"], result["finished_unix_ms"] + delay)
        pending.append((next_slot, next_request))
    return pending


def enqueue_current_snapshot(path: Path, snapshot: dict) -> None:
    """Queue current-policy due slots; never replay an unverified old policy."""
    if (not isinstance(snapshot, dict) or set(snapshot) != {"schema", "observed_unix_ms", "tasks"}
            or snapshot["schema"] != "cre-dispatch-current-snapshot/v1"
            or type(snapshot["observed_unix_ms"]) is not int or snapshot["observed_unix_ms"] < 0
            or not isinstance(snapshot["tasks"], list)):
        raise ValueError("current task snapshot differs")
    seen = set()
    from datetime import datetime, timezone
    for task in snapshot["tasks"]:
        if (not isinstance(task, dict) or set(task) != {"task", "policy", "due"}
                or not isinstance(task["task"], str) or re.fullmatch(r"[A-Za-z0-9_-]{1,72}", task["task"]) is None
                or task["task"] in seen or not isinstance(task["policy"], dict)
                or not isinstance(task["due"], list)):
            raise ValueError("current task entry differs")
        seen.add(task["task"])
        raw = json.dumps(task["policy"], sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        digest = hashlib.sha256(raw).hexdigest()
        dispatch_queue.record_unknown_history(path, task["task"], digest, snapshot["observed_unix_ms"])
        for due in task["due"]:
            if not isinstance(due, str) or re.fullmatch(r"\d{8}T\d{6}Z", due) is None:
                raise ValueError("current task due time differs")
            timestamp = datetime.strptime(due, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            if int(timestamp.timestamp() * 1000) > snapshot["observed_unix_ms"]:
                raise ValueError("current task due time is in the future")
            slot = f"{due}_{task['task']}_{digest[:12]}"
            try:
                if any(task["policy"].get(key) is None for key in ("task_path", "task_name", "task_xml_sha256")):
                    raise SourcePolicyRefusal("current task lacks an exact action binding")
                request = policy_request(task["policy"], slot)
            except SourcePolicyRefusal:
                dispatch_queue.hold_invalid_policy(path, slot, digest)
            else:
                dispatch_queue.enqueue(path, slot, request)


def main(argv=None) -> int:
    if not sys.platform.startswith("linux"):
        raise ValueError("this phase requires the WSL/Linux series control contract")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot")
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--wake", action="store_true", help="drain already recorded due intents")
    parser.add_argument("--wake-snapshot", action="store_true", help="enqueue current-task snapshot from stdin and wake")
    parser.add_argument("--snapshot-only", action="store_true", help="enqueue current-task snapshot without owning the drain")
    args = parser.parse_args(argv)
    waking = args.wake or args.wake_snapshot or args.snapshot_only
    if sum((args.wake, args.wake_snapshot, args.snapshot_only)) > 1:
        parser.error("choose one wake mode")
    if waking and (args.slot is not None or args.policy is not None):
        parser.error("wake cannot submit a new intent")
    if not waking and (args.slot is None or args.policy is None):
        parser.error("submission requires slot and policy")
    if not waking and re.fullmatch(r"\d{8}T\d{6}Z_[A-Za-z0-9_-]{1,96}", args.slot) is None:
        raise ValueError("slot must bind a UTC due time and task identity")
    from datetime import datetime
    if not waking:
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
    if args.wake_snapshot or args.snapshot_only:
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("current task snapshot is too large")
        enqueue_current_snapshot(path, json.loads(raw))
        if args.snapshot_only:
            return 0
    if waking:
        dispatch_queue.drain(path, prepare=prepare_launch, launch=launch, qualify=qualify,
                             successor=successor, wait=False)
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
