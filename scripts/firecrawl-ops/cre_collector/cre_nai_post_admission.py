"""Authenticate an existing NAI source artifact for post-scoped partial work."""
import hashlib
import json
import os
import re
import stat
from pathlib import Path

from cre_code_handoff_git import read_file
from cre_nai_post_partial import SOURCE, _timestamp, partition_posts


def candidate(artifact_path, manifest, *, code_sha, dirty):
    """Recompute the partition from the owner's exact, settled source artifact."""
    config = manifest["config"]
    if (dirty or manifest.get("collector_git_dirty") is not False
            or code_sha != manifest.get("collector_git_sha")
            or not re.fullmatch(r"[0-9a-f]{40}", code_sha or "")
            or config.get("additive") is not True or config.get("mark_missing") is not False
            or config.get("status_activation") is not False
            or config.get("transactions") != ["sale", "lease"] or config.get("max_items") != 0
            or SOURCE not in config.get("sources", [])):
        raise ValueError("NAI partial owner or configuration invalid")
    path = Path(artifact_path)
    root = path.parent.parent
    if (path.name != "nai-global.json" or path.parent.name != "sources"
            or root.name != manifest.get("run_id") or path.resolve() != path.absolute()):
        raise ValueError("NAI partial source path invalid")
    checkpoint = manifest["sources"][SOURCE]
    info = checkpoint["artifact"]
    attempts = checkpoint.get("attempts") or []
    if (info.get("path") != "sources/nai-global.json" or not attempts
            or type(attempts[-1].get("rc")) is not int or attempts[-1]["rc"] != 0
            or not attempts[-1].get("finished_at") or attempts[-1].get("error") is not None
            or attempts[-1].get("started_at") != info.get("attempt_started_at")):
        raise ValueError("NAI source attempt is not a settled success")
    st = path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
            or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o600):
        raise ValueError("NAI source artifact custody invalid")
    raw = read_file(path)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != info.get("sha256") or len(raw) != info.get("bytes"):
        raise ValueError("NAI source artifact differs")
    data = json.loads(raw)
    meta = data["runMeta"]
    if (meta.get("transactions") != ["sale", "lease"] or meta.get("mode") != "full"
            or (meta.get("freshness") or {}).get("generationId") != manifest["run_id"]
            or info.get("freshness_generation_id") != manifest["run_id"]
            or not isinstance(data.get("listings"), list)):
        raise ValueError("NAI source generation differs")
    start = _timestamp(manifest["started_at"])
    attempt_start, attempt_finish = _timestamp(attempts[-1]["started_at"]), _timestamp(attempts[-1]["finished_at"])
    capture_start, capture_finish = _timestamp(meta["startedAt"]), _timestamp(meta["finishedAt"])
    if not start <= attempt_start <= capture_start <= capture_finish <= attempt_finish:
        raise ValueError("NAI source attempt clocks differ")
    partition = partition_posts(data["listings"], manifest["run_id"],
                                capture_start=capture_start, capture_finish=capture_finish)
    if not partition["staged"]:
        raise ValueError("NAI partial has no qualified posts")
    target = manifest["preflight"]["database_target"]
    if target.get("algorithm") != "sha256" or not re.fullmatch(r"[0-9a-f]{64}", target.get("value") or ""):
        raise ValueError("NAI partial target invalid")
    partition_raw = json.dumps(partition, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    if read_file(path) != raw:
        raise ValueError("NAI source artifact changed during validation")
    return {"artifact_sha256": digest, "producer_sha": code_sha, "generation_id": manifest["run_id"],
            "database_target_sha256": target["value"], "partition_sha256": hashlib.sha256(partition_raw).hexdigest(),
            "source_count": len(data["listings"]), "source_held": partition["held"],
            "staged_count": len(partition["staged"])}, partition
