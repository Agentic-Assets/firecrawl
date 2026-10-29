import copy

import pytest
from source_evidence import document_automatic, document_batch, document_jobs, document_pipeline, document_recovery, events
from test_document_acquisition_policy import CODE, TARGET
from test_document_pipeline import install_workers, setup_run


@pytest.mark.parametrize('interrupt', [False, True])
@pytest.mark.parametrize('extraction_state', ['success', 'ocr_needed', 'failed'])
def test_complement_policy_seals_and_replays_two_independent_jobs(monkeypatch, tmp_path, interrupt, extraction_state):
    from source_evidence import automatic, document_acquisition_policy, document_seal
    from source_evidence.document_process import DocumentProcessCleanupUnknown
    manifest, attempt, kwargs = setup_run(tmp_path, retention='keep_unresolved_until_review')
    policy = __import__('json').loads(kwargs['policy_raw'])
    policy.update(schema_version=document_acquisition_policy.SCHEMA_V2,
                  pdf_complement={'mode': 'poppler_pdf_text_v1', 'pdftotext_path': '/usr/bin/pdftotext'})
    policy['limits']['max_documents'] = 1
    raw = events.encoded(policy)
    plans = [kwargs['plan']]
    candidates = document_batch.discover(plans)
    selection = document_batch.prepare(plans, candidates, raw, expected_policy_sha256=events.sha256(raw),
                                       target_sha256=TARGET, collector_git_sha=CODE)
    kwargs.update(plan=plans, candidates=candidates, selection=selection, policy_raw=raw,
                  expected_policy_sha256=events.sha256(raw))
    manifest.update(run_id='test-generation', config={'sources': ['svn']})
    attempt.update(rc=1, finished_at='2026-10-28T12:00:00Z', document_collection_settlement={
        'schema_version': 'cre-document-collection-settlement/v1', 'attempt_number': 1,
        'collector_git_sha': CODE, 'rc': 1, 'finished_at': '2026-10-28T12:00:00Z'})
    policy_path = tmp_path / 'policy.json'
    policy_path.write_bytes(raw)
    policy_path.chmod(0o600)
    manifest['config']['automatic_source_evidence'] = {'source_keys': ['svn'], 'collector_git_sha': CODE,
        'target_sha256': TARGET, 'policy_sha256': 'b' * 64, 'transport': {'kind': 'test'},
        'document_text_formats': {'svn': document_jobs.FORMAT}}
    manifest['config']['source_document_acquisition'] = {'source_keys': ['svn'], 'collector_git_sha': CODE,
        'target_sha256': TARGET, 'policy_sha256': events.sha256(raw), 'policy_path': str(policy_path)}
    monkeypatch.setattr(document_jobs.automatic, '_verify_config', lambda *args: None)
    monkeypatch.setattr(document_jobs.document_automatic, '_verify_config', lambda *args: None)
    install_workers(monkeypatch, extraction_state=extraction_state)
    if extraction_state == 'failed':
        original_extract = document_pipeline.doc_extract_process.run
        def unsupported(*args, **kwargs):
            result = original_extract(*args, **kwargs)
            result['artifact']['extraction']['error_code'] = 'unsupported'
            return result
        monkeypatch.setattr(document_pipeline.doc_extract_process, 'run', unsupported)
    def complement(input_path, first, receipt, outdir, *, executable, source_key):
        if interrupt:
            raise DocumentProcessCleanupUnknown('simulated unknown settlement')
        text = 'Complementary text'
        return {'schema_version': 'cre-document-text/v1', 'source': dict(first['source']),
                'parser': {'name': 'poppler-pdftotext', 'version': '1.2',
                           'config': {'executable': executable, 'ocr': False, 'timeout_seconds': 120,
                                      'max_output_file_bytes': 4 * 1024 * 1024, 'max_text_bytes': 4 * 1024 * 1024}},
                'extraction': {'status': 'extracted', 'coverage': 'unknown', 'text_format': 'plain',
                               'text': text, 'text_sha256': events.sha256(text.encode()),
                               'relationship': 'complement_of', 'previous_attempt_sha256': events.sha256(events.encoded(first))}}
    monkeypatch.setattr(document_pipeline.doc_complement_process, 'run', complement)
    if interrupt:
        with pytest.raises(DocumentProcessCleanupUnknown):
            document_pipeline.run(tmp_path, manifest, 'svn', attempt, lambda *args: None, **kwargs)
        document = attempt['document_source_evidence']['documents'][0]
        assert document['state'] == 'complement_intent' and 'primary_sealed' in document
        assert (tmp_path / document['directory'] / 'fetch' / 'document.bin').exists()
        result = document_recovery.sealed(tmp_path, manifest, 'svn', attempt, lambda *args: None,
            plan=plans, candidates=candidates, policy_raw=raw, selection=selection,
            expected_policy_sha256=events.sha256(raw), target_sha256=TARGET, collector_git_sha=CODE, cleanup=True)
        assert result['state'] == 'incomplete'
        assert result['documents'][0]['primary_seal'] == 'reverified_complement_unresolved'
        assert (tmp_path / document['directory'] / 'fetch' / 'document.bin').exists()
        return
    state = document_pipeline.run(tmp_path, manifest, 'svn', attempt, lambda *args: None, **kwargs)
    document = state['documents'][0]
    if extraction_state != 'success':
        assert document['original_retention'] == 'retained_unresolved'
        assert (tmp_path / document['directory'] / 'fetch' / 'document.bin').exists()
    first_job = document_jobs.mark(tmp_path, manifest, 'svn', attempt, document, lambda *args: None)
    second_job = document['complement_append']
    first_plan = document_jobs.prepare_job(tmp_path, manifest, 'svn', attempt, document, first_job)
    second_plan = document_jobs.prepare_job(tmp_path, manifest, 'svn', attempt, document, second_job)
    assert first_plan['plan_sha256'] != second_plan['plan_sha256']
    assert first_plan['events'][0]['facts'][0]['value']['extraction']['status'] == (
        'extracted' if extraction_state == 'success' else extraction_state)
    assert second_plan['events'][0]['facts'][0]['value']['extraction']['relationship'] == 'complement_of'
    first_job['state'], second_job['state'] = 'appended', 'outcome_unknown'
    assert document_automatic.append_incomplete(manifest)
    automatic.prepare_resume(manifest, replay_unknown=True)
    assert first_job['state'] == 'appended' and second_job['state'] == 'pending'
    assert second_job['exact_replay_required'] is True
    manifest['config']['automatic_source_evidence']['env_file'] = '/unused/test-env'
    writes = []
    monkeypatch.setattr(automatic, 'append_sql', lambda plan: plan)
    monkeypatch.setattr(automatic.cli, 'run_psql', lambda plan, *args, **kwargs: writes.append(plan['plan_sha256']))
    automatic.pump(tmp_path, manifest, lambda *args: None)
    assert writes == [second_job['plan_sha256']]
    assert first_job['state'] == second_job['state'] == 'appended'
    marker = document['sealed']
    seal = tmp_path / document['directory'] / 'sealed'
    context = __import__('json').loads((seal / 'acquisition.json').read_bytes())
    checked = document_seal.read(seal, expected_context=context, expected_marker=marker)
    assert checked['complement']['extraction']['coverage'] == 'unknown'
    document.pop('sealed')
    recovery = document_recovery.sealed(tmp_path, manifest, 'svn', attempt, lambda *args: None,
        plan=plans, candidates=candidates, policy_raw=raw, selection=selection,
        expected_policy_sha256=events.sha256(raw), target_sha256=TARGET, collector_git_sha=CODE, cleanup=False)
    assert recovery['state'] == 'incomplete'
    assert recovery['documents'][0]['primary_seal'] == 'reverified_complement_unresolved'
    document['sealed'] = marker
    (seal / 'complement-plan.json').write_bytes(b'{}')
    with pytest.raises(ValueError):
        document_jobs.prepare_job(tmp_path, manifest, 'svn', attempt, document, second_job)


def prepared_run(monkeypatch, tmp_path, *, extraction_state="success"):
    manifest, attempt, kwargs = setup_run(tmp_path, retention="keep_unresolved_until_review")
    plans = [kwargs["plan"]]
    candidates = document_batch.discover(plans)
    selection = document_batch.prepare(plans, candidates, kwargs["policy_raw"],
        expected_policy_sha256=kwargs["expected_policy_sha256"], target_sha256=TARGET, collector_git_sha=CODE)
    kwargs.update(plan=plans, candidates=candidates, selection=selection)
    manifest.update(run_id="test-generation", config={"sources": ["svn"]})
    attempt.update(rc=1, finished_at="2026-10-28T12:00:00Z", document_collection_settlement={
        "schema_version": "cre-document-collection-settlement/v1", "attempt_number": 1,
        "collector_git_sha": CODE, "rc": 1, "finished_at": "2026-10-28T12:00:00Z"})
    policy_path = tmp_path / "document-policy.json"
    policy_path.write_bytes(kwargs["policy_raw"])
    policy_path.chmod(0o600)
    manifest["config"]["automatic_source_evidence"] = {"source_keys": ["svn"], "collector_git_sha": CODE,
        "target_sha256": TARGET, "policy_sha256": "b" * 64, "transport": {"kind": "test"},
        "document_text_formats": {"svn": document_jobs.FORMAT}}
    manifest["config"]["source_document_acquisition"] = {"source_keys": ["svn"], "collector_git_sha": CODE,
        "target_sha256": TARGET, "policy_sha256": kwargs["expected_policy_sha256"], "policy_path": str(policy_path)}
    monkeypatch.setattr(document_jobs.automatic, "_verify_config", lambda *args: None)
    monkeypatch.setattr(document_jobs.document_automatic, "_verify_config", lambda *args: None)
    install_workers(monkeypatch, extraction_state=extraction_state)
    state = document_pipeline.run(tmp_path, manifest, "svn", attempt, lambda *args: None, **kwargs)
    return manifest, attempt, state


def test_78_sealed_jobs_rederive_exact_plans_without_original_bytes(monkeypatch, tmp_path):
    manifest, attempt, state = prepared_run(monkeypatch, tmp_path)
    assert len(state["documents"]) == 78
    calls = []
    for document in state["documents"]:
        job = document_jobs.mark(tmp_path, manifest, "svn", attempt, document, lambda *args: calls.append(1))
        assert state["database_append"] == "managed_by_append_jobs"
        plan = document_jobs.prepare_job(tmp_path, manifest, "svn", attempt, document, job)
        assert job["state"] == "pending" and job["kind"] == "document_text"
        assert plan["plan_sha256"] == job["plan_sha256"] and plan["events"][0]["capture_id"] == job["capture_id"]
        assert plan["events"][0]["facts"][0]["value"]["acquisition_context"]["candidate"]["occurrences"]
    assert len(calls) == 78


def test_sealed_recovery_accepts_managed_append_marker(monkeypatch, tmp_path):
    manifest, attempt, state = prepared_run(monkeypatch, tmp_path)
    document_jobs.mark(tmp_path, manifest, "svn", attempt, state["documents"][0], lambda *args: None)
    plans = document_automatic._sealed_plans(tmp_path, "svn", attempt)
    candidates = document_batch.discover(plans)
    config = manifest["config"]["source_document_acquisition"]
    raw = (tmp_path / "document-policy.json").read_bytes()
    selected = document_batch.prepare(plans, candidates, raw, expected_policy_sha256=config["policy_sha256"],
                                      target_sha256=TARGET, collector_git_sha=CODE)
    result = document_recovery.sealed(tmp_path, manifest, "svn", attempt, lambda *args: None,
        plan=plans, candidates=candidates, policy_raw=raw, selection=selected,
        expected_policy_sha256=config["policy_sha256"], target_sha256=TARGET, collector_git_sha=CODE, cleanup=False)
    assert result["state"] == "sealed_outputs_reconciled"
    assert state["database_append"] == "managed_by_append_jobs"


@pytest.mark.parametrize("extraction_state", ["success", "ocr_needed", "failed"])
def test_individual_sealed_documents_can_append_despite_partial_selection(monkeypatch, tmp_path, extraction_state):
    manifest, attempt, state = prepared_run(monkeypatch, tmp_path, extraction_state=extraction_state)
    state["state"] = "reconciliation_required"
    document = state["documents"][0]
    job = document_jobs.mark(tmp_path, manifest, "svn", attempt, document, lambda *args: None)
    plan = document_jobs.prepare_job(tmp_path, manifest, "svn", attempt, document, job)
    expected = "extracted" if extraction_state == "success" else extraction_state
    assert plan["events"][0]["facts"][0]["value"]["extraction"]["status"] == expected


def test_existing_unknown_job_is_not_rearmed(monkeypatch, tmp_path):
    manifest, attempt, state = prepared_run(monkeypatch, tmp_path)
    document = state["documents"][0]
    job = document_jobs.mark(tmp_path, manifest, "svn", attempt, document, lambda *args: None)
    job["state"] = "outcome_unknown"
    before = copy.deepcopy(job)
    assert document_jobs.mark(tmp_path, manifest, "svn", attempt, document, lambda *args: pytest.fail("must not save")) == before
    assert job["state"] == "outcome_unknown"


@pytest.mark.parametrize("mutation", ["policy", "missing", "seal", "marker", "settlement", "format"])
def test_refuses_changed_custody_or_policy(monkeypatch, tmp_path, mutation):
    manifest, attempt, state = prepared_run(monkeypatch, tmp_path)
    document = state["documents"][0]
    job = document_jobs.mark(tmp_path, manifest, "svn", attempt, document, lambda *args: None)
    seal = tmp_path / document["directory"] / "sealed"
    if mutation == "policy":
        path = tmp_path / "document-policy.json"
        path.write_bytes(b"{}")
    elif mutation == "missing":
        (seal / "artifact.json").unlink()
    elif mutation == "seal":
        (seal / "plan.json").write_bytes(b"{}")
    elif mutation == "marker":
        document["sealed"]["capture_id"] = "changed"
    elif mutation == "settlement":
        attempt["document_collection_settlement"]["rc"] = False
    else:
        manifest["config"]["automatic_source_evidence"]["document_text_formats"] = {}
    with pytest.raises((ValueError, OSError, KeyError)):
        document_jobs.prepare_job(tmp_path, manifest, "svn", attempt, document, job)


def test_landing_seal_rederives_append_plan_and_rejects_tamper(monkeypatch, tmp_path):
    import json
    import shutil

    from source_evidence import document_landing, document_seal

    manifest, attempt, state = prepared_run(monkeypatch, tmp_path, extraction_state='failed')
    document = state['documents'][0]
    seal = tmp_path / document['directory'] / 'sealed'
    context = json.loads((seal / 'acquisition.json').read_bytes())
    artifact = json.loads((seal / 'converter-artifact.json').read_bytes())
    body = b'<html><a href="/flyer.pdf">Flyer</a></html>'
    context['receipt'].update(body_sha256=events.sha256(body), body_bytes=len(body))
    artifact['source'].update(sha256=events.sha256(body), byte_length=len(body))
    artifact['extraction']['error_code'] = 'unsupported'
    landing = document_landing.page_context(body, final_url=context['receipt']['final_url'])
    shutil.rmtree(seal)
    document['sealed'] = document_seal.write(seal, context, artifact, landing=landing)
    job = document_jobs.mark(tmp_path, manifest, 'svn', attempt, document, lambda *_: None)
    plan = document_jobs.prepare_job(tmp_path, manifest, 'svn', attempt, document, job)
    assert plan['events'][0]['facts'][0]['value']['pageContext']['links'][0]['href'] == '/flyer.pdf'
    (seal / 'landing-page.json').write_bytes(b'{}')
    with pytest.raises(ValueError):
        document_jobs.prepare_job(tmp_path, manifest, 'svn', attempt, document, job)
