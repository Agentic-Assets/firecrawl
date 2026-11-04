"""Exercise the evidence CLI against an owned disposable PostgreSQL cluster."""
import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

from owned_observation_fixture_cluster import OwnedCluster, tool

HERE = Path(__file__).resolve().parent
COLLECTOR = HERE.parents[1]
sys.path.insert(0, str(COLLECTOR))
from cre_ingest import database_target_fingerprint_from_url
from source_evidence import events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-disposable-fixture', action='store_true', required=True)
    parser.parse_args()
    initdb, pg_ctl, psql = (tool(name) for name in ('initdb', 'pg_ctl', 'psql'))
    database = 'cre_cli_' + uuid.uuid4().hex[:12]
    with OwnedCluster(initdb, pg_ctl, port=55444) as cluster:
        def sql(statement, target=database, user=None):
            result = subprocess.run([psql, '-X', '-qAt', '-h', str(cluster.root), '-p', '55444', '-d', target,
                                     '-v', 'ON_ERROR_STOP=1'] + (['-U', user] if user else []), input=statement, capture_output=True, text=True,
                                    timeout=45, check=False, env=cluster.env)
            if result.returncode:
                raise AssertionError(f'fixture SQL failed: {result.stderr[-2000:]}')
            return result.stdout.strip()

        sql('CREATE ROLE evidence_executor LOGIN CREATEROLE CREATEDB BYPASSRLS;', target='postgres')
        sql(f'CREATE DATABASE {database} OWNER evidence_executor;', target='postgres')
        sql('CREATE ROLE cre_source_evidence_owner NOLOGIN NOINHERIT; CREATE ROLE cre_source_evidence_writer NOLOGIN NOINHERIT; CREATE ROLE cre_source_evidence_reader NOLOGIN NOINHERIT;', user='evidence_executor')
        sql('CREATE SCHEMA credeals; CREATE TABLE credeals.cre_listings(id integer, status text); INSERT INTO credeals.cre_listings VALUES (1,\'active\');')
        migration = (HERE.parents[2] / 'sql/019_cre_source_evidence.sql').read_text()
        sql("\\set CRE_SOURCE_EVIDENCE_APPROVE 1\n\\set CRE_SOURCE_EVIDENCE_OPERATOR cayman\n"
            "\\set CRE_SOURCE_EVIDENCE_APPROVAL_REF AGENTIC-2895\n"
            "\\set CRE_SOURCE_EVIDENCE_CONFIRM 'APPLY 019_cre_source_evidence'\n" + migration, user='evidence_executor')
        sql('CREATE ROLE evidence_cli LOGIN; GRANT cre_source_evidence_writer, cre_source_evidence_reader TO evidence_cli;')
        url = f'postgresql://evidence_cli@{quote(str(cluster.root), safe="")}:{cluster.port}/{database}'
        target = database_target_fingerprint_from_url(url)['value']
        env_file = cluster.root / 'fixture.env'
        env_file.write_text(f'POSTGRES_URL={url}\n'); env_file.chmod(0o600)
        env = os.environ.copy(); env['PSQL_BIN'] = psql
        def run_cli(args, success=True):
            result = subprocess.run([sys.executable, '-m', 'source_evidence.cli', *args], cwd=COLLECTOR,
                                    env=env, capture_output=True, text=True, timeout=60, check=False)
            if (result.returncode == 0) != success:
                raise AssertionError(f'CLI unexpected exit {result.returncode}: {result.stderr}')
            return result
        text = 'Inventario naïve café building ' + 'é' * 40000 + ' end'
        artifact = {'schema_version': 'cre-document-text/v1',
                    'source': {'url': 'https://example.test/doc.pdf', 'observed_at': '2026-10-28T12:00:00Z'},
                    'parser': {'name': '@firecrawl/anydoc', 'version': '0.2.4'},
                    'extraction': {'status': 'extracted', 'coverage': 'unknown', 'text': text,
                                   'text_sha256': events.sha256(text.encode())}}
        artifact_path = cluster.root / 'artifact.json'; artifact_path.write_text(json.dumps(artifact))
        plan_path = cluster.root / 'plan.json'
        run_cli(['plan', '--artifact', str(artifact_path), '--source-key', 'cbre', '--out', str(plan_path)])
        plan = json.loads(plan_path.read_text())
        admission_path = cluster.root / 'admission.json'
        receipt = {'schema_version': 'cre-source-evidence-admission/v1', 'plan_sha256': plan['plan_sha256'],
                   'purpose': 'private_unverified_evidence', 'data_scope': 'public_listing_source',
                   'content_policy': 'reviewed_for_private_retention', 'source_keys': ['cbre']}
        admission_path.write_text(json.dumps(receipt))
        args = ['append', '--plan', str(plan_path), '--admission', str(admission_path), '--env-file', str(env_file),
                '--expected-db-target-sha256', target, '--apply']
        run_cli(args); run_cli(args)
        count = sql("SELECT count(*) FROM cre_source_evidence.events WHERE source_key='cbre';")
        assert int(count) == len(plan['events'])
        assert sql("SELECT encode(sha256(convert_to(string_agg(extracted_text,'' ORDER BY item_no),'UTF8')),'hex') FROM cre_source_evidence.events WHERE source_key='cbre' AND extracted_text IS NOT NULL;") == events.sha256(text.encode())
        results = cluster.root / 'search.json'
        run_cli(['search', '--query', 'naïve café', '--mode', 'phrase', '--env-file', str(env_file),
                 '--expected-db-target-sha256', target, '--out', str(results)])
        found = json.loads(results.read_text())['results']
        assert len(found) == 1 and 'naïve café' in found[0]['extracted_text']
        assert results.stat().st_mode & 0o777 == 0o600
        capture_id = plan['events'][0]['capture_id']
        recovered = []
        for event in plan['events'][1:]:
            output = cluster.root / f"get-text-{event['item_no']}.json"
            result = run_cli(['get', '--capture-id', capture_id, '--item-no', str(event['item_no']),
                              '--env-file', str(env_file), '--expected-db-target-sha256', target, '--out', str(output)])
            assert 'naïve café' not in result.stdout and 'naïve café' not in result.stderr
            retrieved = json.loads(output.read_text())['results']
            assert len(retrieved) == 1 and retrieved[0]['text_sha256'] == event['text_sha256']
            recovered.append(retrieved[0]['extracted_text'])
            assert output.stat().st_mode & 0o777 == 0o600
        assert events.sha256(''.join(recovered).encode()) == events.sha256(text.encode())
        record = {'identityResolution': 'unresolved', 'sourceFields': {'unknownDetail': {'label': 'Étage spécial', 'nested': [1, 2]}},
                  'indexAttributes': [{'literal': ['Unknown Field', 'Valeur cachée']}]}
        buildout = {'schema': 'buildout-private-observation-artifact-v1', 'provenance': {'sourceKey': 'svn'},
                    'records': [record], 'summary': {'status': 'partial'}}
        buildout_path = cluster.root / 'buildout.json'; buildout_path.write_text(json.dumps(buildout))
        buildout_plan_path = cluster.root / 'buildout-plan.json'
        run_cli(['plan', '--artifact', str(buildout_path), '--source-key', 'svn', '--observed-at',
                 '2026-10-28T12:00:00Z', '--out', str(buildout_plan_path)])
        buildout_plan = json.loads(buildout_plan_path.read_text())
        buildout_receipt = {**receipt, 'plan_sha256': buildout_plan['plan_sha256'], 'source_keys': ['svn']}
        buildout_receipt_path = cluster.root / 'buildout-admission.json'
        buildout_receipt_path.write_text(json.dumps(buildout_receipt))
        run_cli(['append', '--plan', str(buildout_plan_path), '--admission', str(buildout_receipt_path),
                 '--env-file', str(env_file), '--expected-db-target-sha256', target, '--apply'])
        match = [{'kind': 'source_record', 'value': {'sourceFields': {'unknownDetail': {'label': 'Étage spécial'}}}}]
        match_path = cluster.root / 'contains.json'; match_path.write_text(json.dumps(match, ensure_ascii=False))
        fact_results = cluster.root / 'fact-results.json'
        result = run_cli(['search-facts', '--contains-file', str(match_path), '--env-file', str(env_file),
                          '--expected-db-target-sha256', target, '--out', str(fact_results)])
        assert 'Étage spécial' not in result.stdout and 'Étage spécial' not in result.stderr
        fact_hits = json.loads(fact_results.read_text())['results']
        assert len(fact_hits) == 1 and fact_hits[0]['capture_id'] == buildout_plan['events'][1]['capture_id']
        assert fact_hits[0]['item_no'] == 1 and fact_results.stat().st_mode & 0o777 == 0o600
        full_fact = cluster.root / 'full-fact.json'
        result = run_cli(['get', '--capture-id', fact_hits[0]['capture_id'], '--item-no', '1',
                          '--env-file', str(env_file), '--expected-db-target-sha256', target, '--out', str(full_fact)])
        assert 'Étage spécial' not in result.stdout and 'Étage spécial' not in result.stderr
        retrieved_fact = json.loads(full_fact.read_text())['results']
        assert len(retrieved_fact) == 1 and retrieved_fact[0]['facts'][0]['value'] == record
        assert full_fact.stat().st_mode & 0o777 == 0o600
        plan['events'][1]['source_key'] = 'other'
        plan['plan_sha256'] = events.sha256(events.encoded({k: v for k, v in plan.items() if k != 'plan_sha256'}))
        plan_path.write_bytes(events.encoded(plan)); receipt['plan_sha256'] = plan['plan_sha256']
        admission_path.write_text(json.dumps(receipt))
        run_cli(args, success=False)
        assert sql("SELECT count(*) FROM cre_source_evidence.events WHERE source_key='cbre';") == count
        assert sql('SELECT id,status FROM credeals.cre_listings;') == '1|active'
        print('PASS: CLI append, replay, rejected divergence, Unicode get, fact containment and get, canonical unchanged')


if __name__ == '__main__':
    main()
