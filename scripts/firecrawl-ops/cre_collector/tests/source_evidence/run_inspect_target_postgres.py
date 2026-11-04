"""Exercise target inspection against an owned disposable PostgreSQL 17 cluster."""
import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parent / 'observation_store'))
from cre_ingest import database_target_fingerprint_from_url
from owned_observation_fixture_cluster import OwnedCluster, tool
from source_evidence import cli, target_inspection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-disposable-fixture', action='store_true', required=True)
    parser.parse_args()
    initdb, pg_ctl, psql = (tool(name) for name in ('initdb', 'pg_ctl', 'psql'))
    with OwnedCluster(initdb, pg_ctl, port=55457) as cluster:
        def sql(statement, user=None):
            cmd = [psql, '-X', '-qAt', '-h', str(cluster.root), '-p', str(cluster.port),
                   '-d', 'postgres', '-v', 'ON_ERROR_STOP=1']
            if user:
                cmd += ['-U', user]
            result = subprocess.run(cmd, input=statement, capture_output=True, text=True,
                                    env=cluster.env, timeout=45, check=False)
            assert result.returncode == 0, result.stderr
            return result.stdout.strip()
        sql('CREATE ROLE fixture_login LOGIN NOINHERIT; CREATE SCHEMA fixture; '
            'GRANT USAGE ON SCHEMA fixture TO PUBLIC; '
            'CREATE TABLE fixture.sentinel(value text); INSERT INTO fixture.sentinel VALUES (\'private sentinel\'); '
            'GRANT UPDATE(value) ON fixture.sentinel TO PUBLIC;')
        for n in range(101):
            sql(f"CREATE FUNCTION fixture.f{n:03d}() RETURNS integer LANGUAGE sql SECURITY DEFINER AS 'SELECT 1';")
        result = json.loads(sql(target_inspection.sql(), user='fixture_login'))
        assert result['selected_role_exists'] and not result['writer_role_exists']
        assert result['hazard_count'] == 102 and result['hazards_truncated'] and len(result['hazards']) == 100
        assert result['other_databases_inspected'] is False
        assert all(row['role'] == 'fixture_login' for row in result['hazards'])
        assert sql('SELECT value FROM fixture.sentinel;') == 'private sentinel'
        sql('CREATE ROLE cre_source_evidence_writer NOLOGIN NOINHERIT;')
        selected = json.loads(sql(target_inspection.sql('fixture_login')))
        assert selected['writer_role_exists'] and selected['selected_role_exists']
        assert selected['hazard_count'] == 204
        assert {row['name'] for row in selected['roles']} >= {'fixture_login', 'cre_source_evidence_writer'}
        absent = json.loads(sql(target_inspection.sql('missing_login')))
        assert not absent['selected_role_exists'] and absent['connectable_database_count'] == 0
        try:
            target_inspection.sql("fixture_login'; DROP SCHEMA fixture;--")
        except ValueError:
            pass
        else:
            raise AssertionError('injection accepted')
        assert sql('SELECT value FROM fixture.sentinel;') == 'private sentinel'
        url = f'postgresql://fixture_login@{quote(str(cluster.root), safe="")}:{cluster.port}/postgres'
        env = cluster.root / 'inspection.env'
        env.write_text('POSTGRES_URL=' + url + '\n')
        env.chmod(0o600)
        os.environ['PSQL_BIN'] = psql
        out = cluster.root / 'inspection.json'
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            cli.main(['inspect-target', '--env-file', str(env), '--expected-db-target-sha256',
                      database_target_fingerprint_from_url(url)['value'], '--out', str(out)])
        result = json.loads(out.read_text())
        assert result['hazard_count'] == 204 and out.stat().st_mode & 0o777 == 0o600
        assert 'fixture' not in printed.getvalue() and 'private sentinel' not in out.read_text()
        assert sql('SELECT value FROM fixture.sentinel;') == 'private sentinel'
        print('Disposable PG17 inspection: missing roles, 204 hazards, bounded output, injection and sentinel passed')


if __name__ == '__main__':
    main()
