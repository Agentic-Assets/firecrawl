"""Explicit foreground document lane bound to an existing evidence policy.

This module installs no scheduler, collector hook, database writer or retry.
The caller owns the foreground attempt lock and durable checkpoint callback.
"""

import os
from pathlib import Path

from source_evidence import (acquisition, automatic, cli, document_acquisition_policy,
                             document_batch, document_pipeline, document_recovery, events)
from source_evidence.document_process import DocumentProcessCleanupUnknown, cleanup_unknown_in_chain


def configuration(policy_path, policy_sha256, node_executable, automatic_config, git_sha, sources):
    """Read one private document policy and bind it to existing source scope."""
    if (not isinstance(policy_path, (str, os.PathLike)) or not policy_path
            or not isinstance(node_executable, str) or not os.path.isabs(node_executable)
            or not isinstance(automatic_config, dict)
            or not isinstance(sources, (list, tuple)) or not sources
            or any(not isinstance(source, str) for source in sources) or len(set(sources)) != len(sources)):
        raise ValueError("invalid automatic document configuration")
    target = automatic_config.get("target_sha256")
    if automatic_config.get("collector_git_sha") != git_sha:
        raise ValueError("automatic document collector binding mismatch")
    source_scope = automatic_config.get("source_keys")
    if not isinstance(source_scope, list) or any(not isinstance(source, str) for source in source_scope):
        raise ValueError("invalid automatic source evidence scope")
    path = str(Path(policy_path).expanduser().absolute())
    raw = automatic._read_private(path, 16384)
    if len(raw) > 16384:
        raise ValueError("document policy exceeds bound")
    policy = document_acquisition_policy.validate_policy(raw, expected_policy_sha256=policy_sha256,
                                                          target_sha256=target, collector_git_sha=git_sha)
    if any(source not in source_scope for source in policy["source_keys"]):
        raise ValueError("document sources exceed automatic evidence scope")
    return {"policy_path": path, "policy_sha256": policy_sha256, "node_executable": node_executable,
            "target_sha256": target, "collector_git_sha": git_sha, "source_keys": list(policy["source_keys"])}


def _verify_config(config, automatic_config, sources):
    checked = configuration(config["policy_path"], config["policy_sha256"], config["node_executable"],
                            automatic_config, config["collector_git_sha"], sources)
    if events.encoded(checked) != events.encoded(config):
        raise ValueError("automatic document configuration changed")


def _terminal(attempt, manifest):
    expected = {"schema_version": "cre-document-collection-settlement/v1", "attempt_number": attempt.get("number"),
                "collector_git_sha": manifest.get("collector_git_sha"), "rc": attempt.get("rc"),
                "finished_at": attempt.get("finished_at")}
    try:
        events.timestamp(attempt.get("finished_at"))
        return (type(attempt.get("number")) is int and 1 <= attempt["number"] <= 1000000
                and type(attempt.get("rc")) is int
                and events.encoded(attempt.get("document_collection_settlement")) == events.encoded(expected))
    except (ValueError, TypeError):
        return False


def _sealed_plans(run_dir, source, attempt):
    paths = []
    collected = attempt.get("source_evidence") or {}
    if collected.get("plan_sha256"):
        expected = f"source-evidence/{source}-attempt-{attempt['number']}.json"
        if collected.get("path") != expected:
            raise ValueError("collector source plan path mismatch")
        paths.append((run_dir / expected, collected))
    native = attempt.get("native_source_evidence") or {}
    pages = native.get("pages", [])
    if not isinstance(pages, list) or len(pages) > 128:
        raise ValueError("native source page bound exceeded")
    if pages:
        directory = f"source-evidence/native-{source}-attempt-{attempt['number']}"
        if native.get("directory") != directory:
            raise ValueError("native source directory mismatch")
        names = set()
        for page in pages:
            if not isinstance(page, dict) or not isinstance(page.get("filename"), str):
                raise ValueError("invalid native source page")
            name = page["filename"]
            if Path(name).name != name or name in (".", "..") or name in names:
                raise ValueError("invalid native source page name")
            names.add(name)
            if page.get("plan_sha256"):
                if page.get("plan_name") != name + ".plan":
                    raise ValueError("native source plan name mismatch")
                paths.append((run_dir / directory / page["plan_name"], page))
    result, total = [], 0
    for path, marker in paths:
        raw = automatic._read_private(path, cli.MAX_PLAN_BYTES)
        total += len(raw)
        if len(raw) > cli.MAX_PLAN_BYTES or total > document_batch.MAX_PLANS_BYTES:
            raise ValueError("sealed source plans exceed bound")
        plan, literals = acquisition.decode_json(raw, max_bytes=cli.MAX_PLAN_BYTES)
        if (literals or events.sha256(raw) != marker.get("plan_file_sha256")
                or not isinstance(plan, dict) or plan.get("plan_sha256") != marker.get("plan_sha256")
                or plan.get("source_key") != source or plan["events"][0]["capture_id"] != marker.get("capture_id")):
            raise ValueError("sealed source plan binding mismatch")
        events.validate_content_policy(plan)
        result.append(plan)
    return result


def _settled(state):
    if not isinstance(state, dict):
        return False
    if state.get("state") == "no_candidates":
        return True
    return (state.get("state") == "selection_settled" and isinstance(state.get("documents"), list) and bool(state["documents"])
            and all(isinstance(item, dict) and item.get("state") == "extraction_sealed"
                    and (item.get("extraction_state") == "success"
                         or (item.get("extraction_state") in ("ocr_needed", "failed")
                             and "complement" in (item.get("sealed") or {}))) for item in state["documents"]))


def _known_no_data(state):
    return (isinstance(state, dict) and events.encoded(state) == events.encoded({
        "schema_version": document_pipeline.SCHEMA, "state": "no_data", "reason": "no_sealed_source_plan"}))


def reconciliation_required(manifest):
    """Identify uncertain prior document work without treating no data as unknown."""
    config = manifest.get("config", {}).get("source_document_acquisition")
    if config is None:
        return False
    if manifest.get("source_document_acquisition_paused"):
        return True
    for source in manifest.get("config", {}).get("sources", []):
        if source not in config["source_keys"]:
            continue
        for attempt in manifest.get("sources", {}).get(source, {}).get("attempts", []):
            state = attempt.get("document_source_evidence")
            if state is not None and not _settled(state) and not _known_no_data(state):
                return True
    return False


def _append_states(manifest):
    from source_evidence import document_jobs
    for _, _, job in document_jobs.iter_jobs(manifest):
        yield job


def append_incomplete(manifest):
    """Whether explicitly admitted sealed document outcomes still need append."""
    evidence = manifest.get("config", {}).get("automatic_source_evidence") or {}
    formats = evidence.get("document_text_formats", {})
    for source, checkpoint in manifest.get("sources", {}).items():
        if formats.get(source) != automatic.DOCUMENT_TEXT_FORMAT:
            continue
        for attempt in checkpoint.get("attempts", []):
            for document in (attempt.get("document_source_evidence") or {}).get("documents", []):
                if not isinstance(document, dict):
                    return True
                if document.get("state") == "extraction_sealed":
                    job = document.get("append") or {}
                    if (job.get("kind") != "document_text" or job.get("state") != "appended"
                            or job.get("exact_replay_required")):
                        return True
                    if "complement" in document.get("sealed", {}):
                        job = document.get("complement_append") or {}
                        if (job.get("kind") != "document_text" or job.get("state") != "appended"
                                or job.get("exact_replay_required")):
                            return True
    return False


def require_resume(manifest, *, replay_unknown=False):
    """Refuse restarting an interrupted document lane before other runtime work."""
    config = manifest.get("config", {}).get("source_document_acquisition")
    if config is None:
        return
    if type(replay_unknown) is not bool:
        raise ValueError("invalid document replay option")
    unknown = any(job.get("state") in ("appending", "outcome_unknown") for job in _append_states(manifest))
    if replay_unknown and unknown:
        # This exception permits exact DB reconciliation, never new acquisition.
        # The caller must call require_resume again after the foreground replay
        # and before collection, without this exception enabled.
        return
    if unknown or any(job.get("state") == "refused" for job in _append_states(manifest)):
        raise ValueError("document append requires explicit exact-plan reconciliation")
    if reconciliation_required(manifest):
        raise ValueError("document attempt requires reconciliation before resume")


def _validate_settled(run_dir, manifest, source, attempt, save, plans, candidates, selected, policy_raw, config):
    state = attempt["document_source_evidence"]
    if state.get("state") == "no_candidates":
        expected = {"schema_version": document_pipeline.SCHEMA, "state": "no_candidates",
                    "candidate_manifest_sha256": selected["candidate_manifest_sha256"],
                    "source_evidence_set_sha256": selected["source_evidence_set_sha256"],
                    "selection_sha256": events.sha256(events.encoded(selected)), "coverage": "unverified"}
        if selected["documents"] or events.encoded(state) != events.encoded(expected):
            raise ValueError("document no-candidates checkpoint mismatch")
        return
    if not _settled(state) or len(state["documents"]) != len(selected["documents"]):
        raise ValueError("document settled attempt is incomplete")
    result = document_recovery.sealed(run_dir, manifest, source, attempt, save, plan=plans, candidates=candidates,
        policy_raw=policy_raw, selection=selected, expected_policy_sha256=config["policy_sha256"],
        target_sha256=config["target_sha256"], collector_git_sha=config["collector_git_sha"], cleanup=False)
    if result["state"] != "sealed_outputs_reconciled" or len(result["documents"]) != len(selected["documents"]):
        raise ValueError("document sealed output is incomplete")
    for document in state["documents"]:
        path = run_dir / document["directory"] / "sealed" / "artifact.json"
        raw = automatic._read_private(path, cli.MAX_PLAN_BYTES)
        if len(raw) > cli.MAX_PLAN_BYTES or events.sha256(raw) != document["sealed"]["files"]["artifact.json"]:
            raise ValueError("document sealed artifact changed")
        artifact, literals = acquisition.decode_json(raw, max_bytes=cli.MAX_PLAN_BYTES)
        status = artifact.get("extraction", {}).get("status")
        if literals or (status != "extracted" and not (
                "complement" in document.get("sealed", {})
                and (status == "ocr_needed" or (status == "failed" and artifact["extraction"].get("error_code") == "unsupported")))):
            raise ValueError("document sealed artifact is not extracted")


def pump(run_dir, manifest, save):
    """Run once after worker settlement, under the caller's foreground lock."""
    config = manifest.get("config", {}).get("source_document_acquisition")
    if config is None:
        return
    if not callable(save):
        raise ValueError("document lane requires durable checkpoint save")
    run_dir = Path(os.path.abspath(run_dir))
    try:
        source_config = manifest["config"]["automatic_source_evidence"]
        sources = manifest["config"]["sources"]
        _verify_config(config, source_config, sources)
        if manifest.get("collector_git_dirty") is not False or manifest.get("collector_git_sha") != config["collector_git_sha"]:
            raise ValueError("document lane collector identity mismatch")
        if manifest.get("automatic_source_evidence_paused") or manifest.get("automatic_source_evidence_replay_only"):
            raise ValueError("source evidence lane is paused")
        # Unknown append outcomes take priority even if another capture is usable.
        for checkpoint in manifest["sources"].values():
            for attempt in checkpoint.get("attempts", []):
                states = [attempt.get("source_evidence") or {}]
                states.extend((attempt.get("native_source_evidence") or {}).get("pages", []))
                if any(state.get("state") in ("appending", "outcome_unknown") or state.get("exact_replay_required")
                       for state in states):
                    raise ValueError("source evidence outcome requires reconciliation")
        policy_raw = automatic._read_private(config["policy_path"], 16384)
        incomplete = False
        for source in sources:
            if source not in config["source_keys"]:
                continue
            checkpoint = manifest["sources"][source]
            if not checkpoint.get("attempts"):
                incomplete = True
            for attempt in checkpoint.get("attempts", []):
                if not _terminal(attempt, manifest):
                    raise ValueError("document collection settlement missing")
                existing = attempt.get("document_source_evidence")
                if existing is not None:
                    if _known_no_data(existing):
                        incomplete = True
                        continue
                    if not _settled(existing):
                        incomplete = True
                        raise ValueError("document attempt requires reconciliation")
                    _verify_config(config, source_config, sources)
                    plans = _sealed_plans(run_dir, source, attempt)
                    if not plans:
                        raise ValueError("document settled source plans are missing")
                    candidates = document_batch.discover(plans)
                    selected = document_batch.prepare(plans, candidates, policy_raw,
                        expected_policy_sha256=config["policy_sha256"], target_sha256=config["target_sha256"],
                        collector_git_sha=config["collector_git_sha"])
                    _validate_settled(run_dir, manifest, source, attempt, save, plans, candidates, selected, policy_raw, config)
                    continue
                _verify_config(config, source_config, sources)
                plans = _sealed_plans(run_dir, source, attempt)
                if not plans:
                    attempt["document_source_evidence"] = {"schema_version": document_pipeline.SCHEMA,
                        "state": "no_data", "reason": "no_sealed_source_plan"}
                    incomplete = True
                    save(run_dir, manifest)
                    continue
                candidates = document_batch.discover(plans)
                selected = document_batch.prepare(plans, candidates, policy_raw,
                    expected_policy_sha256=config["policy_sha256"], target_sha256=config["target_sha256"],
                    collector_git_sha=config["collector_git_sha"])
                if not selected["documents"]:
                    attempt["document_source_evidence"] = {"schema_version": document_pipeline.SCHEMA,
                        "state": "no_candidates", "candidate_manifest_sha256": selected["candidate_manifest_sha256"],
                        "source_evidence_set_sha256": selected["source_evidence_set_sha256"],
                        "selection_sha256": events.sha256(events.encoded(selected)), "coverage": "unverified"}
                    save(run_dir, manifest)
                    continue
                _verify_config(config, source_config, sources)
                if automatic._read_private(config["policy_path"], 16384) != policy_raw:
                    raise ValueError("document policy changed before action")
                state = document_pipeline.run(run_dir, manifest, source, attempt, save, plan=plans,
                    candidates=candidates, policy_raw=policy_raw, selection=selected,
                    expected_policy_sha256=config["policy_sha256"], target_sha256=config["target_sha256"],
                    collector_git_sha=config["collector_git_sha"], node_executable=config["node_executable"])
                if not _settled(state):
                    incomplete = True
                    raise ValueError("document selection incomplete")
        manifest["source_document_acquisition_paused"] = False
        manifest["source_document_acquisition_status"] = "incomplete" if incomplete else "settled"
        save(run_dir, manifest)
    except DocumentProcessCleanupUnknown as exc:
        manifest["source_document_acquisition_paused"] = True
        manifest["source_document_acquisition_status"] = "incomplete"
        try:
            save(run_dir, manifest)
        except BaseException as save_error:
            raise exc from save_error
        raise
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, IndexError, AttributeError) as exc:
        manifest["source_document_acquisition_paused"] = True
        manifest["source_document_acquisition_status"] = "incomplete"
        if cleanup_unknown_in_chain(exc):
            try:
                save(run_dir, manifest)
            except BaseException as save_error:
                # Raising a replacement save error can rewrite exception
                # contexts. Preserve the known uncertainty as the top type.
                raise DocumentProcessCleanupUnknown("document cleanup remains unknown after save failure") from save_error
            raise
        save(run_dir, manifest)
