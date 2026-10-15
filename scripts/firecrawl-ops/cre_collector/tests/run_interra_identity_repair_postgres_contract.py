"""AGENTIC-4268 exact identity repair transaction, isolated network-none PG17 only.

Owns a unique bounded container from the already-cached postgres:17 image.
Never accepts application credentials, publishes ports, or mounts host data.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import cre_ingest as ingest  # noqa: E402
import cre_repair_interra_identity as repair  # noqa: E402
from test_interra_identity_repair import fixture  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof-dir", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.proof_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    owner = uuid.uuid4().hex
    label = "cre4268.owner=" + owner
    logs, checks = [], []
    container = None

    def command(argv, **kwargs):
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=kwargs.pop("timeout", 30),
            **kwargs,
        )

    image = json.loads(
        command(["docker", "image", "inspect", "postgres:17"], check=True).stdout
    )[0]["Id"]
    try:
        result = command(
            [
                "docker",
                "create",
                "--pull",
                "never",
                "--name",
                "cre4268-pg-" + owner,
                "--label",
                label,
                "--network",
                "none",
                "--cpus",
                "1",
                "--memory",
                "512m",
                "--memory-swap",
                "512m",
                "--pids-limit",
                "128",
                "--tmpfs",
                "/var/lib/postgresql/data:rw,size=256m",
                "--tmpfs",
                "/var/run/postgresql:rw,size=16m",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                image,
            ],
            check=True,
        )
        container = result.stdout.strip()
        assert re.fullmatch(r"[0-9a-f]{64}", container)
        command(["docker", "start", container], check=True)
        obj = json.loads(command(["docker", "inspect", container], check=True).stdout)[
            0
        ]
        assert obj["Id"] == container and obj["Image"] == image
        assert obj["Config"]["Labels"]["cre4268.owner"] == owner
        assert (
            obj["HostConfig"]["NetworkMode"] == "none"
            and not obj["NetworkSettings"]["Ports"]
        )
        assert not obj["Mounts"] and obj["HostConfig"]["Memory"] == 512 * 1024**2
        assert (
            obj["HostConfig"]["NanoCpus"] == 1_000_000_000
            and obj["HostConfig"]["PidsLimit"] == 128
        )
        (args.proof_dir / "fixture.json").write_text(json.dumps(obj))
        for _ in range(30):
            if (
                command(
                    ["docker", "exec", container, "pg_isready", "-U", "postgres"]
                ).returncode
                == 0
            ):
                break
            time.sleep(1)
        else:
            raise AssertionError(
                "private PostgreSQL startup did not settle within 30 seconds"
            )

        def sql(text, db="postgres", check=True, timeout=35):
            result = command(
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
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
                ],
                input="SET statement_timeout='25s'; SET lock_timeout='2s';\n" + text,
                timeout=timeout,
            )
            logs.append(
                {
                    "database": db,
                    "sql_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "rc": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
            if check and result.returncode:
                raise AssertionError(result.stderr)
            return result

        directory = HERE.parent.parent / "sql"
        schema = re.sub(
            r"^\\i (\S+)\s*$",
            lambda m: (directory / m[1]).read_text(),
            (directory / "000_run_all.sql").read_text(),
            flags=re.M,
        )
        schema += "\n" + "\n".join(
            [
                "\\set CRE_APPROVE_LISTING_LIFECYCLE 1",
                "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
                "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-4268",
                "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
                (directory / "016_cre_listing_lifecycle.sql").read_text(),
            ]
        )

        db = "cre4268_" + owner[:12]
        sql("CREATE DATABASE " + db + ";")
        sql(schema, db, timeout=50)
        sql(
            "CREATE TABLE credeals.deal_listing_bindings(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),listing_id uuid REFERENCES credeals.cre_listings(id)); CREATE TABLE credeals.search_results(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),source_listing_id uuid REFERENCES credeals.cre_listings(id));",
            db,
        )
        plan, artifact = fixture(args.proof_dir)
        brokerage = plan["before"]["parents"][0]["brokerage_id"]
        sql(
            "UPDATE credeals.cre_brokerages SET id="
            + ingest.sql_lit(brokerage)
            + "::uuid WHERE slug='interra-realty'; INSERT INTO credeals.cre_brokerages(id,name,slug) VALUES("
            + ingest.sql_lit(brokerage)
            + "::uuid,'Synthetic','interra-realty') ON CONFLICT(slug) DO NOTHING;",
            db,
        )
        for row in plan["before"]["parents"]:
            sql(
                "INSERT INTO credeals.cre_listings(id,brokerage_id,external_id,source_url,transaction_type,title,status,raw_data) VALUES("
                + ",".join(
                    ingest.sql_lit(row[k])
                    + ("::uuid" if k in {"id", "brokerage_id"} else "")
                    for k in ("id", "brokerage_id", "external_id", "source_url")
                )
                + ",'sale','Synthetic','active',"
                + ingest.sql_lit(json.dumps(row["raw_data"]))
                + "::jsonb);",
                db,
            )
        for row in plan["before"]["contacts"]:
            sql(
                "INSERT INTO credeals.cre_listing_contacts(id,listing_id,name) VALUES("
                + ingest.sql_lit(row["id"])
                + "::uuid,"
                + ingest.sql_lit(row["listing_id"])
                + "::uuid,'Synthetic');",
                db,
            )
        for row in plan["before"]["images"]:
            sql(
                "INSERT INTO credeals.cre_listing_images(id,listing_id,url) VALUES("
                + ingest.sql_lit(row["id"])
                + "::uuid,"
                + ingest.sql_lit(row["listing_id"])
                + "::uuid,"
                + ingest.sql_lit(row["url"])
                + ");",
                db,
            )
        for row in plan["before"]["source_index"]:
            sql(
                "INSERT INTO credeals.cre_source_index(id,brokerage_id,external_id,source_key,url) VALUES("
                + ingest.sql_lit(row["id"])
                + "::uuid,"
                + ingest.sql_lit(row["brokerage_id"])
                + "::uuid,"
                + ingest.sql_lit(row["external_id"])
                + ",'interra-realty',"
                + ingest.sql_lit(row["url"])
                + ");",
                db,
            )
        sql(
            "INSERT INTO credeals.cre_listings(brokerage_id,external_id,source_url,transaction_type,title,raw_data) SELECT "
            + ingest.sql_lit(brokerage)
            + "::uuid,'listing/unmatched-'||n,'https://interrarealty.com/listing/unmatched-'||n||'/','sale','Untouched', '{}' FROM generate_series(1,874) n;",
            db,
        )
        sql(
            "INSERT INTO credeals.cre_listings(brokerage_id,external_id,source_url,transaction_type,title,raw_data) SELECT "
            + ingest.sql_lit(brokerage)
            + "::uuid,(1000+n)::text,'https://interrarealty.com/listing/synthetic-'||n||'/','sale','New unrelated', '{}' FROM generate_series(22,61) n;",
            db,
        )
        parents = ",".join(
            ingest.sql_lit(row["id"]) + "::uuid" for row in plan["before"]["parents"]
        )
        native = ",".join(
            ingest.sql_lit(row["provider_external_id"]) for row in plan["pairs"]
        )

        def rows(table, condition):
            return json.loads(
                sql(
                    "SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY id),'[]') FROM credeals."
                    + table
                    + " t WHERE "
                    + condition
                    + ";",
                    db,
                ).stdout
            )

        def capture():
            before = plan["before"]
            before["parents"] = rows("cre_listings", "id IN (" + parents + ")")
            before["foreign_key_rows"] = {
                "credeals." + table: rows(table, column + " IN (" + parents + ")")
                for table, column in repair.FK_COLUMNS.items()
            }
            before["contacts"] = before["foreign_key_rows"][
                "credeals.cre_listing_contacts"
            ]
            before["images"] = before["foreign_key_rows"]["credeals.cre_listing_images"]
            before["source_index"] = rows(
                "cre_source_index", "external_id IN (" + native + ")"
            )
            by_id = {row["id"]: row for row in before["parents"]}
            for reference in plan["reference_counts"]:
                reference["parent"] = by_id[reference["id"]]
            plan["plan_hash"] = repair.digest(
                {k: v for k, v in plan.items() if k != "plan_hash"}
            )
            repair.validate_plan(plan, artifact)
            return repair.build_sql(plan, "cayman")

        tables = sql(
            "SELECT tablename FROM pg_tables WHERE schemaname='credeals' ORDER BY tablename;",
            db,
        ).stdout.splitlines()

        def snapshot():
            parts = [
                "SELECT "
                + ingest.sql_lit(name)
                + " name,md5(coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text,'[]')) hash FROM credeals."
                + name
                + " t"
                for name in tables
            ]
            return sql(
                "SELECT jsonb_object_agg(name,hash)::text FROM ("
                + " UNION ALL ".join(parts)
                + ") s;",
                db,
            ).stdout.strip()

        first = plan["pairs"][0]
        old = ingest.sql_lit(first["survivor_id"]) + "::uuid"
        image_sql = ingest.sql_lit(first["image_id"]) + "::uuid"
        tests = [
            (
                "parent_drift",
                "UPDATE credeals.cre_listings SET title='Drift' WHERE id=" + old,
                "UPDATE credeals.cre_listings SET title='Synthetic' WHERE id=" + old,
            ),
            (
                "image_drift",
                "UPDATE credeals.cre_listing_images SET alt_text='Drift' WHERE id="
                + image_sql,
                "UPDATE credeals.cre_listing_images SET alt_text=NULL WHERE id="
                + image_sql,
            ),
            (
                "contact_drift",
                "UPDATE credeals.cre_listing_contacts SET name='Drift' WHERE listing_id="
                + old,
                "UPDATE credeals.cre_listing_contacts SET name='Synthetic' WHERE listing_id="
                + old,
            ),
            (
                "index_drift",
                "UPDATE credeals.cre_source_index SET url='https://example.com/drift' WHERE external_id="
                + ingest.sql_lit(first["provider_external_id"]),
                "UPDATE credeals.cre_source_index SET url="
                + ingest.sql_lit(first["source_url"])
                + " WHERE external_id="
                + ingest.sql_lit(first["provider_external_id"]),
            ),
            (
                "queue_drift",
                "INSERT INTO credeals.cre_enrichment_queue(brokerage_id,external_id,source_key,reason) VALUES("
                + ingest.sql_lit(brokerage)
                + "::uuid,"
                + ingest.sql_lit(first["provider_external_id"])
                + ",'interra-realty','changed')",
                "DELETE FROM credeals.cre_enrichment_queue",
            ),
            (
                "deal_reference_drift",
                "INSERT INTO credeals.deal_listing_bindings(listing_id) VALUES("
                + old
                + ")",
                "DELETE FROM credeals.deal_listing_bindings",
            ),
            (
                "search_reference_drift",
                "INSERT INTO credeals.search_results(source_listing_id) VALUES("
                + old
                + ")",
                "DELETE FROM credeals.search_results",
            ),
            (
                "catalog_drift",
                "CREATE TABLE credeals.extra_reference(listing_id uuid REFERENCES credeals.cre_listings(id))",
                "DROP TABLE credeals.extra_reference",
            ),
        ]
        for case, change, restore in tests:
            generated = capture()
            sql(change + ";", db)
            before_hash = snapshot()
            result = sql(generated, db, check=False)
            assert result.returncode != 0, (case, result.stderr)
            assert "drift" in result.stderr or case == "queue_drift", (
                case,
                result.stderr,
            )
            assert snapshot() == before_hash, "partial mutation: " + case
            checks.append(
                {"case": case, "all_tables_unchanged": len(tables), "refused": True}
            )
            sql(restore + ";", db)
        generated = capture()
        before_hash = snapshot()
        faulted = generated.replace(
            "SELECT jsonb_build_object('plan_hash'",
            "SELECT 1/0;\nSELECT jsonb_build_object('plan_hash'",
        )
        assert sql(faulted, db, check=False).returncode != 0
        assert snapshot() == before_hash, "post-mutation failure did not rollback"
        checks.append(
            {
                "case": "post_mutation_fault",
                "all_tables_unchanged": len(tables),
                "refused": True,
            }
        )
        result = sql(generated, db)
        proof = json.loads(result.stdout.splitlines()[-1])
        assert proof["survivors"] == proof["superseded"] == proof["images_moved"] == 22
        checks.append({"case": "exact_22_pair_success", **proof})
        after_hash = snapshot()
        assert sql(generated, db, check=False).returncode != 0
        assert snapshot() == after_hash, "repeat changed committed repair"
        checks.append(
            {
                "case": "repeat_refused",
                "all_tables_unchanged": len(tables),
                "refused": True,
            }
        )
        sql("DROP DATABASE " + db + ";")
    finally:
        (args.proof_dir / "transcript.json").write_text(json.dumps(logs, indent=2))
        (args.proof_dir / "checks.json").write_text(json.dumps(checks, indent=2))
        if container:
            obj = json.loads(
                command(["docker", "inspect", container], check=True).stdout
            )[0]
            assert (
                obj["Config"]["Labels"]["cre4268.owner"] == owner
                and obj["Image"] == image
            )
            command(
                ["docker", "stop", "--timeout", "10", container], check=True, timeout=20
            )
            command(["docker", "rm", container], check=True)
            remaining = command(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    "label=" + label,
                    "--format",
                    "{{.ID}}",
                ],
                check=True,
            ).stdout.strip()
            assert not remaining
            (args.proof_dir / "cleanup.json").write_text(
                json.dumps(
                    {"id": container, "owner": owner, "removed_and_absent": True}
                )
            )
    print(json.dumps({"checks": checks, "container_removed_and_absent": True}))


if __name__ == "__main__":
    main()
