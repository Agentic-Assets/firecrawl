"""Owner-bound local Marcus inventory candidate; no network or DB writes."""
import hashlib
import json
import os
from pathlib import Path

from cre_marcus_inventory import candidate_data, rederive_settled_artifact


def run_replay_worker(run_dir, manifest, owner, save):
    """Seal a candidate from an already settled, rejected source artifact.

    The caller must have reaped the collector group and authenticated its
    attempt receipt. This function never grants gate admission.
    """
    import cre_checkpoint_refresh as refresh
    from cre_colliers_partial_checkpoint import _read_private_file
    from cre_stop_guarded_tier import StopRefused, _safe_stat

    run_dir = Path(run_dir)
    if not run_dir.is_absolute() or not isinstance(owner, refresh.SharedLock) or not callable(save):
        raise ValueError("Marcus inventory owner invalid")

    def check_owner():
        owner._require_authority()
        descriptor = owner._owned_directory_fd()
        os.close(descriptor)
        if owner.path.resolve() != refresh.canonical_shared_lock_dir().resolve():
            raise ValueError("Marcus inventory lock differs")

    check_owner()
    config = manifest["config"]
    if (config.get("additive") is not True or config.get("mark_missing") is not False
            or config.get("status_activation") is not False
            or config.get("transactions") != ["sale", "lease"]
            or "marcus-millichap" not in config.get("sources", [])):
        raise ValueError("Marcus inventory scope invalid")
    checkpoint = manifest["sources"]["marcus-millichap"]
    if checkpoint.get("marcus_inventory_worker") is not None:
        raise ValueError("Marcus inventory replay already attempted")
    attempts = checkpoint.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise ValueError("Marcus inventory source attempt missing")
    number = attempts[-1].get("number")
    producer, dirty = refresh.git_identity()
    if dirty is not False or producer != manifest.get("collector_git_sha"):
        raise ValueError("Marcus inventory code changed")
    try:
        source = rederive_settled_artifact(run_dir, manifest, number, producer, dirty)
        if not source["partition"]["staged"]:
            raise ValueError("Marcus inventory has no qualified identities")
    except (OSError, ValueError, TypeError, KeyError, StopRefused):
        check_owner()
        checkpoint["marcus_inventory_worker"] = {"state": "source_held", "attempt": number}
        checkpoint["state"] = "partial_held_no_write"
        save(run_dir, manifest)
        return None
    checkpoint["marcus_inventory_worker"] = {"state": "preparing", "attempt": number}
    save(run_dir, manifest)
    try:
        candidate = candidate_data(source)
        encoded = json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
        name = f"marcus-millichap-inventory-attempt-{number}.json"
        directory = os.open(run_dir / "rejected", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _safe_stat(directory, directory=True, private=True)
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                os.fsync(directory)
        finally:
            os.close(directory)
        path = run_dir / "rejected" / name
        check_owner()
        if (refresh.git_identity() != (producer, False)
                or rederive_settled_artifact(run_dir, manifest, number, producer, False) != source
                or _read_private_file(path, len(encoded)) != encoded):
            raise ValueError("Marcus inventory replay bytes or code changed")
        identity = path.stat()
        checkpoint["candidate"] = {"path": str(path.relative_to(run_dir)), "attempt_started_at": source["started_at"],
            "attempt_number": number, "file_identity": [identity.st_dev, identity.st_ino, identity.st_size,
                                                           identity.st_mtime_ns, identity.st_ctime_ns],
            "bytes": identity.st_size}
        checkpoint["marcus_inventory_worker"].update(state="settled", sha256=hashlib.sha256(encoded).hexdigest())
        checkpoint["state"] = "candidate"
        save(run_dir, manifest)
        return path
    except BaseException as exc:
        checkpoint.pop("candidate", None)
        checkpoint["marcus_inventory_worker"]["state"] = "failed"
        try:
            save(run_dir, manifest)
        except BaseException as save_error:
            raise BaseExceptionGroup("Marcus inventory replay and checkpoint save failed", [exc, save_error]) from exc
        raise


def verify_candidate_artifact(artifact_path, manifest, *, current_code_sha, current_git_dirty):
    """Recheck source and candidate bytes. Caller retains owner and DB fences."""
    from cre_colliers_partial_checkpoint import _read_private_file

    path = Path(artifact_path)
    run_dir = path.parent.parent
    checkpoint = (manifest.get("sources") or {}).get("marcus-millichap") or {}
    worker, candidate = checkpoint.get("marcus_inventory_worker"), checkpoint.get("candidate")
    if (not isinstance(worker, dict) or worker.get("state") != "settled"
            or type(worker.get("attempt")) is not int or worker["attempt"] < 1
            or not isinstance(candidate, dict)):
        raise ValueError("Marcus inventory worker unsettled")
    number = worker["attempt"]
    relative = f"rejected/marcus-millichap-inventory-attempt-{number}.json"
    if path != run_dir / relative or candidate.get("path") != relative or candidate.get("attempt_number") != number:
        raise ValueError("Marcus inventory candidate path differs")
    source = rederive_settled_artifact(run_dir, manifest, number, current_code_sha, current_git_dirty)
    size = path.lstat().st_size
    raw = _read_private_file(path, size)
    if (len(raw) != size or candidate.get("bytes") != size
            or hashlib.sha256(raw).hexdigest() != worker.get("sha256")
            or json.loads(raw) != candidate_data(source)):
        raise ValueError("Marcus inventory candidate differs from source")
    return {"run_dir": run_dir, "source": source, "staged": source["partition"]["staged"],
            "sha256": worker["sha256"], "bytes": size}
