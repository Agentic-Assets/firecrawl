"""Disposable synthetic PostgreSQL fixture only, never accepts database URLs."""

import argparse
import subprocess
import sys
import uuid
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[2])]

import buildout_observation_fixture_planner as p
from owned_observation_fixture_cluster import OwnedCluster, tool
from test_buildout_observation_fixture_planner import bundle


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-disposable-fixture", action="store_true")
    if not parser.parse_args().execute_disposable_fixture:
        parser.error("explicit --execute-disposable-fixture is required")
    initdb, pg_ctl, psql = (tool(name) for name in ("initdb", "pg_ctl", "psql"))
    raw, artifact, seal, scope = bundle()
    plan = p.plan(original=raw, artifact=artifact, seal=seal, scope=scope)
    migration = (
        Path(__file__).parent / "fixtures/migration.sql"
    ).read_text()
    name = "cre_store_fixture_" + uuid.uuid4().hex[:12]
    with OwnedCluster(initdb, pg_ctl, port=55447) as cluster:
        d = str(cluster.root)
        original = cluster.root / "synthetic-original.json"
        original.write_bytes(raw)
        original.chmod(0o600)

        def sql(text, target=name, good=True):
            result = subprocess.run(
                [
                    psql,
                    "-X",
                    "-qAt",
                    "-h",
                    d,
                    "-p",
                    "55447",
                    "-d",
                    target,
                    "-v",
                    "ON_ERROR_STOP=1",
                ],
                input=text,
                text=True,
                capture_output=True,
                check=False,
                timeout=45,
                env=cluster.env,
            )
            if good and result.returncode:
                raise RuntimeError("fixture SQL failed")
            return result

        def json_expr(value):
            raw = p.manifest.artifact_builder.render(value).encode("ascii")
            return "convert_from(decode('" + raw.hex() + "','hex'),'UTF8')::jsonb"

        def invoke(item):
            args = [
                "'" + item["p_capture_id"] + "'",
                str(item["p_capture_page"]),
                json_expr(item["p_assertions"]),
                json_expr(item["p_provenance"]),
                "'" + item["p_original_sha256"] + "'",
                str(item["p_original_bytes_length"]),
                "decode('" + item["p_artifact_bytes"].hex() + "','hex')",
                "'" + item["p_artifact_sha256"] + "'",
                json_expr(item["p_observations"]),
            ]
            return (
                "SET ROLE cre_buildout_observation_writer; SELECT cre_buildout_observation.append_page("
                + ",".join(args)
                + ");"
            )

        sql("CREATE DATABASE " + name + ";", target="postgres")
        sql(
            "CREATE ROLE cre_buildout_observation_writer NOLOGIN; CREATE ROLE cre_buildout_observation_owner NOLOGIN;",
            target="postgres",
        )
        sql(
            "SET cre_observation_store.approved_draft='fixture-only';\n" + migration
        )
        sql(
            "CREATE SCHEMA credeals; CREATE TABLE credeals.listings(id int, status text); INSERT INTO credeals.listings VALUES(1,'active'); CREATE TABLE credeals.source_index(id int, status text); INSERT INTO credeals.source_index VALUES(1,'present');"
        )
        assert sql(invoke(plan)).stdout.strip() == "inserted"
        assert sql(invoke(plan)).stdout.strip() == "unchanged"
        bad = dict(plan, p_original_sha256="0" * 64)
        assert sql(invoke(bad), good=False).returncode != 0
        assert (
            sql(
                "SELECT count(*),sum(jsonb_array_length(observations)) FROM cre_buildout_observation.capture_pages;"
            ).stdout.strip()
            == "1|85"
        )
        assert (
            sql("SELECT status FROM credeals.listings;").stdout.strip() == "active"
        )
        assert (
            sql("SELECT status FROM credeals.source_index;").stdout.strip()
            == "present"
        )
        assert (
            sql(
                "SELECT observations->0->'sourceFields'->'unknownFacts'->'floorplate'->>'arbitraryLabel' FROM cre_buildout_observation.capture_pages;"
            ).stdout.strip()
            == "fixture data"
        )
        assert (
            sql(
                "SELECT observations->0->'indexAttributes'->0->'literal'->>1, observations->0->'indexAttributes'->1->'literal'->>1, observations->0->'indexAttributes'->0->>'sourceOrdinal', observations->0->'indexAttributes'->1->>'sourceOrdinal' FROM cre_buildout_observation.capture_pages;"
            ).stdout.strip()
            == "6.25%|6.75%|0|1"
        )
        assert (
            sql(
                "SELECT observations->0->'sourceFields'->'unknownFacts'->'floorplate'->>'highPrecision' FROM cre_buildout_observation.capture_pages;"
            ).stdout.strip()
            == "12345678901234567890.1234567890123456789"
        )
        assert original.read_bytes() == raw
        assert (
            "p_original_bytes" not in plan
            and b"fixture secret" not in plan["p_artifact_bytes"]
        )
        print(
            "PASS: 85 full quarantined records; arbitrary nested fields, high-precision numeric and conflicting literals preserved; exact retry unchanged; divergent refused; listing and source index sentinels and synthetic original unchanged"
        )


if __name__ == "__main__":
    run()
