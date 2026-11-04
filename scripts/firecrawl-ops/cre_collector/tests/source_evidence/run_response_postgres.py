"""Round-trip JSON response observations through an owned PostgreSQL 17 cluster."""

import argparse
import copy
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
COLLECTOR = HERE.parents[1]
sys.path.insert(0, str(COLLECTOR))
sys.path.insert(0, str(HERE.parent / "observation_store"))

from cre_ingest import database_target_fingerprint_from_url
from owned_observation_fixture_cluster import OwnedCluster, tool
from source_evidence import acquisition, cli, events


def decimal_json(raw):
    return json.loads(raw, parse_int=Decimal, parse_float=Decimal)


def synthetic_observation():
    rows = []
    for i in range(64):
        # Deliberately omit listing IDs and addresses. Duplicate values, null,
        # unknown fields and precision are native response data, not listings.
        rows.append(f'{{"group":{i % 7},"unknown":{{"parts":[null,{i % 3},{i % 3}],'
                    '"value":1.0000000000000001,"empty":""}}')
    raw = ('{"context":{"unknown":null,"decimal":123456789.123456789},'
           '"payload":{"rows":[' + ','.join(rows) + '],"duplicate":[1,1,null]}}').encode()
    receipt = {"sourceKey": "nai-global", "requestUrl": "https://example.test/native",
               "finalUrl": "https://example.test/native", "method": "GET", "status": 200,
               "startedAt": "2026-10-28T12:00:00Z", "finishedAt": "2026-10-28T12:00:01Z",
               "bodySha256": events.sha256(raw), "bodyBytes": len(raw)}
    return acquisition.convert(raw, receipt=receipt, array_pointer="/payload/rows"), raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-disposable-fixture", action="store_true", required=True)
    parser.add_argument("--saved-observation", type=Path)
    parser.add_argument("--saved-body", type=Path)
    args = parser.parse_args()
    if args.saved_body and not args.saved_observation:
        parser.error("--saved-body requires --saved-observation")
    if args.saved_observation:
        observation = json.loads(args.saved_observation.read_bytes())
        raw = args.saved_body.read_bytes() if args.saved_body else None
    else:
        observation, raw = synthetic_observation()
    acquisition.validate(observation)
    expected = decimal_json(acquisition.reconstruct_json(observation))
    if raw is not None:
        assert events.sha256(raw) == observation["receipt"]["bodySha256"]
        assert len(raw) == observation["receipt"]["bodyBytes"]
        assert decimal_json(raw) == expected
    source = observation["receipt"]["sourceKey"]
    assert len(observation["records"]) >= 60
    initdb, pg_ctl, psql = (tool(name) for name in ("initdb", "pg_ctl", "psql"))
    version = subprocess.check_output([initdb, "--version"], text=True)
    if " 17." not in version:
        raise RuntimeError("PostgreSQL 17 is required")
    database = "cre_response_" + uuid.uuid4().hex[:12]
    with OwnedCluster(initdb, pg_ctl, port=55447) as cluster:
        def sql(statement, *, target=database, user=None, success=True):
            result = subprocess.run(
                [psql, "-X", "-qAt", "-h", str(cluster.root), "-p", str(cluster.port),
                 "-d", target, "-v", "ON_ERROR_STOP=1"] + (["-U", user] if user else []),
                input=statement, capture_output=True, text=True, timeout=45,
                env=cluster.env, check=False,
            )
            if (result.returncode == 0) != success:
                raise AssertionError("disposable SQL returned an unexpected status")
            return result.stdout.strip()

        sql("CREATE ROLE evidence_executor LOGIN CREATEROLE CREATEDB BYPASSRLS;", target="postgres")
        sql(f"CREATE DATABASE {database} OWNER evidence_executor;", target="postgres")
        sql("CREATE ROLE cre_source_evidence_owner NOLOGIN NOINHERIT; "
            "CREATE ROLE cre_source_evidence_writer NOLOGIN NOINHERIT; "
            "CREATE ROLE cre_source_evidence_reader NOLOGIN NOINHERIT;", user="evidence_executor")
        sql("CREATE SCHEMA credeals; CREATE TABLE credeals.sentinel(id integer, status text); "
            "INSERT INTO credeals.sentinel VALUES (1,'active');")
        migration = (HERE.parents[2] / "sql/019_cre_source_evidence.sql").read_text()
        sql("\\set CRE_SOURCE_EVIDENCE_APPROVE 1\n\\set CRE_SOURCE_EVIDENCE_OPERATOR cayman\n"
            "\\set CRE_SOURCE_EVIDENCE_APPROVAL_REF AGENTIC-2895\n"
            "\\set CRE_SOURCE_EVIDENCE_CONFIRM 'APPLY 019_cre_source_evidence'\n" + migration,
            user="evidence_executor")
        sql("CREATE ROLE evidence_response LOGIN NOINHERIT; "
            "GRANT cre_source_evidence_writer TO evidence_response WITH INHERIT FALSE, SET TRUE;")
        url = f"postgresql://evidence_response@{quote(str(cluster.root), safe='')}:{cluster.port}/{database}"
        target = database_target_fingerprint_from_url(url)["value"]
        env_file = cluster.root / "writer.env"
        env_file.write_text("POSTGRES_URL=" + url + "\n")
        env_file.chmod(0o600)
        env = os.environ.copy()
        env["PSQL_BIN"] = psql

        def run_cli(arguments, success=True):
            result = subprocess.run([sys.executable, "-m", "source_evidence.cli", *arguments], cwd=COLLECTOR,
                                    env=env, capture_output=True, text=True, timeout=90, check=False)
            if (result.returncode == 0) != success:
                raise AssertionError(f"CLI unexpected exit {result.returncode}")
            return result

        artifact_path = cluster.root / "observation.json"
        artifact_path.write_bytes(events.encoded(observation))
        artifact_path.chmod(0o600)
        plan_path = cluster.root / "plan.json"
        run_cli(["plan", "--artifact", str(artifact_path), "--source-key", source, "--out", str(plan_path)])
        plan = json.loads(plan_path.read_text())
        events.validate_plan(plan)
        admission = {"schema_version": "cre-source-evidence-admission/v1", "plan_sha256": plan["plan_sha256"],
                     "purpose": "private_unverified_evidence", "data_scope": "public_listing_source",
                     "content_policy": "reviewed_for_private_retention", "source_keys": [source]}
        admission_path = cluster.root / "admission.json"
        admission_path.write_bytes(events.encoded(admission))
        admission_path.chmod(0o600)
        arguments = ["append", "--plan", str(plan_path), "--admission", str(admission_path),
                     "--env-file", str(env_file), "--expected-db-target-sha256", target, "--apply"]
        run_cli(arguments)
        run_cli(arguments)
        assert int(sql("SELECT count(*) FROM cre_source_evidence.events;")) == len(plan["events"])
        columns = ",".join(cli.FIELDS)
        lines = sql(f"SELECT row_to_json(r) FROM (SELECT {columns} FROM cre_source_evidence.events ORDER BY item_no) r;")
        stored = [json.loads(line) for line in lines.splitlines()]
        stored_decimal = [decimal_json(line) for line in lines.splitlines()]
        expected_decimal = decimal_json(events.encoded(plan["events"]))
        assert len(stored) == len(plan["events"])
        for actual, wanted in zip(stored_decimal, expected_decimal, strict=True):
            assert datetime.fromisoformat(actual.pop("observed_at").replace("Z", "+00:00")) == datetime.fromisoformat(wanted.pop("observed_at").replace("Z", "+00:00"))
            assert actual == wanted
        header = stored[0]["facts"][0]["value"]
        recovered = {**header, "records": [row["facts"][0]["value"] for row in stored[1:]]}
        acquisition.validate(recovered)
        assert decimal_json(acquisition.reconstruct_json(recovered)) == expected
        assert recovered == observation
        # Exercise database immutability directly with a real valid plan's SQL,
        # changing one fact while keeping the same capture and item identity.
        first_facts = plan["events"][1]["facts"]
        changed = copy.deepcopy(first_facts)
        changed[0]["value"] = {"divergent_replay_fixture": True}
        original_sql = cli.append_sql(plan)
        before = cli.sql_text(events.encoded(first_facts).decode())
        after = cli.sql_text(events.encoded(changed).decode())
        assert before in original_sql
        divergent = original_sql.replace(before, after, 1)
        sql(divergent, user="evidence_response", success=False)
        assert int(sql("SELECT count(*) FROM cre_source_evidence.events;")) == len(plan["events"])
        assert sql("SELECT id,status FROM credeals.sentinel;") == "1|active"
        print(f"PASS: {len(observation['records'])} records; every event field and Decimal response round-trip; "
              "exact replay, divergent replay rejection and canonical sentinel")


if __name__ == "__main__":
    main()
