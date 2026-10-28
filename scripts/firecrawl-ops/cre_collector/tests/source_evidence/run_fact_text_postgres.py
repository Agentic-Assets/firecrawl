"""Exercise private fact text search against an owned disposable PG17 cluster."""
import argparse
import base64
import hashlib
import json
import select
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parent / "observation_store"))
from owned_observation_fixture_cluster import OwnedCluster, tool
from source_evidence import (
    acquisition,
    cli,
    collector,
    events,
    map_projection,
    native_pages,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-disposable-fixture', action='store_true', required=True)
    parser.add_argument('--faris-aggregate', required=True)
    parser.add_argument('--migration', help='optional migration 020 under test')
    parser.add_argument('--saved-native-page', help='optional saved source-native page')
    args = parser.parse_args()
    initdb, pg_ctl, psql = (tool(name) for name in ('initdb', 'pg_ctl', 'psql'))
    database = 'cre_search_' + uuid.uuid4().hex[:12]
    with OwnedCluster(initdb, pg_ctl, port=55451) as cluster:
        def sql(statement, *, target=database, user=None, check=True):
            result = subprocess.run([psql, '-X', '-qAt', '-h', str(cluster.root), '-p', str(cluster.port),
                                     '-d', target, '-v', 'ON_ERROR_STOP=1'] + (['-U', user] if user else []),
                                    input=statement, capture_output=True, text=True, timeout=120,
                                    env=cluster.env, check=False)
            if check and result.returncode:
                raise AssertionError('fixture SQL failed: ' + result.stderr)
            return result.stdout if check else result
        sql('CREATE ROLE evidence_executor LOGIN CREATEROLE CREATEDB BYPASSRLS;', target='postgres')
        sql(f'CREATE DATABASE {database} OWNER evidence_executor;', target='postgres')
        sql('CREATE ROLE cre_source_evidence_owner NOLOGIN NOINHERIT; CREATE ROLE cre_source_evidence_writer NOLOGIN NOINHERIT; CREATE ROLE cre_source_evidence_reader NOLOGIN NOINHERIT;', user='evidence_executor')
        migration = (HERE.parents[2] / 'sql/019_cre_source_evidence.sql').read_text()
        sql("\\set CRE_SOURCE_EVIDENCE_APPROVE 1\n\\set CRE_SOURCE_EVIDENCE_OPERATOR cayman\n"
            "\\set CRE_SOURCE_EVIDENCE_APPROVAL_REF AGENTIC-2895\n"
            "\\set CRE_SOURCE_EVIDENCE_CONFIRM 'APPLY 019_cre_source_evidence'\n" + migration, user='evidence_executor')
        sql('CREATE EXTENSION pg_trgm WITH SCHEMA public;')
        if args.migration:
            migration_020 = Path(args.migration).read_text()
            approvals = ("\\set CRE_SOURCE_EVIDENCE_APPROVE 1\n\\set CRE_SOURCE_EVIDENCE_OPERATOR cayman\n"
                         "\\set CRE_SOURCE_EVIDENCE_APPROVAL_REF AGENTIC-2895\n"
                         "\\set CRE_SOURCE_EVIDENCE_CONFIRM 'APPLY 020_cre_source_evidence_fact_text_index'\n")
            def invariants():
                return sql("SELECT json_build_object('membership',(SELECT json_agg(row_to_json(x) ORDER BY x.rolname,x.member) FROM "
                           "(SELECT r.rolname,m.member::regrole::text AS member,m.admin_option,m.inherit_option,m.set_option "
                           "FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid WHERE r.rolname LIKE 'cre_source_evidence_%') x),"
                           "'table_acl',(SELECT relacl::text FROM pg_class WHERE oid='cre_source_evidence.events'::regclass),"
                           "'schema_acl',(SELECT nspacl::text FROM pg_namespace WHERE nspname='cre_source_evidence'),"
                           "'rls',(SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid='cre_source_evidence.events'::regclass),"
                           "'policies',(SELECT count(*) FROM pg_policy WHERE polrelid='cre_source_evidence.events'::regclass));")
            baseline = invariants()
            assert sql(migration_020, user='evidence_executor', check=False).returncode != 0
            assert invariants() == baseline
            sql(approvals + migration_020, user='evidence_executor')
            assert invariants() == baseline
            sql(approvals + migration_020, user='evidence_executor')
            assert invariants() == baseline
            for add, undo in [
                ('GRANT UPDATE(source_key) ON cre_source_evidence.events TO PUBLIC',
                 'REVOKE UPDATE(source_key) ON cre_source_evidence.events FROM PUBLIC'),
                ('CREATE POLICY unwanted ON cre_source_evidence.events FOR SELECT TO PUBLIC USING (true)',
                 'DROP POLICY unwanted ON cre_source_evidence.events'),
                ('CREATE ROLE fixture_unexpected NOLOGIN; GRANT cre_source_evidence_reader TO fixture_unexpected',
                 'REVOKE cre_source_evidence_reader FROM fixture_unexpected; DROP ROLE fixture_unexpected'),
            ]:
                sql(add)
                unsafe = invariants()
                assert sql(approvals + migration_020, user='evidence_executor', check=False).returncode != 0
                assert invariants() == unsafe
                sql(undo)
                assert invariants() == baseline
            sql('ALTER EXTENSION pg_trgm DROP OPERATOR CLASS public.gin_trgm_ops USING gin;')
            assert sql(approvals + migration_020, user='evidence_executor', check=False).returncode != 0
            assert invariants() == baseline
            sql('ALTER EXTENSION pg_trgm ADD OPERATOR CLASS public.gin_trgm_ops USING gin;')
            sql('REVOKE USAGE ON SCHEMA public FROM PUBLIC;')
            assert sql(approvals + migration_020, user='evidence_executor', check=False).returncode != 0
            assert invariants() == baseline
            sql('GRANT USAGE ON SCHEMA public TO PUBLIC;')
            sql('DROP INDEX cre_source_evidence.cre_source_evidence_facts_text_trgm_idx;')
            lock_process = subprocess.Popen(
                [psql, '-X', '-qAt', '-h', str(cluster.root), '-p', str(cluster.port),
                 '-d', database, '-v', 'ON_ERROR_STOP=1'], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=cluster.env)
            try:
                lock_process.stdin.write("BEGIN; LOCK TABLE cre_source_evidence.events IN ACCESS EXCLUSIVE MODE; SELECT 'lock_ready';\n")
                lock_process.stdin.flush()
                ready, _, _ = select.select([lock_process.stdout], [], [], 10)
                assert ready and lock_process.stdout.readline().strip() == 'lock_ready'
                started = time.monotonic()
                refused = sql(approvals + migration_020, user='evidence_executor', check=False)
                elapsed = time.monotonic() - started
                assert refused.returncode != 0 and 'lock timeout' in refused.stderr
                assert elapsed < 10
                assert sql("SELECT to_regclass('cre_source_evidence.cre_source_evidence_facts_text_trgm_idx') IS NULL;").strip() == 't'
                assert invariants() == baseline
            finally:
                lock_process.stdin.write('ROLLBACK;\n\\q\n')
                lock_process.stdin.flush()
                lock_process.wait(timeout=10)
            assert lock_process.returncode == 0
            sql(approvals + migration_020, user='evidence_executor')
            assert invariants() == baseline
            print(f'Disposable PG17 lock timeout: {elapsed:.2f}s, rollback and post-release apply passed')
            sql('DROP INDEX cre_source_evidence.cre_source_evidence_facts_text_trgm_idx; '
                'CREATE INDEX cre_source_evidence_facts_text_trgm_idx ON cre_source_evidence.events (source_key);')
            assert sql(approvals + migration_020, user='evidence_executor', check=False).returncode != 0
            assert invariants() == baseline
            assert sql("SELECT pg_get_indexdef('cre_source_evidence.cre_source_evidence_facts_text_trgm_idx'::regclass);").strip().endswith('USING btree (source_key)')
            sql('DROP INDEX cre_source_evidence.cre_source_evidence_facts_text_trgm_idx;')
            sql(approvals + migration_020, user='evidence_executor')
            assert invariants() == baseline
        else:
            sql('CREATE INDEX fixture_facts_text_idx ON cre_source_evidence.events USING gin ((facts::text) public.gin_trgm_ops);')
        # Native-like records deliberately include unknown keys, duplicates, nulls and precision text.
        native = {"data": {"publicPosts": [
                      {"id": i, "unknown_label": "SearchableNativeMarker",
                       "value": ["Mixed Case", None, "0.10000000000000000001", "Café 東京"],
                       "precise": "EXACT_NUMERIC",
                       "special": 'q"uote\\back\nline 100%_!\x01control'} for i in range(100)]}}
        native_raw = json.dumps(native).encode().replace(b'"EXACT_NUMERIC"', b'0.10000000000000000001')
        receipt = {"sourceKey": "nai-global", "requestUrl": "https://example.test/feed",
                   "finalUrl": "https://example.test/feed", "method": "POST", "status": 200,
                   "startedAt": "2026-10-28T09:59:59Z", "finishedAt": "2026-10-28T10:00:00Z",
                   "bodySha256": events.sha256(native_raw), "bodyBytes": len(native_raw)}
        native_plan = events.plan(acquisition.convert(native_raw, receipt=receipt,
                                  array_pointer="/data/publicPosts"), source_key="nai-global")
        sql(cli.append_sql(native_plan))
        actual_native_count = 0
        if args.saved_native_page:
            page = Path(args.saved_native_page)
            actual_observation, reason = native_pages.decode_entry(page.read_bytes(), page.name, {})
            assert reason is None and len(actual_observation['records']) > 50
            actual_native_count = len(actual_observation['records'])
            actual_native_plan = events.plan(actual_observation, source_key='nai-global')
            sql(cli.append_sql(actual_native_plan))
        original = Path(args.faris_aggregate).read_bytes()
        faris, literals, receipt = map_projection.prepare(original, source_key='faris-lee')
        assert len(faris['listings']) == 53
        faris_plan = events.plan(collector.convert(faris, source_key='faris-lee',
                                  numeric_literals=literals or None, projection=receipt), source_key='faris-lee')
        sql(cli.append_sql(faris_plan))
        # Exercise the real table bounds and GIN index with a value near four MiB.
        diverse_text = base64.b64encode(hashlib.shake_256(b'bounded-index-fixture').digest(3_135_000)).decode()
        large = [{'kind': 'source_record', 'value': {'body': diverse_text, 'last_field': 'TailNeedleZebra'}}]
        large_json = events.encoded(large).decode()
        assert 4_180_000 < len(large_json.encode()) < 4_194_304
        sql("BEGIN; SET LOCAL ROLE cre_source_evidence_writer; "
            "SELECT cre_source_evidence.append_event('near-limit',1,'nai-global',NULL,"
            "'2026-10-28T10:00:00Z'::timestamptz,NULL,'{}'::jsonb," +
            cli.sql_text(large_json) + "::jsonb,NULL,NULL,NULL,NULL); COMMIT;")
        def matches(query, **kwargs):
            return [json.loads(line) for line in sql(cli.fact_text_sql(query, 25, **kwargs)).splitlines()]
        assert len(matches('SearchableNativeMarker', source_key='nai-global')) == 25
        assert len(matches('SearchableNativeMarker', source_key='faris-lee')) == 0
        assert len(matches('SearchableNativeMarker', capture_id=native_plan['events'][0]['capture_id'])) == 25
        faris_results = matches('faris-lee', source_key='faris-lee', capture_id=faris_plan['events'][0]['capture_id'])
        assert len(faris_results) == 25 and all(row['source_key'] == 'faris-lee' for row in faris_results)
        assert [row['item_no'] for row in faris_results] == list(range(25))
        assert matches('TailNeedleZebra')[0]['capture_id'] == 'near-limit'
        assert len(matches('q"uote\\back\nline 100%_!', source_key='nai-global')) == 25
        assert len(matches('\x01control', source_key='nai-global')) == 25
        assert len(matches('100X', source_key='nai-global', capture_id=native_plan['events'][0]['capture_id'])) == 0
        assert len(matches('Café 東京', source_key='nai-global')) == 25
        assert any(row['item_no'] == 0 for row in matches('0.10000000000000000001', source_key='nai-global'))
        assert len(matches('Mixed Case', source_key='nai-global')) == 25
        assert len(matches('mixed case', source_key='nai-global')) == 25
        assert matches("'); DROP SCHEMA credeals; --") == []
        assert int(sql("SELECT count(*) FROM cre_source_evidence.events;").strip()) == 156 + (actual_native_count + 1 if actual_native_count else 0)
        if actual_native_count:
            capture = actual_native_plan['events'][0]['capture_id']
            actual_rows = [json.loads(line) for line in sql(
                'SELECT row_to_json(r) FROM (SELECT item_no,facts FROM cre_source_evidence.events WHERE capture_id=' +
                cli.sql_text(capture) + ' ORDER BY item_no) r;').splitlines()]
            assert [row['facts'] for row in actual_rows] == [row['facts'] for row in actual_native_plan['events']]
            assert len(matches('title', source_key='nai-global', capture_id=capture)) == 25
        # Full values remain accessible without rewriting facts.
        native_out = [json.loads(line) for line in sql(cli.get_sql(native_plan['events'][0]['capture_id'], 1)).splitlines()]
        assert native_out[0]['facts'][0]['value']['value'][2] == '0.10000000000000000001'
        header = [json.loads(line) for line in sql(cli.get_sql(native_plan['events'][0]['capture_id'], 0)).splitlines()]
        assert header[0]['facts'][0]['value']['numericLiterals']['/data/publicPosts/0/precise'] == '0.10000000000000000001'
        out = [json.loads(line) for line in sql(cli.get_sql('near-limit', 1)).splitlines()]
        assert out[0]['facts'][0]['value']['last_field'] == 'TailNeedleZebra'
        assert out[0]['facts'][0]['value']['body'] == diverse_text
        print(f'Disposable PG17 fact search: 100 synthetic, {actual_native_count} saved NAI, 53 assembled Faris, near-4MiB fact, filters and literal escapes passed')


if __name__ == '__main__':
    main()
