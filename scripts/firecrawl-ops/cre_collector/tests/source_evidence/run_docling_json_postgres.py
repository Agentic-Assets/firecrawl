"""Synthetic Docling JSON roundtrip using only a new, owned PostgreSQL 17 cluster.

Requires the separately reviewed Docling JSON format integration. No provider,
production, existing database, or caller-supplied database URL is accepted.
"""
import argparse
import copy
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
COLLECTOR = HERE.parents[1]
sys.path.insert(0, str(COLLECTOR))
sys.path.insert(0, str(HERE.parent / 'observation_store'))

from owned_observation_fixture_cluster import OwnedCluster, tool
from source_evidence import cli, document_export, events


def synthetic_document():
    cells = [{'text': f'fixture cell {i}', 'row': i, 'coordinates': [i, 0, i + 1, 1],
              'unknown': {'flag': False, 'units': None}} for i in range(80)]
    value = {'schema_name': 'DoclingDocument', 'version': '1.10.0', 'name': 'synthetic-fixture',
             'body': {'self_ref': '#/body', 'children': [{'$ref': '#/tables/0'}]},
             'furniture': {'self_ref': '#/furniture', 'children': []},
             'groups': [], 'texts': [], 'pictures': [],
             'tables': [{'self_ref': '#/tables/0', 'children': [], 'data': {'table_cells': cells}}],
             'pages': {'1': {'page_no': 1, 'size': {'width': 100, 'height': 100}}},
             'unknown_fixture': {'unicode': 'π' * 31001, 'duplicate': [1, 1, None], 'empty': ''}}
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    assert 60000 < len(raw) < events.MAX_TEXT_BYTES and len(cells) > 50
    return value, raw


def synthetic_plan():
    value, raw = synthetic_document()
    # Synthetic, not a claim that a converter ran or a PDF was acquired.
    artifact = {'schema_version': 'cre-document-text/v1',
                'source': {'url': 'https://example.test/synthetic.pdf', 'observed_at': '2026-10-28T12:00:00Z'},
                'parser': {'name': 'synthetic-fixture', 'version': 'fixture-only'},
                'extraction': {'status': 'extracted', 'coverage': 'unknown', 'text_format': 'docling_json',
                               'text': raw.decode('utf-8'), 'text_sha256': events.sha256(raw)}}
    plan = events.plan(artifact, source_key='svn')
    events.validate_content_policy(plan)
    assert len(plan['events']) >= 3
    return value, raw, plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-disposable-fixture', action='store_true', required=True)
    parser.parse_args()
    value, raw, plan = synthetic_plan()
    # Refuse before creating a database if the new format/export contract is absent.
    with tempfile.TemporaryDirectory(prefix='docling-export-preflight-') as folder:
        metadata = document_export.export(plan['events'], Path(folder) / 'export')
        assert metadata['text_file'] == 'document.docling.json'
        assert (Path(folder) / 'export' / metadata['text_file']).read_bytes() == raw
    initdb, pg_ctl, psql = (tool(name) for name in ('initdb', 'pg_ctl', 'psql'))
    version = subprocess.run([initdb, '--version'], capture_output=True, text=True, timeout=10, check=True)
    if ' 17.' not in version.stdout:
        raise RuntimeError('PostgreSQL 17 is required')
    database = 'cre_docling_' + uuid.uuid4().hex[:12]
    with OwnedCluster(initdb, pg_ctl, port=55449) as cluster:
        def sql(statement, *, target=database, user=None, success=True):
            command = [psql, '-X', '-qAt', '-h', str(cluster.root), '-p', str(cluster.port), '-d', target,
                       '-v', 'ON_ERROR_STOP=1'] + (['-U', user] if user else [])
            result = subprocess.run(command, input=statement, capture_output=True, text=True,
                                    timeout=45, env=cluster.env, check=False)
            if (result.returncode == 0) != success:
                raise AssertionError('disposable SQL returned an unexpected status')
            return result.stdout.strip()

        sql('CREATE ROLE evidence_executor LOGIN CREATEROLE CREATEDB BYPASSRLS;', target='postgres')
        sql(f'CREATE DATABASE {database} OWNER evidence_executor;', target='postgres')
        sql('CREATE ROLE cre_source_evidence_owner NOLOGIN NOINHERIT; '
            'CREATE ROLE cre_source_evidence_writer NOLOGIN NOINHERIT; '
            'CREATE ROLE cre_source_evidence_reader NOLOGIN NOINHERIT;', user='evidence_executor')
        sql("CREATE SCHEMA credeals; CREATE TABLE credeals.sentinel(id integer, status text); "
            "INSERT INTO credeals.sentinel VALUES (1,'active');")
        migration = (HERE.parents[2] / 'sql/019_cre_source_evidence.sql').read_text()
        sql("\\set CRE_SOURCE_EVIDENCE_APPROVE 1\n\\set CRE_SOURCE_EVIDENCE_OPERATOR cayman\n"
            "\\set CRE_SOURCE_EVIDENCE_APPROVAL_REF AGENTIC-2895\n"
            "\\set CRE_SOURCE_EVIDENCE_CONFIRM 'APPLY 019_cre_source_evidence'\n" + migration,
            user='evidence_executor')
        sql('CREATE ROLE evidence_docling_writer LOGIN NOINHERIT; '
            'GRANT cre_source_evidence_writer TO evidence_docling_writer WITH INHERIT FALSE, SET TRUE; '
            'CREATE ROLE evidence_docling_reader LOGIN NOINHERIT; '
            'GRANT cre_source_evidence_reader TO evidence_docling_reader WITH INHERIT FALSE, SET TRUE; '
            'CREATE ROLE evidence_docling_untrusted LOGIN NOINHERIT;')
        append = cli.append_sql(plan)
        sql(append, user='evidence_docling_writer')
        sql(append, user='evidence_docling_writer')
        assert int(sql('SELECT count(*) FROM cre_source_evidence.events;')) == len(plan['events'])
        # Writer has function access only; reader has read access, never append.
        sql('SET ROLE cre_source_evidence_writer; SELECT count(*) FROM cre_source_evidence.events;',
            user='evidence_docling_writer', success=False)
        sql(append, user='evidence_docling_reader', success=False)
        sql(append, user='evidence_docling_untrusted', success=False)
        sql('SELECT count(*) FROM cre_source_evidence.events;', user='evidence_docling_untrusted', success=False)
        sql('SET ROLE cre_source_evidence_reader; DELETE FROM cre_source_evidence.events;',
            user='evidence_docling_reader', success=False)
        assert sql("SELECT has_table_privilege('cre_source_evidence_writer', 'cre_source_evidence.events', 'SELECT');") == 'f'
        function = 'cre_source_evidence.append_event(text,integer,text,text,timestamptz,text,jsonb,jsonb,text,text,text,text)'
        assert sql(f"SELECT has_function_privilege('cre_source_evidence_reader', '{function}', 'EXECUTE');") == 'f'
        columns = ','.join(cli.FIELDS)
        lines = sql('SET ROLE cre_source_evidence_reader; SELECT row_to_json(r) FROM '
                    f'(SELECT {columns} FROM cre_source_evidence.events ORDER BY item_no) r;',
                    user='evidence_docling_reader')
        rows = [json.loads(line) for line in lines.splitlines()]
        matches = [json.loads(line) for line in sql(cli.search_sql('fixture', 'plain', 25),
                                                     user='evidence_docling_reader').splitlines()]
        assert matches and len(matches) <= 25
        assert all(match['capture_id'] == plan['events'][0]['capture_id']
                   and type(match['item_no']) is int and match['item_no'] >= 1 for match in matches)
        recovered, _ = document_export.reconstruct(rows)
        assert recovered.encode('utf-8') == raw and json.loads(recovered) == value
        assert events.sha256(recovered.encode()) == plan['events'][0]['provenance']['text_sha256']
        metadata = document_export.export(rows, cluster.root / 'readback')
        assert metadata['text_file'] == 'document.docling.json'
        assert (cluster.root / 'readback' / metadata['text_file']).read_bytes() == raw
        # Same event identity with altered chunk must be refused by the database.
        original_chunk = plan['events'][1]['extracted_text']
        old = cli.sql_text(original_chunk)
        replacement = cli.sql_text(original_chunk + 'tamper')
        assert old in append
        sql(append.replace(old, replacement, 1), user='evidence_docling_writer', success=False)
        altered = copy.deepcopy(rows); altered[1]['extracted_text'] += 'tamper'
        try:
            document_export.reconstruct(altered)
        except ValueError:
            pass
        else:
            raise AssertionError('tampered chunk accepted')
        assert int(sql('SELECT count(*) FROM cre_source_evidence.events;')) == len(plan['events'])
        assert sql('SELECT id,status FROM credeals.sentinel;') == '1|active'
    print('PASS: synthetic Docling JSON, 80 cells, exact multi-chunk readback, bounded text search, replay and privileges; cluster stopped')


if __name__ == '__main__':
    main()
