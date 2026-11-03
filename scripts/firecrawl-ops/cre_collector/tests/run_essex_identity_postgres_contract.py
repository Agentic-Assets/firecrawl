#!/usr/bin/env python3
"""Opt-in isolated PostgreSQL contract for Essex historical identity checks.

Creates only a uniquely labelled disposable PostgreSQL 17 container; never
accepts an application database URL. Run from a standalone no-hardlinks clone.
"""
import argparse
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cre_essex_identity as identity
import cre_ingest

IMAGE = 'sha256:2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3'
ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(Path.home()), 'LANG': 'C.UTF-8'}
BASE = 'https://essexrealtygroup.com/'


def call(argv, *, data=None, check=True, timeout=30):
    result = subprocess.run(argv, input=data, text=True, capture_output=True, env=ENV, timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError(f'fixture command failed ({result.returncode}): {argv[0]} {result.stderr[-500:]}')
    return result


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def page(url, *ids, invalid=False):
    return {'sourceKey': 'essex-realty', 'claims': [
        {'essexWordpressPage': {'type': 'wrong' if invalid else 'wordpress_properties_post',
                                'id': native, 'canonicalUrl': url}} for native in ids]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-disposable-fixture', action='store_true', required=True)
    parser.add_argument('--proof-dir', required=True)
    args = parser.parse_args()
    proof = Path(args.proof_dir).expanduser()
    if not proof.is_absolute() or proof.exists() or any(p.is_symlink() for p in (proof, *proof.parents)):
        raise ValueError('new unaliased proof directory required')
    proof.mkdir(mode=0o700)
    token = uuid.uuid4().hex
    name = 'cre-essex-contract-' + token
    container = None
    def docker(*args, check=True, timeout=30):
        return call(['docker', *args], check=check, timeout=timeout)
    def cleanup():
        target = container or name
        inspect = docker('inspect', '--format', '{{.Id}} {{.Name}} {{index .Config.Labels "com.agenticassets.fixture-token"}}',
                         target, check=False)
        if inspect.returncode:
            raise RuntimeError('owned fixture cleanup could not be confirmed')
        fields = inspect.stdout.strip().split()
        if len(fields) != 3 or not re.fullmatch('[0-9a-f]{64}', fields[0]) or fields[1] != '/' + name or fields[2] != token:
            raise RuntimeError('fixture identity mismatch; refusing removal')
        if container is not None and fields[0] != container:
            raise RuntimeError('fixture ID mismatch; refusing removal')
        docker('rm', '-f', fields[0])
        if docker('inspect', fields[0], check=False).returncode == 0:
            raise RuntimeError('fixture removal unconfirmed')
        docker('info', '--format', '{{.ID}}')
    try:
        result = docker('create', '--pull', 'never', '--name', name, '--label', 'com.agenticassets.fixture-token=' + token,
                        '--network', 'bridge', '--publish', '127.0.0.1::5432', '--cpus', '0.5',
                        '--memory', '512m', '--memory-swap', '512m', '--pids-limit', '96',
                        '--tmpfs', '/var/lib/postgresql/data:rw,nosuid,size=128m',
                        '--env', 'POSTGRES_HOST_AUTH_METHOD=trust', IMAGE)
        container = result.stdout.strip()
        if not re.fullmatch('[0-9a-f]{64}', container):
            raise RuntimeError('unexpected fixture container ID')
        docker('start', container)
        details = json.loads(docker('inspect', container).stdout)[0]
        limits = details['HostConfig']
        if (details['Image'] != IMAGE or limits['NanoCpus'] != 500000000 or limits['Memory'] != 512 * 1024 * 1024
                or limits['PidsLimit'] != 96 or limits['NetworkMode'] != 'bridge'):
            raise RuntimeError('fixture image or resource limits differ')
        for _ in range(60):
            if docker('exec', container, 'pg_isready', '-U', 'postgres', check=False).returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError('fixture PostgreSQL readiness timed out')
        port = docker('port', container, '5432/tcp').stdout.strip()
        if not re.fullmatch(r'127\.0\.0\.1:[0-9]+', port):
            raise RuntimeError('fixture did not publish only loopback')
        port_number = port.rsplit(':', 1)[1]
        (proof / 'container.json').write_text(json.dumps({
            'id': container, 'name': name, 'fixture_token': token, 'image': IMAGE,
            'published_loopback': port, 'cpus': 0.5, 'memory_bytes': 512 * 1024 * 1024,
            'pids_limit': 96,
        }, indent=2) + '\n')
        def sql(text, *, check=True):
            return call(['/usr/bin/psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-h', '127.0.0.1',
                         '-p', port_number, '-U', 'postgres', '-d', 'postgres'], data=text, check=check)
        for _ in range(60):
            probe = sql('SELECT 1;', check=False)
            if probe.returncode == 0 and probe.stdout.strip() == '1':
                break
            time.sleep(0.5)
        else:
            raise RuntimeError('loopback fixture PostgreSQL readiness timed out')
        version = int(sql('SHOW server_version_num;').stdout.strip())
        if not 170000 <= version < 180000:
            raise RuntimeError('fixture server is not PostgreSQL 17')
        sql('CREATE SCHEMA credeals; CREATE TABLE credeals.cre_listings '
            '(brokerage_id integer, external_id text, source_url text, raw_data jsonb, deleted_at timestamptz);')
        candidate_url = BASE + 'properties/foo/'
        stage = ('CREATE TEMP TABLE _src (brokerage_id integer, external_id text, source_url text, raw_data jsonb);\n'
                 'INSERT INTO _src VALUES (1, ' + literal('properties/foo') + ', ' + literal(candidate_url) + ', '
                 + literal(json.dumps(page(candidate_url, '101'))) + '::jsonb);\n')
        generated = cre_ingest.build_sql([], [], '2026-01-01T00:00:00Z', [])
        start = generated.index('-- Essex keeps historical literal property-path keys.')
        end = generated.index('-- DAUM keeps historical literal property-path keys.', start)
        guard = generated[start:end]
        incoming = identity.incoming_claims_sql().strip().rstrip(';')
        invalid = identity.invalid_incoming_sql().strip().rstrip(';')
        historical = identity.historical_claims_sql().strip().rstrip(';')
        conflict = identity.historical_conflicts_sql().strip().rstrip(';')
        if any(fragment not in guard for fragment in (incoming, invalid, historical, conflict)):
            raise AssertionError('precheck predicates are not the actual generated transaction guard')
        def inspect_count():
            program = ('BEGIN;\n' + stage +
                       'CREATE TEMP TABLE _essex_identity_claims ON COMMIT DROP AS ' + incoming + ';\n'
                       'CREATE TEMP TABLE _essex_historical_identity_claims ON COMMIT DROP AS ' + historical + ';\n'
                       'SELECT count(*) FROM (' + conflict + ') AS conflicts;\nROLLBACK;\n')
            result = sql(program)
            return int(result.stdout.strip())
        def invalid_count(raw_data):
            valid_json = literal(json.dumps(page(candidate_url, '101')))
            mutated = stage.replace(valid_json, literal(json.dumps(raw_data)))
            if mutated == stage:
                raise AssertionError('incoming fixture mutation did not change staging')
            program = ('BEGIN;\n' + mutated + 'CREATE TEMP TABLE _essex_identity_claims ON COMMIT DROP AS '
                       + incoming + ';\nSELECT count(*) FROM (' + invalid + ') AS invalid_rows;\nROLLBACK;\n')
            return int(sql(program).stdout.strip())
        for label, payload in (('malformed incoming native', page(candidate_url, '101', invalid=True)),
                               ('multiple incoming natives', page(candidate_url, '101', '102'))):
            if invalid_count(payload) != 1:
                raise AssertionError(label + ' must fail the actual incoming predicate')
            print('PASS', label)

        def history(external, url, ids=(), *, deleted=False, invalid=False, brokerage=1):
            value = literal(json.dumps(page(url, *ids, invalid=invalid))) + '::jsonb'
            sql('INSERT INTO credeals.cre_listings VALUES (' + str(brokerage) + ', ' + literal(external) + ', '
                + literal(url) + ', ' + value + ', ' + ("'2026-01-01'" if deleted else 'NULL') + ');')
        other_url = BASE + 'properties/bar/'
        cases = [
            ('same URL, different key', 'properties/bar', candidate_url, (), True, False, 1, 1),
            ('same key, different URL', 'properties/foo', other_url, (), False, False, 1, 1),
            ('same native, other identity', 'properties/bar', other_url, ('101',), False, False, 1, 1),
            ('same identity, different native', 'properties/foo', candidate_url, ('102',), False, False, 1, 1),
            ('multiple historical natives', 'properties/foo', candidate_url, ('101', '102'), False, False, 1, 1),
            ('malformed historical native', 'properties/foo', candidate_url, ('101',), False, True, 1, 1),
            ('unknown historical native', 'properties/foo', candidate_url, (), False, False, 1, 0),
            ('different brokerage', 'properties/bar', candidate_url, ('101',), True, False, 2, 0),
        ]
        lines = [f'PASS PostgreSQL {version}, pinned image, loopback-only dynamic port and resource limits']
        for label, external, url, ids, deleted, invalid, brokerage, expected in cases:
            sql('TRUNCATE credeals.cre_listings;')
            history(external, url, ids, deleted=deleted, invalid=invalid, brokerage=brokerage)
            actual = inspect_count()
            if actual != expected:
                raise AssertionError(f'{label}: expected {expected}, got {actual}')
            lines.append(f'PASS {label}: {actual}')
        sql('TRUNCATE credeals.cre_listings;')
        null_claim = literal(json.dumps({'sourceKey': 'essex-realty', 'essexWordpressPage': None}))
        sql('INSERT INTO credeals.cre_listings VALUES (1, ' + literal('properties/foo') + ', '
            + literal(candidate_url) + ', ' + null_claim + '::jsonb, NULL);')
        if inspect_count() != 0:
            raise AssertionError('null historical claim must remain unknown')
        lines.append('PASS null historical native remains unknown')
        sql('TRUNCATE credeals.cre_listings;')
        history('properties/foo', candidate_url, ('101',))
        if inspect_count() != 0:
            raise AssertionError('initial race precheck should be clear')
        history('properties/bar', candidate_url, ('101',), deleted=True)
        final = sql('BEGIN;\n' + stage + guard + '\nSELECT 1;\nCOMMIT;\n', check=False)
        if final.returncode == 0 or 'Essex identity contradiction in historical rows; refusing mutation' not in final.stderr:
            raise AssertionError('actual generated final guard failed to refuse the introduced race')
        if sql('SELECT count(*) FROM credeals.cre_listings;').stdout.strip() != '2':
            raise AssertionError('guard changed fixture history')
        lines.append('PASS clear precheck then injected history race: final generated guard refused before DML')
        (proof / 'results.txt').write_text('\n'.join(lines) + '\n')
        print('\n'.join(lines))
    finally:
        original = sys.exc_info()[1]
        try:
            cleanup()
        except Exception as error:
            if original is None:
                raise
            original.add_note('fixture cleanup unconfirmed: ' + str(error))
            print('fixture cleanup unconfirmed: ' + str(error), file=sys.stderr)
        else:
            (proof / 'cleanup.txt').write_text('unique labelled disposable container removed; no serving container targeted\n')


if __name__ == '__main__':
    main()
