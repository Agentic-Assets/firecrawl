"""Validate bounded Colliers partial detail observations before staging.

This module does not authorize writes: caller must bind the original page spool,
settled owner, artifact, code, target and both checkpoint gates.
"""
import re
from datetime import UTC, datetime, timedelta

_HASH = re.compile(r"^[0-9a-f]{64}$")


def _time(value):
    if not isinstance(value, str):
        raise TypeError("partial observation clock missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("partial observation clock invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("partial observation clock is naive")
    return parsed.astimezone(UTC)


def revalidate_source_receipt(run_dir, manifest, attempt_number, current_code_sha, current_git_dirty):
    """Revalidate source bytes; caller must authenticate manifest and canonical lock.

    Recorded rc/finish are not independent evidence of current owner settlement.
    This helper does not grant admission.
    """
    import os
    import stat
    from copy import deepcopy
    from pathlib import Path

    from cre_partial_page_custody import expected_path, settle

    run_dir = Path(run_dir)
    if (not isinstance(manifest, dict) or manifest.get("run_id") != run_dir.name
            or not isinstance(current_code_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", current_code_sha)
            or current_git_dirty is not False or manifest.get("collector_git_dirty") is not False
            or manifest.get("collector_git_sha") != current_code_sha
            or type(attempt_number) is not int or attempt_number < 1):
        raise ValueError("partial source owner changed")
    target = (manifest.get("preflight") or {}).get("database_target")
    if (not isinstance(target, dict) or target.get("algorithm") != "sha256"
            or not isinstance(target.get("value"), str) or not _HASH.fullmatch(target["value"])):
        raise ValueError("partial source target invalid")
    def directory_identities():
        identities = []
        for directory in (run_dir, run_dir / "rejected", run_dir / "colliers-partial"):
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("partial source directory invalid")
            identities.append((info.st_dev, info.st_ino, info.st_mode, info.st_uid))
        return identities

    directories = directory_identities()
    checkpoint = (manifest.get("sources") or {}).get("colliers") or {}
    attempts = [attempt for attempt in checkpoint.get("attempts", [])
                if isinstance(attempt, dict) and attempt.get("number") == attempt_number]
    numbers = [a.get("number") for a in checkpoint.get("attempts", []) if isinstance(a, dict)]
    if (len(attempts) != 1 or not numbers or any(type(n) is not int for n in numbers)
            or attempt_number != max(numbers)):
        raise ValueError("partial source attempt differs")
    attempt = attempts[0]
    if (type(attempt.get("rc")) is not int or not attempt.get("finished_at")
            or _time(attempt.get("started_at")) < _time(manifest.get("started_at"))
            or _time(attempt["finished_at"]) < _time(attempt.get("started_at"))
            or _time(attempt["finished_at"]) > datetime.now(UTC) + timedelta(minutes=5)):
        raise ValueError("partial source attempt is unsettled")
    state = attempt.get("colliers_partial")
    expected_rel = str(expected_path(run_dir, attempt_number, "colliers").relative_to(run_dir))
    main_rel = f"rejected/colliers-attempt-{attempt_number}.json"
    if (not isinstance(state, dict) or state.get("state") != "sealed_local"
            or state.get("path") != expected_rel or state.get("outcome") != "source_error"
            or state.get("coverage") != "unverified" or attempt.get("rejected_artifact") != main_rel
            or state.get("main_sha256") != attempt.get("rejected_artifact_sha256")):
        raise ValueError("partial source receipt differs")
    probe = deepcopy(attempt)
    probe["colliers_partial"] = {"path": expected_rel, "state": "pending"}
    settle(run_dir, probe, run_dir / main_rel, "colliers")
    keys = ("state", "path", "coverage", "outcome", "bytes", "observationRecords", "sha256", "main_sha256")
    if directory_identities() != directories:
        raise ValueError("partial source directory changed")
    if any(probe["colliers_partial"].get(key) != state.get(key) for key in keys):
        raise ValueError("partial source receipt changed")
    return {key: state[key] for key in keys} | {
        "generation": run_dir.name, "generation_started_at": manifest["started_at"], "attempt": attempt_number,
        "started_at": attempt["started_at"], "finished_at": attempt["finished_at"],
        "producer_sha": current_code_sha, "database_target_sha256": target["value"],
    }


