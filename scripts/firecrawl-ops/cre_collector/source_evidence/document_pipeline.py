"""Finite foreground document acquisition attached to an existing source attempt.

This is an explicit library entrypoint, not an activated collector hook. The
caller owns the existing foreground lock and checkpoint save callback. No
database connection, append, scheduler, canonical promotion or retry lives here.
Interrupted attempts remain visible and cannot be restarted through this API.
"""

import os
import time
from pathlib import Path

from source_evidence import (
    acquisition,
    automatic,
    cli,
    doc_complement_process,
    doc_extract_process,
    doc_fetch_process,
    doc_sparse_ocr_process,
    document_acquisition,
    document_budget,
    document_fetch_worker,
    document_landing,
    document_seal,
    events,
    private_export,
)

SCHEMA = "cre-foreground-documents/v1"
EXTRACTION_SECONDS = 75


def _json(path, limit):
    raw = automatic._read_private(path, limit)
    if len(raw) > limit:
        raise ValueError("document pipeline input exceeds bound")
    value, literals = acquisition.decode_json(raw, max_bytes=limit)
    if literals:
        raise ValueError("unexpected document pipeline numeric literals")
    return raw, value


def _source_plan(run_dir, source, attempt, plan):
    """Require the exact plan bytes already sealed in this source attempt."""
    if isinstance(plan, list):
        if not 1 <= len(plan) <= 129:
            raise ValueError("document source-plan set exceeds bound")
        for item in plan:
            if not isinstance(item, dict) or item.get("source_key") != source:
                raise ValueError("document source-plan set mixes sources")
            _source_plan(run_dir, source, attempt, item)
        return
    candidates = []
    collected = attempt.get("source_evidence") or {}
    if collected.get("plan_sha256") == plan["plan_sha256"]:
        expected = f"source-evidence/{source}-attempt-{attempt['number']}.json"
        if collected.get("path") != expected:
            raise ValueError("document source plan path mismatch")
        candidates.append((run_dir / expected, collected))
    native = attempt.get("native_source_evidence") or {}
    for page in native.get("pages", []):
        if page.get("plan_sha256") != plan["plan_sha256"]:
            continue
        directory = f"source-evidence/native-{source}-attempt-{attempt['number']}"
        name = page.get("filename")
        if (native.get("directory") != directory or not isinstance(name, str)
                or Path(name).name != name or page.get("plan_name") != name + ".plan"):
            raise ValueError("document native source path mismatch")
        candidates.append((run_dir / directory / page["plan_name"], page))
    if len(candidates) != 1:
        raise ValueError("document source plan is not uniquely sealed in this attempt")
    path, marker = candidates[0]
    raw, sealed = _json(path, cli.MAX_PLAN_BYTES)
    if (events.sha256(raw) != marker.get("plan_file_sha256")
            or events.encoded(sealed) != events.encoded(plan)
            or marker.get("capture_id") != plan["events"][0]["capture_id"]):
        raise ValueError("sealed document source plan changed")
    events.validate_content_policy(sealed)


def _remove_owned_body(path, digest, byte_length):
    """Remove a known settled body under the caller's exclusive foreground lock."""
    path = Path(path)
    directory = doc_fetch_process._private_directory(path.parent)
    try:
        before = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        raw = automatic._read_private(path, byte_length)
        after = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_size,
                    info.st_nlink, info.st_mtime_ns, info.st_ctime_ns)
        if (identity(before) != identity(after) or len(raw) != byte_length
                or events.sha256(raw) != digest):
            raise ValueError("settled document body changed before cleanup")
        os.unlink(path.name, dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)


def _seal(outdir, context, *, artifact=None, landing=None, complement=None, ocr=None):
    return document_seal.write(outdir, context, artifact, landing=landing, complement=complement, ocr=ocr)


def run(run_dir, manifest, source, attempt, save, *, plan, candidates, policy_raw,
        selection, expected_policy_sha256, target_sha256, collector_git_sha,
        node_executable):
    """Acquire a finite selection once, preserving results in the caller checkpoint.

    ``save(run_dir, manifest)`` must durably persist the existing checkpoint or
    raise. The caller must hold its foreground lock throughout this invocation.
    One selection is allowed per source attempt; use an ordered plan list and
    document_batch discovery/selection to combine native pages and collector
    evidence under one budget. Another selection or restart cannot reset it.
    """
    run_dir = Path(os.path.abspath(run_dir))
    bindings = {"expected_policy_sha256": expected_policy_sha256,
                "target_sha256": target_sha256, "collector_git_sha": collector_git_sha}
    selected = document_acquisition.validate_selection(plan, candidates, policy_raw, selection, **bindings)
    if (source != selected["source_key"] or not callable(save)
            or manifest.get("collector_git_sha") != collector_git_sha
            or manifest.get("collector_git_dirty") is not False
            or not any(item is attempt for item in manifest.get("sources", {}).get(source, {}).get("attempts", []))
            or type(attempt.get("number")) is not int or not 1 <= attempt["number"] <= 1000000
            or not isinstance(node_executable, str) or not os.path.isabs(node_executable)):
        raise ValueError("document foreground owner or collector binding mismatch")
    if "document_source_evidence" in attempt:
        raise ValueError("existing document attempt requires reconciliation, never automatic restart")
    _source_plan(run_dir, source, attempt, plan)
    parent = run_dir / "source-evidence"
    parent_fd = doc_fetch_process._private_directory(parent)
    os.close(parent_fd)
    root = parent / f"documents-{source}-attempt-{attempt['number']}"
    # Exclusive creation also refuses orphaned files after a checkpoint interruption.
    private_export.write(root, [("selection.json", events.encoded(selected)), ("policy.json", policy_raw)])
    state = {"schema_version": SCHEMA, "state": "running", "directory": str(root.relative_to(run_dir)),
             "selection_sha256": events.sha256(events.encoded(selected)),
             "budget": document_budget.initial(selected), "documents": [],
             "database_append": "not_performed", "coverage": "unknown"}
    attempt["document_source_evidence"] = state
    save(run_dir, manifest)
    started = time.monotonic()
    phase_seconds = (selected["limits"]["document_timeout_seconds"] + doc_fetch_process.STARTUP_GRACE_SECONDS
                     + doc_fetch_process.REAP_GRACE_SECONDS
                     + EXTRACTION_SECONDS + doc_extract_process.REAP_GRACE_SECONDS)
    if "pdf_complement" in selected:
        phase_seconds += doc_complement_process.TIMEOUT_SECONDS + doc_extract_process.REAP_GRACE_SECONDS
    if "sparse_ocr" in selected:
        phase_seconds += doc_sparse_ocr_process.TIMEOUT_SECONDS + doc_extract_process.REAP_GRACE_SECONDS
    try:
        for item in selected["documents"]:
            index = item["candidate_index"]
            # Policy/source bindings are rederived before each possible remote action.
            document_acquisition.validate_selection(plan, candidates, policy_raw, selected, **bindings)
            _source_plan(run_dir, source, attempt, plan)
            budget, reservation = document_budget.reserve(
                state["budget"], selected, index, elapsed_seconds=time.monotonic() - started,
                phase_seconds=phase_seconds)
            state["budget"] = budget
            if reservation["status"] != "reserved":
                state["state"] = "budget_exhausted"
                save(run_dir, manifest)
                return state
            directory = root / f"document-{index}"
            document = {"candidate_index": index, "candidate_sha256": item["candidate_sha256"],
                        "state": "fetch_intent", "directory": str(directory.relative_to(run_dir)),
                        "original_retention": "not_reconciled"}
            state["documents"].append(document)
            save(run_dir, manifest)
            private_export.write(directory, [("candidate.json", events.encoded(item))])
            job = {"schema_version": document_fetch_worker.JOB_SCHEMA, "url": item["candidate"]["url"],
                   "allowed_hosts": selected["allowed_hosts"], "max_bytes": reservation["max_bytes"],
                   "timeout_seconds": selected["limits"]["document_timeout_seconds"],
                   "max_redirects": selected["limits"]["max_redirects"]}
            job_path = directory / "job.json"
            fetched = doc_fetch_process.run(job, job_path, directory / "fetch")
            document["fetch"] = {key: value for key, value in fetched.items() if key != "receipt"}
            if fetched["state"] != "completed":
                state["budget"] = document_budget.settle(state["budget"], selected, index,
                    elapsed_seconds=time.monotonic() - started, outcome="unknown")
                document["state"] = fetched["state"]
                state["state"] = "reconciliation_required"
                save(run_dir, manifest)
                return state
            receipt = fetched["receipt"]
            body = None
            if receipt["outcome"] == "acquired":
                body = automatic._read_private(fetched["body_path"], job["max_bytes"])
                if len(body) > job["max_bytes"]:
                    raise ValueError("document acquired body exceeds reserved cap")
            context = document_acquisition.build_context(plan, candidates, policy_raw, selected,
                                                       index, receipt, body, **bindings)
            document["receipt_sha256"] = events.sha256(events.encoded(receipt))
            document["state"] = "acquired" if body is not None else "fetch_failed"
            save(run_dir, manifest)
            if body is None:
                document["sealed"] = _seal(directory / "sealed", context)
                document["state"] = "fetch_failed_sealed"
                document["original_retention"] = "no_complete_body_published"
            else:
                # Do not launch conversion after the finite run allowance has expired.
                if time.monotonic() - started + EXTRACTION_SECONDS + doc_extract_process.REAP_GRACE_SECONDS > selected["limits"]["run_timeout_seconds"]:
                    state["budget"] = document_budget.settle(state["budget"], selected, index,
                        elapsed_seconds=time.monotonic() - started, outcome="acquired")
                    document["state"] = "acquired_conversion_deferred"
                    state["state"] = "reconciliation_required"
                    save(run_dir, manifest)
                    return state
                document["state"] = "extraction_intent"
                save(run_dir, manifest)
                extracted = doc_extract_process.run(fetched, job_path, directory / "extraction",
                    node_executable=node_executable, source_key=source, timeout_seconds=EXTRACTION_SECONDS)
                document["extraction_state"] = extracted["state"]
                if "artifact" not in extracted:
                    state["budget"] = document_budget.settle(state["budget"], selected, index,
                        elapsed_seconds=time.monotonic() - started, outcome="acquired")
                    document["state"] = "extraction_reconciliation_required"
                    state["state"] = "reconciliation_required"
                    save(run_dir, manifest)
                    return state
                # Rebuild the context from original policy/receipt/bytes, never an editable sidecar alone.
                context = document_acquisition.build_context(plan, candidates, policy_raw, selected,
                                                           index, receipt, body, **bindings)
                landing = None
                extraction = extracted["artifact"].get("extraction", {})
                complement = None
                complement_policy = selected.get("pdf_complement")
                if doc_complement_process.required(extracted["artifact"], selected):
                    document["primary_sealed"] = _seal(directory / "primary-sealed", context,
                                                       artifact=extracted["artifact"])
                    document["state"] = "primary_sealed"
                    save(run_dir, manifest)
                    if (time.monotonic() - started + doc_complement_process.TIMEOUT_SECONDS
                            + doc_extract_process.REAP_GRACE_SECONDS > selected["limits"]["run_timeout_seconds"]):
                        raise ValueError("complement run allowance expired")
                    document["state"] = "complement_intent"
                    save(run_dir, manifest)
                    complement = doc_complement_process.run(Path(extracted["input_path"]), extracted["artifact"], receipt,
                        directory / "complement", executable=complement_policy["pdftotext_path"], source_key=source,
                        mode=complement_policy["mode"])
                    doc_extract_process._input_checked(Path(extracted["directory"]), Path(extracted["input_path"]).name, body)
                ocr = None
                if doc_sparse_ocr_process.required(extracted["artifact"], complement, selected):
                    document["prior_sealed"] = _seal(directory / "prior-sealed", context,
                        artifact=extracted["artifact"], complement=complement)
                    document["state"] = "prior_sealed"
                    save(run_dir, manifest)
                    if (time.monotonic() - started + doc_sparse_ocr_process.TIMEOUT_SECONDS
                            + doc_extract_process.REAP_GRACE_SECONDS > selected["limits"]["run_timeout_seconds"]):
                        raise ValueError("sparse OCR run allowance expired")
                    document["state"] = "ocr_intent"
                    save(run_dir, manifest)
                    ocr = doc_sparse_ocr_process.run(Path(extracted["input_path"]), extracted["artifact"], complement,
                        receipt, directory / "ocr", source_key=source, policy=selected["sparse_ocr"],
                        prior_dir=directory / "prior-sealed")
                    doc_extract_process._input_checked(Path(extracted["directory"]), Path(extracted["input_path"]).name, body)
                if (extraction.get("status") == "failed" and extraction.get("error_code") == "unsupported"
                        and document_landing.is_html(body)):
                    landing = document_landing.protect_scripts(
                        document_landing.page_context(body, final_url=receipt["final_url"]), source_key=source)
                document["sealed"] = _seal(directory / "sealed", context, artifact=extracted["artifact"],
                                           landing=landing, complement=complement, ocr=ocr)
                document["state"] = "extraction_sealed"
                save(run_dir, manifest)
                retain = doc_sparse_ocr_process.retain_unresolved(extracted["artifact"], complement, ocr, selected)
                if retain:
                    document["original_retention"] = "retained_unresolved"
                else:
                    document["original_retention"] = "cleanup_intent"
                    document["cleanup"] = [{"path": path, "state": "pending"}
                                           for path in (fetched["body_path"], extracted["input_path"])]
                    save(run_dir, manifest)
                    for cleanup in document["cleanup"]:
                        _remove_owned_body(cleanup["path"], receipt["body_sha256"], receipt["body_bytes"])
                        cleanup["state"] = "removed"
                        save(run_dir, manifest)
                    document["original_retention"] = "both_owned_body_copies_removed"
            state["budget"] = document_budget.settle(state["budget"], selected, index,
                elapsed_seconds=time.monotonic() - started, outcome=receipt["outcome"])
            save(run_dir, manifest)
            if receipt["http_status"] in (401, 403, 429, 451):
                state["state"] = "rate_limited" if receipt["http_status"] == 429 else "access_denied"
                save(run_dir, manifest)
                return state
        state["state"] = "selection_settled"
        save(run_dir, manifest)
        return state
    except BaseException as exc:
        # Preserve all files and the charged active intent; no retry or cleanup on uncertainty.
        state["state"] = "reconciliation_required"
        state["reason"] = type(exc).__name__
        save(run_dir, manifest)
        raise
