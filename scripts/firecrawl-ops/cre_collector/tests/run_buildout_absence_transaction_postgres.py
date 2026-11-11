"""Disposable, local-only PostgreSQL proof of a WIP SQL fragment.

Requires --execute-disposable-fixture; never accepts an external database URL.
Run from a standalone no-hardlinks clone. This tests the ingest SQL builder, not
the full collector CLI.
"""
import argparse
import copy
import json
import re
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_buildout_current_inventory import CODE, SOURCE, TARGET, refresh, setup

import cre_ingest as ingest
from cre_ingest import artifact_run_identity_from_digests
from source_fields.buildout import (
    prepare_inventory_observation_scope,
    qualify_prior_inventory_binding,
)
from source_fields.buildout_observation_sql import sql_fragment
from source_fields.buildout_partial_receipt import (
    experimental_readback_sql,
    experimental_sql,
    prepare_expected_bindings,
    replay_fence_sql,
    validate_partial_readback,
    validate_partition_consistency,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-disposable-fixture", action="store_true")
    from source_fields.buildout import observation_sources
    parser.add_argument("--source", choices=sorted(observation_sources()), default=SOURCE)
    args = parser.parse_args()
    if not args.execute_disposable_fixture:
        parser.error("explicit fixture opt-in required")
    migrations = ROOT.parent / "sql"
    name = "cre-absence-5337-" + uuid.uuid4().hex
    created = False
    try:
        subprocess.run(["docker", "create", "--pull=never", "--name", name,
                        "--network", "none", "--memory", "512m", "--cpus", "0.25",
                        "--pids-limit", "80", "--tmpfs", "/var/lib/postgresql/data",
                        "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "sha256:2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3"],
                       check=True, stdout=subprocess.DEVNULL, timeout=30)
        created = True
        subprocess.run(["docker", "start", name], check=True, stdout=subprocess.DEVNULL, timeout=30)
        for _ in range(60):
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("fixture database failed to start")
        base = ["docker", "exec", "-i", name, "psql", "-h", "127.0.0.1", "-X", "-qAt", "-U", "postgres", "-v", "ON_ERROR_STOP=1"]
        def run(sql, *, check=True):
            result = subprocess.run(base, input=sql, cwd=migrations, text=True, capture_output=True, timeout=60, check=False)
            if check and result.returncode:
                raise RuntimeError(result.stderr[-4000:])
            return result
        try:
            schema = re.sub(r"^\\i (\S+)\s*$", lambda match: (migrations / match[1]).read_text(), (migrations / "000_run_all.sql").read_text(), flags=re.MULTILINE)
            run(schema)
            run("\\set CRE_APPROVE_LISTING_LIFECYCLE 1\n\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman\n"
                "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-5337\n"
                "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'\n"
                + (migrations / "016_cre_listing_lifecycle.sql").read_text())
            run("\\set CRE_APPROVE_AVAILABLE_SPACE_COUNT 1\n\\set CRE_AVAILABLE_SPACE_COUNT_OPERATOR cayman\n\\set CRE_AVAILABLE_SPACE_COUNT_APPROVAL_REF AGENTIC-5337\n\\set CRE_AVAILABLE_SPACE_COUNT_CONFIRM 'APPLY 017_cre_available_space_count'\n" + (migrations / "017_cre_available_space_count.sql").read_text())
            run("\\set CRE_APPROVE_BUILDING_SOURCE_FACTS 1\n\\set CRE_BUILDING_SOURCE_FACTS_OPERATOR cayman\n\\set CRE_BUILDING_SOURCE_FACTS_APPROVAL_REF AGENTIC-5337\n\\set CRE_BUILDING_SOURCE_FACTS_CONFIRM 'APPLY 018_cre_building_source_facts'\n" + (migrations / "018_cre_building_source_facts.sql").read_text())
            source = args.source
            bid = run(f"SELECT id FROM credeals.cre_brokerages WHERE slug='{source}';").stdout.strip()
            proof_tmp = tempfile.TemporaryDirectory(prefix="cre-buildout-proof-")
            _, artifact_path, artifact, manifest, _ = setup(Path(proof_tmp.name), source=source)
            # Add a real, separately identifiable healthy sale observation so
            # the fixture proves both a held identity and a committed peer.
            example = next(item for item in artifact["listings"] if item["id"] == "10")
            healthy = copy.deepcopy(example)
            healthy["id"] = "50"
            healthy["url"] = healthy["canonicalUrl"] = example["url"].replace("dual-sale", "healthy-sale")
            artifact["listings"].append(healthy)
            artifact["totalListings"] += 1
            for entry in artifact["sources"]:
                entry["totalAvailableOnSource"] += 1
                proof = entry["currentInventoryProof"]
                proof["total"] += 1
                for page in proof["pages"]:
                    page["total"] += 1
                last = copy.deepcopy(proof["pages"][-1])
                last["index"] += 1
                last["endpoint"] = last["endpoint"].replace("page=1", "page=2")
                last["rows"] = [copy.deepcopy(proof["pages"][0]["rows"][0])]
                last["rows"][0]["id"] = "50"
                last["rows"][0]["showLink"] = healthy["url"]
                proof["pages"].append(last)
                if entry["transaction"] == "sale":
                    entry["listingsCollected"] += 1
                    for key in ("listings", "inventoryObserved", "authoritativeInventoryFeed", "childPreservationRows"):
                        entry["freshness"][key] += 1
            refresh(artifact_path, artifact, manifest, source=source)
            scope = prepare_inventory_observation_scope(artifact_path.read_bytes(), manifest, source,
                                                        current_code_sha=CODE, current_git_dirty=False,
                                                        current_target_sha256=TARGET)
            jobkey = artifact_run_identity_from_digests([scope["artifact_sha256"]])[0]
            job = str(uuid.uuid5(ingest._RUN_UUID_NAMESPACE, f"{jobkey}:{source}"))
            observed = datetime.fromisoformat(scope["observed_at"])
            def at(days):
                return (observed + timedelta(days=days)).isoformat()
            old_time = at(-2)
            def create(label, native):
                listing_id = str(uuid.uuid4())
                url = "https://example.test/" + label
                data = {"sourceKey": source, "id": native, "url": url}
                raw = json.dumps(data).replace("'", "''")
                run(f"INSERT INTO credeals.cre_listings(id,brokerage_id,external_id,source_url,status,raw_data) VALUES ('{listing_id}','{bid}','{label}','{url}','active','{raw}'::jsonb);"
                    f"INSERT INTO credeals.cre_source_index(brokerage_id,external_id,source_key,url,observation_present,presence_generation,last_enumerated_at) VALUES ('{bid}','{label}','{source}','{url}',true,0,'{old_time}');")
                listing = {"id": listing_id, "brokerage_id": bid, "external_id": label, "source_url": url, "raw_data": data}
                index = {"brokerage_id": bid, "external_id": label, "source_key": source, "url": url, "observation_present": True, "presence_generation": 0, "last_enumerated_at": old_time}
                return qualify_prior_inventory_binding(source, listing, index)
            absent = create("99", "99")
            run(f"INSERT INTO credeals.cre_listing_images(listing_id,url) VALUES ('{absent['listing_id']}','https://example.test/prior-image');")
            create("closed-visible", "40")
            stale = create("changed-preimage", "88")
            create("duplicate-a", "77")
            create("duplicate-b", "77")
            malformed = create("missing-member-source", "85")
            malformed_raw = json.dumps({"primary": {"sourceKey": source, "id": "85", "url": malformed["source_url"]},
                                        "secondary_pass": {"id": "86", "url": malformed["source_url"]}}).replace("'", "''")
            run(f"UPDATE credeals.cre_listings SET raw_data='{malformed_raw}'::jsonb WHERE id='{malformed['listing_id']}';")
            stale_invalid = create("invalid-latest", "84")
            run(f"UPDATE credeals.cre_listings SET raw_data=raw_data || '{{\"latestInventoryObservation\":null}}'::jsonb WHERE id='{stale_invalid['listing_id']}';")
            # A historical claim on a different parent cannot establish a
            # current identity, but it prevents a false disappearance.
            create("candidate-ambiguous", "91")
            old_ambiguous = create("old-ambiguous", "91")
            run(f"UPDATE credeals.cre_listings SET raw_data=raw_data || '{{\"latestInventoryObservation\":null}}'::jsonb WHERE id='{old_ambiguous['listing_id']}';")
            run(f"UPDATE credeals.cre_listings SET title='user update',raw_data=raw_data || '{{\"url\":\"https://example.test/changed\"}}'::jsonb WHERE id='{stale['listing_id']}';")
            staged = {}
            for listing in artifact["listings"]:
                current = ingest.to_row(listing, {}, scope["observed_at"])
                key = current["external_id"]
                staged[key] = ingest.merge_rows(staged[key], current) if key in staged else current
            rows = list(staged.values())
            # A reused external key with a different native ID must not become
            # a false reappearance or overwrite the prior listing.
            conflict = next(row for row in rows if row["external_id"] == "other")
            conflict_id = str(uuid.uuid4())
            prior_raw = json.dumps({"sourceKey": source, "id": "98", "url": conflict["source_url"]}).replace("'", "''")
            run(f"INSERT INTO credeals.cre_listings(id,brokerage_id,external_id,source_url,status,raw_data) VALUES ('{conflict_id}','{bid}','other','{conflict['source_url']}','active','{prior_raw}'::jsonb);"
                f"INSERT INTO credeals.cre_source_index(brokerage_id,external_id,source_key,url,observation_present,presence_generation,last_enumerated_at) VALUES ('{bid}','other','{source}','{conflict['source_url']}',false,1,'{old_time}');")
            # One provider-native ID under a different historical external key
            # reproduces the Greysteel URL-key transition without rekeying it.
            legacy = create("legacy-dual", "10")
            for listing_id in (conflict_id, legacy["listing_id"]):
                run(f"INSERT INTO credeals.cre_listing_contacts(listing_id,name) VALUES ('{listing_id}','fixture contact');"
                    f"INSERT INTO credeals.cre_listing_documents(listing_id,url) VALUES ('{listing_id}','https://example.test/doc');"
                    f"INSERT INTO credeals.cre_listing_images(listing_id,url) VALUES ('{listing_id}','https://example.test/image');"
                    f"INSERT INTO credeals.cre_listing_media(listing_id,url) VALUES ('{listing_id}','https://example.test/video');"
                    f"INSERT INTO credeals.cre_listing_links(listing_id,url) VALUES ('{listing_id}','https://example.test/link');")
            held_snapshot_sql = f"""
SELECT jsonb_agg(jsonb_build_object('listing',to_jsonb(l),'index',to_jsonb(si),
 'contacts',(SELECT jsonb_agg(to_jsonb(c) ORDER BY c.id) FROM credeals.cre_listing_contacts c WHERE c.listing_id=l.id),
 'documents',(SELECT jsonb_agg(to_jsonb(d) ORDER BY d.id) FROM credeals.cre_listing_documents d WHERE d.listing_id=l.id),
 'images',(SELECT jsonb_agg(to_jsonb(i) ORDER BY i.id) FROM credeals.cre_listing_images i WHERE i.listing_id=l.id),
 'media',(SELECT jsonb_agg(to_jsonb(m) ORDER BY m.id) FROM credeals.cre_listing_media m WHERE m.listing_id=l.id),
 'links',(SELECT jsonb_agg(to_jsonb(x) ORDER BY x.id) FROM credeals.cre_listing_links x WHERE x.listing_id=l.id)) ORDER BY l.external_id)::text
FROM credeals.cre_listings l JOIN credeals.cre_source_index si USING (brokerage_id,external_id)
WHERE l.id IN ('{conflict_id}','{legacy['listing_id']}');
"""
            held_before = json.loads(run(held_snapshot_sql).stdout)
            jobs = [{"slug": source, "discovered": len(artifact["listings"]), "saved": len(rows), "errors": 0, "notes": "synthetic", "finished_at": scope["observed_at"]}]
            tx = ingest.build_sql(rows, jobs, scope["observed_at"], set(), artifact_run_key=jobkey,
                                  finished_at=scope["observed_at"],
                                  buildout_observation_context={"scope": scope, "prior": None})
            # A writer outside the lifecycle advisory lock can commit a job
            # while this builder waits between its precheck and insert. The
            # post-insert check must observe it and abort before listing writes.
            fence = replay_fence_sql([source])
            assert tx.count(fence) == 2
            delayed = tx.replace(fence, fence + "SELECT pg_sleep(3);", 1)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(run, delayed, check=False)
                for _ in range(40):
                    sleeping = run("SELECT count(*) FROM pg_stat_activity WHERE state='active' "
                                   "AND query='SELECT pg_sleep(3);';").stdout.strip()
                    if sleeping == "1":
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError("fixture generic writer did not reach pre-insert pause")
                run(f"INSERT INTO credeals.cre_scrape_jobs(id,brokerage_id,status,artifact_run_key,notes) "
                    f"VALUES ('{job}','{bid}','completed','{jobkey}',E'\\nCRE_BUILDOUT_PARTITION_V1:{{}}');")
                raced = pending.result()
                assert raced.returncode != 0 and "requires reconciliation before replay" in raced.stderr
            assert json.loads(run(held_snapshot_sql).stdout) == held_before
            assert run("SELECT count(*) FROM credeals.cre_listing_events;").stdout.strip() == "0"
            run(f"DELETE FROM credeals.cre_scrape_jobs WHERE id='{job}';")
            # Exercise the candidate receipts in the real ingest builder and
            # schema, in a transaction rolled back before any fixture commit.
            before_job, before_guard, held_snapshot, receipt_sql = experimental_sql(scope, "a" * 32)
            assert run("BEGIN ISOLATION LEVEL REPEATABLE READ;" + before_job + "ROLLBACK;", check=False).returncode != 0
            poisoned = run("BEGIN; CREATE TEMP TABLE _jobmeta(notes text); "
                           "INSERT INTO _jobmeta VALUES (E'\\nCRE_BUILDOUT_OBSERVATION_V1:fake');"
                           + before_job + "ROLLBACK;", check=False)
            assert poisoned.returncode != 0 and "reserved receipt marker" in poisoned.stderr
            # A noncooperating writer cannot alter the guarded listing/child
            # tables while the experimental partition transaction owns them.
            with ThreadPoolExecutor(max_workers=1) as pool:
                held_lock = pool.submit(run, "BEGIN; CREATE TEMP TABLE _jobmeta(notes text);"
                                        + before_job + "SELECT pg_sleep(3); ROLLBACK;")
                for _ in range(40):
                    locked = run("SELECT count(*) FROM pg_locks WHERE relation='credeals.cre_listing_contacts'::regclass "
                                 "AND mode='ShareRowExclusiveLock' AND granted;").stdout.strip()
                    if locked == "1":
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError("fixture partition lock did not appear")
                assert run("SELECT count(*) FROM credeals.cre_listing_contacts;").returncode == 0
                blocked = run("BEGIN; SET LOCAL lock_timeout='400ms'; "
                              f"INSERT INTO credeals.cre_listing_contacts(listing_id,name) VALUES ('{conflict_id}','race');"
                              "COMMIT;", check=False)
                assert blocked.returncode != 0 and "lock timeout" in blocked.stderr
                held_lock.result()
            job_anchor = "-- Fail loudly if a sourceKey maps to a brokerage slug that is not seeded."
            guard_anchor = "-- Lock in lifecycle order before reading prior native identity."
            assert tx.count(job_anchor) == tx.count(guard_anchor) == tx.count("COMMIT;") == 1
            experimental_tx = ingest.build_sql(rows, jobs, scope["observed_at"], set(), artifact_run_key=jobkey,
                finished_at=scope["observed_at"], buildout_observation_context={"scope": scope, "prior": None},
                experimental_buildout_partial_attempt="a" * 32)
            snapshot_anchor = "SELECT 'buildout_identity_held=' || count(*)::text FROM _buildout_identity_held;"
            assert all(experimental_tx.count(fragment) == 1 for fragment in (before_job, before_guard, held_snapshot, receipt_sql))
            assert experimental_tx.count(snapshot_anchor) == 1
            inspect = f"""
SELECT 'PARTITION_RECEIPT:' || split_part(split_part(notes, E'\\nCRE_BUILDOUT_PARTITION_V1:', 2),
                  E'\\nCRE_BUILDOUT_OBSERVATION_V1:', 1)
 FROM credeals.cre_scrape_jobs WHERE id='{job}';
SELECT 'PARTITION_ROWS:' || COALESCE(jsonb_agg(jsonb_build_object('external_id',s.external_id,'listing_id',u.id::text)
 ORDER BY s.external_id),'[]'::jsonb)::text FROM _src s JOIN _up u USING (brokerage_id,external_id);
"""
            experimental_tx = experimental_tx.replace("COMMIT;", inspect + "ROLLBACK;", 1)
            for tamper in (
                f"UPDATE credeals.cre_listings SET title='synthetic corruption' WHERE id='{conflict_id}';",
                f"UPDATE credeals.cre_listings SET title='synthetic corruption' WHERE id='{legacy['listing_id']}';",
                f"DELETE FROM credeals.cre_listing_contacts WHERE listing_id='{conflict_id}';",
                f"INSERT INTO credeals.cre_listing_links(listing_id,url) VALUES ('{legacy['listing_id']}','https://example.test/injected');",
                f"UPDATE credeals.cre_source_index SET observation_present=true WHERE external_id='other' AND brokerage_id='{bid}';",
            ):
                changed_held = run(experimental_tx.replace(receipt_sql, tamper + receipt_sql, 1), check=False)
                assert changed_held.returncode != 0 and "partial emitted partition differs" in changed_held.stderr
            false_event = ("INSERT INTO credeals.cre_listing_events "
                           "(listing_id,brokerage_id,scrape_job_id,event_type,source_value,source_url,presence_generation,detected_at) "
                           f"VALUES ('{conflict_id}','{bid}','{job}','disappeared','synthetic',"
                           f"'https://example.test/other',1,'{scope['observed_at']}');")
            changed_event = run(experimental_tx.replace(receipt_sql, false_event + receipt_sql, 1), check=False)
            assert changed_event.returncode != 0 and "partial observation receipt differs" in changed_event.stderr
            output = run(experimental_tx).stdout.splitlines()
            receipt_line = next(line for line in output if line.startswith("PARTITION_RECEIPT:"))
            rows_line = next(line for line in output if line.startswith("PARTITION_ROWS:"))
            candidate_receipt = json.loads(receipt_line.removeprefix("PARTITION_RECEIPT:"))
            candidate_rows = json.loads(rows_line.removeprefix("PARTITION_ROWS:"))
            partial = validate_partition_consistency(scope, "a" * 32, candidate_receipt, candidate_rows)
            assert partial["held"] == ["dual", "other"] and partial["emitted_count"] == 1
            assert run(f"SELECT count(*) FROM credeals.cre_scrape_jobs WHERE id='{job}';").stdout.strip() == "0"
            committed = experimental_tx.replace("ROLLBACK;", "COMMIT;", 1)
            # A cooperating legacy replay must wait for the experimental
            # writer, then observe its durable receipt and refuse to replay.
            delayed_commit = committed.replace("COMMIT;", "SELECT pg_sleep(3); COMMIT;", 1)
            with ThreadPoolExecutor(max_workers=1) as pool:
                winner = pool.submit(run, delayed_commit)
                for _ in range(40):
                    sleeping = run("SELECT count(*) FROM pg_stat_activity WHERE state='active' "
                                   "AND query='SELECT pg_sleep(3);';").stdout.strip()
                    if sleeping == "1":
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError("fixture experimental writer did not reach commit pause")
                loser = run(tx, check=False)
                winner.result()
                assert loser.returncode != 0 and "requires reconciliation before replay" in loser.stderr
            assert run(f"SELECT status || ':' || listings_saved::text FROM credeals.cre_scrape_jobs WHERE id='{job}';").stdout.strip() == f"partial:{len(rows) - 2}"
            # The fixture's run() starts a new psql connection. Re-read the
            # stored receipt and all source rows after COMMIT, not temp tables.
            persisted = run(f"SELECT split_part(split_part(notes, E'\\nCRE_BUILDOUT_PARTITION_V1:', 2), "
                            f"E'\\nCRE_BUILDOUT_OBSERVATION_V1:', 1) FROM credeals.cre_scrape_jobs WHERE id='{job}';")
            stored_receipt = json.loads(persisted.stdout)
            # A new parent's UUID is generated again after the rolled-back
            # trial, so only stable fields must match across those attempts.
            assert {k: v for k, v in stored_receipt.items() if k != "emitted"} == {
                k: v for k, v in candidate_receipt.items() if k != "emitted"}
            assert json.loads(run(held_snapshot_sql).stdout) == held_before
            from cre_validate import persisted_freshness_provenance
            actual = json.loads(run(f"SELECT COALESCE(jsonb_agg(jsonb_build_object('external_id',l.external_id,"
                                    f"'listing_id',l.id::text,'raw_data',l.raw_data) ORDER BY l.external_id),'[]')::text "
                                    f"FROM credeals.cre_listings l WHERE l.brokerage_id='{bid}' AND l.deleted_at IS NULL;").stdout)
            generation_rows = [{"external_id": row["external_id"], "listing_id": row["listing_id"]}
                               for row in actual if persisted_freshness_provenance(row["raw_data"], source_key=source)
                               .get("generation_id") == scope["generation_id"]]
            assert validate_partition_consistency(scope, "a" * 32, stored_receipt, generation_rows) == partial
            verified_scope, expected = prepare_expected_bindings(artifact_path.read_bytes(), manifest, source,
                                                                  producer_sha=CODE, target_sha=TARGET)
            assert verified_scope == scope
            single_snapshot = json.loads(run(experimental_readback_sql(scope, "a" * 32)).stdout)
            assert single_snapshot["receipt"] == stored_receipt
            assert validate_partial_readback(scope, "a" * 32, expected, single_snapshot["receipt"],
                                             single_snapshot["snapshot"]) == partial
            replay = run(committed, check=False)
            assert replay.returncode != 0 and "requires reconciliation before replay" in replay.stderr
            # The legacy builder must also refuse, even if the original
            # identity conflict is removed in the attempted transaction.
            changed_identity = tx.replace("SET TRANSACTION ISOLATION LEVEL READ COMMITTED;", "SET TRANSACTION ISOLATION LEVEL READ COMMITTED; "
                f"DELETE FROM credeals.cre_source_index WHERE brokerage_id='{bid}' AND external_id='legacy-dual'; "
                f"DELETE FROM credeals.cre_listings WHERE brokerage_id='{bid}' AND external_id='legacy-dual';", 1)
            legacy_replay = run(changed_identity, check=False)
            assert legacy_replay.returncode != 0 and "requires reconciliation before replay" in legacy_replay.stderr
            assert json.loads(run(held_snapshot_sql).stdout) == held_before
            # Continue the separate historical V1-only replay tests with a
            # fixture-only removal of the experimental marker.
            run(f"UPDATE credeals.cre_scrape_jobs SET notes=split_part(notes,E'\\nCRE_BUILDOUT_PARTITION_V1:',1) "
                f"|| E'\\nCRE_BUILDOUT_OBSERVATION_V1:' || split_part(notes,E'\\nCRE_BUILDOUT_OBSERVATION_V1:',2) "
                f"WHERE id='{job}';")
            with ThreadPoolExecutor(max_workers=2) as pool:
                attempts = [pool.submit(run, tx) for _ in range(2)]
                for attempt in attempts:
                    attempt.result()
            state = run("SELECT external_id || ':' || observation_present::text || ':' || presence_generation::text FROM credeals.cre_source_index ORDER BY external_id;").stdout.splitlines()
            assert state == ["99:true:0", "candidate-ambiguous:true:0", "changed-preimage:true:0", "closed-visible:true:0", "duplicate-a:true:0", "duplicate-b:true:0", "healthy:true:0", "invalid-latest:true:0", "legacy-dual:true:0", "missing-member-source:true:0", "old-ambiguous:true:0", "other:false:1"], state
            events = run("SELECT event_type || ':' || source_value FROM credeals.cre_listing_events;").stdout.splitlines()
            assert events == [], events
            assert run(f"SELECT raw_data->>'id' FROM credeals.cre_listings WHERE id='{conflict_id}';").stdout.strip() == "98"
            receipt = run(f"SELECT split_part(notes, E'\\nCRE_BUILDOUT_OBSERVATION_V1:', 2) FROM credeals.cre_scrape_jobs WHERE id='{job}';").stdout.strip()
            receipt = json.loads(receipt)
            assert receipt == {"source": source, "generation": scope["generation_id"],
                               "artifact_sha256": scope["artifact_sha256"],
                               "disappeared": 0, "identity_held": 2,
                               "held_external_ids": ["dual", "other"],
                               "applied": []}, receipt
            from source_fields.buildout_observation_sql import readback
            def fixture_query(_psql, _url, sql, **_kwargs):
                result = run(sql)
                result.stdout = "result\n" + result.stdout
                return result
            with patch("cre_ingest.load_db_url", return_value=("fixture", "fixture")), \
                 patch("cre_ingest.database_target_fingerprint_from_url", return_value={"value": TARGET}), \
                 patch("cre_ingest.find_psql", return_value="fixture"), \
                 patch("cre_validate._run_psql_script", side_effect=fixture_query):
                assert readback(scope, None) == receipt
                run(f"UPDATE credeals.cre_scrape_jobs SET notes='missing receipt' WHERE id='{job}';")
                try:
                    readback(scope, None)
                    raise AssertionError("missing receipt was accepted")
                except ValueError:
                    pass
                receipt_json = json.dumps(receipt).replace("'", "''")
                run(f"UPDATE credeals.cre_scrape_jobs SET notes=E'\\nCRE_BUILDOUT_OBSERVATION_V1:' || '{receipt_json}' WHERE id='{job}';")
            state_rows = run("SELECT external_id || ':' || status || ':' || (deleted_at IS NULL)::text FROM credeals.cre_listings ORDER BY external_id;").stdout.splitlines()
            assert {"99:active:true", "changed-preimage:active:true", "closed-visible:active:true"} <= set(state_rows), state_rows
            assert run(f"SELECT count(*) FROM credeals.cre_listing_images WHERE listing_id='{absent['listing_id']}' AND url='https://example.test/prior-image';").stdout.strip() == "1"
            run(tx)
            assert run("SELECT count(*) FROM credeals.cre_listing_events;").stdout.strip() == "0"
            # Simulate resolving the two conflicting fixture parents. Replaying
            # the old artifact must not create events outside its first receipt.
            run(f"DELETE FROM credeals.cre_source_index WHERE brokerage_id='{bid}' AND external_id IN ('legacy-dual','other');"
                f"DELETE FROM credeals.cre_listings WHERE brokerage_id='{bid}' AND external_id IN ('legacy-dual','other');")
            # An experimental partial must not create a partial receipt for a
            # fully clear source. Its whole transaction, including any events,
            # is rolled back so the normal complete path can run separately.
            clear_scope = {**scope, "generation_id": "clear-experimental", "observed_at": at(1),
                           "artifact_sha256": "d" * 64}
            clear_key = artifact_run_identity_from_digests([clear_scope["artifact_sha256"]])[0]
            clear_rows = [dict(row, scraped_at=clear_scope["observed_at"]) for row in rows]
            clear_jobs = [{**jobs[0], "finished_at": clear_scope["observed_at"]}]
            rollback_tables = ("cre_listings", "cre_source_index", "cre_listing_contacts", "cre_listing_documents",
                               "cre_listing_images", "cre_listing_media", "cre_listing_links", "cre_listing_events")
            def rollback_snapshot():
                return {table: json.loads(run(f"SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),"
                                             f"'[]'::jsonb) FROM credeals.{table} t;").stdout)
                        for table in rollback_tables}
            before_clear = rollback_snapshot()
            clear_sql = ingest.build_sql(clear_rows, clear_jobs, clear_scope["observed_at"], set(),
                artifact_run_key=clear_key, finished_at=clear_scope["observed_at"],
                buildout_observation_context={"scope": clear_scope, "prior": None},
                experimental_buildout_partial_attempt="b" * 32)
            clear_result = run(clear_sql, check=False)
            assert clear_result.returncode != 0 and "partial requires a held identity" in clear_result.stderr, clear_result.stderr
            assert run(f"SELECT count(*) FROM credeals.cre_scrape_jobs WHERE artifact_run_key='{clear_key}';").stdout.strip() == "0"
            assert rollback_snapshot() == before_clear
            run(tx)
            assert run("SELECT count(*) FROM credeals.cre_listing_events;").stdout.strip() == "0"
            assert run("SELECT observation_present::text FROM credeals.cre_source_index WHERE external_id='99';").stdout.strip() == "true"
            # A new, unambiguous source generation can establish absence.
            clean_scope = {**scope, "generation_id": "clean-complete", "observed_at": at(1),
                           "artifact_sha256": "e" * 64}
            clean_key = artifact_run_identity_from_digests([clean_scope["artifact_sha256"]])[0]
            clean_rows = [dict(row, scraped_at=clean_scope["observed_at"]) for row in rows]
            clean_jobs = [{**jobs[0], "finished_at": clean_scope["observed_at"]}]
            run(ingest.build_sql(clean_rows, clean_jobs, clean_scope["observed_at"], set(),
                                 artifact_run_key=clean_key, finished_at=clean_scope["observed_at"],
                                 buildout_observation_context={"scope": clean_scope, "prior": None}))
            assert run("SELECT observation_present::text FROM credeals.cre_source_index WHERE external_id='99';").stdout.strip() == "false"
            assert run("SELECT count(*) FROM credeals.cre_listing_events WHERE event_type='disappeared';").stdout.strip() == "1"
            # Legacy v1 receipts could contain both holds and an absence. Keep
            # their existing events and make them readable without replay writes.
            clean_job = str(uuid.uuid5(ingest._RUN_UUID_NAMESPACE, f"{clean_key}:{source}"))
            clean_receipt = json.loads(run(f"SELECT split_part(notes, E'\\nCRE_BUILDOUT_OBSERVATION_V1:', 2) FROM credeals.cre_scrape_jobs WHERE id='{clean_job}';").stdout.strip())
            clean_receipt.update(identity_held=1, held_external_ids=["legacy-held"])
            legacy_json = json.dumps(clean_receipt).replace("'", "''")
            run(f"UPDATE credeals.cre_scrape_jobs SET notes=E'\\nCRE_BUILDOUT_OBSERVATION_V1:' || '{legacy_json}' WHERE id='{clean_job}';")
            with patch("cre_ingest.load_db_url", return_value=("fixture", "fixture")), \
                 patch("cre_ingest.database_target_fingerprint_from_url", return_value={"value": TARGET}), \
                 patch("cre_ingest.find_psql", return_value="fixture"), \
                 patch("cre_validate._run_psql_script", side_effect=fixture_query):
                assert readback(clean_scope, None) == clean_receipt
            run(ingest.build_sql(clean_rows, clean_jobs, clean_scope["observed_at"], set(),
                                 artifact_run_key=clean_key, finished_at=clean_scope["observed_at"],
                                 buildout_observation_context={"scope": clean_scope, "prior": None}))
            assert run("SELECT count(*) FROM credeals.cre_listing_events WHERE event_type='disappeared';").stdout.strip() == "1"
            wrong_job = sql_fragment(scope, [absent], str(uuid.uuid4()), jobkey)
            assert run("BEGIN; SET LOCAL standard_conforming_strings=on;\n" + wrong_job + "\nCOMMIT;", check=False).returncode != 0
            newer_job = str(uuid.uuid4())
            run(f"INSERT INTO credeals.cre_scrape_jobs(id,brokerage_id,status,artifact_run_key,completed_at) VALUES ('{newer_job}','{bid}','completed','newer-synthetic-job','{at(2)}');")
            assert run(tx, check=False).returncode != 0
            older = dict(scope, generation_id="older", observed_at=at(-3))
            assert run("BEGIN; SET LOCAL standard_conforming_strings=on;\n" + sql_fragment(older, [absent], job, jobkey) + "\nCOMMIT;", check=False).returncode != 0
            assert run("SELECT count(*) FROM credeals.cre_listing_events;").stdout.strip() == "1"
            later = at(3)
            row = ingest.to_row({"sourceKey": source, "id": "99", "url": "https://example.test/99"}, {}, later)
            script = ingest.build_sql([row], [{"slug": source, "discovered": 1, "saved": 1, "errors": 0, "notes": "synthetic"}], later, set())
            run(script)
            assert run("SELECT observation_present::text || ':' || presence_generation::text FROM credeals.cre_source_index WHERE external_id='99';").stdout.strip() == "true:2"
            assert run("SELECT count(*) FROM credeals.cre_listing_events WHERE event_type='reappeared';").stdout.strip() == "1"
            # New canonical keys must not bypass the cross-parent native-ID
            # guard, whether the other claimant is old or also newly staged.
            existing = create("legacy-native", "66")
            incoming = []
            for native, key in [("66", "new-conflict"), ("67", "new-a"),
                                ("67", "new-b"), ("84", "new-historical-conflict"),
                                ("68", "healthy-peer")]:
                incoming.append(ingest.to_row({"sourceKey": source, "id": native,
                    "url": f"https://example.test/listings/?propertyId={key}"}, {}, later))
            collision_sql = ingest.build_sql(incoming, [{"slug": source, "discovered": 5,
                "saved": 5, "errors": 0, "notes": "synthetic"}], later, set())
            run(collision_sql)
            assert run("SELECT external_id FROM credeals.cre_listings WHERE external_id IN "
                       "('new-conflict','new-a','new-b','new-historical-conflict','healthy-peer') ORDER BY external_id;").stdout.strip() == "healthy-peer"
            assert run(f"SELECT raw_data->>'id' FROM credeals.cre_listings WHERE id='{existing['listing_id']}';").stdout.strip() == "66"
            # Equal native IDs belonging to different singleton brokerages do
            # not conflict, including in a multi-source ingest transaction.
            peer_source = "fortis-net-lease" if source != "fortis-net-lease" else "faris-lee"
            mixed = [ingest.to_row({"sourceKey": sk, "id": "77777",
                       "url": f"https://example.test/listings/?propertyId=source-{sk}"}, {}, later)
                     for sk in (source, peer_source)]
            mixed_jobs = [{"slug": sk, "discovered": 1, "saved": 1, "errors": 0,
                           "notes": "synthetic"} for sk in (source, peer_source)]
            run(ingest.build_sql(mixed, mixed_jobs, later, set()))
            keys = ",".join("'" + row["external_id"] + "'" for row in mixed)
            assert run(f"SELECT count(*) FROM credeals.cre_listings WHERE external_id IN ({keys});").stdout.strip() == "2"
            assert run("SELECT count(*) FROM credeals.cre_listing_events WHERE event_type='disappeared';").stdout.strip() == "1"
            # Real ingest of a merged sale/lease property must allow later
            # updates and reappearance while still holding unrelated IDs.
            def dual_row(ids, title, observed):
                result = None
                for index, native in enumerate(ids):
                    mode = "sale" if index == 0 else "lease"
                    listing = {"sourceKey": source, "id": native, "name": title,
                               "transactionMode": mode, "transactionType": mode.title(),
                               "preserveChildCollections": True,
                               "url": f"https://example.test/listings/?propertyId=dual-live-{mode}"}
                    row = ingest.to_row(listing, {}, observed)
                    result = row if result is None else ingest.merge_rows(result, row)
                return result
            def ingest_rows(rows, observed):
                jobs = [{"slug": source, "discovered": len(rows), "saved": len(rows),
                         "errors": 0, "notes": "synthetic"}]
                run(ingest.build_sql(rows, jobs, observed, set()))
            first, second, third, fourth, fifth = (at(day) for day in range(4, 9))
            ingest_rows([dual_row(["101", "102"], "first", first)], first)
            ingest_rows([dual_row(["101", "102"], "updated", second)], second)
            assert run("SELECT title FROM credeals.cre_listings WHERE external_id='dual-live';").stdout.strip() == "updated"
            run(f"UPDATE credeals.cre_source_index SET observation_present=false, presence_generation=presence_generation+1 WHERE brokerage_id='{bid}' AND external_id='dual-live';")
            ingest_rows([dual_row(["101", "102"], "returned", third)], third)
            assert run("SELECT title FROM credeals.cre_listings WHERE external_id='dual-live';").stdout.strip() == "returned"
            assert run("SELECT count(*) FROM credeals.cre_listing_events e JOIN credeals.cre_listings l ON l.id=e.listing_id WHERE l.external_id='dual-live' AND e.event_type='reappeared';").stdout.strip() == "1"
            # A previously merged property may later have one observed tenure,
            # and a flat property may later gain a second supported tenure.
            ingest_rows([dual_row(["101"], "flat", fourth)], fourth)
            ingest_rows([dual_row(["101", "102"], "merged-again", fifth)], fifth)
            assert run("SELECT title FROM credeals.cre_listings WHERE external_id='dual-live';").stdout.strip() == "merged-again"
            healthy = ingest.to_row({"sourceKey": source, "id": "104", "name": "peer",
                      "url": "https://example.test/listings/?propertyId=other-peer"}, {}, fifth)
            ingest_rows([dual_row(["201", "202"], "wrong identity", fifth), healthy], fifth)
            assert run("SELECT title FROM credeals.cre_listings WHERE external_id='dual-live';").stdout.strip() == "merged-again"
            assert run("SELECT count(*) FROM credeals.cre_listings WHERE external_id='other-peer';").stdout.strip() == "1"
            ingest_rows([dual_row(["101", "202"], "changed member", fifth)], fifth)
            assert run("SELECT title FROM credeals.cre_listings WHERE external_id='dual-live';").stdout.strip() == "merged-again"
            malformed_incoming = dual_row(["101", "102"], "missing source", fifth)
            del malformed_incoming["raw_data"]["secondary_pass"]["sourceKey"]
            ingest_rows([malformed_incoming], fifth)
            assert run("SELECT title FROM credeals.cre_listings WHERE external_id='dual-live';").stdout.strip() == "merged-again"
            flat_input = {"sourceKey": source, "id": "301", "name": "flat first",
                          "preserveChildCollections": True,
                          "url": "https://example.test/listings/?propertyId=flat-inventory-sale"}
            ingest_rows([ingest.to_row(flat_input, {}, fifth)], fifth)
            ingest_rows([ingest.to_row({**flat_input, "name": "flat updated"}, {}, fifth)], fifth)
            assert run("SELECT title FROM credeals.cre_listings WHERE external_id='flat-inventory';").stdout.strip() == "flat updated"
            # Retained older top-level context is not the current identity.
            run("UPDATE credeals.cre_listings SET raw_data=raw_data || "
                "jsonb_build_object('id', CASE WHEN external_id='dual-live' THEN '999998' ELSE '999999' END, "
                "'url','https://example.test/stale') WHERE external_id IN ('dual-live','flat-inventory');")
            scope2 = {**scope, "generation_id": "later-complete", "observed_at": at(10),
                      "artifact_sha256": "f" * 64}
            key2 = artifact_run_identity_from_digests([scope2["artifact_sha256"]])[0]
            rows2 = [dict(row, scraped_at=scope2["observed_at"]) for row in rows]
            jobs2 = [{"slug": source, "discovered": len(artifact["listings"]), "saved": len(rows),
                      "errors": 0, "notes": "synthetic", "finished_at": scope2["observed_at"]}]
            run(ingest.build_sql(rows2, jobs2, scope2["observed_at"], set(), artifact_run_key=key2,
                                 finished_at=scope2["observed_at"],
                                 buildout_observation_context={"scope": scope2, "prior": None}))
            states = run("SELECT external_id || ':' || observation_present::text FROM credeals.cre_source_index "
                         "WHERE external_id IN ('dual-live','flat-inventory') ORDER BY external_id;").stdout.splitlines()
            assert states == ["dual-live:false", "flat-inventory:false"], states
            with patch("cre_ingest.load_db_url", return_value=("fixture", "fixture")), \
                 patch("cre_ingest.database_target_fingerprint_from_url", return_value={"value": TARGET}), \
                 patch("cre_ingest.find_psql", return_value="fixture"), \
                 patch("cre_validate._run_psql_script", side_effect=fixture_query):
                assessed = readback(scope2, None)
                assert {"dual-live", "flat-inventory"} <= {row["external_id"] for row in assessed["applied"]}
            returned = at(11)
            ingest_rows([dual_row(["101", "102"], "dual returned again", returned),
                         ingest.to_row({**flat_input, "name": "flat returned"}, {}, returned)], returned)
            states = run("SELECT external_id || ':' || observation_present::text FROM credeals.cre_source_index "
                         "WHERE external_id IN ('dual-live','flat-inventory') ORDER BY external_id;").stdout.splitlines()
            assert states == ["dual-live:true", "flat-inventory:true"], states
            # Also test the opposite ordering: a legacy writer commits first,
            # and the experimental writer refuses its already-recorded job.
            reverse_scope = {**scope, "generation_id": "legacy-first", "observed_at": at(12),
                             "artifact_sha256": "c" * 64}
            reverse_key = artifact_run_identity_from_digests([reverse_scope["artifact_sha256"]])[0]
            reverse_rows = [dict(row, scraped_at=reverse_scope["observed_at"]) for row in rows]
            reverse_jobs = [{**jobs[0], "finished_at": reverse_scope["observed_at"]}]
            reverse_args = (reverse_rows, reverse_jobs, reverse_scope["observed_at"], set())
            reverse_kwargs = {"artifact_run_key": reverse_key, "finished_at": reverse_scope["observed_at"],
                              "buildout_observation_context": {"scope": reverse_scope, "prior": None}}
            reverse_legacy = ingest.build_sql(*reverse_args, **reverse_kwargs)
            reverse_partial = ingest.build_sql(*reverse_args, **reverse_kwargs,
                                               experimental_buildout_partial_attempt="c" * 32)
            with ThreadPoolExecutor(max_workers=1) as pool:
                winner = pool.submit(run, reverse_legacy.replace("COMMIT;", "SELECT pg_sleep(3); COMMIT;", 1))
                for _ in range(40):
                    sleeping = run("SELECT count(*) FROM pg_stat_activity WHERE state='active' "
                                   "AND query='SELECT pg_sleep(3);';").stdout.strip()
                    if sleeping == "1":
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError("fixture legacy writer did not reach commit pause")
                loser = run(reverse_partial, check=False)
                winner.result()
                assert loser.returncode != 0 and "artifact already has a job" in loser.stderr, loser.stderr
            proof_tmp.cleanup()
            print("PASS: actual schema and ingest builder, absent and held peers, concurrent replay, wrong job and stale source job refused, preserved status, actual ingest reappearance")
        finally:
            pass
    finally:
        if created:
            subprocess.run(["docker", "rm", "-f", name], check=True, stdout=subprocess.DEVNULL, timeout=30)


if __name__ == "__main__":
    main()
