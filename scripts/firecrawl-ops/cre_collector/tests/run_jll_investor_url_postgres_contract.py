"""AGENTIC-4360: actual generated SQL, cached network-none PostgreSQL17 only."""

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

STAMP = "2026-10-17T08:37:21.419Z"
URL = "https://invest.jll.com/us/en/listings/retail/7-eleven-memphis-in"
OTHER = URL + "-other"
WOODBURN = "https://invest.jll.com/us/en/listings/retail/7-eleven-woodburn-in"
OLD = "006P500000pXX6IIAW"
NEW = "006P500000wGZejIAG"


def row(source, identity, url):
    return ingest.to_row(
        {
            "sourceKey": source,
            "id": identity,
            "url": url,
            "canonicalUrl": url,
            "name": "Synthetic contract listing",
            "transactionType": "Sale",
            "contactsDetailed": [{"name": "Incoming Contact"}],
            "brochures": [{"url": "https://example.invalid/incoming.pdf"}],
            "photos": ["https://example.invalid/incoming.jpg"],
        },
        {},
        STAMP,
    )


def cases():
    investor = "jll-investor"
    ordinary = "jll"
    return [
        (
            "observed_memphis",
            [(investor, OLD, URL)],
            [(investor, NEW, URL)],
            True,
            None,
        ),
        (
            "observed_woodburn",
            [(investor, "006P500000pZfXZIA0", WOODBURN)],
            [(investor, "006P500000wG1MwIAK", WOODBURN)],
            True,
            None,
        ),
        (
            "same_id_refresh",
            [(investor, OLD, URL)],
            [(investor, OLD, URL)],
            False,
            None,
        ),
        (
            "unchanged_five_groups",
            [
                (investor, f"{i}-{k}", URL + str(i))
                for i in range(5)
                for k in ["a", "b"]
            ],
            [(investor, f"{i}-b", URL + str(i)) for i in range(5)],
            False,
            None,
        ),
        (
            "third_member",
            [(investor, OLD, URL), (investor, "second", URL)],
            [(investor, NEW, URL)],
            True,
            None,
        ),
        (
            "non_investor_counterpart",
            [(ordinary, "old", URL)],
            [(investor, NEW, URL)],
            True,
            None,
        ),
        (
            "null_external_counterpart",
            [(ordinary, "old", URL)],
            [(investor, NEW, URL)],
            True,
            "null_id",
        ),
        (
            "url_move_into_owner",
            [(investor, OLD, OTHER), (ordinary, "owner", URL)],
            [(investor, OLD, URL)],
            True,
            None,
        ),
        (
            "counterpart_moves_away",
            [(ordinary, "owner", URL)],
            [(ordinary, "owner", OTHER), (investor, NEW, URL)],
            False,
            None,
        ),
        (
            "revival_into_owner",
            [(investor, OLD, URL), (ordinary, "owner", URL)],
            [(investor, OLD, URL)],
            True,
            "delete_first",
        ),
        (
            "staged_counterpart_revival",
            [(ordinary, "owner", URL), (investor, OLD, URL)],
            [(ordinary, "owner", URL), (investor, OLD, URL)],
            True,
            "delete_first",
        ),
        (
            "unstaged_deleted_counterpart",
            [(ordinary, "old", URL)],
            [(investor, NEW, URL)],
            False,
            "delete_first",
        ),
        (
            "other_brokerage",
            [("hanley", "old", URL)],
            [(investor, NEW, URL)],
            False,
            None,
        ),
        (
            "non_investor_only_pair",
            [(ordinary, "old", URL)],
            [(ordinary, "new", URL), (investor, NEW, OTHER)],
            False,
            None,
        ),
        (
            "incoming_collision",
            [],
            [(investor, OLD, URL), (investor, NEW, URL)],
            True,
            None,
        ),
        (
            "existing_pair_moves_apart",
            [(investor, OLD, URL), (investor, NEW, URL)],
            [(investor, OLD, OTHER), (investor, NEW, URL)],
            False,
            None,
        ),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof-dir", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.proof_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    owner = uuid.uuid4().hex
    container = None
    transcript, checks = [], []

    def command(argv, **kwargs):
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=kwargs.pop("timeout", 40),
            **kwargs,
        )

    def sql(text, check=True):
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
                "postgres",
                "-f",
                "-",
            ],
            input=text,
            timeout=60,
        )
        transcript.append(
            {
                "sql_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "rc": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        if check and result.returncode:
            raise AssertionError(result.stderr)
        return result

    def snapshot():
        names = sql(
            "SELECT tablename FROM pg_tables WHERE schemaname='credeals' ORDER BY tablename;"
        ).stdout.splitlines()
        parts = [
            "SELECT "
            + ingest.sql_lit(name)
            + " name,md5(coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text,'[]')) hash FROM credeals."
            + name
            + " t"
            for name in names
        ]
        return json.loads(
            sql(
                "SELECT jsonb_object_agg(name,hash)::text FROM ("
                + " UNION ALL ".join(parts)
                + ")s;"
            ).stdout
        )

    try:
        image = json.loads(
            command(["docker", "image", "inspect", "postgres:17"], check=True).stdout
        )[0]["Id"]
        created = command(
            [
                "docker",
                "create",
                "--pull",
                "never",
                "--name",
                "cre4360-" + owner,
                "--label",
                "cre4360.owner=" + owner,
                "--network",
                "none",
                "--cpus",
                "1",
                "--cpuset-cpus",
                "1",
                "--memory",
                "256m",
                "--memory-swap",
                "256m",
                "--pids-limit",
                "128",
                "--tmpfs",
                "/var/lib/postgresql/data:rw,size=128m",
                "--tmpfs",
                "/var/run/postgresql:rw,size=16m",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                image,
            ],
            check=True,
        )
        container = created.stdout.strip()
        command(["docker", "start", container], check=True)
        obj = json.loads(command(["docker", "inspect", container], check=True).stdout)[
            0
        ]
        assert (
            obj["Image"] == image and obj["Config"]["Labels"]["cre4360.owner"] == owner
        )
        assert (
            obj["HostConfig"]["NetworkMode"] == "none"
            and not obj["NetworkSettings"]["Ports"]
        )
        assert not obj["Mounts"] and obj["HostConfig"]["Memory"] == 256 * 1024**2
        (args.proof_dir / "fixture.json").write_text(
            json.dumps(
                {
                    "id": container,
                    "image": image,
                    "network": "none",
                    "memory": obj["HostConfig"]["Memory"],
                    "cpuset": obj["HostConfig"]["CpusetCpus"],
                },
                indent=2,
            )
        )
        for _ in range(60):
            if (
                command(
                    ["docker", "exec", container, "pg_isready", "-U", "postgres"]
                ).returncode
                == 0
            ):
                break
            time.sleep(0.25)
        else:
            raise AssertionError("owned fixture startup did not settle")
        directory = HERE.parent.parent / "sql"
        schema = re.sub(
            r"^\\i (\S+)\s*$",
            lambda m: (directory / m[1]).read_text(),
            (directory / "000_run_all.sql").read_text(),
            flags=re.M,
        )
        schema += (
            "\n\\set CRE_APPROVE_LISTING_LIFECYCLE 1\n\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman\n\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-4360\n\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'\n"
            + (directory / "016_cre_listing_lifecycle.sql").read_text()
        )
        sql(schema)
        for name, seeds, incoming, refuses, mutation in cases():
            sql(
                "TRUNCATE credeals.cre_listings,credeals.cre_scrape_jobs,credeals.cre_source_baseline,credeals.cre_source_index CASCADE;"
            )
            seed_rows = [row(*seed) for seed in seeds]
            for saved in seed_rows:
                sql(
                    "INSERT INTO credeals.cre_listings(brokerage_id,external_id,source_url,canonical_url,title,raw_data,transaction_type) SELECT id,"
                    + ",".join(
                        ingest.sql_lit(str(saved[k]))
                        for k in ["external_id", "source_url", "canonical_url", "title"]
                    )
                    + ","
                    + ingest.sql_lit(json.dumps(saved["raw_data"]))
                    + "::jsonb,'sale' FROM credeals.cre_brokerages WHERE slug="
                    + ingest.sql_lit(saved["slug"])
                    + ";"
                )
            if seed_rows:
                first = seed_rows[0]
                predicate = (
                    "brokerage_id=(SELECT id FROM credeals.cre_brokerages WHERE slug="
                    + ingest.sql_lit(first["slug"])
                    + ") AND external_id="
                    + ingest.sql_lit(first["external_id"])
                )
                if mutation == "null_id":
                    sql(
                        "UPDATE credeals.cre_listings SET external_id=NULL WHERE "
                        + predicate
                        + ";"
                    )
                if mutation == "delete_first":
                    sql(
                        "UPDATE credeals.cre_listings SET deleted_at=now(),status='inactive' WHERE "
                        + predicate
                        + ";"
                    )
                # Nonempty before-state proves rollback for parent, child, index and old job.
                sql(
                    "INSERT INTO credeals.cre_listing_contacts(listing_id,name) SELECT id,'Before Contact' FROM credeals.cre_listings LIMIT 1;"
                )
                sql(
                    "INSERT INTO credeals.cre_listing_images(listing_id,url) SELECT id,'https://example.invalid/before.jpg' FROM credeals.cre_listings LIMIT 1;"
                )
                for family in ["links", "documents", "media"]:
                    sql(
                        "INSERT INTO credeals.cre_listing_"
                        + family
                        + "(listing_id,url) SELECT id,'https://example.invalid/before-"
                        + family
                        + "' FROM credeals.cre_listings LIMIT 1;"
                    )
                sql(
                    "INSERT INTO credeals.cre_source_index(brokerage_id,external_id,source_key,url) SELECT brokerage_id,coalesce(external_id,'null-before'),'jll',source_url FROM credeals.cre_listings LIMIT 1;"
                )
                sql(
                    "INSERT INTO credeals.cre_scrape_jobs(brokerage_id,status,started_at,completed_at,notes) SELECT id,'completed',now(),now(),'Before job' FROM credeals.cre_brokerages WHERE slug='jll';"
                )
            before = snapshot()
            rows = [row(*item) for item in incoming]
            jobs = [
                {
                    "slug": slug,
                    "discovered": sum(x["slug"] == slug for x in rows),
                    "saved": sum(x["slug"] == slug for x in rows),
                    "errors": 0,
                    "notes": name,
                }
                for slug in sorted({x["slug"] for x in rows})
            ]
            generated = ingest.build_sql(rows, jobs, STAMP, set())
            (args.proof_dir / (name + ".sql")).write_text(generated)
            result = sql(generated, check=False)
            after = snapshot()
            record = {
                "case": name,
                "expected_refusal": refuses,
                "rc": result.returncode,
                "all_tables_unchanged": before == after,
                "tables": len(before),
            }
            checks.append(record)
            if refuses:
                assert (
                    result.returncode != 0
                    and "JLL Investor URL collision growth" in result.stderr
                ), (name, result.stderr)
                assert before == after, "partial mutation in " + name
            else:
                assert result.returncode == 0, (name, result.stderr)
            print(json.dumps(record), flush=True)
    finally:
        (args.proof_dir / "transcript.json").write_text(
            json.dumps(transcript, indent=2)
        )
        (args.proof_dir / "checks.json").write_text(json.dumps(checks, indent=2))
        if container:
            obj = json.loads(
                command(["docker", "inspect", container], check=True).stdout
            )[0]
            assert obj["Config"]["Labels"]["cre4360.owner"] == owner
            removed = command(["docker", "rm", "-f", container])
            absent = command(["docker", "inspect", container])
            (args.proof_dir / "cleanup.json").write_text(
                json.dumps(
                    {
                        "container": container,
                        "remove_rc": removed.returncode,
                        "absent": absent.returncode != 0,
                    },
                    indent=2,
                )
            )
            assert removed.returncode == 0 and absent.returncode != 0


if __name__ == "__main__":
    main()
