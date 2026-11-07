"""Disposable, local-only PostgreSQL proof of a WIP SQL fragment.

Requires --execute-disposable-fixture; never accepts an external database URL.
Run from a standalone no-hardlinks clone. This does not test an ingest caller.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cre_ingest as ingest
from cre_ingest import artifact_run_identity_from_digests
from source_fields.buildout import (
    prepare_inventory_observation_scope,
    qualify_prior_inventory_binding,
)
from source_fields.buildout_observation_sql import sql_fragment
from test_buildout_current_inventory import CODE, SOURCE, TARGET, setup


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
            scope = prepare_inventory_observation_scope(artifact_path.read_bytes(), manifest, source,
                                                        current_code_sha=CODE, current_git_dirty=False,
                                                        current_target_sha256=TARGET)
            jobkey = artifact_run_identity_from_digests([scope["artifact_sha256"]])[0]
            job = str(uuid.uuid5(ingest._RUN_UUID_NAMESPACE, f"{jobkey}:{source}"))
            old_time = "2026-11-06T00:00:00Z"
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
            conflict_present = next(row for row in rows if row["external_id"] == "dual")
            present_id = str(uuid.uuid4())
            present_raw = json.dumps({"sourceKey": source, "id": "89", "url": conflict_present["source_url"]}).replace("'", "''")
            run(f"INSERT INTO credeals.cre_listings(id,brokerage_id,external_id,source_url,status,raw_data) VALUES ('{present_id}','{bid}','dual','{conflict_present['source_url']}','active','{present_raw}'::jsonb);"
                f"INSERT INTO credeals.cre_source_index(brokerage_id,external_id,source_key,url,observation_present,presence_generation,last_enumerated_at) VALUES ('{bid}','dual','{source}','{conflict_present['source_url']}',true,0,'{old_time}');")
            jobs = [{"slug": source, "discovered": len(artifact["listings"]), "saved": len(rows), "errors": 0, "notes": "synthetic", "finished_at": scope["observed_at"]}]
            tx = ingest.build_sql(rows, jobs, scope["observed_at"], set(), artifact_run_key=jobkey,
                                  finished_at=scope["observed_at"],
                                  buildout_observation_context={"scope": scope, "prior": None})
            with ThreadPoolExecutor(max_workers=2) as pool:
                attempts = [pool.submit(run, tx) for _ in range(2)]
                for attempt in attempts:
                    attempt.result()
            state = run("SELECT external_id || ':' || observation_present::text || ':' || presence_generation::text FROM credeals.cre_source_index ORDER BY external_id;").stdout.splitlines()
            assert state == ["99:false:1", "candidate-ambiguous:true:0", "changed-preimage:true:0", "closed-visible:true:0", "dual:true:0", "duplicate-a:true:0", "duplicate-b:true:0", "invalid-latest:true:0", "missing-member-source:true:0", "old-ambiguous:true:0", "other:false:1"], state
            events = run("SELECT event_type || ':' || source_value FROM credeals.cre_listing_events;").stdout.splitlines()
            assert events == ["disappeared:not_observed_in_complete_enumeration:" + source + ":" + scope["generation_id"]], events
            assert run(f"SELECT raw_data->>'id' FROM credeals.cre_listings WHERE id='{conflict_id}';").stdout.strip() == "98"
            receipt = run(f"SELECT split_part(notes, E'\\nCRE_BUILDOUT_OBSERVATION_V1:', 2) FROM credeals.cre_scrape_jobs WHERE id='{job}';").stdout.strip()
            receipt = json.loads(receipt)
            assert receipt == {"source": source, "generation": scope["generation_id"],
                               "artifact_sha256": scope["artifact_sha256"],
                               "disappeared": 1, "identity_held": 2,
                               "held_external_ids": ["dual", "other"],
                               "applied": [{"listing_id": absent["listing_id"],
                                            "external_id": "99", "native_ids": ["99"]}]}, receipt
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
            assert run("SELECT count(*) FROM credeals.cre_listing_events;").stdout.strip() == "1"
            wrong_job = sql_fragment(scope, [absent], str(uuid.uuid4()), jobkey)
            assert run("BEGIN; SET LOCAL standard_conforming_strings=on;\n" + wrong_job + "\nCOMMIT;", check=False).returncode != 0
            newer_job = str(uuid.uuid4())
            run(f"INSERT INTO credeals.cre_scrape_jobs(id,brokerage_id,status,artifact_run_key,completed_at) VALUES ('{newer_job}','{bid}','completed','newer-synthetic-job','2026-11-08T00:00:00Z');")
            assert run(tx, check=False).returncode != 0
            older = dict(scope, generation_id="older", observed_at="2026-11-05T00:00:00Z")
            assert run("BEGIN; SET LOCAL standard_conforming_strings=on;\n" + sql_fragment(older, [absent], job, jobkey) + "\nCOMMIT;", check=False).returncode != 0
            assert run("SELECT count(*) FROM credeals.cre_listing_events;").stdout.strip() == "1"
            later = "2026-11-08T00:00:00Z"
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
            first = "2026-11-09T00:00:00Z"
            second = "2026-11-10T00:00:00Z"
            third = "2026-11-11T00:00:00Z"
            fourth = "2026-11-12T00:00:00Z"
            fifth = "2026-11-13T00:00:00Z"
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
            scope2 = {**scope, "generation_id": "later-complete", "observed_at": "2026-11-20T00:00:00Z",
                      "artifact_sha256": "f" * 64}
            key2 = artifact_run_identity_from_digests([scope2["artifact_sha256"]])[0]
            jobs2 = [{"slug": source, "discovered": len(artifact["listings"]), "saved": len(rows),
                      "errors": 0, "notes": "synthetic", "finished_at": scope2["observed_at"]}]
            run(ingest.build_sql(rows, jobs2, scope2["observed_at"], set(), artifact_run_key=key2,
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
            returned = "2026-11-21T00:00:00Z"
            ingest_rows([dual_row(["101", "102"], "dual returned again", returned),
                         ingest.to_row({**flat_input, "name": "flat returned"}, {}, returned)], returned)
            states = run("SELECT external_id || ':' || observation_present::text FROM credeals.cre_source_index "
                         "WHERE external_id IN ('dual-live','flat-inventory') ORDER BY external_id;").stdout.splitlines()
            assert states == ["dual-live:true", "flat-inventory:true"], states
            proof_tmp.cleanup()
            print("PASS: actual schema and ingest builder, absent and held peers, concurrent replay, wrong job and stale source job refused, preserved status, actual ingest reappearance")
        finally:
            pass
    finally:
        if created:
            subprocess.run(["docker", "rm", "-f", name], check=True, stdout=subprocess.DEVNULL, timeout=30)


if __name__ == "__main__":
    main()
