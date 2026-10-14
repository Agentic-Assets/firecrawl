#!/usr/bin/env python3
"""Opt-in synthetic SQL parity/measurement; cached isolated PostgreSQL only."""

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path

COLLECTOR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COLLECTOR))
import cre_ingest as ingest  # noqa: E402
import cre_validate as validate  # noqa: E402

IMAGE = "sha256:2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3"
LABEL = "firecrawl.source-key-contract-owner"
ORACLE = Path(__file__).parent / "fixtures/source_key_sql_before_singletons.sql"
FAMILIES = ("source_counts", "duplicates", "child_counts", "quality_by_source")


def literal(value):
    return "NULL" if value is None else "'" + str(value).replace("'", "''") + "'"


def fixture_rows():
    rows = []
    for source, (slug, prefix) in ingest.SOURCE_TO_BROKERAGE.items():
        other = (
            next(key for key in ingest.SOURCE_KEYS_BY_SLUG[slug] if key != source)
            if len(ingest.SOURCE_KEYS_BY_SLUG[slug]) > 1
            else "foreign-source"
        )
        payloads = [
            None,
            {},
            [],
            [source],
            source,
            1,
            True,
            {"sourceKey": source},
            {"sourceKey": " " + source + " "},
            {"sourceKey": other},
            {"sourceKey": "foreign-source"},
            {"sourceKey": 17},
            {"sourceKey": False},
            {"sourceKey": []},
            {"primary": {"sourceKey": source}},
            {"secondary_pass": {"sourceKey": source}},
            {"latestInventoryObservation": {"sourceKey": source}, "sourceKey": other},
            {
                "latestInventoryObservation": {"primary": {"sourceKey": source}},
                "sourceKey": other,
            },
            {
                "latestInventoryObservation": {"secondary_pass": {"sourceKey": source}},
                "sourceKey": other,
            },
            {"latestInventoryObservation": [], "primary": 2, "secondary_pass": False},
            {"primary": {"sourceKey": ""}, "secondary_pass": {"sourceKey": source}},
            {"sourceKey": {"unexpected": source}},
        ]
        for external in (prefix + "fixture", "legacy-fixture", None):
            rows.extend((slug, external, payload) for payload in payloads)
    for slug in ("unknown", None, " jll ", ""):
        rows.extend(
            (slug, "investor:fixture", raw)
            for raw in (None, {}, {"sourceKey": "jll-investor"}, [], 1)
        )
    return rows


def fixture_sql(rows):
    slugs = list(dict.fromkeys(slug for slug, _, _ in rows))
    broker_rows = ",".join(f"({i + 1},{literal(slug)})" for i, slug in enumerate(slugs))
    broker_ids = {slug: i + 1 for i, slug in enumerate(slugs)}
    values = []
    for i, (slug, external, raw) in enumerate(rows, 1):
        # Duplicates, NULL source URLs, soft-deleted rows and unequal child counts.
        values.append(
            f"({i},{broker_ids[slug]},{literal(external)},{literal(None if i % 7 == 0 else 'https://fixture.invalid/' + str(i % 11))},{literal(json.dumps(raw))}::jsonb,{'now()' if i % 13 == 0 else 'NULL'})"
        )
    sql = """
CREATE SCHEMA credeals;
CREATE TABLE credeals.cre_brokerages(id integer PRIMARY KEY, slug text);
CREATE TABLE credeals.cre_listings(
 id bigint PRIMARY KEY, brokerage_id integer, external_id text, source_url text,
 raw_data jsonb, deleted_at timestamptz, transaction_type text DEFAULT 'sale',
 scraped_at timestamptz DEFAULT '2026-01-01T00:00:00Z', canonical_url text,
 title text, state text, lat numeric, lng numeric, sale_price_usd numeric,
 sale_price_per_sf numeric, lease_rate_min numeric, lease_rate_max numeric, cap_rate numeric);
"""
    sql += "INSERT INTO credeals.cre_brokerages VALUES " + broker_rows + ";\n"
    sql += (
        "INSERT INTO credeals.cre_listings(id,brokerage_id,external_id,source_url,raw_data,deleted_at) VALUES "
        + ",".join(values)
        + ";\n"
    )
    # Include SQL NULL independently of JSON null.
    sql += "INSERT INTO credeals.cre_listings(id,brokerage_id,external_id,raw_data) VALUES (90000,1,NULL,NULL);\n"
    for table in ("contacts", "documents", "images", "media", "links"):
        sql += (
            f"CREATE TABLE credeals.cre_listing_{table}(id bigint,listing_id bigint);\n"
        )
        sql += f"INSERT INTO credeals.cre_listing_{table} SELECT id,id FROM credeals.cre_listings WHERE id % 3=0;\n"
    singleton_ids = [
        broker_ids[slug]
        for slug, keys in ingest.SOURCE_KEYS_BY_SLUG.items()
        if keys == {slug}
    ]
    sql += (
        """
INSERT INTO credeals.cre_listings(id,brokerage_id,external_id,source_url,raw_data)
SELECT 100000+i, (ARRAY["""
        + ",".join(map(str, singleton_ids))
        + """])[1+(i % """
        + str(len(singleton_ids))
        + """ )],
 'large-'||i,'https://fixture.invalid/large-'||i,
 jsonb_build_object('payload',(SELECT string_agg(md5(i::text||':'||g::text),'') FROM generate_series(1,1024) g),'sourceKey','foreign-source')
FROM generate_series(1,256) i;
ANALYZE;
"""
    )
    return sql


def queries():
    old = ORACLE.read_text().strip()
    current = ingest.source_key_sql()
    for name in FAMILIES:
        if validate.QUERIES[name].count(current) != 1:
            raise RuntimeError("Attribution expression is not bound to fixture query")
        if (
            validate.QUERIES[name].replace(current, "\n" + old + "\n")
            == validate.QUERIES[name]
        ):
            raise RuntimeError(
                "Old and current fixture query are unexpectedly identical"
            )
    return {
        name: {
            "old": validate.QUERIES[name]
            .replace(current, "\n" + old + "\n")
            .rstrip()
            .removesuffix(";"),
            "new": validate.QUERIES[name].rstrip().removesuffix(";"),
        }
        for name in FAMILIES
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    output = Path(args.out)
    if output.exists():
        raise RuntimeError("Evidence output already exists")
    owner = uuid.uuid4().hex
    name = "firecrawl-source-key-contract-" + owner
    container_id = None
    creation_attempted = False
    env = dict(os.environ, DOCKER_CONTEXT="default")
    env.pop("DOCKER_HOST", None)

    def docker(*argv, timeout=20, input=None, check=True):
        result = subprocess.run(
            ["docker", *argv],
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
        if check and result.returncode:
            raise RuntimeError("Synthetic Docker command failed")
        return result

    def record():
        rows = json.loads(docker("inspect", container_id or name).stdout)
        if (
            len(rows) != 1
            or rows[0]["Config"]["Labels"].get(LABEL) != owner
            or rows[0]["Image"] != IMAGE
        ):
            raise RuntimeError("Synthetic runtime identity mismatch")
        return rows[0]

    def cleanup():
        nonlocal container_id
        if not creation_attempted:
            return
        row = record()  # Missing/uncertain inspect is never positive absence.
        container_id = row["Id"]
        docker("stop", "--time", "10", container_id, timeout=20)
        if record()["State"]["Running"]:
            raise RuntimeError("Synthetic runtime stop unverified")
        docker("rm", container_id)
        remaining = docker(
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            "id=" + container_id,
            "--format",
            "{{.ID}}",
        )
        if remaining.stdout.strip():
            raise RuntimeError("Synthetic runtime removal unverified")

    def sql(script):
        return docker(
            "exec",
            "--interactive",
            container_id,
            "psql",
            "-X",
            "-q",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            "postgres",
            timeout=90,
            input=script,
        ).stdout

    summary = {
        "kind": "synthetic_source_key_parity",
        "productionPerformanceClaim": False,
        "image": IMAGE,
        "fixtureRows": len(fixture_rows()),
        "largeRows": 256,
        "largePayloadStringBytes": 32768,
        "replicates": 3,
        "oracleSha256": hashlib.sha256(ORACLE.read_bytes()).hexdigest(),
    }
    try:
        docker("image", "inspect", IMAGE)
        creation_attempted = True
        container_id = docker(
            "run",
            "--detach",
            "--pull",
            "never",
            "--name",
            name,
            "--label",
            LABEL + "=" + owner,
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
            "--shm-size",
            "16m",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,nosuid,nodev,size=128m",
            "--env",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "--env",
            "PGDATA=/var/lib/postgresql/data",
            IMAGE,
            "-c",
            "shared_buffers=16MB",
            "-c",
            "max_connections=10",
            "-c",
            "work_mem=8MB",
            "-c",
            "jit=off",
        ).stdout.strip()
        row = record()
        caps = row["HostConfig"]
        if (
            caps["NetworkMode"] != "none"
            or caps["NanoCpus"] != 1000000000
            or caps["Memory"] != 536870912
            or caps["MemorySwap"] != 536870912
            or caps["PidsLimit"] != 128
            or caps["ShmSize"] != 16777216
            or caps.get("PortBindings")
            or caps.get("Binds")
            or caps["Tmpfs"]
            != {"/var/lib/postgresql/data": "rw,nosuid,nodev,size=128m"}
            or any(m["Type"] != "tmpfs" for m in row["Mounts"])
        ):
            raise RuntimeError("Synthetic runtime caps/isolation mismatch")
        summary["ownedRuntime"] = {
            "id": container_id,
            "owner": owner,
            "caps": {
                key: caps[key]
                for key in (
                    "NetworkMode",
                    "NanoCpus",
                    "Memory",
                    "MemorySwap",
                    "PidsLimit",
                    "ShmSize",
                    "Tmpfs",
                )
            },
            "noPublishedPorts": not bool(caps.get("PortBindings")),
            "noHostBinds": not bool(caps.get("Binds")),
        }
        summary["querySha256"] = {
            family: {
                arm: hashlib.sha256(query.encode()).hexdigest()
                for arm, query in arms.items()
            }
            for family, arms in queries().items()
        }
        for _ in range(30):
            if (
                docker(
                    "exec",
                    container_id,
                    "pg_isready",
                    "-U",
                    "postgres",
                    timeout=5,
                    check=False,
                ).returncode
                == 0
            ):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Synthetic PostgreSQL startup unavailable")
        sql(fixture_sql(fixture_rows()))
        old = ORACLE.read_text().strip()
        new = ingest.source_key_sql()
        batch = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
        batch += f"SELECT 'scalar|' || count(*) FROM credeals.cre_listings l JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id WHERE ({old}) IS DISTINCT FROM ({new});\n"
        for family, arms in queries().items():
            batch += f"WITH old_result AS ({arms['old']}), new_result AS ({arms['new']}) SELECT '{family}|'||count(*) FROM ((SELECT * FROM old_result EXCEPT ALL SELECT * FROM new_result) UNION ALL (SELECT * FROM new_result EXCEPT ALL SELECT * FROM old_result)) differences;\n"
        batch += "ROLLBACK;\n"
        differences = dict(line.split("|") for line in sql(batch).strip().splitlines())
        if set(differences) != {"scalar", *FAMILIES} or any(
            value != "0" for value in differences.values()
        ):
            raise RuntimeError("Synthetic source attribution parity rejected")
        summary["parityDifferences"] = differences
        results = {family: {"old": [], "new": []} for family in FAMILIES}
        for replicate in range(3):
            order = ("old", "new") if replicate % 2 == 0 else ("new", "old")
            script = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
            labels = []
            for family, arms in queries().items():
                for arm in order:
                    label = family + ":" + arm
                    labels.append(label)
                    script += (
                        "\\echo MEASURE:"
                        + label
                        + "\nEXPLAIN (ANALYZE, TIMING OFF, SUMMARY ON, FORMAT JSON) "
                        + arms[arm]
                        + ";\n"
                    )
            script += "ROLLBACK;\n"
            chunks = sql(script).split("MEASURE:")[1:]
            if len(chunks) != len(labels):
                raise RuntimeError("Synthetic timing frames incomplete")
            for expected, chunk in zip(labels, chunks, strict=True):
                label, body = chunk.split("\n", 1)
                if label.strip() != expected:
                    raise RuntimeError("Synthetic timing frame mismatch")
                plan = json.loads(body)[0]
                family, arm = expected.split(":")
                results[family][arm].append(plan["Execution Time"])
        summary["serverExecutionMs"] = results
        summary["medianRatioOldOverNew"] = {
            family: statistics.median(arms["old"]) / statistics.median(arms["new"])
            for family, arms in results.items()
        }
    finally:
        cleanup()
    summary["cleanup"] = "stopped_and_removed_verified"
    with output.open("x") as handle:
        json.dump(summary, handle, indent=2)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
