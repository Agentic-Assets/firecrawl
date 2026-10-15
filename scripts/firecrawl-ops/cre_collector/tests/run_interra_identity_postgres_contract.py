"""AGENTIC-4267 generated ingestion transaction, isolated network-none PG17 only.

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof-dir", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.proof_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    owner = uuid.uuid4().hex
    label = "cre4267.owner=" + owner
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
                "cre4267-pg-" + owner,
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
        assert obj["Config"]["Labels"]["cre4267.owner"] == owner
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
                "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-4267",
                "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
                (directory / "016_cre_listing_lifecycle.sql").read_text(),
            ]
        )

        def generated(source, external, url, title, additional_external=None):
            row = ingest.to_row(
                {
                    "sourceKey": source,
                    "id": external,
                    "url": url,
                    "canonicalUrl": url,
                    "name": title,
                    "transactionType": "Sale",
                    "contactsDetailed": [{"name": "Synthetic Contact"}],
                    "brochures": [{"url": "https://example.com/synthetic.pdf"}],
                },
                {},
                "2026-10-15T00:00:00Z",
            )
            rows = [row]
            if additional_external is not None:
                rows.append(
                    {
                        **row,
                        "external_id": additional_external,
                        "raw_data": {**row["raw_data"], "id": additional_external},
                    }
                )
            job = [
                {
                    "slug": row["slug"],
                    "discovered": len(rows),
                    "saved": len(rows),
                    "errors": 0,
                    "notes": "synthetic",
                }
            ]
            return ingest.build_sql(rows, job, "2026-10-15T00:00:00Z", set())

        for index, case in enumerate(
            [
                "legacy_collision",
                "null_legacy_id",
                "same_id",
                "new_url",
                "other_source",
                "other_brokerage",
                "deleted_legacy",
                "same_batch_collision",
                "same_batch_other_source",
            ]
        ):
            db = "cre4267_" + owner[:10] + "_" + str(index)
            sql("CREATE DATABASE " + db + ";")
            sql(schema, db, timeout=50)
            source = (
                "hanley"
                if case in {"other_source", "same_batch_other_source"}
                else "interra-realty"
            )
            seed_source = "hanley" if case == "other_brokerage" else source
            url = "https://interrarealty.com/listing/synthetic/"
            seed_id = "12345" if case == "same_id" else "listing/synthetic"
            if not case.startswith("same_batch_"):
                sql(generated(seed_source, seed_id, url, "Before"), db)
            if case == "null_legacy_id":
                sql("UPDATE credeals.cre_listings SET external_id=NULL;", db)
            if case == "deleted_legacy":
                sql("UPDATE credeals.cre_listings SET deleted_at=now();", db)
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

            before = snapshot()
            incoming_url = url + "new/" if case == "new_url" else url
            incoming = generated(
                source,
                "12345",
                incoming_url,
                "After",
                "67890" if case.startswith("same_batch_") else None,
            )
            result = sql(incoming, db, check=False)
            if case in {"legacy_collision", "null_legacy_id", "same_batch_collision"}:
                assert (
                    result.returncode != 0
                    and (
                        "Interra incoming inventory contains conflicting identities"
                        if case == "same_batch_collision"
                        else "Interra identity repair required"
                    )
                    in result.stderr
                ), case
                assert snapshot() == before, "partial table mutation: " + case
                checks.append(
                    {"case": case, "all_tables_unchanged": len(tables), "refused": True}
                )
            else:
                assert result.returncode == 0, result.stderr
                observed = json.loads(
                    sql(
                        "SELECT jsonb_build_object('count',count(*),'after',count(*) FILTER(WHERE title='After')) FROM credeals.cre_listings;",
                        db,
                    ).stdout
                )
                assert observed == {
                    "count": 1 if case == "same_id" else 2,
                    "after": 2 if case == "same_batch_other_source" else 1,
                }, (case, observed)
                checks.append({"case": case, "accepted": True, **observed})
            sql("DROP DATABASE " + db + ";")
    finally:
        (args.proof_dir / "transcript.json").write_text(json.dumps(logs, indent=2))
        (args.proof_dir / "checks.json").write_text(json.dumps(checks, indent=2))
        if container:
            obj = json.loads(
                command(["docker", "inspect", container], check=True).stdout
            )[0]
            assert (
                obj["Config"]["Labels"]["cre4267.owner"] == owner
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
