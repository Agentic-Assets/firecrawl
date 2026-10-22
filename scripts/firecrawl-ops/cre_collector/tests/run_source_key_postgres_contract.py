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
SOURCE_COUNTS_CONTROL = (
    Path(__file__).parent / "fixtures/source_counts_before_decoded_source_key.sql"
)
SOURCE_COUNTS_CONTROL_SHA256 = (
    "4f7fd081b55e93f8b87199499b6867bd3892a78870f59160d16d606b159d3a93"
)
FAMILIES = ("source_counts", "duplicates", "child_counts", "quality_by_source")
SOURCE_COUNTS_COHORTS = ("small", "compressible", "incompressible")


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


def queries(source_counts_json_root_control=None):
    if source_counts_json_root_control is not None:
        control_bytes = source_counts_json_root_control.read_bytes()
        if hashlib.sha256(control_bytes).hexdigest() != SOURCE_COUNTS_CONTROL_SHA256:
            raise RuntimeError("Frozen source_counts control hash mismatch")
        control = control_bytes.decode("utf-8").strip().removesuffix(";")
        old_expression = ingest.source_key_sql()
        new_expression = ingest.source_key_sql("decoded")
        if control.count(old_expression) != 1:
            raise RuntimeError("Frozen source_counts control is not the expected SQL")
        candidate = validate.QUERIES["source_counts"].strip().removesuffix(";")
        if candidate != control.replace(old_expression, new_expression, 1):
            raise RuntimeError("Candidate source_counts SQL differs beyond alias")
        if control == candidate:
            raise RuntimeError("Source_counts control and candidate are identical")
        return {"source_counts": {"old": control, "new": candidate}}

    old = ORACLE.read_text().strip()
    result = {}
    for name in FAMILIES:
        current = ingest.source_key_sql(
            "decoded" if name == "source_counts" else "l"
        )
        if validate.QUERIES[name].count(current) != 1:
            raise RuntimeError("Attribution expression is not bound to fixture query")
        original = validate.QUERIES[name].replace(current, "\n" + old + "\n")
        if original == validate.QUERIES[name]:
            raise RuntimeError(
                "Old and current fixture query are unexpectedly identical"
            )
        result[name] = {
            "old": original.rstrip().removesuffix(";"),
            "new": validate.QUERIES[name].rstrip().removesuffix(";"),
        }
    return result


def json_root_fixture_sql():
    payloads = [
        None,
        {},
        [],
        [1, {"detailUnavailable": False}],
        "scalar",
        7,
        True,
        {"inventoryObservedAt": None},
        {"inventoryObservedAt": ""},
        {"inventoryObservedAt": "2026-01-02T03:04:05Z"},
        {"detailUnavailable": False},
        {"detailUnavailable": None},
        {"primary": {"inventoryObservedAt": "invalid", "detailUnavailable": True}},
        {"latestInventoryObservation": {"inventoryObservedAt": "invalid"}},
    ]
    return "\n".join(
        "INSERT INTO credeals.cre_listings(id,brokerage_id,external_id,raw_data,deleted_at) VALUES "
        + f"({200000 + i},1,'root-fixture-{i}',{literal(json.dumps(raw))}::jsonb,"
        + ("'2026-01-03'" if deleted else "NULL")
        + ");"
        for i, (raw, deleted) in enumerate(
            (raw, deleted) for raw in payloads for deleted in (False, True)
        )
    )


def source_counts_fallback_fixture_sql(cohort=None):
    if cohort is not None and cohort not in SOURCE_COUNTS_COHORTS:
        raise ValueError("Unknown source_counts fixture cohort")
    selected = SOURCE_COUNTS_COHORTS if cohort is None else (cohort,)
    cohort_values = ", ".join(f"('{item}')" for item in selected)
    return f"""
WITH cohorts(cohort) AS (
  VALUES {cohort_values}
), sources(slug, source_key) AS (
  VALUES ('cbre', 'cbre-dealflow'), ('jll', 'jll-investor'),
         ('colliers', 'colliers-main')
), generated AS (
  SELECT cohorts.cohort, sources.slug, sources.source_key, series.n
  FROM cohorts
  CROSS JOIN sources
  CROSS JOIN generate_series(1, 256) AS series(n)
), payloads AS (
  SELECT generated.*,
         CASE cohort
           WHEN 'small' THEN repeat('s', 64)
           WHEN 'compressible' THEN repeat('c', 32768)
           ELSE (
             SELECT string_agg(
               md5(
                 'source-counts:' || generated.cohort || ':' || generated.slug || ':' ||
                 generated.n::text || ':' || g::text
               ), '' ORDER BY g
             )
             FROM generate_series(1, 1024) AS chunks(g)
           )
         END AS payload
  FROM generated
)
INSERT INTO credeals.cre_listings(
  id, brokerage_id, external_id, source_url, raw_data
)
SELECT 300000 + row_number() OVER (ORDER BY payloads.cohort, payloads.slug, payloads.n),
       brokerages.id,
       'native-source-counts-' || payloads.cohort || '-' || payloads.n::text,
       'https://fixture.invalid/source-counts/' || payloads.cohort || '/' ||
         payloads.n::text,
       jsonb_build_object(
         'secondary_pass', jsonb_build_object(
           'sourceKey', payloads.source_key, 'payload', payloads.payload
         )
       )
FROM payloads JOIN credeals.cre_brokerages AS brokerages
  ON brokerages.slug = payloads.slug;
ANALYZE;
"""


def source_counts_fallback_storage_proof(sql, cohorts=SOURCE_COUNTS_COHORTS):
    result = json.loads(sql("""
SELECT COALESCE(json_agg(json_build_object(
  'cohort', cohort,
  'slug', slug,
  'rows', row_count,
  'lateSourceKeyRows', late_source_key_rows,
  'nonFoldedNativeIdRows', native_id_rows,
  'latePathOnlyRows', late_path_only_rows,
  'maxPayloadBytes', max_payload_bytes,
  'minPgColumnBytes', min_pg_column_bytes,
  'maxPgColumnBytes', max_pg_column_bytes
) ORDER BY cohort, slug), '[]'::json)::text
FROM (
  SELECT cohort, slug, count(*) AS row_count,
         count(*) FILTER (
           WHERE raw_data #>> '{secondary_pass,sourceKey}' = expected_source_key
         ) AS late_source_key_rows,
         count(*) FILTER (
           WHERE external_id NOT LIKE 'dealflow:%'
             AND external_id NOT LIKE 'investor:%'
             AND external_id NOT LIKE 'main:%'
         ) AS native_id_rows,
         count(*) FILTER (
           WHERE raw_data ? 'secondary_pass'
             AND NOT (raw_data ? 'sourceKey')
             AND NOT (raw_data ? 'primary')
             AND NOT (raw_data ? 'latestInventoryObservation')
         ) AS late_path_only_rows,
         max(octet_length(raw_data #>> '{secondary_pass,payload}'))
           AS max_payload_bytes,
         min(pg_column_size(raw_data)) AS min_pg_column_bytes,
         max(pg_column_size(raw_data)) AS max_pg_column_bytes
  FROM (
    SELECT CASE
             WHEN external_id LIKE 'native-source-counts-small-%' THEN 'small'
             WHEN external_id LIKE 'native-source-counts-compressible-%'
               THEN 'compressible'
             ELSE 'incompressible'
           END AS cohort,
           brokerages.slug,
           CASE brokerages.slug
             WHEN 'cbre' THEN 'cbre-dealflow'
             WHEN 'jll' THEN 'jll-investor'
             ELSE 'colliers-main'
           END AS expected_source_key,
           listings.external_id, listings.raw_data
    FROM credeals.cre_listings AS listings
    JOIN credeals.cre_brokerages AS brokerages
      ON brokerages.id = listings.brokerage_id
    WHERE listings.external_id LIKE 'native-source-counts-%'
  ) AS fallback_rows
  GROUP BY cohort, slug
) AS cohort_proof;
"""))
    expected = {
        (cohort, slug)
        for cohort in cohorts
        for slug in ("cbre", "jll", "colliers")
    }
    proof = []
    observed = set()
    for row in result:
        observed.add((row["cohort"], row["slug"]))
        if (
            row["rows"],
            row["lateSourceKeyRows"],
            row["nonFoldedNativeIdRows"],
            row["latePathOnlyRows"],
        ) != (256,) * 4:
            raise RuntimeError("Source_counts fallback fixture identity mismatch")
        payload_bytes = row["maxPayloadBytes"]
        if (row["cohort"] == "small" and payload_bytes != 64) or (
            row["cohort"] != "small" and payload_bytes != 32768
        ):
            raise RuntimeError("Source_counts fallback payload size mismatch")
        proof.append(row)
    if observed != expected or len(result) != len(expected):
        raise RuntimeError("Source_counts fallback fixture cohort rows are incomplete")
    return proof


def compare_and_measure(sql, query_arms, group, replicates, scalar_check=None):
    batch = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
    expected_keys = set(query_arms)
    if scalar_check is not None:
        old_scalar, new_scalar, from_sql = scalar_check
        batch += (
            "SELECT 'scalar|' || count(*) "
            + from_sql
            + f" WHERE ({old_scalar}) IS DISTINCT FROM ({new_scalar});\n"
        )
        expected_keys.add("scalar")
    for family, arms in query_arms.items():
        batch += (
            f"WITH old_result AS ({arms['old']}), new_result AS ({arms['new']}) "
            f"SELECT '{family}|'||count(*) FROM ((SELECT * FROM old_result "
            "EXCEPT ALL SELECT * FROM new_result) UNION ALL (SELECT * FROM "
            "new_result EXCEPT ALL SELECT * FROM old_result)) differences;\n"
        )
    batch += "ROLLBACK;\n"
    differences = dict(line.split("|") for line in sql(batch).strip().splitlines())
    if set(differences) != expected_keys or any(
        value != "0" for value in differences.values()
    ):
        raise RuntimeError("Synthetic source attribution parity rejected")

    results = {family: {"old": [], "new": []} for family in query_arms}
    for replicate in range(replicates):
        order = ("old", "new") if replicate % 2 == 0 else ("new", "old")
        script = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
        labels = []
        for family, arms in query_arms.items():
            for arm in order:
                label = f"{group}:{family}:{arm}"
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
            _group, family, arm = expected.split(":")
            results[family][arm].append(plan["Execution Time"])
    ratios = {
        family: statistics.median(arms["old"]) / statistics.median(arms["new"])
        for family, arms in results.items()
    }
    return differences, results, ratios


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--source-counts-json-root-control",
        type=Path,
        nargs="?",
        const=SOURCE_COUNTS_CONTROL,
        help="compare source_counts with its pinned pre-change SQL control",
    )
    args = parser.parse_args()
    query_arms = queries(args.source_counts_json_root_control)
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
        "replicates": 2 if args.source_counts_json_root_control else 3,
        "oracleSha256": hashlib.sha256(ORACLE.read_bytes()).hexdigest(),
    }
    if args.source_counts_json_root_control:
        summary["sourceCountsControlSha256"] = SOURCE_COUNTS_CONTROL_SHA256
        summary["fallbackFixtureRows"] = 3 * 3 * 256
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
            for family, arms in query_arms.items()
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
        if args.source_counts_json_root_control:
            sql(json_root_fixture_sql())
            sql(source_counts_fallback_fixture_sql())
            summary["fallbackStorageProof"] = source_counts_fallback_storage_proof(sql)
            # Each psql invocation owns a new session: create the temporary function
            # and exercise both arms together in that session, then roll back.
            checks = """BEGIN;
CREATE FUNCTION pg_temp.query_state(q text) RETURNS text LANGUAGE plpgsql AS $$
BEGIN EXECUTE q; RETURN '00000'; EXCEPTION WHEN OTHERS THEN RETURN SQLSTATE; END $$;
"""
            for bad in ("invalid", " ", [], {}, True, "2026-99-99"):
                for deleted in (False, True):
                    checks += (
                        "UPDATE credeals.cre_listings SET raw_data="
                        + literal(json.dumps({"inventoryObservedAt": bad}))
                        + "::jsonb, deleted_at="
                        + ("'2026-01-03'" if deleted else "NULL")
                        + " WHERE id=90000;\n"
                    )
                    for arm, query in query_arms["source_counts"].items():
                        checks += (
                            "SELECT 'error:"
                            + arm
                            + "|'||pg_temp.query_state("
                            + literal(query)
                            + ");\n"
                        )
            checks += "ROLLBACK;\n"
            states = [line.split("|")[1] for line in sql(checks).splitlines()]
            if len(states) != 24 or any(
                states[i] != states[i + 1] or not states[i].startswith("22")
                for i in range(0, 24, 2)
            ):
                raise RuntimeError("Timestamp error SQLSTATE parity rejected")
            summary["timestampErrorSqlstates"] = states
            root = """WITH vals(raw_data) AS (VALUES (NULL::jsonb),('null'::jsonb),('{}'::jsonb),('[]'::jsonb),('1'::jsonb),('true'::jsonb),('"scalar"'::jsonb)) SELECT count(*) FROM vals WHERE raw_data IS DISTINCT FROM raw_data #> '{}'::text[] OR jsonb_typeof(raw_data) IS DISTINCT FROM jsonb_typeof(raw_data #> '{}'::text[]);"""
            if sql(root).strip() != "0":
                raise RuntimeError("JSON root value/type parity rejected")
            summary["rootValueAndTypeDifferences"] = 0
        if args.source_counts_json_root_control:
            old = ingest.source_key_sql("l")
            new = ingest.source_key_sql("decoded")
            scalar_from = """FROM credeals.cre_listings l
JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id
CROSS JOIN LATERAL (
  SELECT l.external_id, l.raw_data #> '{}'::text[] AS raw_data OFFSET 0
) AS decoded"""
        else:
            old = ORACLE.read_text().strip()
            new = ingest.source_key_sql()
            scalar_from = "FROM credeals.cre_listings l JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id"
        scalar_check = (old, new, scalar_from)
        mixed = compare_and_measure(
            sql,
            query_arms,
            "mixed",
            summary["replicates"],
            scalar_check,
        )
        if args.source_counts_json_root_control:
            summary["parityDifferences"] = {"mixed": mixed[0]}
            summary["serverExecutionMs"] = {"mixed": mixed[1]}
            summary["medianRatioOldOverNew"] = {"mixed": mixed[2]}
            summary["cohortStorageProof"] = {}
            for cohort in SOURCE_COUNTS_COHORTS:
                sql("TRUNCATE credeals.cre_listings;\n")
                sql(source_counts_fallback_fixture_sql(cohort))
                summary["cohortStorageProof"][cohort] = (
                    source_counts_fallback_storage_proof(sql, (cohort,))
                )
                comparison = compare_and_measure(
                    sql,
                    query_arms,
                    cohort,
                    summary["replicates"],
                )
                summary["parityDifferences"][cohort] = comparison[0]
                summary["serverExecutionMs"][cohort] = comparison[1]
                summary["medianRatioOldOverNew"][cohort] = comparison[2]
            summary["timingScope"] = (
                "mixed source_counts plus separately repopulated fallback cohorts"
            )
        else:
            summary["parityDifferences"] = mixed[0]
            summary["serverExecutionMs"] = mixed[1]
            summary["medianRatioOldOverNew"] = mixed[2]
            summary["timingScope"] = "all configured query families"
    finally:
        cleanup()
    summary["cleanup"] = "stopped_and_removed_verified"
    with output.open("x") as handle:
        json.dump(summary, handle, indent=2)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
