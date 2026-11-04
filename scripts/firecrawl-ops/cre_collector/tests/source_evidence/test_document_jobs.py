import copy

import pytest
from source_evidence import (
    document_automatic,
    document_batch,
    document_jobs,
    document_pipeline,
    document_recovery,
    events,
)
from test_document_acquisition_policy import CODE, TARGET
from test_document_pipeline import install_workers, setup_run


@pytest.mark.parametrize('mode', ['poppler_pdf_text_v1', 'poppler_pdf_layout_v1'])
@pytest.mark.parametrize('interrupt', [False, True])
@pytest.mark.parametrize('extraction_state', ['success', 'ocr_needed', 'failed'])
def test_complement_policy_seals_and_replays_two_independent_jobs(monkeypatch, tmp_path, interrupt, extraction_state, mode):
    from source_evidence import automatic, document_acquisition_policy, document_seal
    from source_evidence.document_process import DocumentProcessCleanupUnknown
    manifest, attempt, kwargs = setup_run(tmp_path, retention='keep_unresolved_until_review')
    policy = __import__('json').loads(kwargs['policy_raw'])
    policy.update(schema_version=document_acquisition_policy.SCHEMA_V2,
                  pdf_complement={'mode': mode, 'pdftotext_path': '/usr/bin/pdftotext'})
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
    def complement(input_path, first, receipt, outdir, *, executable, source_key, mode):
        if interrupt:
            raise DocumentProcessCleanupUnknown('simulated unknown settlement')
        text = 'Complementary text'
        return {'schema_version': 'cre-document-text/v1', 'source': dict(first['source']),
                'parser': {'name': 'poppler-pdftotext', 'version': '1.2',
                           'config': {'executable': executable, 'ocr': False, 'timeout_seconds': 120,
                                      'max_output_file_bytes': 4 * 1024 * 1024, 'max_text_bytes': 4 * 1024 * 1024,
                                      **({'mode': mode} if mode == 'poppler_pdf_layout_v1' else {})}},
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

@pytest.mark.parametrize('version', ['v4', 'v5'])
@pytest.mark.parametrize('interrupt', [False, True, 'completed'])
@pytest.mark.parametrize('extraction_state', ['success'])
def test_sparse_ocr_preserves_prior_custody_and_replays_all_three_jobs(monkeypatch, tmp_path, interrupt, extraction_state, version):
    if version == 'v5' and interrupt is False:
        pytest.skip('the v5 full Docling path is covered separately')
    from source_evidence import automatic, document_acquisition_policy
    from source_evidence.document_process import DocumentProcessCleanupUnknown
    manifest, attempt, kwargs = setup_run(tmp_path, retention='keep_unresolved_until_review')
    policy = __import__('json').loads(kwargs['policy_raw'])
    policy.update(schema_version=document_acquisition_policy.SCHEMA_V3,
                  sparse_ocr={'mode': 'sparse_ocr_v1', 'strategy': 'all_pages', 'max_pages': 2},
                  pdf_complement={'mode': 'poppler_pdf_text_v1', 'pdftotext_path': '/usr/bin/pdftotext'})
    if version == 'v4':
        policy.update(schema_version=document_acquisition_policy.SCHEMA_V4,
                      selection_order='property_occurrences_first_v1')
    if version == 'v5':
        policy.update(schema_version=document_acquisition_policy.SCHEMA_V5,
                      selection_order='property_occurrences_first_v1',
                      docling_json={'mode': 'docling_json_v1', 'max_pages': 1})
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
    def complement(input_path, first, receipt, outdir, *, executable, source_key, mode):
        text = 'Complementary text'
        return {'schema_version': 'cre-document-text/v1', 'source': dict(first['source']),
                'parser': {'name': 'poppler-pdftotext', 'version': '1.2',
                           'config': {'executable': executable, 'ocr': False, 'timeout_seconds': 120,
                                      'max_output_file_bytes': 4 * 1024 * 1024, 'max_text_bytes': 4 * 1024 * 1024,
                                      **({'mode': mode} if mode == 'poppler_pdf_layout_v1' else {})}},
                'extraction': {'status': 'extracted', 'coverage': 'unknown', 'text_format': 'plain',
                               'text': text, 'text_sha256': events.sha256(text.encode()),
                               'relationship': 'complement_of', 'previous_attempt_sha256': events.sha256(events.encoded(first))}}
    monkeypatch.setattr(document_pipeline.doc_complement_process, 'run', complement)
    def ocr(input_path, first, second, receipt, outdir, *, source_key, policy, prior_dir):
        assert (prior_dir / 'converter-artifact.json').read_bytes() == events.encoded(first)
        assert (prior_dir / 'complement-converter-artifact.json').read_bytes() == events.encoded(second)
        if interrupt is True:
            raise DocumentProcessCleanupUnknown('simulated OCR settlement unknown')
        text = 'OCR text'
        result = {'schema_version': 'cre-document-text/v1', 'source': dict(first['source']),
                'parser': {'name': 'pdfium-tesseract-sparse', 'version': '1', 'config': {
                    'image': 'quay.io/docling-project/docling-serve-cpu@sha256:528f52f5ce56be1739df117560512ffaa483be53934bf5b0458b4ca088b1a6b0',
                    'pypdfium2': '5.8.0', 'pdfium': '149.0.7825.0', 'tesseract': '4.1.1',
                    'language': 'eng', 'psm': 11, 'osd': False, 'dpi': 200, 'max_pixels_per_page': 25_000_000,
                    'max_pages': 2, 'trigger_policy': 'all_pages', 'timeout_seconds': 210, 'max_text_bytes': 4 * 1024 * 1024, 'max_layout_bytes': 1024 * 1024,
                    'per_page_timeout_seconds': 60, 'network': 'none', 'cpu': 1, 'memory_bytes': 3 * 1024**3,
                    'worker_sha256': events.sha256((__import__('pathlib').Path(document_pipeline.__file__).parent.parent / 'document_text' / 'sparse_ocr_worker.py').read_bytes())}},
                'extraction': {'status': 'extracted', 'coverage': 'unknown', 'text_format': 'plain',
                    'text': text, 'text_sha256': events.sha256(text.encode()), 'converter_exit_code': 0, 'relationship': 'complement_of',
                    'previous_attempt_sha256': events.sha256(events.encoded(second)),
                    'page_count': 3, 'pages': [{'page': 1, 'worker_exit': 0, 'text_bytes': 4, 'text_start_byte': 0, 'text_end_byte': 4, 'layout_tsv': 'box',
                              'layout_bytes': 3, 'layout_sha256': events.sha256(b'box')},
                             {'page': 2, 'worker_exit': 1, 'text_bytes': 4, 'text_start_byte': 4, 'text_end_byte': 8, 'layout_tsv': 'raw',
                              'layout_bytes': 3, 'layout_sha256': events.sha256(b'raw'), 'layout_error': 'layout_partial'}],
                    'omitted_pages': 1, 'layout_omitted_pages': 2,
                    'warnings': ['coverage_unknown', 'pages_omitted', 'layout_omitted', 'page_failure_or_partial_output']}}
        if interrupt == 'completed':
            from source_evidence import doc_sparse_ocr_process, private_export
            nonce = 'a' * 32
            intent = {'schema_version': 'cre-sparse-ocr-owner/v1', 'name': 'cre-sparse-' + nonce, 'nonce': nonce,
                'source_sha256': receipt['body_sha256'], 'prior_attempt_sha256': [events.sha256(events.encoded(first)),
                events.sha256(events.encoded(second))], 'worker_sha256': result['parser']['config']['worker_sha256'],
                'config_sha256': events.sha256(events.encoded(result['parser']['config'])), 'uid': __import__('os').getuid()}
            settled = {'schema_version': 'cre-sparse-ocr-settlement/v1', 'intent_sha256': events.sha256(events.encoded(intent)),
                       'container_id': 'b' * 64, 'state': 'removed_verified'}
            artifact_raw = events.encoded(result)
            marker = doc_sparse_ocr_process._completion_marker(artifact_raw, result, first, second, receipt,
                source_key, policy, 0, events.sha256(events.encoded(settled)))
            private_export.write(outdir, [('owner-intent.json', events.encoded(intent)),
                ('owner-settled.json', events.encoded(settled)), ('sparse-ocr.json', artifact_raw)])
            private_export.write(outdir / 'ocr-completion', [('marker.json', events.encoded(marker))])
            raise RuntimeError('simulated parent crash after settled OCR completion')
        return result
    monkeypatch.setattr(document_pipeline.doc_sparse_ocr_process, 'run', ocr)
    if interrupt:
        with pytest.raises(DocumentProcessCleanupUnknown if interrupt is True else RuntimeError):
            document_pipeline.run(tmp_path, manifest, 'svn', attempt, lambda *args: None, **kwargs)
        document = attempt['document_source_evidence']['documents'][0]
        assert document['state'] == 'ocr_intent' and 'prior_sealed' in document
        assert (tmp_path / document['directory'] / 'fetch' / 'document.bin').exists()
        if interrupt == 'completed':
            outdir = tmp_path / document['directory'] / 'ocr'
            completion = outdir / 'ocr-completion' / 'marker.json'
            saved_completion = completion.read_bytes()
            completion.write_bytes(b'{')
            blocked = document_recovery.sealed(tmp_path, manifest, 'svn', attempt, lambda *args: None,
                plan=plans, candidates=candidates, policy_raw=raw, selection=selection,
                expected_policy_sha256=events.sha256(raw), target_sha256=TARGET, collector_git_sha=CODE, cleanup=True)
            assert blocked['state'] == 'incomplete' and 'sealed' not in document
            completion.write_bytes(saved_completion)
            import json
            original_marker = json.loads(saved_completion)
            for key in ('artifact_sha256', 'anydoc_sha256', 'complement_sha256', 'receipt_sha256',
                        'source_key', 'policy_sha256', 'worker_sha256', 'settlement_sha256'):
                changed = dict(original_marker)
                changed[key] = 'changed'
                completion.write_bytes(events.encoded(changed))
                blocked = document_recovery.sealed(tmp_path, manifest, 'svn', attempt, lambda *args: None,
                    plan=plans, candidates=candidates, policy_raw=raw, selection=selection,
                    expected_policy_sha256=events.sha256(raw), target_sha256=TARGET, collector_git_sha=CODE, cleanup=True)
                assert blocked['state'] == 'incomplete' and 'sealed' not in document
            completion.write_bytes(saved_completion)
            settled = outdir / 'owner-settled.json'
            saved = settled.read_bytes()
            settled.write_bytes(b'{}')
            blocked = document_recovery.sealed(tmp_path, manifest, 'svn', attempt, lambda *args: None,
                plan=plans, candidates=candidates, policy_raw=raw, selection=selection,
                expected_policy_sha256=events.sha256(raw), target_sha256=TARGET, collector_git_sha=CODE, cleanup=True)
            assert blocked['state'] == 'incomplete' and 'sealed' not in document
            settled.write_bytes(saved)
            body = tmp_path / document['directory'] / 'fetch' / 'document.bin'
            saved_body = body.read_bytes()
            body.write_bytes(b'tampered')
            blocked = document_recovery.sealed(tmp_path, manifest, 'svn', attempt, lambda *args: None,
                plan=plans, candidates=candidates, policy_raw=raw, selection=selection,
                expected_policy_sha256=events.sha256(raw), target_sha256=TARGET, collector_git_sha=CODE, cleanup=True)
            assert blocked['state'] == 'incomplete' and 'sealed' not in document
            body.write_bytes(saved_body)
        monkeypatch.setattr(document_pipeline.doc_sparse_ocr_process.subprocess, 'Popen',
                            lambda *a, **k: pytest.fail('recovery must not spawn'))
        recovery = document_recovery.sealed(tmp_path, manifest, 'svn', attempt, lambda *args: None,
            plan=plans, candidates=candidates, policy_raw=raw, selection=selection,
            expected_policy_sha256=events.sha256(raw), target_sha256=TARGET, collector_git_sha=CODE, cleanup=True)
        if interrupt is True:
            assert recovery['state'] == 'incomplete'
            return
        if version == 'v5':
            assert recovery['state'] == 'incomplete'
            assert recovery['documents'][0]['reason'] == 'settled_ocr_reverified_docling_not_started'
            assert document['state'] == 'docling_prior_sealed' and 'sealed' not in document
            assert 'append' not in document and 'docling_append' not in document
            assert attempt['document_source_evidence']['budget']['attempts'][0]['outcome'] is None
            prior_dir = tmp_path / document['directory'] / 'docling-prior-sealed'
            marker_before = copy.deepcopy(document['docling_prior_sealed'])
            altered = prior_dir / 'ocr-plan.json'; original = altered.read_bytes(); altered.write_bytes(b'{}')
            blocked = document_recovery.sealed(tmp_path, manifest, 'svn', attempt, lambda *args: None,
                plan=plans, candidates=candidates, policy_raw=raw, selection=selection,
                expected_policy_sha256=events.sha256(raw), target_sha256=TARGET, collector_git_sha=CODE, cleanup=True)
            assert blocked['state'] == 'incomplete' and 'sealed' not in document
            altered.write_bytes(original)
            replayed = document_recovery.sealed(tmp_path, manifest, 'svn', attempt, lambda *args: None,
                plan=plans, candidates=candidates, policy_raw=raw, selection=selection,
                expected_policy_sha256=events.sha256(raw), target_sha256=TARGET, collector_git_sha=CODE, cleanup=True)
            assert replayed['state'] == 'incomplete'
            assert replayed['documents'][0]['reason'] == 'settled_ocr_reverified_docling_not_started'
            assert document['docling_prior_sealed'] == marker_before and 'sealed' not in document
            assert (tmp_path / document['directory'] / 'fetch/document.bin').is_file()
            assert (tmp_path / document['directory'] / 'extraction/document.bin').is_file()
            return
        assert recovery['state'] == 'sealed_outputs_reconciled'
        assert recovery['documents'][0]['reason'] == 'settled_ocr_completion_replayed'
        assert document['state'] == 'extraction_sealed'
        assert attempt['document_source_evidence']['budget']['attempts'][0]['outcome'] is None
        assert document_jobs.mark(tmp_path, manifest, 'svn', attempt, document, lambda *args: None)['state'] == 'pending'
        assert document['ocr_append']['state'] == 'pending'
        return
    state = document_pipeline.run(tmp_path, manifest, 'svn', attempt, lambda *args: None, **kwargs)
    document = state['documents'][0]
    assert document['original_retention'] == 'retained_unresolved'
    first_job = document_jobs.mark(tmp_path, manifest, 'svn', attempt, document, lambda *args: None)
    second_job, third_job = document['complement_append'], document['ocr_append']
    plans_out = [document_jobs.prepare_job(tmp_path, manifest, 'svn', attempt, document, job)
                 for job in (first_job, second_job, third_job)]
    assert len({plan['plan_sha256'] for plan in plans_out}) == 3
    assert plans_out[2]['events'][0]['facts'][0]['value']['extraction']['pages'][1]['layout_tsv'] == 'raw'
    import json

    from source_evidence import doc_sparse_ocr_process, document_seal
    seal_dir = tmp_path / document['directory'] / 'sealed'
    context = json.loads((seal_dir / 'acquisition.json').read_bytes())
    checked = document_seal.read(seal_dir, expected_context=context, expected_marker=document['sealed'])
    bad_page = copy.deepcopy(checked['ocr'])
    bad_page['extraction']['pages'][0]['text_bytes'] += 1
    with pytest.raises(ValueError, match='page text bytes'):
        doc_sparse_ocr_process.validate(bad_page, checked['artifact'], checked['complement'], context['receipt'],
                                        'svn', selection['sparse_ocr'])
    bad_page = copy.deepcopy(checked['ocr'])
    text = 'éabcdef'
    bad_page['extraction']['text'] = text
    bad_page['extraction']['text_sha256'] = events.sha256(text.encode())
    bad_page['extraction']['pages'][0].update(text_bytes=1, text_end_byte=1)
    bad_page['extraction']['pages'][1].update(text_bytes=7, text_start_byte=1, text_end_byte=8)
    with pytest.raises(ValueError, match='UTF-8'):
        doc_sparse_ocr_process.validate(bad_page, checked['artifact'], checked['complement'], context['receipt'],
                                        'svn', selection['sparse_ocr'])
    bad_page = copy.deepcopy(checked['ocr'])
    bad_page['extraction']['warnings'].remove('page_failure_or_partial_output')
    with pytest.raises(ValueError, match='warnings'):
        doc_sparse_ocr_process.validate(bad_page, checked['artifact'], checked['complement'], context['receipt'],
                                        'svn', selection['sparse_ocr'])
    oversized = copy.deepcopy(checked['ocr'])
    oversized['extraction']['pages'][0]['extra'] = 'x' * (2 * 1024 * 1024)
    with pytest.raises(ValueError, match='layout bounds'):
        doc_sparse_ocr_process.validate(oversized, checked['artifact'], checked['complement'], context['receipt'],
                                        'svn', selection['sparse_ocr'])
    oversized = copy.deepcopy(checked['ocr'])
    text = 'x' * (4 * 1024 * 1024 - 4)
    oversized['extraction']['text'] = text
    oversized['extraction']['text_sha256'] = events.sha256(text.encode())
    oversized['extraction']['pages'][0]['text_end_byte'] = len(text)
    oversized['extraction']['pages'][0]['text_bytes'] = len(text)
    oversized['extraction']['pages'][1]['text_start_byte'] = len(text)
    oversized['extraction']['pages'][1]['text_end_byte'] = len(text)
    oversized['extraction']['pages'][1]['text_bytes'] = 0
    with pytest.raises(ValueError, match='layout bounds'):
        doc_sparse_ocr_process.validate(oversized, checked['artifact'], checked['complement'], context['receipt'],
                                        'svn', selection['sparse_ocr'])
    first_job['state'] = second_job['state'] = 'appended'
    third_job['state'] = 'outcome_unknown'
    automatic.prepare_resume(manifest, replay_unknown=True)
    assert third_job['state'] == 'pending' and third_job['exact_replay_required']
    manifest['config']['automatic_source_evidence']['env_file'] = '/unused/test-env'
    writes = []
    monkeypatch.setattr(automatic, 'append_sql', lambda plan: plan)
    monkeypatch.setattr(automatic.cli, 'run_psql', lambda plan, *args, **kwargs: writes.append(plan['plan_sha256']))
    automatic.pump(tmp_path, manifest, lambda *args: None)
    assert writes == [third_job['plan_sha256']]
    recovery = document_recovery.sealed(tmp_path, manifest, 'svn', attempt, lambda *args: None,
        plan=plans, candidates=candidates, policy_raw=raw, selection=selection,
        expected_policy_sha256=events.sha256(raw), target_sha256=TARGET, collector_git_sha=CODE, cleanup=True)
    assert recovery['state'] == 'sealed_outputs_reconciled'
    assert (tmp_path / document['directory'] / 'fetch' / 'document.bin').exists()
