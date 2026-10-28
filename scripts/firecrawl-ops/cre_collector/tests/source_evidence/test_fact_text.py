import json
import os

import pytest
from source_evidence import cli


def test_fact_text_query_safe_literal_and_controls():
    query = 'A%_!\\"\nDROP SCHEMA credeals;'
    sql = cli.fact_text_sql(query, 25, source_key='faris-lee', capture_id='evidence-123')
    assert query not in sql and 'DROP SCHEMA' not in sql
    assert 'SET LOCAL ROLE cre_source_evidence_reader' in sql
    assert "(facts::text) ILIKE" in sql and "ESCAPE '!'" in sql
    assert 'ORDER BY observed_at DESC,capture_id,item_no LIMIT 25' in sql
    assert 'facts_bytes' in sql
    for args in [('', 1), ('\x00', 1), ('x' * 513, 1), ('x', 0), ('x', 26), ('x', True), ('\ud800', 1)]:
        with pytest.raises(ValueError):
            cli.fact_text_sql(*args)
    for kw in ({'source_key': 'bad;sql'}, {'capture_id': 'bad;sql'}, {'source_key': 1}):
        with pytest.raises(ValueError):
            cli.fact_text_sql('x', 1, **kw)


def test_fact_text_cli_writes_private_metadata_only(tmp_path, monkeypatch, capsys):
    calls = []
    def run(sql, env, target):
        calls.append((sql, env, target))
        return (json.dumps({'capture_id': 'evidence-1', 'item_no': 2, 'facts_bytes': 42}) + '\n').encode()
    monkeypatch.setattr(cli, 'run_psql', run)
    out = tmp_path / 'matches.json'
    cli.main(['search-fact-text', '--query', 'secret query', '--limit', '2', '--source-key', 'nai-global',
              '--capture-id', 'evidence-1', '--out', str(out), '--env-file', 'env',
              '--expected-db-target-sha256', 'a' * 64])
    assert os.stat(out).st_mode & 0o777 == 0o600
    assert json.loads(out.read_bytes())['results'] == [{'capture_id': 'evidence-1', 'item_no': 2, 'facts_bytes': 42}]
    assert 'secret query' not in capsys.readouterr().out
    assert 'source_key=' in calls[0][0] and 'capture_id=' in calls[0][0]
