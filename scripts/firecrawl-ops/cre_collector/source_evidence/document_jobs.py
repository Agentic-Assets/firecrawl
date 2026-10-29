"""Bind sealed document outcomes to the existing foreground append lane.

Jobs are checkpoint metadata, not a queue or permission to acquire documents.
Deleted original binaries are not reverified by this module.
"""

import os
from pathlib import Path

from source_evidence import (
    automatic,
    doc_complement_process,
    doc_extract_process,
    doc_sparse_ocr_process,
    document_acquisition,
    document_automatic,
    document_batch,
    document_budget,
    document_pipeline,
    document_seal,
    events,
)

SCHEMA = "cre-document-evidence-append-job/v1"
FORMAT = "local_anydoc_public_listing_v1"


def iter_jobs(manifest):
    """Yield primary jobs first, then complements, preserving checkpoint ownership."""
    for key in ("append", "complement_append", "ocr_append"):
        for source, checkpoint in manifest.get("sources", {}).items():
            for attempt in checkpoint.get("attempts", []):
                for document in (attempt.get("document_source_evidence") or {}).get("documents", []):
                    if isinstance(document, dict) and isinstance(document.get(key), dict):
                        yield source, attempt, document[key]


def _derive(run_dir, manifest, source, attempt, document, *, role="primary"):
    run_dir = Path(os.path.abspath(run_dir))
    configs = manifest["config"]
    evidence = configs["automatic_source_evidence"]
    doc_config = configs["source_document_acquisition"]
    sources = configs["sources"]
    automatic._verify_config(evidence, sources)
    document_automatic._verify_config(doc_config, evidence, sources)
    if (source not in sources or source not in evidence["source_keys"] or source not in doc_config["source_keys"]
            or evidence.get("document_text_formats", {}).get(source) != FORMAT
            or manifest.get("collector_git_dirty") is not False
            or manifest.get("collector_git_sha") != evidence["collector_git_sha"]
            or manifest.get("collector_git_sha") != doc_config["collector_git_sha"]
            or evidence["target_sha256"] != doc_config["target_sha256"]
            or not isinstance(manifest.get("run_id"), str) or not manifest["run_id"]
            or not any(item is attempt for item in manifest["sources"][source]["attempts"])
            or not document_automatic._terminal(attempt, manifest)):
        raise ValueError("document append scope or settlement mismatch")
    state = attempt.get("document_source_evidence")
    if (not isinstance(state, dict) or state.get("schema_version") != document_pipeline.SCHEMA
            or not isinstance(state.get("documents"), list) or len(state["documents"]) > 1000
            or sum(item is document for item in state["documents"]) != 1
            or document.get("state") != "extraction_sealed"):
        raise ValueError("document append requires a sealed attempt member")
    plans = document_automatic._sealed_plans(run_dir, source, attempt)
    if not plans:
        raise ValueError("document append source plans missing")
    candidates = document_batch.discover(plans)
    policy_raw = automatic._read_private(doc_config["policy_path"], 16384)
    bindings = {"expected_policy_sha256": doc_config["policy_sha256"],
                "target_sha256": doc_config["target_sha256"], "collector_git_sha": doc_config["collector_git_sha"]}
    selected = document_batch.prepare(plans, candidates, policy_raw, **bindings)
    document_pipeline._source_plan(run_dir, source, attempt, plans)
    root_rel = f"source-evidence/documents-{source}-attempt-{attempt['number']}"
    if (state.get("directory") != root_rel or state.get("selection_sha256") != events.sha256(events.encoded(selected))
            or state.get("database_append") not in ("not_performed", "managed_by_append_jobs")):
        raise ValueError("document append selection binding mismatch")
    document_budget._validate(state["budget"], selected)
    root = run_dir / root_rel
    saved_selection, _ = document_pipeline._json(root / "selection.json", 16 * 1024 * 1024)
    saved_policy, _ = document_pipeline._json(root / "policy.json", 16384)
    if saved_selection != events.encoded(selected) or saved_policy != policy_raw:
        raise ValueError("document append saved policy or selection changed")
    position = next(i for i, item in enumerate(state["documents"]) if item is document)
    if position >= len(selected["documents"]) or position >= len(state["budget"]["attempts"]):
        raise ValueError("document append candidate position mismatch")
    candidate = selected["documents"][position]
    index = candidate["candidate_index"]
    directory = f"{root_rel}/document-{index}"
    if (type(index) is not int or type(document.get("candidate_index")) is not int
            or document["candidate_index"] != index
            or document.get("candidate_sha256") != candidate["candidate_sha256"]
            or document.get("directory") != directory or state["budget"]["attempts"][position]["index"] != index):
        raise ValueError("document append candidate binding mismatch")
    marker = document.get("sealed")
    if not isinstance(marker, dict) or not isinstance(marker.get("files"), dict):
        raise ValueError("document append seal marker missing")
    seal_dir = run_dir / directory / "sealed"
    context_raw, context = document_pipeline._json(seal_dir / "acquisition.json", document_seal.MAX_FILE)
    context_sha = marker["files"].get("acquisition.json")
    if events.sha256(context_raw) != context_sha:
        raise ValueError("document append acquisition context changed")
    context = document_acquisition.validate_archived_context(plans, candidates, policy_raw, selected, context,
                                                             expected_context_sha256=context_sha, **bindings)
    if context["candidate_index"] != index:
        raise ValueError("document append context candidate mismatch")
    checked = document_seal.read(seal_dir, expected_context=context, expected_marker=marker)
    first = checked["artifact"]
    required = doc_complement_process.required(first, selected)
    if ("complement" in checked) != required:
        raise ValueError("document complement disagrees with policy")
    if required and checked["complement"]["parser"]["config"]["executable"] != selected["pdf_complement"]["pdftotext_path"]:
        raise ValueError("document complement executable changed")
    if required:
        doc_complement_process.validate(checked["complement"], first, context["receipt"], source,
                                        selected["pdf_complement"]["pdftotext_path"], selected["pdf_complement"]["mode"])
    if required:
        document_seal.check_primary(run_dir / directory / "primary-sealed", context, document.get("primary_sealed"), first)
    ocr_required = doc_sparse_ocr_process.required(first, checked.get("complement"), selected)
    if ("ocr" in checked) != ocr_required:
        raise ValueError("document OCR disagrees with policy")
    if ocr_required:
        doc_sparse_ocr_process.validate(checked["ocr"], first, checked["complement"], context["receipt"], source,
                                        selected["sparse_ocr"])
        prior = document_seal.read(run_dir / directory / "prior-sealed", expected_context=context,
                                   expected_marker=document.get("prior_sealed"))
        if (not isinstance(document.get("prior_sealed"), dict) or prior.get("artifact") != first
                or prior.get("complement") != checked["complement"] or "ocr" in prior):
            raise ValueError("OCR prior seal disagrees with final artifact")
    if role not in ("primary", "complement", "ocr") or (role == "complement" and not required) or (role == "ocr" and not ocr_required):
        raise ValueError("document parser role not admitted")
    artifact = first if role == "primary" else checked[role]
    if not isinstance(artifact, dict):
        raise ValueError("document append converter artifact missing")
    status = artifact.get("extraction", {}).get("status")
    if role == "primary":
        doc_extract_process.validate_artifact(artifact, context["receipt"], source_key=source,
                                              exit_code=0 if status == "extracted" else 2)
    enriched = {**artifact, "acquisition_context": context,
                "acquisition_context_origin": "foreground_coordinator_not_converter",
                **({"pageContext": checked["landing"]} if "landing" in checked else {})}
    _, plan = document_acquisition.bind_extraction(context, enriched)
    plan_name = "plan.json" if role == "primary" else role + "-plan.json"
    plan_marker = marker if role == "primary" else marker[role]
    saved_plan_raw, saved_plan = document_pipeline._json(seal_dir / plan_name, document_seal.MAX_FILE)
    if (events.encoded(saved_plan) != events.encoded(plan) or events.sha256(saved_plan_raw) != marker["files"].get(plan_name)
            or plan["plan_sha256"] != plan_marker.get("plan_sha256")
            or plan["events"][0]["capture_id"] != plan_marker.get("capture_id") or not 1 <= len(plan["events"]) <= 71):
        raise ValueError("document append evidence plan changed")
    events.validate_content_policy(plan)
    automatic._refuse_credential_fields(plan)
    expected = {"schema_version": SCHEMA, "kind": "document_text", "source_key": source, "attempt_number": attempt["number"],
                "candidate_index": index, "candidate_sha256": candidate["candidate_sha256"], "generation": manifest["run_id"],
                "policy_sha256": evidence["policy_sha256"], "document_policy_sha256": doc_config["policy_sha256"],
                "target_sha256": evidence["target_sha256"], "collector_git_sha": evidence["collector_git_sha"],
                "transport_sha256": events.sha256(events.encoded(evidence["transport"])),
                "seal_sha256": events.sha256(events.encoded(marker)), "plan_sha256": plan["plan_sha256"],
                "capture_id": plan["events"][0]["capture_id"], "directory": directory,
                "format": FORMAT}
    if role != "primary":
        expected["parser_role"] = {"complement": "pdf_complement_v1", "ocr": "sparse_ocr_v1"}[role]
    return expected, plan


def prepare_job(run_dir, manifest, source, attempt, document, job):
    """Revalidate one existing job and return its exact sealed evidence plan."""
    roles = {"append": "primary", "complement_append": "complement", "ocr_append": "ocr"}
    matches = [role for key, role in roles.items() if job is not None and job is document.get(key)]
    if (not isinstance(job, dict) or len(matches) != 1
            or job.get("state") not in ("pending", "appending", "outcome_unknown", "appended")):
        raise ValueError("invalid document append job state")
    expected, plan = _derive(run_dir, manifest, source, attempt, document, role=matches[0])
    if any(events.encoded(job.get(key)) != events.encoded(value) for key, value in expected.items()):
        raise ValueError("document append job binding changed")
    return plan


def mark(run_dir, manifest, source, attempt, document, save):
    """Attach a pending job once; never rearm an existing job."""
    if not callable(save):
        raise ValueError("document append job requires durable save")
    roles = (("append", "primary"), ("complement_append", "complement"), ("ocr_append", "ocr"))
    admitted = {"primary"} | {role for role in ("complement", "ocr") if role in document.get("sealed", {})}
    if document.get("append") is not None:
        for key, role in roles:
            if role in admitted:
                prepare_job(run_dir, manifest, source, attempt, document, document.get(key))
            elif key in document:
                raise ValueError("unexpected document append job")
        return document["append"]
    expected = {key: _derive(run_dir, manifest, source, attempt, document, role=role)[0]
                for key, role in roles if role in admitted}
    for key, value in expected.items():
        document[key] = {**value, "state": "pending"}
    attempt["document_source_evidence"]["database_append"] = "managed_by_append_jobs"
    save(run_dir, manifest)
    return document["append"]
