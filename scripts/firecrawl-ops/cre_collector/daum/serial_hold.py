"""Reprove a serial DAUM all-target-held outcome without database writes."""

import hashlib
import json
import os
import re
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


def verified_stale(series_dir, parent, source, attempt, child_path, child):
    """Require the exact controlled child receipt and absence of its group."""
    from cre_checkpoint_refresh import _group_alive
    from cre_checkpoint_series import _exact_json_equal, _expected_child_config
    from cre_code_handoff_git import read_file
    from cre_series_control import _open_directories, _read_private_receipt

    try:
        config = parent["config"]
        invocation = parent["latest_invocation_control"]["invocation_id"]
        child_id = attempt["series_control_child_id"]
        if (child_path.parent.parent != series_dir / "runs"
                or source != "daum-commercial" or config.get("pipeline") is True
                or config.get("sources") != [source]
                or not re.fullmatch(r"[a-f0-9]{32}", invocation)
                or not re.fullmatch(r"[a-f0-9]{32}", child_id)
                or child.get("status") != "failed"
                or child.get("collector_git_sha") != parent.get("collector_git_sha")
                or not _exact_json_equal(child.get("config"), _expected_child_config(source, config))
                or not _exact_json_equal(child.get("preflight", {}).get("database_target"), parent.get("database_target"))
                or not isinstance(parent.get("database_target"), dict)
                or not verified(child_path.parent, child)):
            return False
        root, private, root_stat, private_stat = _open_directories(child_path.parent)
        try:
            raw, _ = _read_private_receipt(private, child_id + ".json")
            receipt = json.loads(raw)
            pid = receipt.get("pid")
            if (receipt.get("schema_version") != 1 or isinstance(receipt.get("schema_version"), bool)
                    or receipt.get("invocation_id") != invocation or receipt.get("child_invocation_id") != child_id
                    or receipt.get("run_id") != child_path.parent.name
                    or receipt.get("collector_git_sha") != child.get("collector_git_sha")
                    or receipt.get("guards_stopped") is not True or receipt.get("nested_cleanup_confirmed") is not True
                    or receipt.get("terminal_evidence_saved") is not True
                    or receipt.get("pre_acquisition_refusal") is not False
                    or type(pid) is not int or pid <= 1 or pid == os.getpgrp() or _group_alive(pid)):
                return False
            root_now = child_path.parent.stat(follow_symlinks=False)
            private_now = os.stat(".series-control", dir_fd=root, follow_symlinks=False)
            identity = lambda info: (info.st_dev, info.st_ino, info.st_uid, info.st_mode)
            return (identity(root_stat) == identity(root_now)
                    and identity(private_stat) == identity(private_now)
                    and json.loads(read_file(child_path)) == child
                    and _read_private_receipt(private, child_id + ".json")[0] == raw
                    and verified(child_path.parent, child))
        finally:
            os.close(private)
            os.close(root)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        return False
