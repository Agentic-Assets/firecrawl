"""Targeted enrichment evidence using a real guarded-tier receipt."""
import hashlib
import json
from pathlib import Path

import cre_checkpoint_refresh as checkpoint
from source_evidence import automatic, enrichment_custody, guarded_fence


class FenceCleanupUnknown(OSError):
    """The settled receipt was saved, but marker removal was not acknowledged."""


def binding(run_dir, receipt):
    evidence = receipt["source_evidence"]
    state, config = evidence["job"], evidence["config"]
    return {"schema": "cre-targeted-evidence-fence/v1", "receipt": str(run_dir / "receipt.json"),
            "plan_sha256": state["plan_sha256"], "source": receipt["source"],
            "policy_sha256": config["policy_sha256"], "target_sha256": config["target_sha256"]}


def validate_custody(run_dir, receipt):
    """Recheck both the exact output and claim custody after worker settlement."""
    marker = {"schema_version": enrichment_custody.SCHEMA, "source_key": receipt["source"],
              "state": "awaiting_worker", "artifact_path": "enriched.json", "claims_path": "claims.json",
              "database_retention": "not_performed"}
    if enrichment_custody.settle(run_dir, marker) != receipt.get("enrichment_artifact"):
        raise ValueError("settled enrichment custody changed")


def seal_only(run_dir, receipt, lock, save):
    """Seal a settled worker artifact and publish its exact pending receipt."""
    lock._require_authority()
    if checkpoint.git_identity() != (receipt["expected_sha"], False):
        raise ValueError("collector identity changed")
    custody = receipt["enrichment_artifact"]
    if (receipt["local_settlement"].get("state") != "confirmed"
            or custody.get("state") != "settled" or custody.get("source_key") != receipt["source"]):
        raise ValueError("enrichment settlement is not proved")
    validate_custody(run_dir, receipt)
    evidence = receipt["source_evidence"]
    config = evidence["config"]
    artifact = custody.get("artifact", {})
    if artifact.get("state") != "retained":
        evidence["job"] = {"state": "no_artifact"}
        evidence["status"] = "incomplete"
        save()
        return
    raw = automatic._read_private(run_dir / "enriched.json", config["max_artifact_bytes"])
    if len(raw) != artifact.get("bytes") or hashlib.sha256(raw).hexdigest() != artifact.get("sha256"):
        raise ValueError("settled enrichment artifact changed")
    guarded_fence.require_clear(lock.path)
    state = automatic.seal_collector(run_dir, config, [receipt["source"]], receipt["expected_sha"],
                                     False, run_dir.name, receipt["source"], 1, run_dir / "enriched.json",
                                     raw_bytes=raw)
    evidence["job"] = state
    save()
    if not state or state.get("state") != "pending":
        evidence["status"] = "incomplete"
        save()
        return
    marker = binding(run_dir, receipt)
    guarded_fence._sync(run_dir / "source-evidence")
    guarded_fence._sync(run_dir)
    guarded_fence.publish(lock.path, marker)
    return marker


def seal_and_append(run_dir, receipt, lock, save):
    marker = seal_only(run_dir, receipt, lock, save)
    if marker is not None:
        append(run_dir, receipt, lock, save, marker)


def append(run_dir, receipt, lock, save, marker):
    lock._require_authority()
    if guarded_fence.read(lock.path) != marker or binding(run_dir, receipt) != marker:
        raise ValueError("targeted receipt and recovery fence differ")
    evidence = receipt["source_evidence"]
    state, config = evidence["job"], evidence["config"]
    def prepare():
        lock._require_authority()
        if checkpoint.git_identity() != (receipt["expected_sha"], False):
            raise ValueError("collector identity changed")
        if guarded_fence.read(lock.path) != marker:
            raise ValueError("targeted recovery fence changed")
        validate_custody(run_dir, receipt)
        return automatic.prepare_collector_job(run_dir, config, run_dir.name, receipt["source"], 1, state)
    def incomplete():
        evidence["status"] = "incomplete"
    automatic.append_job(config, [receipt["source"]], state, prepare, save, incomplete)
    if state["state"] == "appended":
        lock._require_authority()
        evidence["status"] = "settled"
        save()
        try:
            guarded_fence.clear(lock.path, marker)
        except Exception as exc:
            from source_evidence.document_process import cleanup_unknown_in_chain
            if cleanup_unknown_in_chain(exc):
                raise
            raise FenceCleanupUnknown("settled evidence fence cleanup was not acknowledged") from exc


def reconcile(receipt_path, lock_path=None, *, source=None, expected_sha=None):
    """Explicit exact replay after the canonical owner is cleared; no worker dispatch."""
    receipt_path = Path(receipt_path)
    if not receipt_path.is_absolute() or receipt_path.name != "receipt.json":
        raise ValueError("an absolute guarded receipt is required")
    run_dir = receipt_path.parent
    _, fd = enrichment_custody._directory(run_dir)
    import os
    os.close(fd)
    lock = checkpoint.SharedLock(checkpoint.canonical_shared_lock_dir() if lock_path is None else lock_path,
                                 recovery_required=True)
    with lock:
        try:
            raw = automatic._read_private(receipt_path, 1024 * 1024)
            if len(raw) > 1024 * 1024:
                raise ValueError("guarded receipt exceeds bound")
            receipt = json.loads(raw.decode(), object_pairs_hook=automatic.unique_object,
                                 parse_constant=automatic.invalid_number)
            marker = guarded_fence.read(lock.path)
            if ((marker is not None and marker.get("receipt") != str(receipt_path))
                    or receipt.get("tier") != "enrich" or not receipt.get("apply")
                    or (source is not None and receipt.get("source") != source)
                    or (expected_sha is not None and receipt.get("expected_sha") != expected_sha)
                    or receipt.get("local_settlement", {}).get("state") != "confirmed"
                    or receipt.get("enrichment_artifact", {}).get("state") != "settled"
                    or checkpoint.git_identity() != (receipt.get("expected_sha"), False)
                    or (marker is not None and binding(run_dir, receipt) != marker)):
                raise ValueError("guarded receipt cannot be reconciled")
            state = receipt["source_evidence"]["job"]
            if state.get("state") not in {"pending", "appending", "outcome_unknown", "appended"}:
                raise ValueError("guarded receipt is not replayable")
            already_settled = (marker is None and state["state"] == "appended"
                               and receipt["source_evidence"].get("status") == "settled")
            if marker is None and state["state"] != "pending" and not already_settled:
                raise ValueError("missing recovery fence for a dispatched evidence job")
            def save():
                checkpoint.atomic_write_text(receipt_path, json.dumps(receipt, sort_keys=True) + "\n")
            validate_custody(run_dir, receipt)
            config = receipt["source_evidence"]["config"]
            automatic._verify_config(config, [receipt["source"]])
            automatic.prepare_collector_job(run_dir, config, run_dir.name, receipt["source"], 1, state)
            artifact = receipt["enrichment_artifact"].get("artifact", {})
            original = automatic._read_private(run_dir / "enriched.json", config["max_artifact_bytes"])
            if (artifact.get("state") != "retained" or len(original) != artifact.get("bytes")
                    or hashlib.sha256(original).hexdigest() != artifact.get("sha256")):
                raise ValueError("settled artifact changed")
            if already_settled:
                receipt["source_evidence"]["recovery_cleanup"] = {"state": "settled"}
                save()
                lock.clear_recovery_requirement()
                return
            if marker is None:
                marker = binding(run_dir, receipt)
                guarded_fence._sync(run_dir / "source-evidence")
                guarded_fence._sync(run_dir)
                guarded_fence.publish(lock.path, marker)
            if state["state"] == "appended":
                receipt["source_evidence"]["status"] = "settled"
                save()
                guarded_fence.clear(lock.path, marker)
            else:
                state["state"] = "pending"
                state["exact_replay_required"] = True
                save()
                append(run_dir, receipt, lock, save, marker)
            if state.get("state") != "appended":
                raise ValueError("evidence reconciliation remains incomplete")
            lock.clear_recovery_requirement()
        except BaseException as exc:
            from source_evidence.document_process import cleanup_unknown_in_chain
            if not cleanup_unknown_in_chain(exc):
                lock.clear_recovery_requirement()
            raise
