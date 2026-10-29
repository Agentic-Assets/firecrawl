import json
import os
from pathlib import Path

import pytest
from document_text.docling_json import artifact as native
from source_evidence import doc_docling_json_process as process
from source_evidence import events, private_export
from test_doc_fetch_process import receipt as make_receipt


def fixture():
    body = b'pdf'
    source = {'url': 'https://docs.example.test/a', 'observed_at': '2026-10-28T12:00:01.000000Z',
              'format': 'pdf', 'byte_length': len(body), 'sha256': events.sha256(body)}
    first = {'schema_version': 'cre-document-text/v1', 'source': source,
             'parser': {'name': '@firecrawl/anydoc', 'version': '0.2.4', 'config': {'ocr': 'disabled',
                        'max_bytes': 100 * 1024 * 1024, 'max_text_bytes': events.MAX_TEXT_BYTES, 'timeout_ms': 60000}},
             'extraction': {'status': 'ocr_needed', 'coverage': 'unknown', 'text': None, 'text_sha256': None}}
    second = {'schema_version': 'cre-document-text/v1', 'source': source,
              'parser': {'name': 'poppler-pdftotext', 'version': '26.10.0',
                         'config': {'executable': '/usr/bin/pdftotext', 'ocr': False, 'timeout_seconds': 120,
                                    'max_output_file_bytes': events.MAX_TEXT_BYTES, 'max_text_bytes': events.MAX_TEXT_BYTES}},
              'extraction': {'status': 'failed', 'error_code': 'empty_output', 'text': None, 'text_sha256': None,
                             'text_format': 'plain', 'relationship': 'complement_of', 'warnings': [], 'coverage': 'unknown',
                             'previous_attempt_sha256': events.sha256(events.encoded(first))}}
    worker = Path(process.__file__).resolve().parent.parent / 'document_text/docling_json/worker.py'
    config = {'image': 'quay.io/docling-project/docling-serve-cpu@sha256:528f52f5ce56be1739df117560512ffaa483be53934bf5b0458b4ca088b1a6b0',
              'worker_sha256': events.sha256(worker.read_bytes()), 'network': 'none', 'device': 'cpu', 'cpu': 1,
              'memory_bytes': 3 * 1024**3, 'max_pages': 1, 'max_text_bytes': events.MAX_TEXT_BYTES,
              'document_timeout_seconds': 90, 'client_timeout_seconds': 120, 'ocr': False, 'tables': True,
              'table_mode': 'accurate', 'generated_images': False}
    document = {'schema_name': 'DoclingDocument', 'version': '1.10.0', 'name': 'test',
                'body': {'self_ref': '#/body', 'children': []}, 'furniture': {'self_ref': '#/furniture', 'children': []},
                'pages': {'1': {'page_no': 1, 'size': {'width': 1, 'height': 1}}},
                'groups': [], 'texts': [], 'pictures': [], 'tables': [], 'unknown': {'coordinate': 0.125}}
    artifact = native.build(body, events.encoded(first), events.encoded(second), events.encoded(document),
                            config=config, page_count=1, last_page=1)
    artifact['extraction'].update(converter_exit_code=0, error_code=None)
    return body, first, second, artifact, make_receipt(), {'mode': 'docling_json_v1', 'max_pages': 1}


def test_docling_validator_and_selective_retention():
    _body, first, second, artifact, receipt, policy = fixture()
    process.validate(artifact, first, second, receipt, 'svn', policy)
    selection = {'original_retention': 'keep_unresolved_until_review', 'pdf_complement': {}, 'docling_json': policy}
    assert not process.retain_unresolved(first, second, artifact, selection)
    partial = json.loads(json.dumps(artifact))
    partial['extraction']['warnings'].append('converter_partial')
    assert process.retain_unresolved(first, second, partial, selection)
    selection['original_retention'] = 'temporary_only'
    assert not process.retain_unresolved(first, second, partial, selection)


def test_no_spawn_replay_requires_completion_and_rejects_tampering(tmp_path):
    body, first, second, artifact, receipt, policy = fixture()
    tmp_path.chmod(0o700)
    source = tmp_path / 'document.bin'; source.write_bytes(body); source.chmod(0o600)
    prior = tmp_path / 'prior'; private_export.write(prior, [('converter-artifact.json', events.encoded(first)),
                                                               ('complement-converter-artifact.json', events.encoded(second))])
    out = tmp_path / 'out'; out.mkdir(mode=0o700)
    config = artifact['parser']['config']; nonce = 'a' * 32
    intent = {'schema_version': 'cre-docling-json-owner/v1', 'name': 'cre-docling-json-' + nonce,
              'nonce': nonce, 'source_sha256': receipt['body_sha256'],
              'prior_attempt_sha256': [events.sha256(events.encoded(first)), events.sha256(events.encoded(second))],
              'worker_sha256': config['worker_sha256'], 'config_sha256': events.sha256(events.encoded(config)), 'uid': os.getuid()}
    intent_raw = events.encoded(intent)
    settled = {'schema_version': 'cre-docling-json-settlement/v1', 'intent_sha256': events.sha256(intent_raw),
               'container_id': 'b' * 64, 'state': 'removed_verified'}
    raw = events.encoded(artifact)
    for name, value in [('owner-intent.json', intent_raw), ('owner-settled.json', events.encoded(settled)), ('docling-json.json', raw)]:
        path = out / name; path.write_bytes(value); path.chmod(0o600)
    with pytest.raises((OSError, ValueError)):
        process.replay(source, first, second, receipt, out, source_key='svn', policy=policy, prior_dir=prior)
    marker = process._completion_marker(raw, artifact, first, second, receipt, 'svn', policy, 0, events.sha256(events.encoded(settled)))
    process._publish_completion(out, marker, process.doc_extract_process._directory_identity(out))
    assert process.replay(source, first, second, receipt, out, source_key='svn', policy=policy, prior_dir=prior) == artifact
    marker_file = out / 'docling-completion/marker.json'
    marker_original = marker_file.read_bytes()
    bad_marker = json.loads(marker_original); bad_marker['exit_code'] = False
    marker_file.write_bytes(events.encoded(bad_marker))
    with pytest.raises(ValueError):
        process.replay(source, first, second, receipt, out, source_key='svn', policy=policy, prior_dir=prior)
    marker_file.write_bytes(marker_original)
    source.write_bytes(b'bad')
    with pytest.raises(ValueError):
        process.replay(source, first, second, receipt, out, source_key='svn', policy=policy, prior_dir=prior)
    source.write_bytes(body)
    settlement_file = out / 'owner-settled.json'
    settlement_original = settlement_file.read_bytes()
    settlement_file.write_bytes(b'{}')
    with pytest.raises((ValueError, RuntimeError)):
        process.replay(source, first, second, receipt, out, source_key='svn', policy=policy, prior_dir=prior)
    settlement_file.write_bytes(settlement_original)
    (out / 'docling-json.json').write_bytes(b'{}')
    with pytest.raises((ValueError, KeyError)):
        process.replay(source, first, second, receipt, out, source_key='svn', policy=policy, prior_dir=prior)


def test_v5_explicit_policy_and_previous_versions_unchanged():
    from source_evidence import document_acquisition_policy as ap
    from source_evidence import document_candidates
    from test_document_acquisition_policy import policy_value, preflight
    from test_document_candidates import artifact, plan_for
    source_plan = plan_for(artifact())
    candidates = document_candidates.discover(source_plan)
    legacy = policy_value()
    before = preflight(source_plan, candidates, value=legacy)
    policy = json.loads(json.dumps(legacy))
    policy.update(schema_version=ap.SCHEMA_V5,
                  pdf_complement={'mode': 'poppler_pdf_layout_v1', 'pdftotext_path': '/usr/bin/pdftotext'},
                  sparse_ocr={'mode': 'sparse_ocr_v1', 'strategy': 'no_text', 'max_pages': 2},
                  selection_order='property_occurrences_first_v1',
                  docling_json={'mode': 'docling_json_v1', 'max_pages': 3})
    policy['limits']['run_timeout_seconds'] = 3600
    selected = preflight(source_plan, candidates, value=policy)
    assert selected['docling_json'] == policy['docling_json']
    assert preflight(source_plan, candidates, value=legacy) == before
    policy['docling_json']['max_pages'] = True
    with pytest.raises(ValueError):
        preflight(source_plan, candidates, value=policy)


def test_docling_seal_exact_set_and_reconstruction(tmp_path):
    from source_evidence import document_export, document_seal
    from test_document_seal import context_and_artifact
    context, _ = context_and_artifact()
    body, first, second, old, _, _policy = fixture()
    context['receipt'].update(body_sha256=events.sha256(body), body_bytes=len(body))
    source = first['source']
    source.update(url=context['receipt']['final_url'], observed_at=context['receipt']['finished_at'],
                  sha256=context['receipt']['body_sha256'], byte_length=context['receipt']['body_bytes'])
    second['source'] = dict(source)
    second['extraction']['previous_attempt_sha256'] = events.sha256(events.encoded(first))
    content = old['extraction']['text']
    docling = native.build(body, events.encoded(first), events.encoded(second), content.encode(),
                           config=old['parser']['config'], page_count=1, last_page=1)
    docling['extraction'].update(converter_exit_code=0, error_code=None)
    tmp_path.chmod(0o700)
    marker = document_seal.write(tmp_path / 'seal', context, first, complement=second, docling=docling)
    checked = document_seal.read(tmp_path / 'seal', expected_context=context, expected_marker=marker)
    assert checked['docling'] == docling and 'docling' in marker
    plan = json.loads((tmp_path / 'seal/docling-plan.json').read_bytes())
    text, metadata = document_export.reconstruct(plan['events'])
    assert text == content and metadata['coverage'] == 'unknown'
    (tmp_path / 'seal/docling-plan.json').write_bytes(b'{}')
    with pytest.raises(ValueError):
        document_seal.read(tmp_path / 'seal', expected_context=context, expected_marker=marker)


@pytest.mark.parametrize('interrupt', ['none', 'partial', 'unknown_cleanup', 'before_completion', 'after_completion'])
def test_v5_sealed_role_jobs_unknown_append_and_recovery(monkeypatch, tmp_path, interrupt):
    from source_evidence import (
        automatic,
        document_acquisition_policy,
        document_automatic,
        document_batch,
        document_jobs,
        document_pipeline,
        document_recovery,
    )
    from test_document_acquisition_policy import CODE, TARGET
    from test_document_pipeline import install_workers, setup_run
    manifest, attempt, kwargs = setup_run(tmp_path, retention='keep_unresolved_until_review')
    policy = json.loads(kwargs['policy_raw'])
    policy.update(schema_version=document_acquisition_policy.SCHEMA_V5,
                  pdf_complement={'mode': 'poppler_pdf_layout_v1', 'pdftotext_path': '/usr/bin/pdftotext'},
                  sparse_ocr={'mode': 'sparse_ocr_v1', 'strategy': 'no_text', 'max_pages': 1},
                  selection_order='property_occurrences_first_v1',
                  docling_json={'mode': 'docling_json_v1', 'max_pages': 1})
    policy['limits']['max_documents'] = 1
    raw = events.encoded(policy); plans = [kwargs['plan']]; candidates = document_batch.discover(plans)
    selection = document_batch.prepare(plans, candidates, raw, expected_policy_sha256=events.sha256(raw),
                                       target_sha256=TARGET, collector_git_sha=CODE)
    kwargs.update(plan=plans, candidates=candidates, selection=selection, policy_raw=raw,
                  expected_policy_sha256=events.sha256(raw))
    manifest.update(run_id='test-generation', config={'sources': ['svn']})
    attempt.update(rc=1, finished_at='2026-10-28T12:00:00Z', document_collection_settlement={
        'schema_version': 'cre-document-collection-settlement/v1', 'attempt_number': 1,
        'collector_git_sha': CODE, 'rc': 1, 'finished_at': '2026-10-28T12:00:00Z'})
    policy_path = tmp_path / 'policy.json'; policy_path.write_bytes(raw); policy_path.chmod(0o600)
    manifest['config']['automatic_source_evidence'] = {'source_keys': ['svn'], 'collector_git_sha': CODE,
        'target_sha256': TARGET, 'policy_sha256': 'b' * 64, 'transport': {'kind': 'test'},
        'document_text_formats': {'svn': document_jobs.FORMAT}}
    manifest['config']['source_document_acquisition'] = {'source_keys': ['svn'], 'collector_git_sha': CODE,
        'target_sha256': TARGET, 'policy_sha256': events.sha256(raw), 'policy_path': str(policy_path)}
    monkeypatch.setattr(document_jobs.automatic, '_verify_config', lambda *args: None)
    monkeypatch.setattr(document_jobs.document_automatic, '_verify_config', lambda *args: None)
    install_workers(monkeypatch)
    original_extract = document_pipeline.doc_extract_process.run
    def extract(*args, **opts):
        result = original_extract(*args, **opts)
        result['artifact']['parser']['config'].update(max_bytes=100*1024*1024, max_text_bytes=events.MAX_TEXT_BYTES,
                                                       timeout_ms=60000)
        return result
    monkeypatch.setattr(document_pipeline.doc_extract_process, 'run', extract)
    def complement(input_path, first, receipt, outdir, *, executable, source_key, mode):
        from document_text import poppler_modes
        value = {'schema_version': 'cre-document-text/v1', 'source': dict(first['source']),
                 'parser': {'name': 'poppler-pdftotext', 'version': '26.10.0',
                            'config': poppler_modes.config(mode, executable=executable)},
                 'extraction': {'status': 'extracted', 'coverage': 'unknown', 'text_format': 'plain', 'text': 'text',
                                'text_sha256': events.sha256(b'text'), 'relationship': 'complement_of',
                                'previous_attempt_sha256': events.sha256(events.encoded(first)),
                                'warnings': [], 'error_code': None, 'converter_exit_code': 0}}
        return value
    monkeypatch.setattr(document_pipeline.doc_complement_process, 'run', complement)
    def docling_run(input_path, first, second, receipt, outdir, *, source_key, policy, prior_dir):
        _, _, _, template, _, _ = fixture()
        result = native.build(input_path.read_bytes(), events.encoded(first), events.encoded(second),
                              template['extraction']['text'].encode(), config=template['parser']['config'],
                              page_count=1, last_page=1, partial=(interrupt == 'partial'))
        result['extraction'].update(converter_exit_code=0, error_code=None)
        if interrupt in ('unknown_cleanup', 'before_completion', 'after_completion'):
            config = result['parser']['config']; nonce = 'a' * 32
            intent = {'schema_version': 'cre-docling-json-owner/v1', 'name': 'cre-docling-json-' + nonce,
                      'nonce': nonce, 'source_sha256': receipt['body_sha256'],
                      'prior_attempt_sha256': [events.sha256(events.encoded(first)), events.sha256(events.encoded(second))],
                      'worker_sha256': config['worker_sha256'], 'config_sha256': events.sha256(events.encoded(config)), 'uid': os.getuid()}
            intent_raw = events.encoded(intent)
            settled = {'schema_version': 'cre-docling-json-settlement/v1', 'intent_sha256': events.sha256(intent_raw),
                       'container_id': 'b' * 64, 'state': 'removed_verified'}
            artifact_raw = events.encoded(result); settled_raw = events.encoded(settled)
            files = [('owner-intent.json', intent_raw), ('docling-json.json', artifact_raw)]
            if interrupt != 'unknown_cleanup':
                files.append(('owner-settled.json', settled_raw))
            private_export.write(outdir, files)
            if interrupt == 'after_completion':
                marker = process._completion_marker(artifact_raw, result, first, second, receipt, source_key, policy, 0,
                                                    events.sha256(settled_raw))
                private_export.write(outdir / 'docling-completion', [('marker.json', events.encoded(marker))])
            raise RuntimeError('simulated parent interruption')
        return result
    monkeypatch.setattr(document_pipeline.doc_docling_json_process, 'run', docling_run)
    if interrupt in ('unknown_cleanup', 'before_completion', 'after_completion'):
        with pytest.raises(RuntimeError, match='parent interruption'):
            document_pipeline.run(tmp_path, manifest, 'svn', attempt, lambda *args: None, **kwargs)
        document = attempt['document_source_evidence']['documents'][0]
        assert document['state'] == 'docling_intent' and 'sealed' not in document
        monkeypatch.setattr(process.subprocess, 'Popen', lambda *args, **opts: pytest.fail('recovery spawned'))
        recovered = document_recovery.sealed(tmp_path, manifest, 'svn', attempt, lambda *args: None,
            plan=plans, candidates=candidates, policy_raw=raw, selection=selection,
            expected_policy_sha256=events.sha256(raw), target_sha256=TARGET, collector_git_sha=CODE, cleanup=False)
        if interrupt in ('unknown_cleanup', 'before_completion'):
            assert recovered['state'] == 'incomplete' and 'sealed' not in document
            return
        assert recovered['documents'][0]['reason'] == 'settled_docling_completion_replayed'
        assert document['state'] == 'extraction_sealed'
    else:
        state = document_pipeline.run(tmp_path, manifest, 'svn', attempt, lambda *args: None, **kwargs)
        document = state['documents'][0]
    assert document['state'] == 'extraction_sealed' and 'docling' in document['sealed']
    if interrupt == 'partial':
        assert document['original_retention'] == 'retained_unresolved'
        assert (tmp_path / document['directory'] / 'fetch/document.bin').is_file()
    document_jobs.mark(tmp_path, manifest, 'svn', attempt, document, lambda *args: None)
    assert document['docling_append']['state'] == 'pending'
    plan = document_jobs.prepare_job(tmp_path, manifest, 'svn', attempt, document, document['docling_append'])
    assert plan['events'][0]['facts'][0]['value']['extraction']['text_format'] == 'docling_json'
    for key in ('append', 'complement_append'):
        document[key]['state'] = 'appended'
    document['docling_append']['state'] = 'outcome_unknown'
    assert document_automatic.append_incomplete(manifest)
    automatic.prepare_resume(manifest, replay_unknown=True)
    assert document['docling_append']['state'] == 'pending' and document['docling_append']['exact_replay_required']
    writes = []
    monkeypatch.setattr(automatic, 'append_sql', lambda plan: plan)
    monkeypatch.setattr(automatic.cli, 'run_psql', lambda plan, *args, **opts: writes.append(plan['plan_sha256']))
    manifest['config']['automatic_source_evidence']['env_file'] = '/unused/test-env'
    automatic.pump(tmp_path, manifest, lambda *args: None)
    assert writes == [plan['plan_sha256']] and document['docling_append']['state'] == 'appended'
    recovered = document_recovery.sealed(tmp_path, manifest, 'svn', attempt, lambda *args: None,
        plan=plans, candidates=candidates, policy_raw=raw, selection=selection,
        expected_policy_sha256=events.sha256(raw), target_sha256=TARGET, collector_git_sha=CODE, cleanup=False)
    assert recovered['state'] == 'sealed_outputs_reconciled' and recovered['documents'][0]['state'] == 'seal_reverified'


@pytest.mark.parametrize('state', ['missing', 'pending', 'appending', 'outcome_unknown', 'replay_required'])
def test_docling_append_barriers_refuse_incomplete(state):
    import cre_checkpoint_series
    from source_evidence import document_automatic
    primary = {'kind': 'document_text', 'state': 'appended'}
    document = {'state': 'extraction_sealed', 'sealed': {'docling': {}}, 'append': primary}
    if state != 'missing':
        document['docling_append'] = {'kind': 'document_text', 'state': 'appended' if state == 'replay_required' else state}
        if state == 'replay_required':
            document['docling_append']['exact_replay_required'] = True
    attempt = {'source_evidence': {'state': 'appended'}, 'document_source_evidence': {'documents': [document]}}
    manifest = {'config': {'automatic_source_evidence': {'source_keys': ['svn'],
                 'document_text_formats': {'svn': 'local_anydoc_public_listing_v1'}}},
                'sources': {'svn': {'attempts': [attempt]}}}
    assert document_automatic.append_incomplete(manifest)
    assert cre_checkpoint_series.evidence_reconciliation_required(manifest)


def test_prior_layout_and_mode_tampering_are_checked():
    from document_text import poppler_modes
    body, first, second, docling, receipt, policy = fixture()
    second['parser']['config'] = poppler_modes.config(poppler_modes.LAYOUT, executable='/usr/bin/pdftotext')
    rebuilt = native.build(body, events.encoded(first), events.encoded(second), docling['extraction']['text'].encode(),
                           config=docling['parser']['config'], page_count=1, last_page=1)
    rebuilt['extraction'].update(converter_exit_code=0, error_code=None)
    process.validate(rebuilt, first, second, receipt, 'svn', policy)
    second['parser']['config']['mode'] = 'unknown'
    with pytest.raises(ValueError):
        native.initialize(body, events.encoded(first), events.encoded(second), config={})


def test_capped_and_failed_docling_are_retained_when_selected():
    body, first, second, artifact, receipt, policy = fixture()
    capped = native.build(body, events.encoded(first), events.encoded(second), artifact['extraction']['text'].encode(),
                          config=artifact['parser']['config'], page_count=2, last_page=1)
    capped['extraction'].update(converter_exit_code=0, error_code=None)
    process.validate(capped, first, second, receipt, 'svn', policy)
    selection = {'original_retention': 'keep_unresolved_until_review', 'pdf_complement': {}, 'docling_json': policy}
    assert process.retain_unresolved(first, second, capped, selection)
    failed = native.initialize(body, events.encoded(first), events.encoded(second), config=artifact['parser']['config'])
    failed['extraction']['error_code'] = 'worker_failed_or_timeout'
    process.validate(failed, first, second, receipt, 'svn', policy)
    assert process.retain_unresolved(first, second, failed, selection)


def test_forged_page_and_config_types_refused():
    _body, first, second, artifact, receipt, policy = fixture()
    for field, value in [('page_count', 0), ('page_range', [True, 1]), ('omitted_pages', False),
                         ('converter_exit_code', False), ('converter_exit_code', 0.0)]:
        changed = json.loads(json.dumps(artifact)); changed['extraction'][field] = value
        with pytest.raises(ValueError):
            process.validate(changed, first, second, receipt, 'svn', policy)
    artifact['parser']['config']['cpu'] = True
    with pytest.raises(ValueError):
        process.validate(artifact, first, second, receipt, 'svn', policy)


@pytest.mark.parametrize('uncertain', [False, True])
def test_supervisor_publishes_completion_only_after_verified_settlement(monkeypatch, tmp_path, uncertain):
    from types import SimpleNamespace

    from source_evidence.document_process import DocumentProcessCleanupUnknown
    body, first, second, artifact, receipt, policy = fixture()
    tmp_path.chmod(0o700)
    source = tmp_path / 'input'; source.write_bytes(body); source.chmod(0o600)
    prior = tmp_path / 'prior'; private_export.write(prior, [('converter-artifact.json', events.encoded(first)),
                                                               ('complement-converter-artifact.json', events.encoded(second))])
    out = tmp_path / 'out'; calls = []
    def spawn(command, **kwargs):
        calls.append(command)
        config = artifact['parser']['config']; nonce = 'a' * 32
        intent = {'schema_version': 'cre-docling-json-owner/v1', 'name': 'cre-docling-json-' + nonce,
                  'nonce': nonce, 'source_sha256': receipt['body_sha256'],
                  'prior_attempt_sha256': [events.sha256(events.encoded(first)), events.sha256(events.encoded(second))],
                  'worker_sha256': config['worker_sha256'], 'config_sha256': events.sha256(events.encoded(config)), 'uid': os.getuid()}
        intent_raw = events.encoded(intent)
        settled = {'schema_version': 'cre-docling-json-settlement/v1', 'intent_sha256': events.sha256(intent_raw),
                   'container_id': 'b' * 64, 'state': 'removed_verified'}
        value = json.loads(json.dumps(artifact))
        if uncertain:
            value['extraction']['warnings'].append('container_cleanup_unconfirmed')
        files = [('owner-intent.json', intent_raw), ('docling-json.json', events.encoded(value))]
        if not uncertain:
            files.append(('owner-settled.json', events.encoded(settled)))
        private_export.write(out, files)
        return SimpleNamespace(pid=1234, wait=lambda timeout: 0)
    monkeypatch.setattr(process.subprocess, 'Popen', spawn)
    monkeypatch.setattr(process.OwnedProcessGroup, 'from_spawn', lambda child: child)
    monkeypatch.setattr(process, 'settle_owned_groups', lambda *args, **kwargs: SimpleNamespace(settled=True))
    if uncertain:
        with pytest.raises(DocumentProcessCleanupUnknown):
            process.run(source, first, second, receipt, out, source_key='svn', policy=policy, prior_dir=prior)
        assert not (out / 'docling-completion').exists()
    else:
        assert process.run(source, first, second, receipt, out, source_key='svn', policy=policy, prior_dir=prior) == artifact
        assert (out / 'docling-completion/marker.json').is_file()
        assert process.replay(source, first, second, receipt, out, source_key='svn', policy=policy, prior_dir=prior) == artifact
    assert len(calls) == 1
