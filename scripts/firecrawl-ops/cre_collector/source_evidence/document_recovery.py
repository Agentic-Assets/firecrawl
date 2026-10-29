"""Recover sealed document custody under the existing foreground owner lock.

No request or converter is started. A persisted seal marker is required;
plausible files alone never establish completion. Acquisition budgets remain
unchanged and this API never rearms an interrupted selection.
"""

import os
from pathlib import Path

from source_evidence import (
    doc_complement_process,
    doc_extract_process,
    doc_fetch_process,
    doc_sparse_ocr_process,
    document_acquisition,
    document_budget,
    document_pipeline,
    document_seal,
    events,
)


class CheckpointSaveError(Exception):
    """Persistence failed: do not continue artifact cleanup or recovery."""


def _checkpoint_save(save):
    def persist(run_dir, manifest):
        try:
            save(run_dir, manifest)
        except Exception as exc:
            raise CheckpointSaveError("document recovery checkpoint save failed") from exc
    return persist


def _cleanup(run_dir, manifest, document, receipt, directory, save):
    paths = [str(directory / "fetch" / "document.bin"),
             str(directory / "extraction" / doc_extract_process.input_filename(receipt))]
    cleanup = document.get("cleanup")
    if cleanup is None:
        cleanup = [{"path": path, "state": "pending"} for path in paths]
        document["cleanup"] = cleanup
    if (not isinstance(cleanup, list) or len(cleanup) != 2
            or any(not isinstance(item, dict) or set(item) != {"path", "state"}
                   or item["path"] != path or item["state"] not in ("pending", "removed", "reconciled_absent")
                   for item, path in zip(cleanup, paths))):
        raise ValueError("document cleanup paths or states changed")
    document["original_retention"] = "cleanup_intent"
    save(run_dir, manifest)
    for item in cleanup:
        path = Path(item["path"])
        parent = doc_fetch_process._private_directory(path.parent)
        try:
            try:
                os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                # Unlink can precede a lost checkpoint save. Absence does not
                # tell us who removed the file or freshly verify its bytes.
                if item["state"] == "pending":
                    item["state"] = "reconciled_absent"
                save(run_dir, manifest)
                continue
            if item["state"] != "pending":
                raise ValueError("previously absent document copy reappeared")
            document_pipeline._remove_owned_body(path, receipt["body_sha256"], receipt["body_bytes"])
            item["state"] = "removed"
            save(run_dir, manifest)
        finally:
            os.close(parent)
    document["original_retention"] = "owned_body_copies_absent_after_reconciliation"
    save(run_dir, manifest)


def sealed(run_dir, manifest, source, attempt, save, *, plan, candidates, policy_raw,
           selection, expected_policy_sha256, target_sha256, collector_git_sha,
           cleanup=False):
    """Verify prior sealed output and optionally finish its selected file cleanup.

    The supplied manifest is the caller's trusted, durably saved checkpoint;
    hashes copied from the same untrusted files are not independent authority.
    Missing seal markers stay unresolved even if a converter artifact exists.
    ``cleanup`` never removes unresolved failed/OCR inputs when policy retains
    them. This operation preserves all reservations and starts no new work.
    """
    run_dir = Path(os.path.abspath(run_dir))
    bindings = {"expected_policy_sha256": expected_policy_sha256,
                "target_sha256": target_sha256, "collector_git_sha": collector_git_sha}
    selected = document_acquisition.validate_selection(plan, candidates, policy_raw, selection, **bindings)
    if (type(cleanup) is not bool or not callable(save) or source != selected["source_key"]
            or manifest.get("collector_git_sha") != collector_git_sha
            or manifest.get("collector_git_dirty") is not False
            or not any(item is attempt for item in manifest.get("sources", {}).get(source, {}).get("attempts", []))
            or type(attempt.get("number")) is not int or not 1 <= attempt["number"] <= 1000000):
        raise ValueError("document recovery foreground owner mismatch")
    # Keep persistence errors outside recoverable per-file errors. A lost save
    # after unlink must halt before any additional copy or document is touched.
    save = _checkpoint_save(save)
    document_pipeline._source_plan(run_dir, source, attempt, plan)
    state = attempt.get("document_source_evidence")
    relative = f"source-evidence/documents-{source}-attempt-{attempt['number']}"
    if (not isinstance(state, dict) or state.get("schema_version") != document_pipeline.SCHEMA
            or state.get("directory") != relative
            or state.get("selection_sha256") != events.sha256(events.encoded(selected))
            or state.get("database_append") not in ("not_performed", "managed_by_append_jobs")):
        raise ValueError("document recovery checkpoint binding mismatch")
    document_budget._validate(state["budget"], selected)
    documents = state.get("documents")
    budget_attempts = state["budget"]["attempts"]
    if (not isinstance(documents, list) or len(documents) != len(budget_attempts)
            or len(documents) > len(selected["documents"])
            or any(not isinstance(item, dict) for item in documents)):
        raise ValueError("document recovery attempt list mismatch")
    root = run_dir / relative
    fd = doc_fetch_process._private_directory(root)
    os.close(fd)
    raw, _ = document_pipeline._json(root / "selection.json", 16 * 1024 * 1024)
    policy, _ = document_pipeline._json(root / "policy.json", 16384)
    if raw != events.encoded(selected) or policy != policy_raw:
        raise ValueError("document recovery private policy or selection changed")
    result = {"state": "inspecting", "documents": [], "original_bytes_reverified": False,
              "network_execution": "not_performed", "conversion_execution": "not_performed",
              "new_acquisition_allowed": False}
    state["recovery"] = result
    save(run_dir, manifest)
    for position, document in enumerate(documents):
        item = selected["documents"][position]
        index = item["candidate_index"]
        expected_directory = f"{relative}/document-{index}"
        if (events.encoded(document.get("candidate_index")) != events.encoded(index)
                or budget_attempts[position]["index"] != index
                or document.get("candidate_sha256") != item["candidate_sha256"]
                or document.get("directory") != expected_directory):
            raise ValueError("document recovery candidate binding mismatch")
        report = {"candidate_index": index, "state": "unresolved"}
        result["documents"].append(report)
        marker = document.get("sealed")
        if not isinstance(marker, dict):
            report["reason"] = "missing_persisted_seal_marker"
            primary = document.get("primary_sealed")
            if isinstance(primary, dict):
                try:
                    directory = run_dir / expected_directory / "primary-sealed"
                    raw_context, context = document_pipeline._json(directory / "acquisition.json", document_seal.MAX_FILE)
                    context_sha = primary.get("files", {}).get("acquisition.json")
                    if events.sha256(raw_context) != context_sha:
                        raise ValueError("primary context changed")
                    context = document_acquisition.validate_archived_context(
                        plan, candidates, policy_raw, selected, context, expected_context_sha256=context_sha, **bindings)
                    checked = document_seal.read(directory, expected_context=context, expected_marker=primary)
                    first = checked["artifact"]
                    if context["candidate_index"] != index or not doc_complement_process.required(first, selected):
                        raise ValueError("primary seal outside complement policy")
                    status = first["extraction"]["status"]
                    doc_extract_process.validate_artifact(first, context["receipt"], source_key=source,
                                                         exit_code=0 if status == "extracted" else 2)
                    report["primary_seal"] = "reverified_complement_unresolved"
                except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                    report["primary_seal"] = "unresolved"
            prior_marker = document.get("prior_sealed")
            if isinstance(prior_marker, dict):
                try:
                    prior_dir = run_dir / expected_directory / "prior-sealed"
                    raw_context, context = document_pipeline._json(prior_dir / "acquisition.json", document_seal.MAX_FILE)
                    context_sha = prior_marker.get("files", {}).get("acquisition.json")
                    if events.sha256(raw_context) != context_sha:
                        raise ValueError("prior context changed")
                    context = document_acquisition.validate_archived_context(
                        plan, candidates, policy_raw, selected, context, expected_context_sha256=context_sha, **bindings)
                    prior = document_seal.read(prior_dir, expected_context=context, expected_marker=prior_marker)
                    if context["candidate_index"] != index or not doc_sparse_ocr_process.required(
                            prior.get("artifact"), prior.get("complement"), selected):
                        raise ValueError("prior seal outside OCR policy")
                    report["prior_seal"] = "reverified_ocr_unresolved"
                    if document.get("state") == "ocr_intent":
                        directory = run_dir / expected_directory
                        receipt = context["receipt"]
                        input_path = directory / "extraction" / doc_extract_process.input_filename(receipt)
                        fetch_path = directory / "fetch" / "document.bin"
                        for path in (fetch_path, input_path):
                            body = document_pipeline.automatic._read_private(path, receipt["body_bytes"])
                            if len(body) != receipt["body_bytes"] or events.sha256(body) != receipt["body_sha256"]:
                                raise ValueError("retained OCR source changed")
                        document_seal.check_primary(directory / "primary-sealed", context,
                                                    document.get("primary_sealed"), prior["artifact"])
                        ocr = doc_sparse_ocr_process.replay(input_path, prior["artifact"], prior["complement"],
                            receipt, directory / "ocr", source_key=source, policy=selected["sparse_ocr"], prior_dir=prior_dir)
                        _, marker = document_seal.prepare(context, prior["artifact"], complement=prior["complement"], ocr=ocr)
                        seal_dir = directory / "sealed"
                        if seal_dir.exists():
                            document_seal.read(seal_dir, expected_context=context, expected_marker=marker)
                        else:
                            actual = document_seal.write(seal_dir, context, prior["artifact"],
                                                         complement=prior["complement"], ocr=ocr)
                            if actual != marker:
                                raise ValueError("OCR final seal changed")
                        document["sealed"] = marker
                        document["state"] = "extraction_sealed"
                        document["original_retention"] = "retained_pending_reconciliation"
                        report["state"] = "seal_reverified"
                        report["reason"] = "settled_ocr_completion_replayed"
                        report["original_bytes_reverified"] = True
                        save(run_dir, manifest)
                except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                    report["prior_seal"] = "unresolved"
            save(run_dir, manifest)
            continue
        try:
            directory = run_dir / expected_directory
            fd = doc_fetch_process._private_directory(directory)
            os.close(fd)
            acquisition_raw, context = document_pipeline._json(directory / "sealed" / "acquisition.json",
                                                               document_seal.MAX_FILE)
            context_sha = marker.get("files", {}).get("acquisition.json")
            if events.sha256(acquisition_raw) != context_sha:
                raise ValueError("document recovery context file changed")
            context = document_acquisition.validate_archived_context(
                plan, candidates, policy_raw, selected, context,
                expected_context_sha256=context_sha, **bindings)
            if context["candidate_index"] != index:
                raise ValueError("sealed document candidate changed")
            checked = document_seal.read(directory / "sealed", expected_context=context, expected_marker=marker)
            artifact = checked["artifact"]
            complement_required = doc_complement_process.required(artifact, selected)
            if ("complement" in checked) != complement_required:
                raise ValueError("document complement disagrees with policy")
            if complement_required and checked["complement"]["parser"]["config"]["executable"] != selected["pdf_complement"]["pdftotext_path"]:
                raise ValueError("document complement executable changed")
            if complement_required:
                doc_complement_process.validate(checked["complement"], artifact, context["receipt"], source,
                                                selected["pdf_complement"]["pdftotext_path"], selected["pdf_complement"]["mode"])
            if complement_required:
                document_seal.check_primary(directory / "primary-sealed", context, document.get("primary_sealed"), artifact)
            ocr_required = doc_sparse_ocr_process.required(artifact, checked.get("complement"), selected)
            if ("ocr" in checked) != ocr_required:
                raise ValueError("document OCR disagrees with policy")
            if ocr_required:
                doc_sparse_ocr_process.validate(checked["ocr"], artifact, checked["complement"], context["receipt"],
                                                source, selected["sparse_ocr"])
                prior_marker = document.get("prior_sealed")
                if not isinstance(prior_marker, dict):
                    raise ValueError("OCR prior seal marker missing")
                prior = document_seal.read(directory / "prior-sealed", expected_context=context, expected_marker=prior_marker)
                if prior.get("artifact") != artifact or prior.get("complement") != checked["complement"] or "ocr" in prior:
                    raise ValueError("OCR prior seal disagrees with final artifact")
            report["state"] = "seal_reverified"
            report["original_bytes_reverified"] = False
            if artifact is not None:
                status = artifact["extraction"]["status"]
                doc_extract_process.validate_artifact(artifact, context["receipt"], source_key=source,
                                                     exit_code=0 if status == "extracted" else 2)
                retain = doc_sparse_ocr_process.retain_unresolved(artifact, checked.get("complement"), checked.get("ocr"), selected)
                if retain:
                    report["retention_decision"] = "keep_unresolved_until_review"
                elif cleanup:
                    _cleanup(run_dir, manifest, document, context["receipt"], directory, save)
                    report["cleanup"] = "reconciled"
            save(run_dir, manifest)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            report["state"] = "unresolved"
            report["reason"] = type(exc).__name__
            save(run_dir, manifest)
    result["state"] = ("incomplete" if any(item["state"] == "unresolved" for item in result["documents"])
                       else "sealed_outputs_reconciled")
    # Reconciliation is not permission to resume unattempted candidates, close
    # unknown budgets, or declare source inventory/extraction coverage complete.
    save(run_dir, manifest)
    return result
