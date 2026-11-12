"""Bounded local-process settlement evidence for one Windows-launched series.

This is an observation, not DB, provider, source-completeness or recovery proof.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import time

import cre_stop_guarded_tier as safe

COLLECTOR = Path(__file__).resolve().parent
SERIES_ID = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z")
RUN_ID = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z-[0-9a-f]{12}")
HEX_ID = re.compile(r"[0-9a-f]{32}")


def require(condition):
    if not condition:
        raise ValueError("local settlement evidence incomplete")


def attest(series_directory, sources, pipeline, expected_sha, admission_ms, windows_invocation_id):
    """Read stable owner and last-child receipts; caller must separately prove capture."""
    selected = Path(series_directory)
    require(selected.parent == COLLECTOR / "out/checkpoint-series")
    require(SERIES_ID.fullmatch(selected.name) is not None)
    require(isinstance(expected_sha, str) and re.fullmatch(r"[0-9a-f]{40}", expected_sha))
    require(type(admission_ms) is int and admission_ms > 0)
    require(isinstance(windows_invocation_id, str) and HEX_ID.fullmatch(windows_invocation_id))
    require(isinstance(sources, list) and 0 < len(sources) <= 51)
    require(len(set(sources)) == len(sources))
    require(all(isinstance(s, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", s) for s in sources))
    require(type(pipeline) is bool)
    snapshots = []
    directories = []
    with ExitStack() as stack:
        def directory(parent, name, private=False):
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            stack.callback(os.close, fd)
            observed = safe._safe_stat(fd, directory=True, private=private)
            directories.append((parent, name, fd, safe._identity(observed)))
            return fd

        def read(parent, name, limit):
            raw, observed = safe._read_at(parent, name, limit)
            value = json.loads(raw)
            require(isinstance(value, dict))
            snapshots.append((parent, name, limit, raw, safe._identity(observed)))
            return value

        root = directory(None, str(COLLECTOR))
        output = directory(root, "out")
        series_root = directory(output, "checkpoint-series")
        series_fd = directory(series_root, selected.name)
        parent = read(series_fd, "manifest.json", 1048576)
        require(type(parent.get("schema_version")) is int and parent["schema_version"] == 1)
        require(parent.get("series_id") == selected.name)
        require(parent.get("collector_git_sha") == expected_sha and parent.get("collector_git_dirty") is False)
        config = parent["config"]
        require(config.get("sources") == sources and config.get("pipeline", False) is pipeline)
        require(config.get("transactions") == ["sale", "lease"])
        admission = parent["latest_invocation_admission"]
        require(admission.get("expected_sha") == expected_sha)
        require(admission.get("windows_admission_unix_ms") == admission_ms)
        require(admission.get("windows_invocation_id") == windows_invocation_id)
        invocation = parent["latest_invocation_control"]["invocation_id"]
        require(isinstance(invocation, str) and HEX_ID.fullmatch(invocation))
        require(parent["latest_invocation_control"].get("receipt") == f".series-control/{invocation}.json")
        private = directory(series_fd, ".series-control", private=True)
        control = read(private, invocation + ".json", 16384)
        owner = control["stop_owner"]
        require(type(owner.get("schema_version")) is int and owner["schema_version"] == 1)
        require(owner.get("uid") == os.geteuid())
        require(owner.get("owner_role") == "checkpoint_series_one_shot_cli")
        require(owner.get("invocation_id") == invocation)
        require(owner.get("series_directory_identity") == safe._identity(os.fstat(series_fd)))
        require(owner.get("control_directory_identity") == safe._identity(os.fstat(private)))
        require(type(owner.get("pid")) is int and owner["pid"] > 1)
        require(type(owner.get("start_ticks")) is int and owner["start_ticks"] > 0)
        require(isinstance(owner.get("boot_id"), str) and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", owner["boot_id"]))
        # Narrow supported cases. Unknown/interrupted and no-work receipts stay unverified.
        status = parent.get("status")
        require(status in {"complete", "qualified_partial_subset", "failed"})
        require(control.get("status") == status and control.get("local_settlement") == "confirmed")
        started, finished = control["started_unix_ms"], control["finished_unix_ms"]
        now = int(time.time() * 1000)
        require(type(started) is int and type(finished) is int)
        require(admission_ms <= started <= finished <= now + 60000)
        require(0 <= now - finished <= 300000)
        checkpoint = control["last_checkpoint"]
        run_id, child_id, pid = checkpoint["run_id"], checkpoint["child_invocation_id"], checkpoint["pid"]
        require(isinstance(run_id, str) and RUN_ID.fullmatch(run_id))
        require(isinstance(child_id, str) and HEX_ID.fullmatch(child_id))
        require(type(pid) is int and pid > 1)
        runs = directory(series_fd, "runs")
        child_fd = directory(runs, run_id)
        child_private = directory(child_fd, ".series-control", private=True)
        child = read(child_private, child_id + ".json", 16384)
        require(type(child.get("schema_version")) is int and child["schema_version"] == 1)
        require(child.get("invocation_id") == invocation and child.get("child_invocation_id") == child_id)
        require(child.get("run_id") == run_id and child.get("collector_git_sha") == expected_sha)
        require(type(child.get("pid")) is int and child["pid"] == pid)
        require(child.get("guards_stopped") is True and child.get("nested_cleanup_confirmed") is True)
        require(child.get("terminal_evidence_saved") is True)
        for directory_parent, name, fd, identity in directories:
            require(safe._identity(os.fstat(fd)) == identity)
            require(safe._identity(os.stat(name, dir_fd=directory_parent, follow_symlinks=False)) == identity)
        for fd, name, limit, raw, identity in snapshots:
            again, observed = safe._read_at(fd, name, limit)
            require(again == raw and safe._identity(observed) == identity)
        return {
            "schema_version": 1,
            "scope": "owned_local_processes_only",
            "local_settlement": "confirmed",
            "series_id": selected.name,
            "invocation_id": invocation,
            "collector_git_sha": expected_sha,
            "windows_admission_unix_ms": admission_ms,
            "windows_invocation_id": windows_invocation_id,
            "source_keys": sources,
            "pipeline": pipeline,
            "terminal_status": status,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--series-directory", required=True)
    parser.add_argument("--source-keys", required=True)
    parser.add_argument("--pipeline", action="store_true")
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--windows-admission-unix-ms", required=True, type=int)
    parser.add_argument("--windows-invocation-id", required=True)
    args = parser.parse_args()
    try:
        result = attest(args.series_directory, args.source_keys.split(","), args.pipeline,
                        args.expected_sha, args.windows_admission_unix_ms, args.windows_invocation_id)
    except (OSError, ValueError, TypeError, KeyError, safe.StopRefused):
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())