"""AGENTIC-4206: actual generated SQL in a root-provisioned, isolated fixture.

Never creates/removes Docker resources or accepts an application database URL.
The required exact ID, image and owner label must describe network-none PG17.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE)]
import cre_enrich as worker  # noqa: E402
import cre_ingest as ingest  # noqa: E402
from test_matthews_targeted_refresh import fixture_batch  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--proof-dir", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{64}", args.container_id):
        parser.error("exact fixture ID required")
    args.proof_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    inspected = subprocess.run(
        ["docker", "inspect", args.container_id],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    obj = json.loads(inspected.stdout)[0]
    assert obj["Id"] == args.container_id and obj["Image"] == args.image_id
    assert obj["Config"]["Labels"].get("cre4206.owner") == args.owner
    assert (
        obj["HostConfig"]["NetworkMode"] == "none"
        and not obj["NetworkSettings"]["Ports"]
    )
    assert not obj["Mounts"] and obj["HostConfig"]["Memory"] == 512 * 1024**2
    assert (
        obj["HostConfig"]["NanoCpus"] == 1_000_000_000
        and obj["HostConfig"]["PidsLimit"] == 128
    )
    database = "cre4206_author_" + uuid.uuid4().hex[:12]
    logs, checks = [], []

    def sql(text, *, db=database, check=True, timeout=25):
        command = [
            "docker",
            "exec",
            "-i",
            args.container_id,
            "psql",
            "-X",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            db,
            "-f",
            "-",
        ]
        result = subprocess.run(
            command, input=text, capture_output=True, text=True, timeout=timeout
        )
        logs.append(
            {
                "command": command,
                "sql_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "exit": result.returncode,
                "stderr": result.stderr,
                "stdout": result.stdout,
            }
        )
        if check and result.returncode:
            raise AssertionError(result.stderr)
        return result

    try:
        sql("CREATE DATABASE " + database + ";", db="postgres")
        directory = HERE.parent.parent / "sql"
        master = (directory / "000_run_all.sql").read_text()
        master = re.sub(
            r"^\\i (\S+)\s*$",
            lambda m: (directory / m[1]).read_text(),
            master,
            flags=re.M,
        )
        schema = (
            master
            + "\n"
            + "\n".join(
                [
                    "\\set CRE_APPROVE_LISTING_LIFECYCLE 1",
                    "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
                    "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-4206",
                    "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
                    (directory / "016_cre_listing_lifecycle.sql").read_text(),
                ]
            )
        )
        sql(schema, timeout=45)
        claims, artifact = fixture_batch()
        rows = [
            ingest.to_row(row, {}, row["detailObservedAt"])
            for row in artifact["listings"]
        ]
        targets = ingest.matthews_targeted_sql_context(artifact, claims)
        job = [
            {
                "slug": "matthews",
                "discovered": 2,
                "saved": 2,
                "errors": 0,
                "notes": "synthetic",
            }
        ]
        seed_rows = [
            {**row, "scraped_at": claims["items"][0]["claimedAt"], "title": "Before"}
            for row in rows
        ]
        seed = ingest.build_sql(seed_rows, job, artifact["runMeta"]["startedAt"], set())
        sql(seed)
        values = ",".join(
            "("
            + ",".join(
                ingest.sql_lit(item[k])
                for k in ("claimId", "externalId", "url", "claimedAt")
            )
            + ")"
            for item in claims["items"]
        )
        sql(f"""INSERT INTO credeals.cre_enrichment_queue(id,brokerage_id,source_key,external_id,url,reason,claimed_at)
 SELECT v.id::uuid,b.id,'matthews',v.external_id,v.url,'changed',v.claimed_at::timestamptz
 FROM (VALUES {values}) v(id,external_id,url,claimed_at) JOIN credeals.cre_brokerages b ON b.slug='matthews';""")
        targeted_sql = ingest.build_sql(
            rows,
            job,
            artifact["runMeta"]["startedAt"],
            set(),
            matthews_targeted_targets=targets,
        )
        tables = [
            x
            for x in sql(
                "SELECT tablename FROM pg_tables WHERE schemaname='credeals' ORDER BY tablename;"
            ).stdout.splitlines()
        ]

        def snapshot():
            parts = [
                "SELECT "
                + ingest.sql_lit(name)
                + " AS name,md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text,'[]')) AS hash FROM credeals."
                + name
                + " t"
                for name in tables
            ]
            return sql(
                "SELECT jsonb_object_agg(name,hash)::text FROM ("
                + " UNION ALL ".join(parts)
                + ") s;"
            ).stdout.strip()

        def rejection(name, change, restore):
            sql(change)
            before = snapshot()
            result = sql(targeted_sql, check=False)
            assert result.returncode != 0, name
            assert snapshot() == before, "partial mutation: " + name
            checks.append({"case": name, "rollback": True})
            sql(restore)

        for name, field, value, restore in [
            ("inactive", "status", "'sold'", "'active'"),
            ("deleted", "deleted_at", "now()", "NULL"),
            (
                "newer_detail",
                "scraped_at",
                "now()+interval '1 minute'",
                ingest.sql_lit(seed_rows[0]["scraped_at"]) + "::timestamptz",
            ),
            (
                "nonfinite_detail",
                "scraped_at",
                "'infinity'::timestamptz",
                ingest.sql_lit(seed_rows[0]["scraped_at"]) + "::timestamptz",
            ),
        ]:
            rejection(
                name,
                f"UPDATE credeals.cre_listings SET {field}={value};",
                f"UPDATE credeals.cre_listings SET {field}={restore};",
            )
        for name, field, value, restore in [
            ("not_present", "observation_present", "false", "true"),
            ("retired_index", "soft_deleted", "true", "false"),
            (
                "newer_inventory",
                "last_enumerated_at",
                "now()+interval '1 minute'",
                ingest.sql_lit(seed_rows[0]["scraped_at"]) + "::timestamptz",
            ),
        ]:
            rejection(
                name,
                f"UPDATE credeals.cre_source_index SET {field}={value};",
                f"UPDATE credeals.cre_source_index SET {field}={restore};",
            )
        before = snapshot()
        assert (
            sql(
                "UPDATE credeals.cre_source_index SET observation_present=NULL;",
                check=False,
            ).returncode
            != 0
        )
        assert snapshot() == before
        checks.append(
            {"case": "real_schema_rejects_unknown_presence", "rollback": True}
        )
        rejection(
            "reclaimed_claim",
            "UPDATE credeals.cre_enrichment_queue SET claimed_at=now();",
            "UPDATE credeals.cre_enrichment_queue q SET claimed_at=v.t::timestamptz FROM (VALUES "
            + ",".join(
                "("
                + ingest.sql_lit(i["claimId"])
                + ","
                + ingest.sql_lit(i["claimedAt"])
                + ")"
                for i in claims["items"]
            )
            + ") v(id,t) WHERE q.id=v.id::uuid;",
        )
        sql("CREATE TABLE public.saved_index AS TABLE credeals.cre_source_index;")
        rejection(
            "missing_source_index",
            "DELETE FROM credeals.cre_source_index;",
            "INSERT INTO credeals.cre_source_index SELECT * FROM public.saved_index;",
        )
        # A missing canonical target must fail before INSERT, not be resurrected.
        sql("DELETE FROM credeals.cre_listings;")
        before = snapshot()
        assert sql(targeted_sql, check=False).returncode != 0
        assert snapshot() == before
        checks.append({"case": "missing_listing_cannot_insert", "rollback": True})
        sql(seed)  # Reset the synthetic fixture; no production data is involved.
        before_state = sql(
            "SELECT jsonb_agg(jsonb_build_array(l.id,l.status,l.deleted_at,si.presence_generation,si.presence_changed_at,si.first_seen) ORDER BY l.external_id)::text FROM credeals.cre_listings l JOIN credeals.cre_source_index si USING(brokerage_id,external_id);"
        ).stdout
        sql("UPDATE credeals.cre_listings SET transaction_type='sale_or_lease';")
        sql(targeted_sql)
        assert (
            sql(worker.build_targeted_readback_sql(artifact, claims)).stdout.strip()
            == "2"
        )
        after_state = sql(
            "SELECT jsonb_agg(jsonb_build_array(l.id,l.status,l.deleted_at,si.presence_generation,si.presence_changed_at,si.first_seen) ORDER BY l.external_id)::text FROM credeals.cre_listings l JOIN credeals.cre_source_index si USING(brokerage_id,external_id);"
        ).stdout
        assert before_state == after_state
        assert (
            sql(
                "SELECT count(*) FROM credeals.cre_listings WHERE transaction_type='sale_or_lease';"
            ).stdout.strip()
            == "2"
        )
        checks.append(
            {
                "case": "sale_lease_dual_admitted_clocks_lifecycle_preserved",
                "pass": True,
            }
        )
        sql(
            "UPDATE credeals.cre_listings SET transaction_type=CASE WHEN raw_data->>'transactionMode'='sale' THEN 'lease' ELSE 'sale' END;"
        )
        sql(targeted_sql)
        assert (
            sql(
                "SELECT count(*) FROM credeals.cre_listings WHERE transaction_type='sale_or_lease';"
            ).stdout.strip()
            == "2"
        )
        assert (
            sql(worker.build_targeted_readback_sql(artifact, claims)).stdout.strip()
            == "2"
        )
        checks.append(
            {
                "case": "opposite_prior_widens_preserving_observed_single_tenure",
                "pass": True,
            }
        )
        complete = worker.build_targeted_queue_sql(
            claims["items"], "complete", artifact=artifact, claims=claims
        )
        for name, change, restore in [
            (
                "wrong_stored_single_tenure",
                "UPDATE credeals.cre_listings SET transaction_type=CASE WHEN raw_data->>'transactionMode'='sale' THEN 'lease' ELSE 'sale' END;",
                "UPDATE credeals.cre_listings SET transaction_type='sale_or_lease';",
            ),
            (
                "wrong_raw_tenure",
                "UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data,'{transactionMode}','\"wrong\"');",
                targeted_sql,
            ),
            (
                "wrong_index_source",
                "UPDATE credeals.cre_source_index SET source_key='other';",
                "UPDATE credeals.cre_source_index SET source_key='matthews';",
            ),
            (
                "wrong_index_url",
                "UPDATE credeals.cre_source_index SET url=url||'-wrong';",
                "UPDATE credeals.cre_source_index SET url=left(url,length(url)-6);",
            ),
        ]:
            sql(change)
            before = snapshot()
            assert (
                sql(worker.build_targeted_readback_sql(artifact, claims)).stdout.strip()
                == "0"
            )
            assert sql(complete, check=False).returncode != 0
            assert snapshot() == before
            checks.append({"case": name + "_refuses_completion", "rollback": True})
            sql(restore)
        sql("UPDATE credeals.cre_listings SET status='sold';")
        before = snapshot()
        assert sql(complete, check=False).returncode != 0
        assert snapshot() == before
        checks.append(
            {"case": "retirement_after_readback_refuses_completion", "rollback": True}
        )
        sql("UPDATE credeals.cre_listings SET status='active';")
        sql(complete)
        assert (
            sql("SELECT count(*) FROM credeals.cre_enrichment_queue;").stdout.strip()
            == "0"
        )
        checks.append(
            {"case": "exact_current_proof_completes_owned_claims", "pass": True}
        )
        print(
            json.dumps({"database": database, "checks": len(checks), "all_pass": True})
        )
    finally:
        (args.proof_dir / "postgres-transcript.json").write_text(
            json.dumps(logs, indent=2)
        )
        (args.proof_dir / "postgres-checks.json").write_text(
            json.dumps(checks, indent=2)
        )
        (args.proof_dir / "fixture.json").write_text(
            json.dumps(
                {
                    "container": args.container_id,
                    "image": args.image_id,
                    "owner": args.owner,
                    "database": database,
                }
            )
        )


if __name__ == "__main__":
    main()
