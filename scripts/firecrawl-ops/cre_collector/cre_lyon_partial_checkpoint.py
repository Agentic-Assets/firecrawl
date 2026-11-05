"""Authenticate and replay the source-owned Lyon search capture.

This read-only helper never grants gate admission or starts database writes.
The caller must authenticate the manifest and hold the canonical owner lock.
"""
import hashlib
import json
import os
import re
import stat
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cre_colliers_partial_checkpoint import _read_private_file
from cre_lyon_partial import _validate_staged, stage_record
from cre_lyon_search_replay import replay_search, validate_source_artifact
from cre_partial_page_custody import expected_path, settle


def replay_settled_source(run_dir, manifest, attempt_number, current_code_sha, current_git_dirty):
    """Revalidate source custody, outcomes, and exact rows under the owner."""
    run_dir = Path(run_dir)
    if (not isinstance(manifest, dict) or manifest.get("run_id") != run_dir.name
            or not isinstance(current_code_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", current_code_sha)
            or current_git_dirty is not False or manifest.get("collector_git_dirty") is not False
            or manifest.get("collector_git_sha") != current_code_sha
            or type(attempt_number) is not int or attempt_number < 1):
        raise ValueError("Lyon source owner changed")
    target = (manifest.get("preflight") or {}).get("database_target")
    if (not isinstance(target, dict) or target.get("algorithm") != "sha256"
            or not isinstance(target.get("value"), str) or not re.fullmatch(r"[a-f0-9]{64}", target["value"])):
        raise ValueError("Lyon source target invalid")

    def directories():
        result = []
        for directory in (run_dir, run_dir / "rejected", run_dir / "lyon-stahl-partial"):
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Lyon source directory invalid")
            result.append((info.st_dev, info.st_ino, info.st_mode, info.st_uid))
        return result

    before = directories()
    checkpoint = (manifest.get("sources") or {}).get("lyon-stahl") or {}
    attempts = checkpoint.get("attempts", [])
    numbers = [item.get("number") for item in attempts if isinstance(item, dict)]
    matches = [item for item in attempts if isinstance(item, dict) and item.get("number") == attempt_number]
    if (len(matches) != 1 or not numbers or any(type(number) is not int for number in numbers)
            or attempt_number != max(numbers)):
        raise ValueError("Lyon attempt differs")
    attempt = matches[0]
    from cre_colliers_partial import _time
    if (type(attempt.get("rc")) is not int or attempt["rc"] != 80 or not attempt.get("finished_at")
            or _time(attempt.get("started_at")) < _time(manifest.get("started_at"))
            or _time(attempt["finished_at"]) < _time(attempt.get("started_at"))
            or _time(attempt["finished_at"]) > datetime.now(UTC) + timedelta(minutes=5)):
        raise ValueError("Lyon attempt unsettled")
    state = attempt.get("lyon-stahl_partial")
    relative = str(expected_path(run_dir, attempt_number, "lyon-stahl").relative_to(run_dir))
    main_rel = f"rejected/lyon-stahl-attempt-{attempt_number}.json"
    if (not isinstance(state, dict) or state.get("state") != "sealed_local" or state.get("path") != relative
            or state.get("outcome") != "source_error" or state.get("coverage") != "unverified"
            or attempt.get("rejected_artifact") != main_rel
            or state.get("main_sha256") != attempt.get("rejected_artifact_sha256")):
        raise ValueError("Lyon source receipt differs")
    probe = deepcopy(attempt)
    probe["lyon-stahl_partial"] = {"path": relative, "state": "pending"}
    settle(run_dir, probe, run_dir / main_rel, "lyon-stahl")
    keys = ("state", "path", "coverage", "outcome", "bytes", "observationRecords", "sha256", "main_sha256")
    if directories() != before or any(probe["lyon-stahl_partial"].get(key) != state.get(key) for key in keys):
        raise ValueError("Lyon source receipt changed")
    main_raw = _read_private_file(run_dir / main_rel, 32 * 1024 * 1024)
    source_raw = _read_private_file(run_dir / relative, 128 * 1024 * 1024)
    if (hashlib.sha256(main_raw).hexdigest() != state["main_sha256"]
            or hashlib.sha256(source_raw).hexdigest() != state["sha256"]
            or len(source_raw) != state["bytes"]):
        raise ValueError("Lyon source bytes changed")
    for line in source_raw.splitlines()[1:]:
        page = json.loads(line)
        observed = _time(page.get("observedAt"))
        if not (_time(attempt["started_at"]) <= observed <= _time(attempt["finished_at"])):
            raise ValueError("Lyon observation is outside its source attempt")
    summary = validate_source_artifact(json.loads(main_raw))
    records, held = replay_search(source_raw, run_dir.name, attempt_number, summary=summary)
    staged = [stage_record(record) for record in records]
    if staged:
        _validate_staged(staged)
    if directories() != before:
        raise ValueError("Lyon source directory changed")
    return {"generation": run_dir.name, "attempt": attempt_number, "started_at": attempt["started_at"],
            "finished_at": attempt["finished_at"], "producer_sha": current_code_sha,
            "database_target_sha256": target["value"], "source_sha256": state["sha256"],
            "main_sha256": state["main_sha256"], "records": records, "staged": staged, "held": held}
