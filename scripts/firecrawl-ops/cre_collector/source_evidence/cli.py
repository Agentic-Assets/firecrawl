"""Private evidence planning and guarded access to the separate evidence store."""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from buildout_observation_io import read_consistent
from cre_ingest import (
    assert_expected_database_target,
    find_psql,
    load_db_url,
    psql_connection_args,
    psql_connection_env,
)

from source_evidence import acquisition, collector, events

MAX_PLAN_BYTES = 32 * 1024 * 1024
MAX_RESULTS_BYTES = 8 * 1024 * 1024
FIELDS = events.EVENT_FIELDS


def read_json(path, limit):
    raw = read_consistent(path, limit)
    if len(raw) > limit:
        raise ValueError('input exceeds limit')
    value, literals = acquisition.decode_json(raw, max_bytes=limit)
    if literals:
        raise ValueError('exceptional numeric values require a lossless source conversion')
    return value


def write_private(path, value):
    payload = events.encoded(value) + b'\n'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(payload)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def sql_text(value):
    if value is None:
        return 'NULL'
    if not isinstance(value, str) or '\x00' in value:
        raise ValueError('invalid SQL text')
    # Hex binds arbitrary UTF-8 as data, independent of SQL quote settings.
    return "convert_from(decode('" + value.encode('utf-8').hex() + "','hex'),'UTF8')"


def append_sql(plan_value):
    events.validate_plan(plan_value)
    rows = plan_value.get('events')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10001:
        raise ValueError('invalid event count')
    calls = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(FIELDS):
            raise ValueError('invalid event fields')
        ordinal = row['item_no']
        if type(ordinal) is not int or not 0 <= ordinal <= 1000000:
            raise ValueError('invalid item number')
        values = []
        for field in FIELDS:
            value = row[field]
            if field == 'item_no':
                values.append(str(value))
            elif field in ('provenance', 'facts'):
                values.append(sql_text(events.encoded(value).decode()) + '::jsonb')
            elif field == 'observed_at':
                values.append(sql_text(value) + '::timestamptz')
            else:
                values.append(sql_text(value))
        calls.append('SELECT cre_source_evidence.append_event(' + ','.join(values) + ');')
    return 'BEGIN;\nSET LOCAL search_path = pg_catalog;\nSET LOCAL ROLE cre_source_evidence_writer;\nSET LOCAL statement_timeout = 30000;\n' + '\n'.join(calls) + '\nCOMMIT;\n'


def search_sql(query, mode, limit):
    if not isinstance(query, str) or not query.strip() or len(query.encode()) > 512 or '\x00' in query:
        raise ValueError('invalid search query')
    if mode not in ('plain', 'phrase') or type(limit) is not int or not 1 <= limit <= 25:
        raise ValueError('invalid search controls')
    function = 'plainto_tsquery' if mode == 'plain' else 'phraseto_tsquery'
    return ("BEGIN READ ONLY;\nSET LOCAL search_path = pg_catalog;\nSET LOCAL ROLE cre_source_evidence_reader;\nSET LOCAL statement_timeout = 30000;\n"
            "SELECT row_to_json(r) FROM (SELECT capture_id,item_no,source_key,external_id,observed_at,source_url,"
            "provenance,document_url,extractor_version,extracted_text,text_sha256 FROM cre_source_evidence.events "
            "WHERE text_search @@ pg_catalog." + function + "('pg_catalog.simple'::regconfig," + sql_text(query) + ") "
            "ORDER BY observed_at DESC,capture_id,item_no LIMIT " + str(limit) + ") r;\nCOMMIT;\n")


def facts_sql(contains, limit):
    if not isinstance(contains, list) or not contains or len(events.encoded(contains)) > 16384:
        raise ValueError('fact search requires a bounded nonempty JSON array')
    if type(limit) is not int or not 1 <= limit <= 25:
        raise ValueError('invalid fact search limit')
    return ("BEGIN READ ONLY;\nSET LOCAL search_path = pg_catalog;\nSET LOCAL ROLE cre_source_evidence_reader;\n"
            "SET LOCAL statement_timeout = 30000;\nSELECT row_to_json(r) FROM (SELECT capture_id,item_no,source_key,"
            "external_id,observed_at,source_url,provenance,pg_column_size(facts) AS facts_bytes "
            "FROM cre_source_evidence.events WHERE facts @> " + sql_text(events.encoded(contains).decode()) +
            "::jsonb ORDER BY observed_at DESC,capture_id,item_no LIMIT " + str(limit) + ") r;\nCOMMIT;\n")



def fact_text_sql(query, limit, *, source_key=None, capture_id=None):
    """Search literal text in JSONB's serialized facts under the reader role."""
    if not isinstance(query, str) or not query or "\x00" in query:
        raise ValueError("invalid fact text query")
    try:
        if len(query.encode("utf-8")) > 512:
            raise ValueError("fact text query exceeds limit")
    except UnicodeEncodeError as exc:
        raise ValueError("invalid fact text query") from exc
    if type(limit) is not int or not 1 <= limit <= 25:
        raise ValueError("invalid fact text search limit")
    if source_key is not None and (not isinstance(source_key, str) or
                                   not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", source_key)):
        raise ValueError("invalid source key")
    if capture_id is not None and (not isinstance(capture_id, str) or
                                   not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", capture_id)):
        raise ValueError("invalid capture identity")
    needle = json.dumps(query, ensure_ascii=False)[1:-1]
    needle = needle.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    pattern = ("%" + needle + "%").replace("\\", "\\\\").replace("'", "''")
    predicates = ["(facts::text) ILIKE E'" + pattern + "' ESCAPE '!' "]
    if source_key is not None:
        predicates.append("source_key=" + sql_text(source_key))
    if capture_id is not None:
        predicates.append("capture_id=" + sql_text(capture_id))
    return ("BEGIN READ ONLY;\nSET LOCAL client_encoding = 'UTF8';\nSET LOCAL search_path = pg_catalog;\nSET LOCAL ROLE cre_source_evidence_reader;\n"
            "SET LOCAL statement_timeout = 30000;\nSELECT row_to_json(r) FROM (SELECT capture_id,item_no,source_key,"
            "external_id,observed_at,source_url,provenance,pg_column_size(facts) AS facts_bytes "
            "FROM cre_source_evidence.events WHERE " + " AND ".join(predicates) +
            " ORDER BY observed_at DESC,capture_id,item_no LIMIT " + str(limit) + ") r;\nCOMMIT;\n")

def get_sql(capture_id, item_no):
    if not isinstance(capture_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', capture_id):
        raise ValueError('invalid capture identity')
    if type(item_no) is not int or not 0 <= item_no <= 1000000:
        raise ValueError('invalid event ordinal')
    return ("BEGIN READ ONLY;\nSET LOCAL search_path = pg_catalog;\nSET LOCAL ROLE cre_source_evidence_reader;\n"
            "SET LOCAL statement_timeout = 30000;\nSELECT row_to_json(r) FROM (SELECT " + ','.join(FIELDS) +
            " FROM cre_source_evidence.events WHERE capture_id=" + sql_text(capture_id) +
            " AND item_no=" + str(item_no) + ") r;\nCOMMIT;\n")


def document_sql(capture_id):
    """Retrieve one bounded document capture, including its reconstruction header."""
    return capture_sql(capture_id, 72)


def collector_sql(capture_id):
    """Read a whole collector capture plus one row to detect an oversized set."""
    return capture_sql(capture_id, 10002)


def capture_sql(capture_id, limit):
    if not isinstance(capture_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', capture_id):
        raise ValueError('invalid capture identity')
    if type(limit) is not int or not 1 <= limit <= 10002:
        raise ValueError('invalid capture row limit')
    # Read one extra row so oversized captures cannot look like complete ones.
    return ("BEGIN READ ONLY;\nSET LOCAL search_path = pg_catalog;\nSET LOCAL ROLE cre_source_evidence_reader;\n"
            "SET LOCAL statement_timeout = 30000;\nSELECT row_to_json(r) FROM (SELECT " + ','.join(FIELDS) +
            " FROM cre_source_evidence.events WHERE capture_id=" + sql_text(capture_id) +
            " ORDER BY item_no LIMIT " + str(limit) + ") r;\nCOMMIT;\n")


def checked_plan(value, admission):
    if not isinstance(value, dict) or value.get('schema_version') != 'cre-source-evidence-plan/v1':
        raise ValueError('invalid plan')
    unsigned = {key: val for key, val in value.items() if key != 'plan_sha256'}
    if value.get('plan_sha256') != events.sha256(events.encoded(unsigned)):
        raise ValueError('plan digest mismatch')
    events.validate_admission(value, admission)


def run_psql(sql, env_file, target, *, validate_transport=None, max_results_bytes=MAX_RESULTS_BYTES):
    if not env_file or not re.fullmatch(r'[0-9a-f]{64}', target or ''):
        raise ValueError('explicit env file and target fingerprint required')
    if type(max_results_bytes) is not int or not 1 <= max_results_bytes <= MAX_PLAN_BYTES:
        raise ValueError('invalid database result bound')
    db_url, _ = load_db_url(env_file)
    assert_expected_database_target(db_url, target)
    if validate_transport is not None:
        validate_transport(db_url)
    cmd = [find_psql(), '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1', *psql_connection_args(db_url)]
    try:
        result = subprocess.run(cmd, input=sql.encode(), capture_output=True,
                                env=psql_connection_env(db_url), timeout=45, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('database outcome unknown after timeout') from exc
    if result.returncode != 0:
        raise RuntimeError('database operation failed; outcome may be unknown')
    if len(result.stdout) > max_results_bytes:
        raise ValueError('database output exceeds limit')
    return result.stdout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan')
    p.add_argument('--artifact', required=True); p.add_argument('--source-key', required=True)
    p.add_argument('--observed-at'); p.add_argument('--external-id'); p.add_argument('--out', required=True)
    documents = sub.add_parser('discover-documents')
    documents.add_argument('--plan', required=True); documents.add_argument('--out', required=True)
    acquisition_plan = sub.add_parser('plan-document-acquisition')
    acquisition_plan.add_argument('--plan', required=True); acquisition_plan.add_argument('--candidates', required=True)
    acquisition_plan.add_argument('--policy', required=True); acquisition_plan.add_argument('--policy-sha256', required=True)
    acquisition_plan.add_argument('--expected-db-target-sha256', required=True)
    acquisition_plan.add_argument('--collector-git-sha', required=True); acquisition_plan.add_argument('--out', required=True)
    c = sub.add_parser('convert-collector')
    c.add_argument('--artifact', required=True); c.add_argument('--source-key', required=True)
    c.add_argument('--out', required=True)
    c.add_argument('--project-map-credentials', action='store_true')
    r = sub.add_parser('convert-response')
    r.add_argument('--body', required=True); r.add_argument('--receipt', required=True)
    selection = r.add_mutually_exclusive_group(required=True)
    selection.add_argument('--array-pointer'); selection.add_argument('--whole-response', action='store_true')
    r.add_argument('--out', required=True)
    r.add_argument('--capture-context')
    a = sub.add_parser('append')
    a.add_argument('--plan', required=True); a.add_argument('--admission', required=True)
    a.add_argument('--env-file', required=True); a.add_argument('--expected-db-target-sha256', required=True)
    a.add_argument('--apply', action='store_true', required=True)
    s = sub.add_parser('search')
    s.add_argument('--query', required=True); s.add_argument('--mode', choices=('plain', 'phrase'), default='plain')
    s.add_argument('--limit', type=int, default=10); s.add_argument('--out', required=True)
    s.add_argument('--env-file', required=True); s.add_argument('--expected-db-target-sha256', required=True)
    f = sub.add_parser('search-facts')
    f.add_argument('--contains-file', required=True); f.add_argument('--limit', type=int, default=10)
    f.add_argument('--out', required=True); f.add_argument('--env-file', required=True)
    f.add_argument('--expected-db-target-sha256', required=True)
    ft = sub.add_parser('search-fact-text')
    ft.add_argument('--query', required=True); ft.add_argument('--limit', type=int, default=10)
    ft.add_argument('--source-key'); ft.add_argument('--capture-id'); ft.add_argument('--out', required=True)
    ft.add_argument('--env-file', required=True); ft.add_argument('--expected-db-target-sha256', required=True)
    g = sub.add_parser('get')
    g.add_argument('--capture-id', required=True); g.add_argument('--item-no', type=int, required=True)
    g.add_argument('--out', required=True); g.add_argument('--env-file', required=True)
    g.add_argument('--expected-db-target-sha256', required=True)
    export = sub.add_parser('export-document-text')
    export.add_argument('--capture-id', required=True); export.add_argument('--out-dir', required=True)
    export.add_argument('--env-file', required=True); export.add_argument('--expected-db-target-sha256', required=True)
    export_collector = sub.add_parser('export-collector-capture')
    export_collector.add_argument('--capture-id', required=True); export_collector.add_argument('--out-dir', required=True)
    export_collector.add_argument('--env-file', required=True)
    export_collector.add_argument('--expected-db-target-sha256', required=True)
    args = parser.parse_args(argv)
    if args.command == 'convert-collector':
        raw = read_consistent(args.artifact, events.MAX_ARTIFACT_BYTES)
        projection = None
        if args.project_map_credentials:
            from source_evidence import map_projection
            artifact, literals, projection = map_projection.prepare(raw, source_key=args.source_key)
        else:
            artifact, literals = acquisition.decode_json(raw)
        result = collector.convert(artifact, source_key=args.source_key, numeric_literals=literals or None,
                                   projection=projection)
        write_private(args.out, result)
        print(f"Converted collector artifact: {collector.record_count(result)} records and observations")
    elif args.command == 'convert-response':
        raw = read_consistent(args.body, events.MAX_ARTIFACT_BYTES)
        result = acquisition.convert(raw, receipt=read_json(args.receipt, 16384),
                                     array_pointer=None if args.whole_response else args.array_pointer,
                                     capture_context=read_json(args.capture_context, 65536) if args.capture_context else None)
        write_private(args.out, result)
        print(f"Converted response: {len(result['records'])} records")
    elif args.command == 'plan':
        result = events.plan(read_json(args.artifact, events.MAX_ARTIFACT_BYTES), source_key=args.source_key,
                             observed_at=args.observed_at, external_id=args.external_id)
        write_private(args.out, result)
        print(f"Plan created: {len(result['events'])} events; SHA-256 {result['plan_sha256']}")
    elif args.command == 'discover-documents':
        from source_evidence import document_candidates
        result = document_candidates.discover(read_json(args.plan, MAX_PLAN_BYTES))
        write_private(args.out, result)
        print(f"Document candidates saved: {len(result['candidates'])}; acquisition is not enabled.")
    elif args.command == 'plan-document-acquisition':
        from source_evidence import automatic, document_acquisition_policy, document_candidates
        raw_policy = automatic._read_private(Path(args.policy).expanduser(), 16384)
        result = document_acquisition_policy.prepare(
            read_json(args.plan, MAX_PLAN_BYTES), read_json(args.candidates, document_candidates.MAX_MANIFEST_BYTES),
            raw_policy, expected_policy_sha256=args.policy_sha256,
            target_sha256=args.expected_db_target_sha256, collector_git_sha=args.collector_git_sha)
        write_private(args.out, result)
        print(f"Document acquisition selection saved: {len(result['documents'])}; no requests performed.")
    elif args.command == 'append':
        value = read_json(args.plan, MAX_PLAN_BYTES)
        checked_plan(value, read_json(args.admission, 16384))
        run_psql(append_sql(value), args.env_file, args.expected_db_target_sha256)
        print(f"Append completed: {len(value['events'])} events")
    elif args.command == 'export-document-text':
        from source_evidence import document_export
        raw = run_psql(document_sql(args.capture_id), args.env_file, args.expected_db_target_sha256,
                       max_results_bytes=MAX_PLAN_BYTES)
        rows = [json.loads(line) for line in raw.decode('utf8').splitlines() if line.strip()]
        if any(not isinstance(row, dict) or row.get('capture_id') != args.capture_id for row in rows):
            raise ValueError('document readback contains another capture')
        document_export.export(rows, args.out_dir)
        print('Document text exported to a new private directory; coverage remains unknown.')
    elif args.command == 'export-collector-capture':
        from source_evidence import collector_export
        raw = run_psql(collector_sql(args.capture_id), args.env_file, args.expected_db_target_sha256,
                       max_results_bytes=MAX_PLAN_BYTES)
        rows = [json.loads(line) for line in raw.decode('utf8').splitlines() if line.strip()]
        if any(not isinstance(row, dict) or row.get('capture_id') != args.capture_id for row in rows):
            raise ValueError('collector readback contains another capture')
        collector_export.export(rows, args.out_dir)
        print('Collector capture exported to a new private directory; coverage remains unverified.')
    else:
        if args.command == 'search':
            sql = search_sql(args.query, args.mode, args.limit)
        elif args.command == 'search-facts':
            sql = facts_sql(read_json(args.contains_file, 16384), args.limit)
        elif args.command == 'search-fact-text':
            sql = fact_text_sql(args.query, args.limit, source_key=args.source_key, capture_id=args.capture_id)
        else:
            sql = get_sql(args.capture_id, args.item_no)
        raw = run_psql(sql, args.env_file, args.expected_db_target_sha256)
        rows = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
        write_private(args.out, {'schema_version': 'cre-source-evidence-search/v1', 'results': rows})
        print(f'Search completed: {len(rows)} results')


if __name__ == '__main__':
    try:
        main()
    except RuntimeError:
        print('Database operation failed or timed out; the write outcome may be unknown. Reconcile, then retry the exact admitted plan if appropriate.', file=sys.stderr)
        sys.exit(1)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f'Operation refused or failed ({type(exc).__name__}); no content printed.', file=sys.stderr)
        sys.exit(1)
