"""Disposable real-schema proof for guarded, unconnected Colliers partial SQL."""
import base64
import copy
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import cre_colliers_partial as partial
import cre_ingest as ingest
from cre_colliers_partial import (
    build_partial_sql,
    identity_probe_sql,
    stage_detail_record,
)
from tests.test_colliers_partial_record import sample


def command(*args, text=None, check=True):
    return subprocess.run(args, input=text, text=True, capture_output=True, check=check, timeout=120)


def main():
    name = "colliers-window-" + uuid.uuid4().hex[:12]

    def sql(query):
        result = command("docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA",
                         "-v", "ON_ERROR_STOP=1", text=query, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip()

    try:
        command("docker", "run", "-d", "--rm", "--name", name, "--network=none", "--cpus=0.5",
                "--memory=512m", "--pids-limit=128", "--tmpfs", "/var/lib/postgresql/data:rw,size=256m",
                "-e", "POSTGRES_PASSWORD=synthetic-only", "postgres:17")
        for _ in range(100):
            logs = command("docker", "logs", name, check=False)
            ready = command("docker", "exec", name, "pg_isready", "-U", "postgres", check=False).returncode == 0
            if ready and (logs.stdout + logs.stderr).count("database system is ready to accept connections") >= 2:
                break
            time.sleep(.2)
        else:
            raise RuntimeError("disposable fixture not ready")
        directory = HERE.parent.parent / "sql"
        master = (directory / "000_run_all.sql").read_text()
        master = re.sub(r"^\\i (\S+)\s*$", lambda m: (directory / m[1]).read_text(), master, flags=re.MULTILINE)
        schema = master + "\n" + "\n".join([
            "\\set CRE_APPROVE_LISTING_LIFECYCLE 1", "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-5261",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text()])
        sql(schema)
        # Exercise the actual native replay, source/aggregate claims and CLI SQL
        # generator before applying its output to this disposable real schema.
        from tests.test_colliers_partial_record import (
            test_source_bytes_through_node_replay_into_python_staging,
        )
        with tempfile.TemporaryDirectory(prefix="colliers-e2e-") as temporary:
            root = Path(temporary)
            test_source_bytes_through_node_replay_into_python_staging(root)
            artifact = json.loads((root / "artifact").read_text())
            records = partial.unpack_window_records(artifact)
            stage = [stage_detail_record(record, "gen", 1, "2026-11-04T15:00:00Z",
                     "2026-11-04T16:00:00Z", artifact["detailFinishedAt"]) for record in records]
            preimage = [{"external_id": "native-e2e", "listing": None, "source_index": None}]
            readback, held = partial.partial_readback_sql(records, stage, [], preimage, "gen", 1,
                "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", artifact["detailFinishedAt"], "e" * 32)
            assert not held
            sql((root / "dry" / "ingest.sql").read_text())
            assert sql(readback) == "1|1|0"
            assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='native-e2e' AND status IS NULL") == "1"
            import cre_checkpoint_refresh as refresh
            import cre_validate
            import pytest

            claims = json.loads((root / "gen" / "colliers-partial" / "claims.json").read_text())
            source = artifact["source"]
            receipt = {"generation": "gen", "generation_started_at": "2026-11-04T14:00:00Z", "attempt": 1,
                       "started_at": "2026-11-04T15:00:00Z", "finished_at": "2026-11-04T16:00:00Z",
                       "bytes": source["bytes"], "sha256": source["sha256"], "main_sha256": "c" * 64,
                       "producer_sha": "a" * 40, "database_target_sha256": "b" * 64}
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(partial, "revalidate_source_receipt", lambda *_: receipt)
                patch.setattr(refresh, "git_identity", lambda: ("a" * 40, False))
                patch.setattr(ingest, "load_db_url", lambda _: ("synthetic", None))
                patch.setattr(ingest, "database_target_fingerprint_from_url", lambda _: {"value": "b" * 64})
                patch.setattr(ingest, "find_psql", lambda: "synthetic")
                patch.setattr(cre_validate, "_run_psql_script", lambda _psql, _url, script, timeout:
                    command("docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-A",
                            "-F", "\t", "-P", "footer=off", "-v", "ON_ERROR_STOP=1", text=script, check=False))
                actual = partial.run_partial_readback(root / "gen" / "colliers-partial" / "window-1-artifact.json",
                    claims, code_sha="a" * 40, target="b" * 64, attempt_id="e" * 32, env_file=None)
            assert actual["ok"] is True and actual["outcome"] == "exact_commit" and actual["expected"] == 1
        first = sample()
        url = first["listing"]["url"]
        old = {"sourceKey": "colliers", "transactionMode": "sale", "id": "42",
               "url": url, "name": "Old title", "transactionType": "Investment Sale",
               "photos": ["https://example.test/old.jpg"]}
        jobs = [{"slug": "colliers", "discovered": 1, "saved": 1, "errors": 0, "notes": "synthetic"}]
        sql(ingest.build_sql([ingest.to_row(old, {}, "2026-11-04T14:00:00Z")],
                             jobs, "2026-11-04T14:00:00Z", set()))
        sql("ALTER TABLE credeals.cre_listings DISABLE TRIGGER trg_cre_listings_updated_at; UPDATE credeals.cre_listings SET updated_at='2026-11-04T14:00:00Z'; ALTER TABLE credeals.cre_listings ENABLE TRIGGER trg_cre_listings_updated_at;")
        before_index = sql("SELECT row_to_json(si)::text FROM credeals.cre_source_index si ORDER BY external_id;")
        before_image = sql("SELECT row_to_json(i)::text FROM credeals.cre_listing_images i ORDER BY id;")
        first["listing"].update(name="New title", city="Source City", state="TX", units=10)
        second = copy.deepcopy(first)
        second["decodedDetail"] = json.dumps({"ProjectSummary": {"ProjectId": "43"}})
        second["nativeBase64"] = base64.b64encode(second["decodedDetail"].encode()).decode()
        second["source"].update(mapProjectId="43",
            decodedDetailSha256=hashlib.sha256(second["decodedDetail"].encode()).hexdigest(),
            nativeSha256=hashlib.sha256(second["decodedDetail"].encode()).hexdigest(),
            detailTokenSha256=hashlib.sha256(b"new").hexdigest())
        second["listing"].update(id="43", url=url.replace("fixture", "new"),
            colliersSalesTrackerDetail={"projectId": "43"},
            colliersPartialEvidence={"schema": "colliers-partial-detail-v1", **second["source"]})
        items = [first, second]
        staged = [stage_detail_record(item, "gen", 1, "2026-11-04T15:00:00Z",
                  "2026-11-04T16:00:00Z", "2026-11-04T16:02:00Z") for item in items]
        cross = copy.deepcopy(staged)
        cross[0]["canonical_url"] = cross[1]["source_url"]
        cross_probe = sql(identity_probe_sql(cross)).split("|", 3)
        assert json.loads(cross_probe[2]) == ["42", "43"], cross_probe
        cross_persisted = [copy.deepcopy(staged[1])]
        cross_persisted[0]["canonical_url"] = staged[0]["source_url"]
        persisted_probe = sql(identity_probe_sql(cross_persisted)).split("|", 3)
        assert json.loads(persisted_probe[2]) == ["43"], persisted_probe
        probe = sql(identity_probe_sql(staged)).split("|", 3)
        assert probe[:3] == ["2", "2", "[]"], probe
        job_id = str(uuid.uuid4())
        output, holds = build_partial_sql(items, staged, json.loads(probe[2]), json.loads(probe[3]),
            "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z",
            "2026-11-04T16:02:00Z", job_id)
        assert not holds
        readback, readback_holds = partial.partial_readback_sql(items, staged, json.loads(probe[2]), json.loads(probe[3]),
            "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", "2026-11-04T16:02:00Z", job_id)
        assert not readback_holds
        assert sql(readback) == "0|2|2"
        sql(output)
        assert sql(readback) == "1|2|0"
        assert sql("BEGIN; UPDATE credeals.cre_listings SET raw_data=raw_data || '{\"unexpected\":true}'::jsonb WHERE external_id='42'; " + readback + "; ROLLBACK;") == "1|2|1"
        parent = json.loads(sql("SELECT json_build_array(title,city,state,status,raw_data)::text FROM credeals.cre_listings WHERE external_id='42';"))
        assert parent[:4] == ["Old title", "Source City", "TX", "active"]
        assert sql("SELECT units FROM credeals.cre_listings WHERE external_id='42';") == "10"
        assert parent[4]["sourceKey"] == "colliers"
        assert parent[4]["colliersNativeObservations"]["entries"][0]["nativeBase64"] == first["nativeBase64"]
        assert sql("SELECT row_to_json(si)::text FROM credeals.cre_source_index si ORDER BY external_id;") == before_index
        assert sql("SELECT row_to_json(i)::text FROM credeals.cre_listing_images i ORDER BY id;") == before_image
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='43' AND status IS NULL;") == "1"
        assert sql("SELECT count(*) FROM credeals.v_cre_listings_full WHERE external_id='43';") == "1"
        assert sql("SELECT count(*) FROM credeals.v_cre_active_for_sale WHERE external_id='43';") == "0"
        # A later ordinary full ingest must retain the exact history member.
        retained = parent[4]["colliersNativeObservations"]
        full = {**old, "name": "Full source title", "city": "Full City"}
        sql(ingest.build_sql([ingest.to_row(full, {}, "2026-11-04T16:05:00Z")],
                             jobs, "2026-11-04T16:05:00Z", set()))
        post_full = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='42';"))
        assert post_full["colliersNativeObservations"] == retained
        collision = {**full, "colliersNativeObservations": {"unknown": "do not overwrite"}}
        try:
            sql(ingest.build_sql([ingest.to_row(collision, {}, "2026-11-04T16:06:00Z")],
                                 jobs, "2026-11-04T16:06:00Z", set()))
            raise AssertionError("incoming reserved member was not rejected")
        except RuntimeError as exc:
            assert "Colliers native evidence key collision" in str(exc)
        assert json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='42';")) == post_full

        # A newer observation for the previously created NULL-status parent is allowed.
        sql("ALTER TABLE credeals.cre_listings DISABLE TRIGGER trg_cre_listings_updated_at; UPDATE credeals.cre_listings SET updated_at='2026-11-04T16:01:30Z' WHERE external_id='43'; ALTER TABLE credeals.cre_listings ENABLE TRIGGER trg_cre_listings_updated_at;")
        second["source"]["detailObservedAt"] = "2026-11-04T16:03:00Z"
        second["listing"]["detailObservedAt"] = "2026-11-04T16:03:00Z"
        second["listing"]["colliersPartialEvidence"] = {"schema": "colliers-partial-detail-v1", **second["source"]}
        next_staged = [stage_detail_record(second, "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", "2026-11-04T16:04:00Z")]
        next_probe = sql(identity_probe_sql(next_staged)).split("|", 3)
        assert next_probe[:3] == ["1", "1", "[]"], next_probe
        next_sql, holds = build_partial_sql([second], next_staged, json.loads(next_probe[2]), json.loads(next_probe[3]),
            "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", "2026-11-04T16:04:00Z", str(uuid.uuid4()))
        assert not holds
        sql(next_sql)
        assert sql("SELECT jsonb_array_length(raw_data#>'{colliersNativeObservations,entries}') FROM credeals.cre_listings WHERE external_id='43';") == "1"
        # A bounded history probe holds a large existing member but admits a new peer.
        sql("ALTER TABLE credeals.cre_listings DISABLE TRIGGER trg_cre_listings_updated_at; UPDATE credeals.cre_listings SET updated_at='2026-11-04T16:02:00Z' WHERE external_id='43'; ALTER TABLE credeals.cre_listings ENABLE TRIGGER trg_cre_listings_updated_at;")
        bounded_peer = copy.deepcopy(second)
        bounded_peer["decodedDetail"] = json.dumps({"ProjectSummary": {"ProjectId": "47"}})
        bounded_peer["nativeBase64"] = base64.b64encode(bounded_peer["decodedDetail"].encode()).decode()
        bounded_peer["source"].update(mapProjectId="47",
            decodedDetailSha256=hashlib.sha256(bounded_peer["decodedDetail"].encode()).hexdigest(),
            nativeSha256=hashlib.sha256(bounded_peer["decodedDetail"].encode()).hexdigest(),
            detailTokenSha256=hashlib.sha256(b"bounded").hexdigest())
        bounded_peer["listing"].update(id="47", url=url.replace("fixture", "bounded"),
            colliersSalesTrackerDetail={"projectId": "47"},
            colliersPartialEvidence={"schema": "colliers-partial-detail-v1", **bounded_peer["source"]})
        bounded_items = [second, bounded_peer]
        bounded_staged = [stage_detail_record(item, "gen", 1, "2026-11-04T15:00:00Z",
                          "2026-11-04T16:00:00Z", "2026-11-04T16:04:00Z") for item in bounded_items]
        prior43 = sql("SELECT row_to_json(t)::text FROM credeals.cre_listings t WHERE external_id='43';")
        old_limit = partial.MAX_HISTORY_PROBE_BYTES
        try:
            partial.MAX_HISTORY_PROBE_BYTES = 1
            bounded_probe = sql(identity_probe_sql(bounded_staged)).split("|", 3)
            bounded_preimage = json.loads(bounded_probe[3])
            prior = next(row["listing"] for row in bounded_preimage if row["external_id"] == "43")
            assert prior["native_history_available"] is False and prior["native_history"] is None
            assert "raw_data" not in prior and len(prior["raw_data_sha256"]) == 64
            bounded_sql, bounded_holds = build_partial_sql(bounded_items, bounded_staged, json.loads(bounded_probe[2]),
                bounded_preimage, "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z",
                "2026-11-04T16:04:00Z", str(uuid.uuid4()))
            assert list(bounded_holds) == ["43"]
            sql(bounded_sql)
        finally:
            partial.MAX_HISTORY_PROBE_BYTES = old_limit
        assert sql("SELECT row_to_json(t)::text FROM credeals.cre_listings t WHERE external_id='43';") == prior43
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='47' AND status IS NULL;") == "1"

        # An unrecognized reserved member holds just that identity, not a valid peer.
        unknown_old = {**old, "id": "44", "url": url.replace("fixture", "unknown")}
        sql(ingest.build_sql([ingest.to_row(unknown_old, {}, "2026-11-04T14:00:00Z")],
                             jobs, "2026-11-04T14:00:00Z", set()))
        sql("UPDATE credeals.cre_listings SET raw_data=raw_data || '{\"colliersNativeObservations\":{}}'::jsonb WHERE external_id='44';")
        sql("ALTER TABLE credeals.cre_listings DISABLE TRIGGER trg_cre_listings_updated_at; UPDATE credeals.cre_listings SET updated_at='2026-11-04T14:00:00Z' WHERE external_id='44'; ALTER TABLE credeals.cre_listings ENABLE TRIGGER trg_cre_listings_updated_at;")
        held_before = sql("SELECT row_to_json(t)::text FROM credeals.cre_listings t WHERE external_id='44';")
        def make_item(identifier, token):
            item = copy.deepcopy(first)
            item["decodedDetail"] = json.dumps({"ProjectSummary": {"ProjectId": identifier}})
            item["nativeBase64"] = base64.b64encode(item["decodedDetail"].encode()).decode()
            item["source"].update(mapProjectId=identifier,
                decodedDetailSha256=hashlib.sha256(item["decodedDetail"].encode()).hexdigest(),
                nativeSha256=hashlib.sha256(item["decodedDetail"].encode()).hexdigest(),
                detailTokenSha256=hashlib.sha256(token.encode()).hexdigest())
            item["listing"].update(id=identifier, url=url.replace("fixture", token),
                colliersSalesTrackerDetail={"projectId": identifier},
                colliersPartialEvidence={"schema": "colliers-partial-detail-v1", **item["source"]})
            return item
        peers = [make_item("44", "unknown"), make_item("45", "peer")]
        peers[1]["decodedDetail"] = json.dumps({"ProjectSummary": {"ProjectId": "45", "Address": {"City": "Unsafe\u0000Town"}}})
        peers[1]["nativeBase64"] = base64.b64encode(peers[1]["decodedDetail"].encode()).decode()
        peers[1]["source"]["nativeSha256"] = hashlib.sha256(peers[1]["decodedDetail"].encode()).hexdigest()
        peers[1]["source"]["decodedDetailSha256"] = peers[1]["source"]["nativeSha256"]
        peers[1]["listing"]["city"] = "Unsafe\u0000Town"
        peers[1]["listing"]["colliersPartialEvidence"] = {"schema": "colliers-partial-detail-v1", **peers[1]["source"]}
        peer_staged = [stage_detail_record(item, "gen", 1, "2026-11-04T15:00:00Z",
                       "2026-11-04T16:00:00Z", "2026-11-04T16:02:00Z") for item in peers]
        peer_probe = sql(identity_probe_sql(peer_staged)).split("|", 3)
        assert peer_probe[:3] == ["2", "2", "[]"], peer_probe
        peer_sql, peer_holds = build_partial_sql(peers, peer_staged, json.loads(peer_probe[2]), json.loads(peer_probe[3]),
            "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", "2026-11-04T16:02:00Z", str(uuid.uuid4()))
        assert list(peer_holds) == ["44"]
        sql(peer_sql)
        assert sql("SELECT row_to_json(t)::text FROM credeals.cre_listings t WHERE external_id='44';") == held_before
        # An all-held window records a partial zero-save job, leaving the row
        # unchanged so its settled cursor can reach later source positions.
        only_held_probe = sql(identity_probe_sql([peer_staged[0]])).split("|", 3)
        zero_job = str(uuid.uuid4())
        zero_sql, zero_holds = build_partial_sql([peers[0]], [peer_staged[0]], json.loads(only_held_probe[2]),
            json.loads(only_held_probe[3]), "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z",
            "2026-11-04T16:02:00Z", zero_job)
        assert list(zero_holds) == ["44"]
        sql(zero_sql)
        assert sql("SELECT status||'|'||listings_saved FROM credeals.cre_scrape_jobs WHERE id='" + zero_job + "'::uuid;") == "partial|0"
        assert sql("SELECT row_to_json(t)::text FROM credeals.cre_listings t WHERE external_id='44';") == held_before
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='45' AND status IS NULL AND city IS NULL;") == "1"
        stored_native = sql("SELECT raw_data#>>'{colliersNativeObservations,entries,0,nativeBase64}' FROM credeals.cre_listings WHERE external_id='45';")
        assert stored_native == peers[1]["nativeBase64"]
        # A conflicting prior location prevents mixing in any incoming component.
        location_old = {**old, "id": "46", "url": url.replace("fixture", "location"), "state": "FL"}
        sql(ingest.build_sql([ingest.to_row(location_old, {}, "2026-11-04T14:00:00Z")],
                             jobs, "2026-11-04T14:00:00Z", set()))
        sql("ALTER TABLE credeals.cre_listings DISABLE TRIGGER trg_cre_listings_updated_at; UPDATE credeals.cre_listings SET updated_at='2026-11-04T14:00:00Z' WHERE external_id='46'; ALTER TABLE credeals.cre_listings ENABLE TRIGGER trg_cre_listings_updated_at;")
        location_item = make_item("46", "location")
        location_staged = [stage_detail_record(location_item, "gen", 1, "2026-11-04T15:00:00Z",
                           "2026-11-04T16:00:00Z", "2026-11-04T16:02:00Z")]
        location_probe = sql(identity_probe_sql(location_staged)).split("|", 3)
        location_sql, location_holds = build_partial_sql([location_item], location_staged, json.loads(location_probe[2]),
            json.loads(location_probe[3]), "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z",
            "2026-11-04T16:02:00Z", str(uuid.uuid4()))
        assert not location_holds
        sql(location_sql)
        assert json.loads(sql("SELECT json_build_array(city,state)::text FROM credeals.cre_listings WHERE external_id='46';")) == [None, "FL"]
        # A cooperating writer holding the normal lifecycle lock wins first;
        # the waiting partial transaction must reject its stale preimage.
        sql("ALTER TABLE credeals.cre_listings DISABLE TRIGGER trg_cre_listings_updated_at; UPDATE credeals.cre_listings SET updated_at='2026-11-04T14:00:00Z' WHERE external_id='45'; ALTER TABLE credeals.cre_listings ENABLE TRIGGER trg_cre_listings_updated_at;")
        race_staged = [peer_staged[1]]
        race_probe = sql(identity_probe_sql(race_staged)).split("|", 3)
        assert race_probe[:3] == ["1", "1", "[]"], race_probe
        race_job = str(uuid.uuid4())
        race_sql, holds = build_partial_sql([peers[1]], race_staged, json.loads(race_probe[2]), json.loads(race_probe[3]),
            "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", "2026-11-04T16:02:00Z", race_job)
        assert not holds
        psql = ["docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1"]
        writer = subprocess.Popen(psql, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        writer.stdin.write("BEGIN;" + ingest.lifecycle_transaction_lock_sql() +
                           "UPDATE credeals.cre_listings SET status='withdrawn' WHERE external_id='45'; SELECT pg_sleep(3); COMMIT;")
        writer.stdin.close()
        writer.stdin = None
        for _ in range(100):
            if int(sql("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted;")):
                break
            time.sleep(.02)
        else:
            raise AssertionError("cooperating writer did not acquire lifecycle lock")
        follower = subprocess.Popen(psql, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        follower.stdin.write(race_sql)
        follower.stdin.close()
        follower.stdin = None
        for _ in range(100):
            if int(sql("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted;")):
                break
            time.sleep(.02)
        else:
            raise AssertionError("partial transaction did not wait on lifecycle lock")
        writer_out, writer_err = writer.communicate(timeout=15)
        assert writer.returncode == 0, (writer_out, writer_err)
        _, follower_err = follower.communicate(timeout=15)
        assert follower.returncode != 0 and "Colliers partial identity partition changed" in follower_err
        assert sql("SELECT status FROM credeals.cre_listings WHERE external_id='45';") == "withdrawn"
        assert sql("SELECT count(*) FROM credeals.cre_scrape_jobs WHERE id='" + race_job + "'::uuid;") == "0"
        print("disposable real-schema bounded Colliers partial proof passed")
    finally:
        command("docker", "rm", "-f", name, check=False)


if __name__ == "__main__":
    main()
