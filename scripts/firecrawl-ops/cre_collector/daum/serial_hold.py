"""Reprove a serial DAUM all-target-held outcome without database writes."""

import hashlib
import json
from pathlib import Path


def binding(run_dir, manifest):
    """Rederive the source hold from the immutable artifact and raw gate."""
    import cre_checkpoint_refresh as refresh
    from cre_code_handoff_git import read_file

    source = "daum-commercial"
    root = Path(run_dir)
    config = manifest["config"]
    checkpoint = manifest["sources"][source]
    info = checkpoint["artifact"]
    if (config.get("pipeline") is True or config.get("sources") != [source]
            or not refresh.current_inventory.is_verified_subset(config, source)
            or checkpoint.get("ingest") is not None or checkpoint.get("dry_run") is not None
            or checkpoint.get("readback") is not None or manifest.get("aggregate_gate") is not None
            or manifest.get("validation") is not None
            or info.get("path") != f"sources/{source}.json"
            or root.name != manifest.get("run_id")):
        raise ValueError("DAUM serial hold scope or no-write evidence differs")
    code = manifest["collector_git_sha"]
    target = refresh.manifest_database_target_sha256(manifest)
    if not target or manifest.get("collector_git_dirty") is not False or refresh.git_identity() != (code, False):
        raise ValueError("DAUM serial hold code or target differs")
    artifact = root / info["path"]
    gate = root / "gates" / f"{source}.json"
    raw_artifact, raw_gate = read_file(artifact), read_file(gate)
    digest = hashlib.sha256(raw_artifact).hexdigest()
    if digest != info.get("sha256") or len(raw_artifact) != info.get("bytes"):
        raise ValueError("DAUM serial hold artifact changed")
    report = json.loads(raw_gate)
    try:
        refresh._admit_current_inventory(root, manifest, report, artifact, "source", source=source, local=True)
    except refresh.DaumAllTargetHeld:
        pass
    else:
        raise ValueError("DAUM serial all-held evidence did not reproduce")
    if (refresh.git_identity() != (code, False) or refresh.manifest_database_target_sha256(manifest) != target
            or read_file(artifact) != raw_artifact or read_file(gate) != raw_gate):
        raise ValueError("DAUM serial hold evidence changed")
    return {"schema": "daum-serial-all-held/v1", "source": source,
            "artifact_sha256": digest, "gate_sha256": hashlib.sha256(raw_gate).hexdigest(),
            "generation_id": manifest["run_id"], "collector_git_sha": code,
            "database_target_sha256": target, "write_authority": False}


def verified(run_dir, manifest):
    """Fail closed unless the saved no-write binding still rederives exactly."""
    try:
        checkpoint = manifest["sources"]["daum-commercial"]
        return (checkpoint.get("state") == "gate_blocked"
                and checkpoint.get("partial_probe_hold") == "all_emitted_target_held"
                and checkpoint.get("serial_all_held") == binding(run_dir, manifest))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        return False
