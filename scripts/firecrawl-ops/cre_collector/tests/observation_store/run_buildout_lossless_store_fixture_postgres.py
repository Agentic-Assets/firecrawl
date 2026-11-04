"""Disposable synthetic PostgreSQL fixture only, never accepts database URLs."""

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[2])]

import buildout_lossless_store_fixture as p
from owned_observation_fixture_cluster import OwnedCluster, tool
from test_buildout_lossless_store_fixture import raw_fixture

import buildout_lossless_cache_evidence as lossless


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-disposable-fixture", action="store_true")
    if not parser.parse_args().execute_disposable_fixture:
        parser.error("explicit --execute-disposable-fixture is required")
    initdb, pg_ctl, psql = (tool(name) for name in ("initdb", "pg_ctl", "psql"))
    raw = raw_fixture()
    evidence, _ = lossless.build(raw)
    plan = p.plan(original=raw, evidence=evidence, capture_id="synthetic")
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
            raw = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
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
        tampered = dict(plan, p_observations=[])
        assert sql(invoke(tampered), good=False).returncode != 0
        saved = json.loads(sql("SELECT observations::text FROM cre_buildout_observation.capture_pages;").stdout)
        assert saved == plan["p_observations"] and len(saved) == 85
        assert sql("SELECT status FROM credeals.listings;").stdout.strip() == "active"
        assert sql("SELECT status FROM credeals.source_index;").stdout.strip() == "present"
        ambiguous_raw = b'{"syntheticFixture":true,"data":{"inventory":[],"inventory":[{"id":null}]}}'
        ambiguous_evidence, _ = lossless.build(ambiguous_raw)
        ambiguous = p.plan(original=ambiguous_raw, evidence=ambiguous_evidence, capture_id="ambiguous")
        assert sql(invoke(ambiguous)).stdout.strip() == "inserted"
        ambiguous_saved = json.loads(sql("SELECT observations::text FROM cre_buildout_observation.capture_pages WHERE capture_id='ambiguous';").stdout)
        assert len(ambiguous_saved) == 1 and ambiguous_saved[0]["inventoryResolution"] == "ambiguous_or_missing"
        assert ambiguous_saved == ambiguous["p_observations"]
        assert original.read_bytes() == raw
        assert "p_original_bytes" not in plan and b"DUMMY_SECRET_SENTINEL" not in plan["p_artifact_bytes"]
        row = saved[0]["evidence"]
        assert [entry["value"]["token"] for entry in row["entries"] if entry.get("key") == "size"] == ["1.00e+02", "-0"]
        print("PASS: 84 duplicate-key records, exact replay, divergence and artifact tampering refusal, unchanged canonical sentinels")


if __name__ == "__main__":
    run()
