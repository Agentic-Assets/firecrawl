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


def policy_request(policy: dict, slot: str) -> dict:
    """Build both launcher arguments and expected policy from one input."""
    keys = {"sources", "admissions", "daum_subset", "concurrency", "workers", "page_cap",
            "attempts", "deadline_seconds", "expected_sha", "env_file", "windows_launcher",
            "windows_launcher_sha256", "windows_repo", "distro", "linux_user", "alert_file", "notification_mode"}
    if not isinstance(policy, dict) or set(policy) != keys:
        raise ValueError("dispatch policy fields differ")
    match = re.fullmatch(r"(\d{8}T\d{6}Z)_[A-Za-z0-9_-]{1,96}", slot)
    if match is None:
        raise ValueError("slot must bind a UTC due time and task identity")
    from datetime import datetime
    due = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
    raw_sources = policy["sources"]
    admissions = policy["admissions"]
    if (not isinstance(raw_sources, list) or not raw_sources or not isinstance(admissions, list)
            or any(not isinstance(value, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]*", value) is None
                   for value in [*raw_sources, *admissions])
            or len(set(raw_sources)) != len(raw_sources) or len(set(admissions)) != len(admissions)):
        raise ValueError("invalid source policy")
    if type(policy["daum_subset"]) is not bool:
        raise ValueError("invalid DAUM policy")
    for key, low, high in (("concurrency", 1, 6), ("workers", 1, 4), ("page_cap", 0, 2147483647),
                           ("attempts", 1, 2147483647), ("deadline_seconds", 30, 86400)):
        if type(policy[key]) is not int or not low <= policy[key] <= high:
            raise ValueError("invalid bounded series policy")
    if re.fullmatch(r"[a-f0-9]{40}", policy["expected_sha"] or "") is None:
        raise ValueError("invalid expected revision")
    if re.fullmatch(r"[a-f0-9]{64}", policy["windows_launcher_sha256"] or "") is None:
        raise ValueError("invalid launcher hash")
    if (not isinstance(policy["env_file"], str) or not policy["env_file"].startswith("/")
            or policy["windows_repo"] != str(refresh.REPO_ROOT)
            or not isinstance(policy["windows_launcher"], str)
            or not re.fullmatch(r"[A-Za-z]:\\[^\r\n]+\\Invoke-CreGuardedTier\.ps1", policy["windows_launcher"])
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", policy["distro"] or "")
            or not re.fullmatch(r"[a-z_][a-z0-9_-]*", policy["linux_user"] or "")
            or policy["notification_mode"] not in {"Failure", "StartFinish"}
            or not (policy["alert_file"] is None or isinstance(policy["alert_file"], str))):
        raise ValueError("invalid launcher custody")
    # Resolve and validate the same source and admission rules as the real series.
    sources = series.parse_sources(",".join(raw_sources))
    recovery = series.RecoveryConfig(max_recoveries_per_source=0)
    config = series.series_config(sources=sources, page_cap=policy["page_cap"], concurrency=policy["concurrency"],
        attempts_per_source=policy["attempts"], max_resume_age_hours=series.DEFAULT_MAX_RESUME_AGE_HOURS,
        max_host_cpu_percent=90, cpu_sustain_seconds=30, cpu_sample_seconds=2,
        memory_sustain_seconds=20, memory_sample_seconds=2, nice=15, recovery=recovery,
        admit_current_inventory_source=admissions, admit_daum_verified_subset=policy["daum_subset"],
        daum_subset_policy="tenure-and-empty-title-v2" if policy["daum_subset"] else "tenure-only-v1",
        pipeline=True, source_workers=policy["workers"])
    target = refresh.database_target_fingerprint(policy["env_file"])
    series_id = due.strftime("%Y-%m-%dT%H%M%SZ") + "-" + hashlib.sha256(slot.encode()).hexdigest()[:12]
    return {"policy": policy, "config": config, "target": target, "series_id": series_id}


def launch(request: dict) -> None:
    """Invoke the existing native launcher; it performs fresh Windows admission."""
    policy = request["policy"]
    if refresh.database_target_fingerprint(policy["env_file"]) != request["target"]:
        raise ValueError("database target changed before dispatch")
    # The OS executes the exact pinned launcher; never evaluate shell text.
    local = subprocess.run(["wslpath", "-u", policy["windows_launcher"]], check=True,
                           capture_output=True, text=True, timeout=10).stdout.strip()
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
        expected = subprocess.run(["git", "show", f"{policy['expected_sha']}:scripts/firecrawl-ops/cre_collector/{relative}"],
            cwd=refresh.REPO_ROOT, check=True, capture_output=True, timeout=10).stdout
        raw = observed.read_bytes()
        if raw != expected or (observed == launcher and hashlib.sha256(raw).hexdigest() != policy["windows_launcher_sha256"]):
            raise ValueError("Windows launcher custody changed before dispatch")
    argv = ["/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", "-NoProfile", "-NonInteractive", "-File", policy["windows_launcher"],
        "-Tier", "series", "-Pipeline", "-Sources", ",".join(policy["sources"]),
        "-Concurrency", str(policy["concurrency"]), "-SourceWorkers", str(policy["workers"]),
        "-PageCap", str(policy["page_cap"]), "-AttemptsPerSource", str(policy["attempts"]),
        "-ExpectedSha", policy["expected_sha"], "-FreshSeriesId", request["series_id"],
        "-WorkDeadlineSeconds", str(policy["deadline_seconds"]), "-AcknowledgeDatabaseWrites",
        "-LinuxRepo", policy["windows_repo"], "-LinuxEnvFile", policy["env_file"],
        "-Distro", policy["distro"], "-LinuxUser", policy["linux_user"], "-NotificationMode", policy["notification_mode"]]
    if policy["admissions"]:
        argv += ["-AdmitCurrentInventorySources", ",".join(policy["admissions"])]
    if policy["daum_subset"]:
        argv += ["-AdmitDaumVerifiedSubset", "-DaumSubsetPolicy", "tenure-and-empty-title-v2"]
    if policy["alert_file"] is not None:
        argv += ["-AlertWebhookFile", policy["alert_file"]]
    subprocess.run(argv, check=False)


def qualify(request: dict) -> dict:
    """Require an exact controlled receipt and existing write-fence checks."""
    policy = request["policy"]
    if refresh.database_target_fingerprint(policy["env_file"]) != request["target"]:
        raise ValueError("database target changed during dispatch")
    root = HERE / "out" / "checkpoint-series"
    manifest = json.loads((root / request["series_id"] / "manifest.json").read_text())
    invocation = manifest["latest_invocation_control"]["invocation_id"]
    with refresh.SharedLock(refresh.canonical_shared_lock_dir()) as owner:
        return dispatch_settlement.qualify(root, request["series_id"], invocation, owner=owner,
            expected_sha=policy["expected_sha"], expected_target=request["target"],
            expected_config=request["config"], expected_deadline_seconds=policy["deadline_seconds"])


def main(argv=None) -> int:
    if not sys.platform.startswith("linux"):
        raise ValueError("this phase requires the WSL/Linux series control contract")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--policy", required=True, type=Path)
    args = parser.parse_args(argv)
    request = policy_request(json.loads(args.policy.read_text()), args.slot)
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
    dispatch_queue.enqueue(path, args.slot, request)
    dispatch_queue.drain(path, launch=launch, qualify=qualify)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
