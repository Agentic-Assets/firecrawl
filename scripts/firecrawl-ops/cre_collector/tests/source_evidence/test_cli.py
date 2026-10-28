import json
import os
import subprocess

import pytest
from source_evidence import cli, events


def document(text="Office '); DROP SCHEMA credeals; --"):
    return {'schema_version': 'cre-document-text/v1',
            'source': {'url': 'https://example.test/a.pdf', 'observed_at': '2026-10-28T12:00:00Z'},
            'parser': {'name': '@firecrawl/anydoc', 'version': '0.2.4'},
            'extraction': {'status': 'extracted', 'coverage': 'unknown', 'text': text,
                           'text_sha256': events.sha256(text.encode())}}


def admission(value):
    return {'schema_version': 'cre-source-evidence-admission/v1', 'plan_sha256': value['plan_sha256'],
            'purpose': 'private_unverified_evidence', 'data_scope': 'public_listing_source',
            'content_policy': 'reviewed_for_private_retention',
            'source_keys': [value['source_key']]}


def test_plan_private_output_and_admission_binding(tmp_path, capsys):
    source = tmp_path / 'artifact.json'
    source.write_text(json.dumps(document()))
    output = tmp_path / 'plan.json'
    cli.main(['plan', '--artifact', str(source), '--source-key', 'cbre', '--out', str(output)])
    result = json.loads(output.read_text())
    assert os.stat(output).st_mode & 0o777 == 0o600
    assert "DROP SCHEMA" not in capsys.readouterr().out
    cli.checked_plan(result, admission(result))
    result['events'][1]['extracted_text'] = 'tampered'
    with pytest.raises(ValueError, match='digest'):
        cli.checked_plan(result, admission(result))
    with pytest.raises(FileExistsError):
        cli.write_private(output, {})


def test_sql_encodes_untrusted_text_and_limits_query():
    value = events.plan(document(), source_key='cbre')
    sql = cli.append_sql(value)
    assert 'DROP SCHEMA' not in sql
    assert 'SET LOCAL ROLE cre_source_evidence_writer' in sql
    assert "cre_source_evidence.append_event" in sql
    query = "office'); DROP SCHEMA credeals; --"
    search = cli.search_sql(query, 'phrase', 25)
    assert query not in search
    assert 'pg_catalog.phraseto_tsquery' in search
    assert 'SET LOCAL ROLE cre_source_evidence_reader' in search
    for kwargs in ({'query': '', 'mode': 'plain', 'limit': 1},
                   {'query': 'x', 'mode': 'plain', 'limit': 26},
                   {'query': 'x\x00', 'mode': 'plain', 'limit': 1}):
        with pytest.raises(ValueError):
            cli.search_sql(**kwargs)


def test_append_never_reaches_database_without_matching_admission(tmp_path, monkeypatch):
    value = events.plan(document(), source_key='cbre')
    path = tmp_path / 'plan'; path.write_bytes(events.encoded(value))
    receipt = tmp_path / 'admission'; receipt.write_text('{}')
    monkeypatch.setattr(cli, 'run_psql', lambda *args: pytest.fail('unexpected database access'))
    with pytest.raises(ValueError):
        cli.main(['append', '--plan', str(path), '--admission', str(receipt), '--env-file', 'x',
                  '--expected-db-target-sha256', 'a' * 64, '--apply'])


def test_bounded_read_refuses_symlink_and_oversize(tmp_path):
    source = tmp_path / 'data'; source.write_text('{}')
    link = tmp_path / 'link'; link.symlink_to(source)
    with pytest.raises(OSError):
        cli.read_json(link, 10)
    with pytest.raises(ValueError, match='limit'):
        cli.read_json(source, 1)
    source.write_text('{"outer":{"x":1,"x":2}}')
    with pytest.raises(ValueError, match='duplicate'):
        cli.read_json(source, 100)


def test_append_builder_rejects_rehashed_but_inconsistent_rows():
    value = events.plan(document(), source_key='cbre')
    value['events'][1]['source_key'] = 'other'
    value['plan_sha256'] = events.sha256(events.encoded({k: v for k, v in value.items() if k != 'plan_sha256'}))
    with pytest.raises(ValueError):
        cli.append_sql(value)


def test_database_timeout_is_reported_as_unknown_outcome(monkeypatch):
    monkeypatch.setattr(cli, 'load_db_url', lambda _: ('postgresql://localhost/test', 'file'))
    monkeypatch.setattr(cli, 'assert_expected_database_target', lambda *_: None)
    monkeypatch.setattr(cli, 'find_psql', lambda: 'psql')
    monkeypatch.setattr(cli, 'psql_connection_args', lambda _: [])
    monkeypatch.setattr(cli, 'psql_connection_env', lambda _: {})
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired('psql', 45)
    monkeypatch.setattr(cli.subprocess, 'run', timeout)
    with pytest.raises(RuntimeError, match='outcome unknown'):
        cli.run_psql('BEGIN; COMMIT;', '/private/env', 'a' * 64)


def test_fact_and_get_queries_bind_values_and_enforce_limits():
    payload = [{'unknown': "'); DROP SCHEMA credeals; --"}]
    query = cli.facts_sql(payload, 25)
    assert 'DROP SCHEMA' not in query
    assert 'facts @>' in query and 'SET LOCAL ROLE cre_source_evidence_reader' in query
    for bad, limit in [([], 1), ({'x': 1}, 1), ([{'x': 'a' * 20000}], 1), ([{'x': 1}], 26)]:
        with pytest.raises(ValueError):
            cli.facts_sql(bad, limit)
    assert 'SET LOCAL ROLE cre_source_evidence_reader' in cli.get_sql('evidence-123', 1)
    for capture, ordinal in [("x';DROP", 1), ('valid', -1), ('valid', 1000001)]:
        with pytest.raises(ValueError):
            cli.get_sql(capture, ordinal)


def test_document_query_is_read_only_bounded_and_literal_bound():
    sql = cli.document_sql('evidence-document')
    assert 'BEGIN READ ONLY;' in sql
    assert 'SET LOCAL ROLE cre_source_evidence_reader;' in sql
    assert 'ORDER BY item_no LIMIT 72' in sql
    assert 'evidence-document' not in sql
    with pytest.raises(ValueError):
        cli.document_sql("bad'; DROP SCHEMA credeals; --")


def test_export_document_text_uses_private_exporter_without_printing_text(monkeypatch, capsys):
    from source_evidence import document_export
    value = events.plan(document('Retained document content'), source_key='cbre')
    capture = value['events'][0]['capture_id']
    calls = []
    def read(sql, env, target, **kwargs):
        assert 'ORDER BY item_no LIMIT 72' in sql and kwargs['max_results_bytes'] == cli.MAX_PLAN_BYTES
        return b'\n'.join(events.encoded(row) for row in value['events'])
    monkeypatch.setattr(cli, 'run_psql', read)
    monkeypatch.setattr(document_export, 'export', lambda rows, outdir: calls.append((rows, outdir)))
    cli.main(['export-document-text', '--capture-id', capture, '--out-dir', '/private/new-document',
              '--env-file', '/private/env', '--expected-db-target-sha256', 'a' * 64])
    assert calls == [(value['events'], '/private/new-document')]
    assert 'Retained document content' not in capsys.readouterr().out


def test_collector_query_is_read_only_and_detects_oversized_capture():
    sql = cli.collector_sql('evidence-collector')
    assert 'BEGIN READ ONLY;' in sql
    assert 'SET LOCAL ROLE cre_source_evidence_reader;' in sql
    assert 'ORDER BY item_no LIMIT 10002' in sql
    assert 'evidence-collector' not in sql
    for identity in ("bad'; DROP SCHEMA credeals; --", '', None):
        with pytest.raises(ValueError):
            cli.collector_sql(identity)
    for limit in (True, 0, 10003):
        with pytest.raises(ValueError):
            cli.capture_sql('evidence-collector', limit)


def test_export_collector_uses_bound_reader_and_does_not_print_facts(monkeypatch, capsys):
    from source_evidence import collector, collector_export
    original = {'runMeta': {'finishedAt': '2026-10-28T12:00:00Z'},
                'sources': [{'sourceKey': 'svn', 'retainedObservations': [
                    {'unknown': 'Private retained source content'} for _ in range(78)]}],
                'listings': []}
    value = events.plan(collector.convert(original, source_key='svn'), source_key='svn')
    capture = value['events'][0]['capture_id']
    calls = []
    def read(sql, env, target, **kwargs):
        assert sql == cli.collector_sql(capture)
        assert env == '/private/env' and target == 'b' * 64
        assert kwargs['max_results_bytes'] == cli.MAX_PLAN_BYTES
        return b'\n'.join(events.encoded(row) for row in value['events'])
    monkeypatch.setattr(cli, 'run_psql', read)
    monkeypatch.setattr(collector_export, 'export', lambda rows, outdir: calls.append((rows, outdir)))
    args = ['export-collector-capture', '--capture-id', capture, '--out-dir', '/private/new-collector',
            '--env-file', '/private/env', '--expected-db-target-sha256', 'b' * 64]
    cli.main(args)
    assert calls == [(value['events'], '/private/new-collector')]
    assert 'Private retained source content' not in capsys.readouterr().out
    value['events'][1]['capture_id'] = 'evidence-another-capture'
    with pytest.raises(ValueError, match='another capture'):
        cli.main(args)
    assert len(calls) == 1


def test_document_discovery_is_offline_private_and_does_not_print_urls(tmp_path, monkeypatch, capsys):
    from source_evidence import collector
    original = {'runMeta': {'finishedAt': '2026-10-28T12:00:00Z'},
                'sources': [{'sourceKey': 'svn'}],
                'listings': [{'sourceKey': 'svn', 'id': str(index),
                              'brochures': [{'name': 'Offering memorandum',
                                             'url': f'https://example.test/private-doc-{index}.pdf'}]}
                             for index in range(78)]}
    value = events.plan(collector.convert(original, source_key='svn'), source_key='svn')
    plan_path = tmp_path / 'plan.json'
    plan_path.write_bytes(events.encoded(value))
    output = tmp_path / 'candidates.json'
    monkeypatch.setattr(cli, 'run_psql', lambda *args, **kwargs: pytest.fail('database contacted'))
    monkeypatch.setattr(cli.subprocess, 'run', lambda *args, **kwargs: pytest.fail('subprocess started'))
    cli.main(['discover-documents', '--plan', str(plan_path), '--out', str(output)])
    manifest = json.loads(output.read_bytes())
    assert len(manifest['candidates']) == 78
    assert os.stat(output).st_mode & 0o777 == 0o600
    assert 'example.test' not in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        cli.main(['discover-documents', '--plan', str(plan_path), '--out', str(output)])
