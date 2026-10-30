"""Offline end-to-end source hints with the real converter and sealed recovery."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from source_evidence import (
    document_acquisition,
    document_candidates,
    document_pipeline,
    events,
    private_export,
)
from test_doc_fetch_process import receipt as fetch_receipt
from test_document_acquisition_policy import policy_value, preflight
from test_document_candidates import artifact, plan_for
from test_document_pipeline import execute, setup_run
from test_document_recovery import recover


@pytest.mark.parametrize("case", ["csv", "csv_extension_unresolved", "conflict", "legacy"])
def test_real_conversion_seal_and_no_spawn_recovery(monkeypatch, tmp_path, case):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    adapter = Path(__file__).resolve().parents[2] / "document_text"
    if not (adapter / "node_modules/@firecrawl/anydoc/package.json").is_file():
        pytest.skip("Install document_text dependencies with npm ci --ignore-scripts for real AnyDoc integration")
    manifest, attempt, kwargs = setup_run(tmp_path)
    data = artifact()
    data["sources"][0]["retainedObservations"] = []
    data["listings"] = data["listings"][:1]
    labels = data["listings"][0]["brochures"][0]
    labels.update(name="inventory.csv", type="text/csv")
    if case == "conflict":
        labels["name"] = "inventory.pdf"
    if case == "csv_extension_unresolved":
        labels.update(url="https://docs.example.test/inventory.csv", type="text/csv; charset=utf-8")
    plan = plan_for(data)
    candidates = document_candidates.discover(plan)
    policy = policy_value()
    policy["limits"].update(max_documents=1, max_document_bytes=1024, max_total_bytes=1024, run_timeout_seconds=3600)
    raw = events.encoded(policy)
    selection = preflight(plan, candidates, raw=raw)
    plan_raw = events.encoded(plan)
    plan_path = tmp_path / "source-evidence/svn-attempt-1.json"
    plan_path.write_bytes(plan_raw)
    attempt["source_evidence"].update(plan_sha256=plan["plan_sha256"], plan_file_sha256=events.sha256(plan_raw),
                                      capture_id=plan["events"][0]["capture_id"])
    kwargs.update(plan=plan, candidates=candidates, policy_raw=raw, selection=selection,
                  expected_policy_sha256=events.sha256(raw), node_executable=node)
    body = b"name,price,active\nExample,0,false\n"

    def fetch(job, job_path, outdir):
        job_raw = events.encoded(job)
        job_path.write_bytes(job_raw)
        job_path.chmod(0o600)
        job_digest = events.sha256(job_raw)
        receipt = {**fetch_receipt(), "request_url": job["url"], "final_url": job["url"],
                   "hops": [{"url": job["url"], "status": 200}],
                   "body_bytes": len(body), "body_sha256": events.sha256(body)}
        private_export.write(outdir, [("document.bin", body),
            ("receipt.json", events.encoded({"job_sha256": job_digest, "receipt": receipt}))])
        return {"state": "completed", "directory": str(outdir), "job_sha256": job_digest,
                "receipt": receipt, "body_path": str(outdir / "document.bin")}

    monkeypatch.setattr(document_pipeline.doc_fetch_process, "run", fetch)
    original_build = document_acquisition.build_context
    if case == "legacy":
        def old_context(*args, **options):
            options["format_hints"] = False
            return original_build(*args, **options)
        monkeypatch.setattr(document_acquisition, "build_context", old_context)
    result = execute(tmp_path, manifest, attempt, kwargs)
    document = result["documents"][0]
    assert document["state"] == "extraction_sealed"
    directory = tmp_path / document["directory"]
    context = json.loads((directory / "sealed/acquisition.json").read_bytes())
    converted = json.loads((directory / "sealed/converter-artifact.json").read_bytes())
    marker = json.loads((directory / "extraction/conversion-complete.json").read_bytes())
    if case == "legacy":
        assert "format_hint" not in context and "format_hint" not in converted["source"]
        assert "format_hint_sha256" not in marker
    else:
        hint = context["format_hint"]
        assert hint["status"] == ("resolved" if case == "csv" else "unresolved")
        assert converted["source"]["format_hint"] == hint
        assert marker["format_hint_sha256"] == events.sha256(events.encoded(hint))
    if case in ("csv", "csv_extension_unresolved"):
        assert converted["extraction"]["status"] == "extracted"
        assert converted["source"]["format_detection"] == ("source_hint" if case == "csv" else "extension")
        assert "Example" in converted["extraction"]["text"]
    else:
        assert converted["extraction"]["status"] == "failed"
    monkeypatch.setattr(document_acquisition, "build_context", original_build)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("recovery spawned a process"))
    monkeypatch.setattr(document_pipeline.doc_fetch_process, "run", lambda *a, **k: pytest.fail("recovery fetched"))
    recovered = recover(tmp_path, manifest, attempt, kwargs)
    assert recovered["state"] == "sealed_outputs_reconciled"
    assert recovered["documents"][0]["state"] == "seal_reverified"
    if case != "legacy":
        context["format_hint"]["status"] = "tampered"
        tampered = events.encoded(context)
        with pytest.raises(ValueError):
            document_acquisition.validate_archived_context(
                plan, candidates, raw, selection, context, expected_context_sha256=events.sha256(tampered),
                expected_policy_sha256=kwargs["expected_policy_sha256"], target_sha256=kwargs["target_sha256"],
                collector_git_sha=kwargs["collector_git_sha"])
        (directory / "sealed/acquisition.json").write_bytes(tampered)
        recovered = recover(tmp_path, manifest, attempt, kwargs)
        assert recovered["state"] == "incomplete"
        assert recovered["documents"][0]["state"] == "unresolved"
