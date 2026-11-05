"""Authenticate an existing NAI source artifact for post-scoped partial work."""
import hashlib
import json
import os
import re
import stat
import subprocess
from decimal import Decimal
from pathlib import Path

from cre_code_handoff_git import read_file
from cre_nai_post_partial import SOURCE, _timestamp, partition_posts


class NaiSourceProbeRefusal(ValueError):
    """A settled source-local read-only refusal; eligible peers can proceed."""


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


def run_identity_probe(artifact_path, manifest, env_file):
    """Probe the bound target read-only and retain exact numeric preimage bytes."""
    from cre_checkpoint_refresh import git_identity
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_nai_post_partial import _partition, identity_probe_sql
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    producer, dirty = git_identity()
    binding, partition = candidate(artifact_path, manifest, code_sha=producer, dirty=dirty)
    target = binding["database_target_sha256"]
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != target:
        raise ValueError("NAI probe target differs")

    def recheck():
        url, _ = load_db_url(env_file)
        current_sha, current_dirty = git_identity()
        current, current_partition = candidate(artifact_path, manifest, code_sha=current_sha, dirty=current_dirty)
        if (database_target_fingerprint_from_url(url).get("value") != target
                or current != binding or current_partition != partition):
            raise ValueError("NAI probe input or target changed")

    def source_refusal(cause):
        recheck()
        try:
            health = _run_psql_script(find_psql(), db_url,
                                      "BEGIN TRANSACTION READ ONLY; SELECT 1 AS healthy; ROLLBACK;", timeout=10)
            healthy = health.returncode == 0 and parse_tsv(health.stdout) == [{"healthy": "1"}]
        except (OSError, RuntimeError, ValueError, TypeError, SystemExit, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("NAI probe target health unknown") from exc
        recheck()
        if not healthy:
            raise RuntimeError("NAI probe target health unknown") from cause
        raise NaiSourceProbeRefusal("NAI source identity query refused") from cause

    sql = ("\\set VERBOSITY verbose\nBEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
           + PIN_STRING_LITERALS + "\n" + identity_probe_sql(partition["staged"]) + ";\nROLLBACK;\n")
    try:
        result = _run_psql_script(find_psql(), db_url, sql, timeout=60)
    except subprocess.TimeoutExpired as exc:
        source_refusal(exc)
    if result.returncode:
        state = re.search(r"ERROR:\s+([0-9A-Z]{5}):", getattr(result, "stderr", "") or "")
        if (result.returncode == 3 and state
                and (state.group(1).startswith("22") or state.group(1) in {"42601", "57014"})):
            source_refusal(RuntimeError("NAI source query refused"))
        raise RuntimeError("NAI identity query or target failed")
    recheck()
    rows = parse_tsv(result.stdout)
    count = str(len(partition["staged"]))
    if (len(rows) != 1 or set(rows[0]) != {"staged", "joined", "held", "preimage"}
            or rows[0]["staged"] != count or rows[0]["joined"] != count):
        raise RuntimeError("NAI probe scope differs")
    preimage_text = rows[0]["preimage"]
    try:
        held = json.loads(rows[0]["held"])
        before = json.loads(preimage_text, parse_float=Decimal)
        _partition(partition["staged"], held, before)
    except (ValueError, TypeError, KeyError) as exc:
        raise NaiSourceProbeRefusal("NAI target identity partition invalid") from exc
    # Keep the exact JSON text as a string in claims; the ordinary manifest
    # encoder would round arbitrary numeric preimages through binary floats.
    return {**binding, "held": held, "preimage": preimage_text,
            "preimage_sha256": hashlib.sha256(preimage_text.encode("utf-8")).hexdigest()}


def admit(artifact_path, manifest, report, *, code_sha, dirty, phase):
    """Replace only the bound NAI count hold with a no-retirement post subset."""
    from cre_checkpoint_refresh import subset_gate_can_admit
    from cre_current_inventory_admission import validate_report_scope
    from cre_gate import count_artifacts
    from cre_nai_post_partial import _partition

    if phase not in {"source", "aggregate"}:
        raise ValueError("NAI partial gate phase invalid")
    binding, partition = candidate(artifact_path, manifest, code_sha=code_sha, dirty=dirty)
    probe = manifest["sources"][SOURCE].get("nai_post_identity_probe")
    if not isinstance(probe, dict) or any(probe.get(key) != value for key, value in binding.items()):
        raise ValueError("NAI target probe differs")
    preimage = probe.get("preimage")
    if (not isinstance(preimage, str)
            or hashlib.sha256(preimage.encode("utf-8")).hexdigest() != probe.get("preimage_sha256")):
        raise ValueError("NAI target preimage changed")
    before = json.loads(preimage, parse_float=Decimal)
    held = probe.get("held")
    _partition(partition["staged"], held, before)
    if len(held) == len(partition["staged"]):
        raise NaiSourceProbeRefusal("NAI target has no unambiguous source posts")
    current, errors, sources, _, rejects = count_artifacts([str(artifact_path)], True)
    if sources != {SOURCE} or errors.get(SOURCE) or rejects or type(current.get(SOURCE)) is not int:
        raise ValueError("NAI source gate counts differ")
    if candidate(artifact_path, manifest, code_sha=code_sha, dirty=dirty) != (binding, partition):
        raise ValueError("NAI source artifact changed during gate")
    validate_report_scope(report, manifest, SOURCE, Path(artifact_path))
    summary, gate = report["summary"], report["per_source"][SOURCE]
    if (summary.get("mode") != "live" or summary.get("torow_errors") != 0
            or summary.get("baseline_updated") is not False or summary.get("baseline_update_sources") != []
            or gate.get("current_active") != current[SOURCE] or gate.get("mark_missing_safe") is not False):
        raise ValueError("NAI source gate scope invalid")
    if gate.get("verdict") == "ok_additive_verified_subset":
        raw = {"verdict": gate.get("raw_verdict"), "reason": gate.get("raw_reason")}
    else:
        raw = {"verdict": gate.get("verdict"), "reason": gate.get("reason")}
    if raw["verdict"] != "hold" or not subset_gate_can_admit(raw):
        raise ValueError("NAI gate is not a coverage-only hold")
    proof = {**binding, "schema": "nai-public-post-partial-v1", "scope": "nai_public_post_partial_v1",
             "source_key": SOURCE, "held": held, "preimage_sha256": probe["preimage_sha256"],
             "held_count": len(binding["source_held"]) + len(held),
             "emitted_count": len(partition["staged"]) - len(held), "whole_source_coverage": False}
    if phase == "aggregate" and manifest["sources"][SOURCE].get("gate", {}).get("current_inventory_proof") != proof:
        raise ValueError("NAI source gate proof changed")
    gate.update(raw_verdict=raw["verdict"], raw_reason=raw["reason"], verdict="ok_additive_verified_subset",
                reason="NAI source posts admitted as a partial subset; source coverage remains incomplete",
                mark_missing_safe=False, admission_scope="nai_public_post_partial_v1", current_inventory_proof=proof)
    for key in ("hold_sources", "mark_missing_safe_sources", "mark_missing_safe_brokerages"):
        if isinstance(summary.get(key), list):
            summary[key] = [value for value in summary[key] if value != SOURCE]
    if isinstance(report.get("per_brokerage"), dict) and isinstance(report["per_brokerage"].get(SOURCE), dict):
        report["per_brokerage"][SOURCE]["mark_missing_safe"] = False
    counts = {}
    for source_gate in report["per_source"].values():
        verdict = source_gate.get("verdict")
        counts[verdict] = counts.get(verdict, 0) + 1
    summary["verdict_counts"] = counts
    report["scope"] = {"kind": ("nai_public_post_partial_v1" if phase == "source"
                                else "collector_registry_verified_subset"),
                       "transactions": ["sale", "lease"], "whole_source_coverage": False}
    manifest["scope"].update(kind="collector_registry_verified_subset", whole_source_coverage=False)
    return proof
