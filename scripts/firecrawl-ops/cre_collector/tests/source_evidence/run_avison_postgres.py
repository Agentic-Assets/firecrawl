"""Prove projected Avison source capture in an owned disposable PG17 cluster."""
import argparse
import base64
import copy
import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parent / 'observation_store'))
import cre_checkpoint_refresh as refresh
from cre_ingest import database_target_fingerprint_from_url
from owned_observation_fixture_cluster import OwnedCluster, tool
from source_evidence import (
    acquisition,
    automatic,
    avison_native,
    avison_pages,
    avison_response,
    cli,
    events,
    native_pages,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-disposable-fixture', action='store_true', required=True)
    parser.add_argument('--saved-native-page', required=True)
    args = parser.parse_args()
    page = Path(args.saved_native_page)
    raw = page.read_bytes()
    entry = json.loads(raw)
    body = base64.b64decode(entry['responseBodyBase64'], validate=True)
    original, literals = acquisition.decode_json(body)
    projected, projected_literals, projection = avison_native.project(original, literals)
    observation, diagnostic = avison_pages.decode_entry(raw, page.name, {})
    assert diagnostic is None and observation['records'] == projected['items']
    assert observation['numericLiterals'] == projected_literals and observation['projection'] == projection
    assert avison_response.reconstruct(observation) == projected
    assert acquisition.decode_json(acquisition.encode_json(projected, projected_literals).encode()) == (projected, projected_literals)
    assert len(original['items']) == len(projected['items']) == 2088
    # Do not merge repeated or addressless identities: source order is authoritative here.
    original_ids = [row.get('id') if isinstance(row, dict) else None for row in original['items']]
    projected_ids = [row.get('id') if isinstance(row, dict) else None for row in projected['items']]
    assert original_ids == projected_ids
    duplicate_ids = sum(count - 1 for count in Counter(str(v) for v in original_ids if v is not None).values())
    plan = events.plan(observation, source_key='avison-young')
    events.validate_content_policy(plan)
    assert len(plan['events']) == 2089
    assert [row['facts'][0]['value'] for row in plan['events'][1:]] == projected['items']
    initdb, pg_ctl, psql = (tool(name) for name in ('initdb', 'pg_ctl', 'psql'))
    database = 'cre_avison_' + uuid.uuid4().hex[:12]
    with OwnedCluster(initdb, pg_ctl, port=55453) as cluster:
        def sql(statement, *, target=database, user=None, check=True):
            result = subprocess.run([psql, '-X', '-qAt', '-h', str(cluster.root), '-p', str(cluster.port),
                                     '-d', target, '-v', 'ON_ERROR_STOP=1'] + (['-U', user] if user else []),
                                    input=statement, capture_output=True, text=True, timeout=120,
                                    env=cluster.env, check=False)
            if check and result.returncode:
                raise AssertionError('disposable fixture SQL failed')
            return result.stdout if check else result
        sql('CREATE ROLE evidence_executor LOGIN CREATEROLE CREATEDB BYPASSRLS;', target='postgres')
        sql(f'CREATE DATABASE {database} OWNER evidence_executor;', target='postgres')
        sql('CREATE ROLE cre_source_evidence_owner NOLOGIN NOINHERIT; CREATE ROLE cre_source_evidence_writer NOLOGIN NOINHERIT; CREATE ROLE cre_source_evidence_reader NOLOGIN NOINHERIT;', user='evidence_executor')
        sql("CREATE SCHEMA credeals; CREATE TABLE credeals.sentinel(id integer, status text); INSERT INTO credeals.sentinel VALUES (1,'active');")
        migration = (HERE.parents[2] / 'sql/019_cre_source_evidence.sql').read_text()
        sql("\\set CRE_SOURCE_EVIDENCE_APPROVE 1\n\\set CRE_SOURCE_EVIDENCE_OPERATOR cayman\n"
            "\\set CRE_SOURCE_EVIDENCE_APPROVAL_REF AGENTIC-2895\n"
            "\\set CRE_SOURCE_EVIDENCE_CONFIRM 'APPLY 019_cre_source_evidence'\n" + migration, user='evidence_executor')
        sql("CREATE EXTENSION pg_trgm WITH SCHEMA public;")
        migration_020 = (HERE.parents[2] / 'sql/020_cre_source_evidence_fact_text_index.sql').read_text()
        sql("\\set CRE_SOURCE_EVIDENCE_APPROVE 1\n\\set CRE_SOURCE_EVIDENCE_OPERATOR cayman\n"
            "\\set CRE_SOURCE_EVIDENCE_APPROVAL_REF AGENTIC-2895\n"
            "\\set CRE_SOURCE_EVIDENCE_CONFIRM 'APPLY 020_cre_source_evidence_fact_text_index'\n" + migration_020, user='evidence_executor')
        sql("CREATE ROLE evidence_ingest LOGIN NOINHERIT; "
            "GRANT cre_source_evidence_writer TO evidence_ingest WITH INHERIT FALSE, SET TRUE;")
        url = f"postgresql://evidence_ingest@{quote(str(cluster.root), safe='')}:{cluster.port}/{database}"
        target = database_target_fingerprint_from_url(url)["value"]
        env = cluster.root / "writer.env"
        env.write_text("POSTGRES_URL=" + url + "\n")
        env.chmod(0o600)
        git = "a" * 40
        policy = {"schema_version": automatic.SCHEMA_V2, "purpose": "private_unverified_evidence",
                  "data_scope": "public_listing_source", "format": "collector_artifact_v1",
                  "collector_git_sha": git, "target_sha256": target, "source_keys": ["avison-young"],
                  "max_artifact_bytes": 16 * 1024 * 1024, "max_records": 10000,
                  "collector_projections": {}, "native_source_formats": {"avison-young": avison_pages.FORMAT},
                  "max_native_pages": 4}
        policy_raw = events.encoded(policy)
        policy_path = cluster.root / "policy.json"
        policy_path.write_bytes(policy_raw)
        policy_path.chmod(0o600)
        config = automatic.configuration(str(policy_path), hashlib.sha256(policy_raw).hexdigest(),
                                         str(env), target, git, ["avison-young"])
        run = cluster.root / "generation"
        run.mkdir(mode=0o700)
        manifest = refresh.new_manifest(run, git_sha=git, git_dirty=False, sources=("avison-young",),
                                        page_cap=4, concurrency=1, automatic_source_evidence=config)
        attempt = {"number": 1, "source_evidence": {"state": "no_artifact"}}
        manifest["sources"]["avison-young"]["attempts"].append(attempt)
        directory = native_pages.mark(run, manifest, "avison-young", attempt)
        local_page = directory / page.name
        local_page.write_bytes(raw)
        local_page.chmod(0o600)
        native_pages.settle(run, manifest, "avison-young", attempt, lambda *_: None)
        pages = attempt["native_source_evidence"]["pages"]
        assert len(pages) == 1 and pages[0]["state"] == "pending"
        plan = native_pages.prepare_job(run, manifest, "avison-young", attempt, pages[0])
        assert [row["facts"][0]["value"] for row in plan["events"][1:]] == projected["items"]
        os.environ["PSQL_BIN"] = psql
        automatic.pump(run, manifest, lambda *_: None)
        assert pages[0]["state"] == "appended"
        actual = [json.loads(line, parse_float=Decimal) for line in sql(
            'SELECT row_to_json(r) FROM (SELECT capture_id,item_no,source_key,external_id,observed_at,source_url,'
            'provenance,facts,document_url,extractor_version,extracted_text,text_sha256 '
            'FROM cre_source_evidence.events ORDER BY item_no) r;').splitlines()]
        expected = json.loads(events.encoded(plan['events']), parse_float=Decimal)
        for observed, wanted in zip(actual, expected, strict=True):
            assert datetime.fromisoformat(observed.pop('observed_at').replace('Z', '+00:00')) == datetime.fromisoformat(wanted.pop('observed_at').replace('Z', '+00:00'))
            assert observed == wanted
        pages[0]["state"] = "outcome_unknown"
        # An earlier ordinary candidate must not block the uncertain native replay.
        attempt["source_evidence"] = {"state": "pending"}
        automatic.prepare_resume(manifest, replay_unknown=True)
        assert pages[0]["exact_replay_required"]
        automatic.pump(run, manifest, lambda *_: None)
        assert pages[0]["state"] == "appended"
        assert attempt["source_evidence"]["state"] == "refused"
        assert int(sql('SELECT count(*) FROM cre_source_evidence.events;')) == 2089
        first = plan['events'][1]
        conflicting = copy.deepcopy(first['facts'])
        conflicting.append({'kind': 'fixture_divergent_replay', 'value': True})
        def value(field):
            item = first[field]
            if field == 'item_no':
                return str(item)
            if field in ('facts', 'provenance'):
                return cli.sql_text(events.encoded(conflicting if field == 'facts' else item).decode()) + '::jsonb'
            if field == 'observed_at':
                return cli.sql_text(item) + '::timestamptz'
            return cli.sql_text(item)
        divergent = 'BEGIN; SET LOCAL ROLE cre_source_evidence_writer; SELECT cre_source_evidence.append_event(' + ','.join(value(field) for field in cli.FIELDS) + '); COMMIT;'
        assert sql(divergent, check=False).returncode != 0
        assert int(sql('SELECT count(*) FROM cre_source_evidence.events;')) == 2089
        synthetic = {"items": [{"id": "same", "hide_price": 0, "street": None},
                               {"id": "same", "hide_price": 0}]}
        synthetic_document, synthetic_literals, synthetic_receipt = avison_native.project(synthetic, {})
        synthetic_observation = avison_response.convert(synthetic_document, synthetic_literals, synthetic_receipt,
                                                        receipt=entry["receipt"])
        synthetic_plan = events.plan(synthetic_observation, source_key="avison-young")
        events.validate_content_policy(synthetic_plan)
        sql(cli.append_sql(synthetic_plan))
        synthetic_rows = [json.loads(line) for line in sql(
            'SELECT row_to_json(r) FROM (SELECT item_no,facts FROM cre_source_evidence.events WHERE capture_id=' +
            cli.sql_text(synthetic_plan["events"][0]["capture_id"]) + ' ORDER BY item_no) r;').splitlines()]
        assert len(synthetic_rows) == 3
        assert [row["facts"][0]["value"] for row in synthetic_rows[1:]] == synthetic["items"]
        assert sql('SELECT status FROM credeals.sentinel WHERE id=1;').strip() == 'active'
        print(f'Disposable PG17 Avison: {len(projected_ids)} projected records, {duplicate_ids} repeated ID occurrences, 2089 events; restricted foreground pump, all fields, exact and divergent replay and synthetic duplicate/no-address passed')


if __name__ == '__main__':
    main()
