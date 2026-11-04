"""AGENTIC-4268: exact 22-pair Interra identity repair, dry-run by default.

No discovery, fuzzy matching, provider-absence inference or automatic retry.
An approved immutable plan, current artifact and separate operator contract are
required for apply. Uncertain database outcomes retain the canonical fence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cre_checkpoint_refresh as checkpoint
import cre_ingest as ingest

OPERATION = "interra-native-identity-v1"
APPROVAL_REF = "AGENTIC-4268"
FK_COLUMNS = {
    "cre_listing_contacts": "listing_id",
    "cre_listing_documents": "listing_id",
    "cre_listing_events": "listing_id",
    "cre_listing_images": "listing_id",
    "cre_listing_links": "listing_id",
    "cre_listing_media": "listing_id",
    "cre_listing_om_facts": "listing_id",
    "cre_listing_price_history": "listing_id",
    "cre_scrape_log": "listing_id",
    "deal_listing_bindings": "listing_id",
    "search_results": "source_listing_id",
}
ARCHIVES = tuple(
    "cre_listing_" + name + "_archive"
    for name in ("contacts", "documents", "links", "media", "om_facts")
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read_private_json(path):
    path = Path(path)
    observed = path.lstat()
    require(
        stat.S_ISREG(observed.st_mode)
        and observed.st_nlink == 1
        and observed.st_uid == os.getuid()
        and observed.st_mode & 0o077 == 0,
        "JSON input must be an owned private regular file",
    )
    require(observed.st_size <= 8 * 1024 * 1024, "JSON input too large")
    return json.loads(path.read_text())


def validate_plan(plan, artifact_path, now=None):
    require(
        plan.get("schema_version") == 1
        and plan.get("repair") == OPERATION
        and plan.get("approval_ref") == APPROVAL_REF,
        "unexpected repair plan",
    )
    require(
        plan.get("plan_hash")
        == digest({k: v for k, v in plan.items() if k != "plan_hash"}),
        "immutable plan digest mismatch",
    )
    require(
        re.fullmatch(r"[0-9a-f]{64}", plan["database_target_sha256"]),
        "invalid database target",
    )
    artifact_info = plan["artifact"]
    require(str(Path(artifact_path)) == artifact_info["path"], "artifact path drift")
    path = Path(artifact_path)
    require(not path.is_symlink() and path.is_file(), "unsafe artifact")
    require(
        path.stat().st_size <= 32 * 1024 * 1024, "artifact exceeds private repair bound"
    )
    raw = path.read_bytes()
    require(
        hashlib.sha256(raw).hexdigest() == artifact_info["sha256"],
        "artifact bytes drifted",
    )
    artifact = json.loads(raw)
    scope = ingest.strict_full_source_scope(artifact, "interra-realty")
    meta = artifact["runMeta"]
    finished = datetime.fromisoformat(meta["finishedAt"].replace("Z", "+00:00"))
    current = now or datetime.now(UTC)
    require(
        timedelta(0) <= current - finished <= timedelta(hours=24), "artifact expired"
    )
    require(
        meta["finishedAt"] == artifact_info["finished_at"]
        and meta["freshness"]["generationId"] == artifact_info["generation_id"],
        "artifact generation drift",
    )
    require(
        re.fullmatch(r"[0-9a-f]{40}", artifact_info["collector_sha"]),
        "invalid artifact collector SHA",
    )
    pairs, before = plan["pairs"], plan["before"]
    require(
        len(pairs) == 22 and len(before["parents"]) == 44,
        "repair requires exactly 22 pairs",
    )
    require(
        len(before["contacts"]) == 40
        and len(before["images"]) == 22
        and len(before["source_index"]) == 22,
        "unexpected protected family counts",
    )
    parents = {row["id"]: row for row in before["parents"]}
    require(len(parents) == 44, "duplicate parent identity")
    require(
        len({row["id"] for row in before["contacts"]}) == 40
        and {pair["image_id"] for pair in pairs}
        == {row["id"] for row in before["images"]}
        and len({pair["image_id"] for pair in pairs}) == 22,
        "duplicate protected child identity",
    )
    ids = [pair[key] for pair in pairs for key in ("survivor_id", "superseded_id")]
    require(len(set(ids)) == 44 and set(ids) == set(parents), "pair coverage drift")
    provider_ids = [pair["provider_external_id"] for pair in pairs]
    require(
        len(set(provider_ids)) == 22
        and all(re.fullmatch(r"[1-9][0-9]*", x) for x in provider_ids),
        "provider identities ambiguous",
    )
    listings = {str(row["id"]): row for row in scope["listings"]}
    require(
        len(listings) == len(scope["listings"]) == 62,
        "provider artifact identities ambiguous",
    )
    require(
        len({row["url"] for row in listings.values()}) == 62, "provider URL collision"
    )
    brokerage = {row["brokerage_id"] for row in parents.values()}
    require(
        len(brokerage) == 1 and plan["untouched_unmatched_legacy_rows"] == 874,
        "brokerage or unmatched scope drift",
    )
    for pair in pairs:
        survivor, duplicate = (
            parents[pair[key]] for key in ("survivor_id", "superseded_id")
        )
        for key in ("survivor_id", "superseded_id", "image_id"):
            require(str(uuid.UUID(pair[key])) == pair[key], "invalid pair UUID")
        require(
            pair["superseded_external_id"]
            == "interra-superseded:v1:" + pair["superseded_id"],
            "unexpected superseded namespace",
        )
        require(
            pair["legacy_external_id"].startswith("listing/")
            and survivor["external_id"] == pair["legacy_external_id"]
            and duplicate["external_id"] == pair["provider_external_id"],
            "parent identity mismatch",
        )
        require(
            survivor["source_url"]
            == duplicate["source_url"]
            == pair["source_url"]
            == listings[pair["provider_external_id"]]["url"],
            "exact URL mapping mismatch",
        )
        require(
            all(
                row["status"] == "active" and row["deleted_at"] is None
                for row in (survivor, duplicate)
            ),
            "parent lifecycle mismatch",
        )
        images = [row for row in before["images"] if row["id"] == pair["image_id"]]
        require(
            len(images) == 1 and images[0]["listing_id"] == pair["superseded_id"],
            "image mapping mismatch",
        )
    survivors = {pair["survivor_id"] for pair in pairs}
    require(
        all(row["listing_id"] in survivors for row in before["contacts"]),
        "contact scope mismatch",
    )
    require(
        {row["external_id"] for row in before["source_index"]} == set(provider_ids)
        and all(
            row["source_key"] == "interra-realty" and row["brokerage_id"] in brokerage
            for row in before["source_index"]
        ),
        "source index scope mismatch",
    )
    definitions = [
        {"table": "credeals." + table, "column": column, "arity": 1}
        for table, column in sorted(FK_COLUMNS.items())
    ]
    require(
        before["foreign_key_definitions"] == definitions
        and before["fk_surfaces"] == [row["table"] for row in definitions],
        "foreign key surface mismatch",
    )
    require(
        set(before["foreign_key_rows"]) == {row["table"] for row in definitions},
        "foreign key rows incomplete",
    )
    for table in FK_COLUMNS:
        rows = before["foreign_key_rows"]["credeals." + table]
        expected = (
            before["contacts"]
            if table == "cre_listing_contacts"
            else before["images"]
            if table == "cre_listing_images"
            else []
        )
        require(rows == expected, "unexpected references: " + table)
    require(
        before["soft_reference_rows"] == {name: [] for name in ARCHIVES},
        "unexpected archive references",
    )
    require(
        len(plan["reference_counts"]) == 44
        and {row["id"] for row in plan["reference_counts"]} == set(parents),
        "reference accounting incomplete",
    )
    for row in plan["reference_counts"]:
        require(
            set(row["references"])
            == {table for table in FK_COLUMNS if table.startswith("cre_")}
            | set(ARCHIVES)
            | {"queue", "sourceIndex"},
            "reference accounting family mismatch",
        )
        require(row["parent"] == parents[row["id"]], "reference parent drift")
        expected = {
            table: sum(x["listing_id"] == row["id"] for x in before["contacts"])
            if table == "cre_listing_contacts"
            else sum(x["listing_id"] == row["id"] for x in before["images"])
            if table == "cre_listing_images"
            else 0
            for table in row["references"]
        }
        if row["id"] not in survivors:
            expected["sourceIndex"] = 1
        require(row["references"] == expected, "reference counts drift")
    return plan


def _rows_assert(query, expected, message):
    value = ingest.sql_lit(canonical(expected))
    return f"""IF (SELECT coalesce(jsonb_agg(v ORDER BY v::text),'[]'::jsonb) FROM ({query}) rows(v))
 IS DISTINCT FROM (SELECT coalesce(jsonb_agg(value ORDER BY value::text),'[]'::jsonb)
 FROM jsonb_array_elements({value}::jsonb)) THEN RAISE EXCEPTION {ingest.sql_lit(message)}; END IF;"""


def build_sql(plan, operator):
    before, pairs = plan["before"], plan["pairs"]
    parent_ids = ",".join(
        ingest.sql_lit(row["id"]) + "::uuid" for row in before["parents"]
    )
    all_external = ",".join(
        ingest.sql_lit(row[key])
        for row in pairs
        for key in (
            "legacy_external_id",
            "provider_external_id",
            "superseded_external_id",
        )
    )
    brokerage = ingest.sql_lit(before["parents"][0]["brokerage_id"]) + "::uuid"
    job = str(uuid.uuid5(uuid.NAMESPACE_URL, OPERATION + ":" + plan["plan_hash"]))
    event_ids = {
        row["superseded_id"]: str(uuid.uuid5(uuid.UUID(job), row["superseded_id"]))
        for row in pairs
    }
    notes = (
        "approved identity supersession; operator="
        + operator
        + "; ref="
        + APPROVAL_REF
        + "; plan="
        + plan["plan_hash"]
    )
    w = [
        "BEGIN ISOLATION LEVEL SERIALIZABLE;",
        "SET LOCAL statement_timeout='120s'; SET LOCAL lock_timeout='5s';",
        ingest.lifecycle_transaction_lock_sql(),
        f"SELECT id FROM credeals.cre_source_index WHERE brokerage_id={brokerage} AND external_id IN ({all_external}) ORDER BY id FOR UPDATE;",
        f"SELECT id FROM credeals.cre_listings WHERE id IN ({parent_ids}) ORDER BY id FOR UPDATE;",
        f"SELECT id FROM credeals.cre_listing_images WHERE listing_id IN ({parent_ids}) ORDER BY id FOR UPDATE;",
        "CREATE TEMP TABLE _interra_untouched AS SELECT md5(coalesce(jsonb_agg(to_jsonb(t) ORDER BY t.id)::text,'[]')) hash FROM credeals.cre_listings t WHERE brokerage_id="
        + brokerage
        + " AND id NOT IN ("
        + parent_ids
        + ");",
        "DO $$ BEGIN",
    ]
    # Lock all existing references too: a contact-only writer need not update
    # its parent. Empty non-FK archive scopes remain protected by the shared
    # lifecycle writer contract and serializable pre/post assertions.
    reference_locks = [
        f"SELECT 1 FROM credeals.{table} t WHERE {column} IN ({parent_ids}) ORDER BY to_jsonb(t)::text FOR UPDATE OF t;"
        for table, column in sorted(FK_COLUMNS.items())
        if table != "cre_listing_images"
    ]
    w[6:6] = reference_locks
    catalog = """SELECT jsonb_build_object('table',n.nspname||'.'||t.relname,'column',a.attname,'arity',cardinality(c.conkey))
 FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace
 JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey)
 WHERE c.contype='f' AND c.confrelid='credeals.cre_listings'::regclass"""
    w.append(
        _rows_assert(
            catalog,
            before["foreign_key_definitions"],
            "listing foreign key catalog drift",
        )
    )
    w.append(
        _rows_assert(
            f"SELECT to_jsonb(t) FROM credeals.cre_listings t WHERE id IN ({parent_ids})",
            before["parents"],
            "parent preimage drift",
        )
    )
    si_query = f"SELECT to_jsonb(t) FROM credeals.cre_source_index t WHERE brokerage_id={brokerage} AND external_id IN ({all_external})"
    w.append(
        _rows_assert(si_query, before["source_index"], "source index preimage drift")
    )
    for table, column in FK_COLUMNS.items():
        w.append(
            _rows_assert(
                f"SELECT to_jsonb(t) FROM credeals.{table} t WHERE {column} IN ({parent_ids})",
                before["foreign_key_rows"]["credeals." + table],
                "reference preimage drift: " + table,
            )
        )
    for table in ARCHIVES:
        w.append(
            _rows_assert(
                f"SELECT to_jsonb(t) FROM credeals.{table} t WHERE source_listing_id IN ({parent_ids})",
                [],
                "archive preimage drift: " + table,
            )
        )
    w.extend(
        [
            f"IF EXISTS(SELECT 1 FROM credeals.cre_enrichment_queue WHERE brokerage_id={brokerage} AND external_id IN ({all_external})) THEN RAISE EXCEPTION 'queue namespace is not empty'; END IF;",
            f"IF (SELECT count(*) FROM credeals.cre_listings WHERE brokerage_id={brokerage} AND external_id LIKE 'listing/%' AND id NOT IN ({parent_ids})) <> 874 THEN RAISE EXCEPTION 'unmatched legacy count drift'; END IF;",
            f"IF EXISTS(SELECT 1 FROM credeals.cre_listings WHERE brokerage_id={brokerage} AND external_id IN ({all_external}) AND id NOT IN ({parent_ids})) THEN RAISE EXCEPTION 'identity namespace collision'; END IF; END $$;",
            f"INSERT INTO credeals.cre_scrape_jobs(id,brokerage_id,status,artifact_run_key,started_at,completed_at,listings_discovered,listings_scraped,listings_saved,errors_count,notes) VALUES ({ingest.sql_lit(job)}::uuid,{brokerage},'completed',{ingest.sql_lit(OPERATION + ':' + plan['plan_hash'])},now(),now(),0,0,22,0,{ingest.sql_lit(notes)});",
        ]
    )
    for pair in pairs:
        q = {key: ingest.sql_lit(value) for key, value in pair.items()}
        w.append(f"""DO $$ DECLARE changed bigint; BEGIN
 UPDATE credeals.cre_listings SET external_id={q["superseded_external_id"]},status='inactive',deleted_at=now(),updated_at=now() WHERE id={q["superseded_id"]}::uuid;
 GET DIAGNOSTICS changed=ROW_COUNT; IF changed<>1 THEN RAISE EXCEPTION 'superseded row count drift'; END IF;
 UPDATE credeals.cre_listings SET external_id={q["provider_external_id"]},updated_at=now() WHERE id={q["survivor_id"]}::uuid;
 GET DIAGNOSTICS changed=ROW_COUNT; IF changed<>1 THEN RAISE EXCEPTION 'survivor row count drift'; END IF;
 UPDATE credeals.cre_listing_images SET listing_id={q["survivor_id"]}::uuid WHERE id={q["image_id"]}::uuid AND listing_id={q["superseded_id"]}::uuid;
 GET DIAGNOSTICS changed=ROW_COUNT; IF changed<>1 THEN RAISE EXCEPTION 'image row count drift'; END IF; END $$;
 INSERT INTO credeals.cre_listing_events(id,listing_id,brokerage_id,scrape_job_id,event_type,field,old_value,new_value,source_value,source_url,reconciliation_provenance,evidence_observed_at,evidence_time_semantics,reconciliation_evidence_sha256)
 VALUES ({ingest.sql_lit(event_ids[pair["superseded_id"]])}::uuid,{q["superseded_id"]}::uuid,{brokerage},{ingest.sql_lit(job)}::uuid,'status_change','status','active','inactive','approved_identity_supersession',{q["source_url"]},{ingest.sql_lit(notes)},{ingest.sql_lit(plan["artifact"]["finished_at"])}::timestamptz,'provider_inventory_observed_at_not_absence',{ingest.sql_lit(plan["artifact"]["sha256"])});""")
    w.append("DO $$ BEGIN")
    by_id = {row["id"]: row for row in before["parents"]}
    for pair in pairs:
        for key, omitted in (
            ("survivor_id", ("external_id", "updated_at")),
            ("superseded_id", ("external_id", "updated_at", "status", "deleted_at")),
        ):
            drops = "".join("-" + ingest.sql_lit(x) for x in omitted)
            expected = [{k: v for k, v in by_id[pair[key]].items() if k not in omitted}]
            w.append(
                _rows_assert(
                    f"SELECT to_jsonb(t){drops} FROM credeals.cre_listings t WHERE id={ingest.sql_lit(pair[key])}::uuid",
                    expected,
                    "protected parent values changed",
                )
            )
        w.append(
            f"IF NOT EXISTS(SELECT 1 FROM credeals.cre_listings WHERE id={ingest.sql_lit(pair['survivor_id'])}::uuid AND external_id={ingest.sql_lit(pair['provider_external_id'])} AND status='active' AND deleted_at IS NULL) OR NOT EXISTS(SELECT 1 FROM credeals.cre_listings WHERE id={ingest.sql_lit(pair['superseded_id'])}::uuid AND external_id={ingest.sql_lit(pair['superseded_external_id'])} AND status='inactive' AND deleted_at=transaction_timestamp()) THEN RAISE EXCEPTION 'identity postcondition failed'; END IF;"
        )
    w.append(_rows_assert(si_query, before["source_index"], "source index changed"))
    image_survivors = {pair["image_id"]: pair["survivor_id"] for pair in pairs}
    for table, column in FK_COLUMNS.items():
        expected = before["foreign_key_rows"]["credeals." + table]
        if table == "cre_listing_images":
            expected = [
                {**row, "listing_id": image_survivors[row["id"]]} for row in expected
            ]
        condition = f"{column} IN ({parent_ids})"
        if table == "cre_listing_events":
            condition += (
                f" AND scrape_job_id IS DISTINCT FROM {ingest.sql_lit(job)}::uuid"
            )
        w.append(
            _rows_assert(
                f"SELECT to_jsonb(t) FROM credeals.{table} t WHERE {condition}",
                expected,
                "protected reference changed: " + table,
            )
        )
    for table in ARCHIVES:
        w.append(
            _rows_assert(
                f"SELECT to_jsonb(t) FROM credeals.{table} t WHERE source_listing_id IN ({parent_ids})",
                [],
                "archive changed: " + table,
            )
        )
    w.append(
        f"IF (SELECT md5(coalesce(jsonb_agg(to_jsonb(t) ORDER BY t.id)::text,'[]')) FROM credeals.cre_listings t WHERE brokerage_id={brokerage} AND id NOT IN ({parent_ids})) IS DISTINCT FROM (SELECT hash FROM _interra_untouched) THEN RAISE EXCEPTION 'unmatched parent changed'; END IF;"
    )
    w.append(
        f"IF (SELECT count(*) FROM credeals.cre_listing_events WHERE scrape_job_id={ingest.sql_lit(job)}::uuid AND event_type='status_change' AND source_value='approved_identity_supersession')<>22 THEN RAISE EXCEPTION 'audit event count mismatch'; END IF; END $$;"
    )
    w.append(
        f"SELECT jsonb_build_object('plan_hash',{ingest.sql_lit(plan['plan_hash'])},'job_id',{ingest.sql_lit(job)},'survivors',22,'superseded',22,'images_moved',22,'contacts_preserved',40,'source_index_preserved',22,'unmatched_legacy_preserved',874); COMMIT;"
    )
    return "\n".join(w) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approval-contract", type=Path)
    parser.add_argument("--plan-hash")
    parser.add_argument("--confirm")
    parser.add_argument("--expected-sha")
    parser.add_argument("--expected-db-target-sha256")
    parser.add_argument("--env-file")
    args = parser.parse_args()
    os.umask(0o077)
    plan = validate_plan(read_private_json(args.plan), args.artifact)
    operator = "cayman"
    if args.apply:
        require(
            args.approval_contract is not None,
            "separate operator approval contract required",
        )
        contract = read_private_json(args.approval_contract)
        require(
            contract
            == {
                "schema_version": 1,
                "operation": OPERATION,
                "operator": operator,
                "approval_ref": APPROVAL_REF,
                "plan_hash": plan["plan_hash"],
            },
            "approval contract mismatch",
        )
        require(
            args.plan_hash == plan["plan_hash"]
            and args.confirm == "APPLY " + OPERATION + " " + plan["plan_hash"],
            "exact approval confirmation required",
        )
        require(
            args.expected_db_target_sha256 == plan["database_target_sha256"],
            "database target drift",
        )
        require(
            args.expected_sha and re.fullmatch(r"[0-9a-f]{40}", args.expected_sha),
            "reviewed SHA required",
        )
        sha, dirty = checkpoint.git_identity()
        require(
            sha == args.expected_sha and not dirty, "checkout is not clean reviewed SHA"
        )
    parent = args.out_dir.parent.lstat()
    require(
        stat.S_ISDIR(parent.st_mode)
        and not args.out_dir.parent.is_symlink()
        and parent.st_uid == os.getuid()
        and parent.st_mode & 0o077 == 0,
        "output parent must be owned and private",
    )
    args.out_dir.mkdir(mode=0o700)
    (args.out_dir / "preimage.json").write_text(canonical(plan))
    sql = build_sql(plan, operator)
    sql_path = args.out_dir / "repair.sql"
    sql_path.write_text(sql)
    if not args.apply:
        print(
            json.dumps(
                {
                    "apply": False,
                    "plan_hash": plan["plan_hash"],
                    "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
                }
            )
        )
        return 0
    db_url, _ = ingest.load_db_url(args.env_file)
    ingest.assert_expected_database_target(db_url, plan["database_target_sha256"])
    lock = checkpoint.SharedLock(
        checkpoint.canonical_shared_lock_dir(), recovery_required=True
    )
    with lock:
        require(
            checkpoint.git_identity() == (args.expected_sha, False),
            "reviewed checkout drifted before lock admission",
        )
        validate_plan(plan, args.artifact)
        attempt = {
            "plan_hash": plan["plan_hash"],
            "expected_sha": args.expected_sha,
            "pid": os.getpid(),
            "lease_token": lock.lease_token,
            "started_at": datetime.now(UTC).isoformat(),
            "status": "running",
            "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
        }
        (args.out_dir / "attempt.json").write_text(canonical(attempt))
        # psql has one process; timeout is an uncertain database outcome, never
        # permission to retry. The existing recovery fence remains armed.
        result = subprocess.run(
            [
                ingest.find_psql(),
                *ingest.psql_connection_args(db_url),
                "-X",
                "-qAt",
                "-v",
                "ON_ERROR_STOP=1",
                "-f",
                str(sql_path),
            ],
            env=ingest.psql_connection_env(db_url),
            capture_output=True,
            timeout=180,
        )
        (args.out_dir / "stdout.log").write_bytes(result.stdout)
        (args.out_dir / "stderr.log").write_bytes(result.stderr)
        require(
            result.returncode == 0,
            "repair refused or outcome uncertain; inspect private evidence",
        )
        proof = json.loads(result.stdout.decode().splitlines()[-1])
        require(proof["plan_hash"] == plan["plan_hash"], "commit proof mismatch")
        (args.out_dir / "proof.json").write_text(canonical(proof))
        attempt.update(
            status="committed_verified",
            finished_at=datetime.now(UTC).isoformat(),
            proof=proof,
        )
        (args.out_dir / "attempt.json").write_text(canonical(attempt))
        lock.clear_recovery_requirement()
    print(
        json.dumps(
            {
                "apply": True,
                "plan_hash": plan["plan_hash"],
                "proof": str(args.out_dir / "proof.json"),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
