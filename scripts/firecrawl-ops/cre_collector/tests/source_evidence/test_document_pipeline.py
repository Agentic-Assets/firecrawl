"""Synthetic foreground tests. Fetch and conversion never contact providers."""

import copy
import json

import pytest

from source_evidence import document_candidates, document_pipeline, events, private_export
from test_doc_extract_process import artifact as text_artifact
from test_doc_fetch_process import receipt as fetch_receipt
from test_document_acquisition_policy import CODE, TARGET, policy_value, preflight
from test_document_candidates import artifact, plan_for


def setup_run(tmp_path, *, total=78000, retention="temporary_only"):
    tmp_path.chmod(0o700)
    directory = tmp_path / "source-evidence"
    directory.mkdir(mode=0o700)
    plan = plan_for(artifact())
    candidates = document_candidates.discover(plan)
    policy = policy_value()
    policy["limits"].update(max_document_bytes=9, max_total_bytes=total, run_timeout_seconds=3600)
    policy["original_retention"] = retention
    raw = events.encoded(policy)
    selection = preflight(plan, candidates, raw=raw)
    plan_raw = events.encoded(plan)
    plan_path = directory / "svn-attempt-1.json"
    plan_path.write_bytes(plan_raw)
    plan_path.chmod(0o600)
    attempt = {"number": 1, "source_evidence": {"path": "source-evidence/svn-attempt-1.json",
                "plan_sha256": plan["plan_sha256"], "plan_file_sha256": events.sha256(plan_raw),
                "capture_id": plan["events"][0]["capture_id"]}}
    manifest = {"collector_git_sha": CODE, "collector_git_dirty": False,
                "sources": {"svn": {"attempts": [attempt]}}}
    kwargs = {"plan": plan, "candidates": candidates, "policy_raw": raw, "selection": selection,
              "expected_policy_sha256": events.sha256(raw), "target_sha256": TARGET,
              "collector_git_sha": CODE, "node_executable": "/usr/bin/node"}
    return manifest, attempt, kwargs


def install_workers(monkeypatch, *, fetch_state="completed", extraction_state="success", denial=None):
    calls = {"fetch": [], "extract": []}
    def fetch(job, job_path, outdir):
        calls["fetch"].append(job["url"])
        if fetch_state != "completed":
            return {"state": fetch_state, "directory": str(outdir), "job_sha256": "a" * 64}
        receipt = {**fetch_receipt(), "request_url": job["url"], "final_url": job["url"],
                   "hops": [{"url": job["url"], "status": 200}]}
        if denial is not None:
            receipt.update(http_status=denial, hops=[{"url": job["url"], "status": denial}],
                           outcome="failed", failure_code="http_status", completion="incomplete",
                           body_bytes=None, body_sha256=None)
            private_export.write(outdir, [("receipt.json", events.encoded(receipt))])
            return {"state": "completed", "directory": str(outdir), "job_sha256": "a" * 64,
                    "receipt": receipt, "body_path": None}
        private_export.write(outdir, [("document.bin", b"pdf")])
        return {"state": "completed", "directory": str(outdir), "job_sha256": "a" * 64,
                "receipt": receipt, "body_path": str(outdir / "document.bin")}

    def extract(fetched, job_path, outdir, **kwargs):
        calls["extract"].append(fetched["receipt"]["request_url"])
        private_export.write(outdir, [("document.bin", b"pdf")])
        if extraction_state == "timeout":
            return {"state": "timeout", "directory": str(outdir)}
        status = {"success": "extracted", "failed": "failed", "ocr_needed": "ocr_needed"}[extraction_state]
        value = text_artifact(status)
        value["source"].update(url=fetched["receipt"]["final_url"],
                               observed_at=fetched["receipt"]["finished_at"],
                               sha256=fetched["receipt"]["body_sha256"],
                               byte_length=fetched["receipt"]["body_bytes"])
        return {"state": extraction_state, "directory": str(outdir), "artifact": value,
                "input_path": str(outdir / "document.bin")}
    monkeypatch.setattr(document_pipeline.doc_fetch_process, "run", fetch)
    monkeypatch.setattr(document_pipeline.doc_extract_process, "run", extract)
    monkeypatch.setattr(document_pipeline.time, "monotonic", lambda: 1.0)
    return calls


def execute(tmp_path, manifest, attempt, kwargs, save=lambda *_: None):
    return document_pipeline.run(tmp_path, manifest, "svn", attempt, save, **kwargs)


def test_78_distinct_synthetic_documents_are_sealed_once(monkeypatch, tmp_path):
    manifest, attempt, kwargs = setup_run(tmp_path)
    calls = install_workers(monkeypatch)
    checkpoints = []
    result = execute(tmp_path, manifest, attempt, kwargs,
                     lambda _, current: checkpoints.append(copy.deepcopy(current)))
    assert result["state"] == "selection_settled"
    assert len(result["documents"]) == len(set(calls["fetch"])) == len(calls["extract"]) == 78
    assert all(item["state"] == "extraction_sealed" for item in result["documents"])
    assert all(item["original_retention"] == "both_owned_body_copies_removed" for item in result["documents"])
    assert result["database_append"] == "not_performed" and result["coverage"] == "unknown"
    first = result["documents"][0]
    sealed = tmp_path / first["directory"] / "sealed"
    converter = json.loads((sealed / "converter-artifact.json").read_bytes())
    plan = json.loads((sealed / "plan.json").read_bytes())
    outcome = plan["events"][0]["facts"][0]
    assert outcome["kind"] == "document_outcome"
    assert outcome["value"]["acquisition_context"]["candidate"]["occurrences"] == (
        kwargs["selection"]["documents"][0]["candidate"]["occurrences"])
    assert outcome["value"]["acquisition_context_origin"] == "foreground_coordinator_not_converter"
    assert "acquisition_context" not in converter and "acquisition_context_origin" not in converter
    first_snapshots = [snap["sources"]["svn"]["attempts"][0]["document_source_evidence"]["documents"][0]
                       for snap in checkpoints
                       if snap["sources"]["svn"]["attempts"][0]["document_source_evidence"]["documents"]]
    assert any([entry["state"] for entry in doc.get("cleanup", [])] == ["removed", "pending"]
               for doc in first_snapshots)


def test_unknown_fetch_stops_and_cannot_restart(monkeypatch, tmp_path):
    manifest, attempt, kwargs = setup_run(tmp_path)
    calls = install_workers(monkeypatch, fetch_state="fetch_outcome_unknown")
    result = execute(tmp_path, manifest, attempt, kwargs)
    assert result["state"] == "reconciliation_required" and len(calls["fetch"]) == 1
    assert result["budget"]["attempts"][0]["outcome"] == "unknown" and not calls["extract"]
    with pytest.raises(ValueError):
        execute(tmp_path, manifest, attempt, kwargs)
    assert len(calls["fetch"]) == 1


@pytest.mark.parametrize("status,expected", [(401, "access_denied"), (403, "access_denied"),
                                             (429, "rate_limited"), (451, "access_denied")])
def test_access_denial_stops_after_sealing_first_receipt(monkeypatch, tmp_path, status, expected):
    manifest, attempt, kwargs = setup_run(tmp_path)
    calls = install_workers(monkeypatch, denial=status)
    result = execute(tmp_path, manifest, attempt, kwargs)
    assert result["state"] == expected and len(calls["fetch"]) == 1 and not calls["extract"]
    assert result["documents"][0]["state"] == "fetch_failed_sealed"
    assert result["budget"]["attempts"][0]["outcome"] == "failed"


def test_unknown_conversion_keeps_both_originals(monkeypatch, tmp_path):
    manifest, attempt, kwargs = setup_run(tmp_path)
    calls = install_workers(monkeypatch, extraction_state="timeout")
    result = execute(tmp_path, manifest, attempt, kwargs)
    directory = tmp_path / result["documents"][0]["directory"]
    assert result["state"] == "reconciliation_required" and len(calls["fetch"]) == 1
    assert (directory / "fetch" / "document.bin").read_bytes() == b"pdf"
    assert (directory / "extraction" / "document.bin").read_bytes() == b"pdf"


@pytest.mark.parametrize("status", ["failed", "ocr_needed"])
def test_unresolved_conversion_is_sealed_and_original_retained(monkeypatch, tmp_path, status):
    manifest, attempt, kwargs = setup_run(tmp_path, total=10, retention="keep_unresolved_until_review")
    install_workers(monkeypatch, extraction_state=status)
    result = execute(tmp_path, manifest, attempt, kwargs)
    document = result["documents"][0]
    directory = tmp_path / document["directory"]
    assert result["state"] == "budget_exhausted"
    assert document["state"] == "extraction_sealed" and document["extraction_state"] == status
    assert document["original_retention"] == "retained_unresolved"
    assert (directory / "sealed" / "artifact.json").is_file()
    assert (directory / "fetch" / "document.bin").read_bytes() == b"pdf"
    assert (directory / "extraction" / "document.bin").read_bytes() == b"pdf"


def test_checkpoint_interruption_leaves_intent_and_refuses_restart(monkeypatch, tmp_path):
    manifest, attempt, kwargs = setup_run(tmp_path)
    calls = install_workers(monkeypatch)
    snapshots = []
    def save(_, current):
        snapshots.append(copy.deepcopy(current))
        if len(snapshots) == 2:
            raise OSError("synthetic checkpoint interruption")
    with pytest.raises(OSError):
        execute(tmp_path, manifest, attempt, kwargs, save)
    assert not calls["fetch"]
    assert attempt["document_source_evidence"]["state"] == "reconciliation_required"
    assert attempt["document_source_evidence"]["budget"]["attempts"][0]["outcome"] is None
    with pytest.raises(ValueError):
        execute(tmp_path, manifest, attempt, kwargs)
    assert not calls["fetch"]


def test_unsupported_html_landing_is_sealed_before_cleanup(monkeypatch, tmp_path):
    manifest, attempt, kwargs = setup_run(tmp_path)
    policy = json.loads(kwargs['policy_raw'])
    policy['limits'].update(max_documents=1, max_document_bytes=1024, max_total_bytes=1024)
    raw = events.encoded(policy)
    kwargs.update(policy_raw=raw, selection=preflight(kwargs['plan'], kwargs['candidates'], raw=raw),
                  expected_policy_sha256=events.sha256(raw))
    body = b'<html><h1>Property</h1><a href="/flyer.pdf">Flyer</a></html>'

    def fetch(job, job_path, outdir):
        receipt = {**fetch_receipt(), 'request_url': job['url'], 'final_url': job['url'],
                   'hops': [{'url': job['url'], 'status': 200}], 'body_bytes': len(body),
                   'body_sha256': events.sha256(body)}
        private_export.write(outdir, [('document.bin', body)])
        return {'state': 'completed', 'directory': str(outdir), 'job_sha256': 'a' * 64,
                'receipt': receipt, 'body_path': str(outdir / 'document.bin')}

    def extract(fetched, job_path, outdir, **_kwargs):
        private_export.write(outdir, [('document.bin', body)])
        value = text_artifact('failed')
        value['source'].update(url=fetched['receipt']['final_url'], observed_at=fetched['receipt']['finished_at'],
                               sha256=events.sha256(body), byte_length=len(body))
        value['extraction']['error_code'] = 'unsupported'
        return {'state': 'failed', 'directory': str(outdir), 'artifact': value,
                'input_path': str(outdir / 'document.bin')}

    monkeypatch.setattr(document_pipeline.doc_fetch_process, 'run', fetch)
    monkeypatch.setattr(document_pipeline.doc_extract_process, 'run', extract)
    monkeypatch.setattr(document_pipeline.time, 'monotonic', lambda: 1.0)
    result = execute(tmp_path, manifest, attempt, kwargs)
    assert result['state'] == 'selection_settled'
    document = result['documents'][0]
    assert document['original_retention'] == 'both_owned_body_copies_removed'
    sealed = tmp_path / document['directory'] / 'sealed'
    assert 'landing-page.json' in document['sealed']['files']
    plan = json.loads((sealed / 'plan.json').read_bytes())
    assert plan['events'][0]['facts'][0]['value']['pageContext']['links'][0]['href'] == '/flyer.pdf'
    assert 'pageContext' not in json.loads((sealed / 'converter-artifact.json').read_bytes())
    from test_document_recovery import recover
    verified = recover(tmp_path, manifest, attempt, kwargs)
    assert verified['state'] == 'sealed_outputs_reconciled'
    (sealed / 'landing-page.json').write_bytes(b'{}')
    refused = recover(tmp_path, manifest, attempt, kwargs)
    assert refused['state'] == 'incomplete'
    assert refused['documents'][0]['state'] == 'unresolved'
