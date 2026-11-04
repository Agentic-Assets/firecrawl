"""Offline disposable PostgreSQL proof for JLL partial SQL building blocks."""
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
import cre_ingest as ingest
from cre_jll_partial import (
    identity_probe_sql,
    readback_sql,
    run_identity_probe,
    run_readback,
)


def run(*args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)
    if result.returncode:
        raise RuntimeError(f"fixture command failed: {result.stderr}")
    return result.stdout.strip()


def main():
    name = "jll-partial-proof-" + uuid.uuid4().hex[:12]
    def sql(statement, database="postgres"):
        return run("docker", "exec", "-i", name, "psql", "-U", "postgres", "-d", database, "-X", "-q", "-tA",
                   "-v", "ON_ERROR_STOP=1", input=statement)
    try:
        run("docker", "run", "-d", "--rm", "--name", name, "--network=none", "--cpus=0.5",
            "--memory=512m", "--pids-limit=128", "-e", "POSTGRES_PASSWORD=synthetic-only", "postgres:17")
        for _ in range(100):
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True, check=False)
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"],
                                   capture_output=True, check=False).returncode == 0
            if ready and (logs.stdout + logs.stderr).count("database system is ready to accept connections") >= 2:
                break
            time.sleep(.2)
        else:
            raise RuntimeError("disposable postgres not ready")
        directory = HERE.parent.parent / "sql"
        master = (directory / "000_run_all.sql").read_text()
        master = re.sub(r"^\\i (\S+)\s*$", lambda m: (directory / m[1]).read_text(), master, flags=re.MULTILINE)
        schema = master + "\n" + "\n".join([
            "\\set CRE_APPROVE_LISTING_LIFECYCLE 1",
            "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-5270",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text()])
        sql(schema)
        # Synthetic producer -> artifact worker -> both real admission gates ->
        # guarded CLI-generated SQL -> actual PostgreSQL transaction and
        # exact source-payload readback, on an otherwise empty database.
        import pytest
        from tests.test_jll_partial_members import test_real_collector_to_artifact_worker_keeps_valid_peer_and_held_evidence
        from cre_jll_partial import stage_members
        sql("CREATE DATABASE jll_partial_e2e;")
        sql(schema, "jll_partial_e2e")
        with tempfile.TemporaryDirectory(prefix="jll-partial-e2e-") as e2e_directory:
            root = Path(e2e_directory)
            patch = pytest.MonkeyPatch()
            try:
                test_real_collector_to_artifact_worker_keeps_valid_peer_and_held_evidence(root, patch)
            finally:
                patch.undo()
            source_path = root / "sources" / "jll-investor.json"
            data = json.loads(source_path.read_text())
            staged_e2e = stage_members(data, "jll-test")
            assert sql(identity_probe_sql(staged_e2e), "jll_partial_e2e") == "1|1|[]"
            sql((root / "dryrun" / "ingest.sql").read_text(), "jll_partial_e2e")
            assert sql("SELECT count(*) FROM credeals.cre_listings WHERE status IS NULL AND transaction_type='sale';",
                       "jll_partial_e2e") == "1"
            assert sql("SELECT count(*) FROM credeals.cre_source_index;", "jll_partial_e2e") == "0"
            raw_map = {staged_e2e[0]["external_id"]: data["listings"][0]}
            assert sql(readback_sql(staged_e2e, [], "jll-test", raw_map), "jll_partial_e2e") == "1|[]"
        identity = "006" + "a" * 15
        source_url = "https://invest.jll.com/us/en/listings/office/fixture"
        item = {"sourceKey": "jll-investor", "transactionMode": "sale", "id": identity, "url": source_url,
                "canonicalUrl": source_url, "name": "Synthetic", "transactionType": "Sale (investment)",
                "photos": ["https://example.test/old.jpg"],
                "contactsDetailed": [{"name":"Existing Broker","email":"broker@example.test","title":"Old title","isPrimary":True}],
                "freshnessProvenance": {"generationId": "old"}}
        row = ingest.to_row(item, {}, "2026-11-04T00:00:00Z")
        job = [{"slug": "jll", "discovered": 1, "saved": 1, "errors": 0, "notes": "synthetic"}]
        sql(ingest.build_sql([row], job, "2026-11-04T00:00:00Z", set()))
        sql("UPDATE credeals.cre_listings SET status='inactive', transaction_type='lease', "
            "deleted_at='2026-11-03T01:02:03Z' WHERE external_id='investor:" + identity + "';")
        before_index = sql("SELECT row_to_json(si)::text FROM credeals.cre_source_index si;")
        before_raw = sql("SELECT raw_data::text FROM credeals.cre_listings;")
        item.update(name="New detail", photos=["https://example.test/new.jpg"],
                    contactsDetailed=[{"name":"Existing Broker","email":"broker@example.test","title":"New title","isPrimary":True},
                                      {"name":"Additional Broker","email":"new@example.test","isPrimary":True}],
                    preserveChildCollections=True, detailObservedWithChildPreservation=True,
                    jllInvestorDetail={"id": identity, "customAttributes":{"detailError":"source field, not a failed scrape"},
                                       "scrape": {"rawHtmlLength":0,"markdownLength":0,"linkCount":0}},
                    freshnessProvenance={"generationId":"new","detailScope":"detail_page",
                                         "method":"jll_investor_next_data_detail","cacheDisposition":"live","identityMethod":"provider_listing_id"})
        row = ingest.to_row(item, {}, "2026-11-04T00:01:00Z")
        sql(ingest.build_sql([row], job, "2026-11-04T00:01:00Z", set(),
                             jll_partial_ids=(row["external_id"],)))
        assert sql("SELECT status || '|' || transaction_type || '|' || (deleted_at IS NOT NULL)::text "
                   "FROM credeals.cre_listings;") == "inactive|lease|true"
        assert sql("SELECT row_to_json(si)::text FROM credeals.cre_source_index si;") == before_index
        actual = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings;"))
        for key, value in json.loads(before_raw).items():
            assert actual.get(key) == value
        assert actual["latestJllPartialObservation"] == row["raw_data"]
        assert actual["latestJllPartialObservation"]["freshnessProvenance"]["generationId"] == "new"
        assert sql("SELECT (scraped_at IS NOT NULL)::text FROM credeals.cre_listings;") == "true"
        assert sql("SELECT count(*) FROM credeals.cre_listing_images;") == "2"
        assert sql("SELECT url FROM credeals.cre_listing_images WHERE is_primary;") == "https://example.test/old.jpg"
        assert sql("SELECT title FROM credeals.cre_listing_contacts WHERE email='broker@example.test';") == "Old title"
        assert sql("SELECT count(*) FROM credeals.cre_listing_contacts WHERE email='new@example.test';") == "1"
        assert sql("SELECT count(*) FROM credeals.cre_listing_contacts WHERE is_primary;") == "1"
        # A later ordinary full observation retains the last partial source
        # payload without inventing a longer history.
        full_item = dict(item, jllInvestorDetail={"id":identity, "scrape":{"rawHtmlLength":0,"markdownLength":0,"linkCount":0}},
                         freshnessProvenance={"generationId":"full","detailScope":"detail_page",
                                              "method":"jll_investor_next_data_detail","cacheDisposition":"live","identityMethod":"provider_listing_id"})
        full = ingest.to_row(full_item, {}, "2026-11-04T00:01:10Z")
        sql(ingest.build_sql([full], job, "2026-11-04T00:01:10Z", set()))
        later_raw = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings;"))
        assert later_raw["latestJllPartialObservation"] == row["raw_data"]
        assert later_raw["freshnessProvenance"]["generationId"] == "full"
        # Restore only disposable fixture lifecycle for the collision scenario.
        sql("UPDATE credeals.cre_listings SET status='inactive', deleted_at='2026-11-03T01:02:03Z' "
            "WHERE external_id='investor:" + identity + "';")
        # A partial update does not revive a deleted parent and therefore does
        # not create a collision with an active ordinary JLL counterpart.
        other = ingest.to_row({"sourceKey": "jll", "id": "ordinary", "url": source_url,
                               "transactionMode": "sale", "name": "Ordinary"}, {}, "2026-11-04T00:01:30Z")
        sql(ingest.build_sql([other], job, "2026-11-04T00:01:30Z", set()))
        sql(ingest.build_sql([row], job, "2026-11-04T00:01:40Z", set(),
                             jll_partial_ids=(row["external_id"],)))
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE deleted_at IS NOT NULL;") == "1"
        # The source identity probe holds the conflicting new identity while
        # retaining the deleted existing parent and an unrelated valid peer.
        conflicting_id = "006" + "c" * 15
        free_id = "006" + "d" * 15
        staged = [{"slug": "jll", "external_id": "investor:" + value,
                   "source_url": url, "canonical_url": url, "transaction_type": "sale"}
                  for value, url in ((identity, source_url), (conflicting_id, source_url),
                                     (free_id, source_url + "-free"))]
        probe = sql(identity_probe_sql(staged))
        assert probe == '3|3|["investor:' + conflicting_id + '"]', probe
        # Exercise the actual target-bound read-only probe through the same
        # network-none PostgreSQL, with only transport resolution substituted.
        import copy

        import cre_checkpoint_refresh
        import cre_validate
        from tests.test_jll_partial_members import artifact as source_artifact
        candidate = source_artifact()
        original = candidate["listings"][0]
        candidate["listings"] = []
        for candidate_id, alias in ((conflicting_id, "office/fixture"), (free_id, "office/fixture-free")):
            value = copy.deepcopy(original)
            url = "https://invest.jll.com/us/en/listings/" + alias
            value.update(id=candidate_id, url=url, canonicalUrl=url)
            value["jllInvestorSearchRow"].update(id=candidate_id, alias=alias)
            value["jllInvestorDetail"].update(id=candidate_id, alias=alias)
            value["freshnessProvenance"]["generationId"] = "new"
            candidate["listings"].append(value)
        candidate["totalListings"] = candidate["sources"][0]["listingsCollected"] = 2
        candidate["runMeta"] = {"finishedAt":"2026-11-04T00:01:55Z",
                                "freshness":{"generationId":"new"}}
        prior = (cre_validate._run_psql_script, ingest.load_db_url,
                 ingest.database_target_fingerprint_from_url, ingest.find_psql,
                 cre_checkpoint_refresh.git_identity)
        try:
            cre_validate._run_psql_script = lambda _psql, _url, query, **kw: subprocess.run(
                ["docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-A", "-F", "\t",
                 "-P", "footer=off", "-v", "ON_ERROR_STOP=1"], input=query, capture_output=True, text=True, check=False)
            ingest.load_db_url = lambda _env: ("synthetic", None)
            ingest.database_target_fingerprint_from_url = lambda _url: {"value":"b"*64}
            ingest.find_psql = lambda: "synthetic"
            cre_checkpoint_refresh.git_identity = lambda: ("a"*40, False)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "artifact.json"
                path.write_text(json.dumps(candidate))
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                actual_probe = run_identity_probe(path, digest, "new", "a"*40, "b"*64, None)
                assert actual_probe["held"] == ["investor:" + conflicting_id]
        finally:
            (cre_validate._run_psql_script, ingest.load_db_url, ingest.database_target_fingerprint_from_url,
             ingest.find_psql, cre_checkpoint_refresh.git_identity) = prior
        # A new investor identity sharing that active URL is still refused.
        conflict_item = dict(item, id=conflicting_id)
        conflict = ingest.to_row(conflict_item, {}, "2026-11-04T00:01:50Z")
        try:
            sql(ingest.build_sql([conflict], job, "2026-11-04T00:01:50Z", set(),
                                 jll_partial_ids=(conflict["external_id"],)))
        except RuntimeError as exc:
            assert "URL collision" in str(exc)
        else:
            raise AssertionError("active collision accepted")
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='investor:" + conflicting_id + "';") == "0"
        free_item = dict(item, id=free_id, url=source_url + "-free", canonicalUrl=source_url + "-free",
                         jllInvestorDetail=dict(item["jllInvestorDetail"], id=free_id))
        free = ingest.to_row(free_item, {}, "2026-11-04T00:01:55Z")
        claims = {"staged": staged, "held": ["investor:" + conflicting_id]}
        ids = tuple(member["external_id"] for member in staged)
        sql(ingest.build_sql([row, conflict, free], job, "2026-11-04T00:01:55Z", set(),
                             jll_partial_ids=ids, jll_partial_claims=claims))
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='investor:" + conflicting_id + "';") == "0"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='investor:" + free_id + "';") == "1"
        expected_raw = {value["external_id"]: value["raw_data"] for value in (row, conflict, free)}
        assert sql(readback_sql(staged, claims["held"], "new", expected_raw)) == "2|[]"
        assert sql(readback_sql(staged, claims["held"], "other", expected_raw)).startswith("2|[")
        changed_raw = dict(expected_raw)
        changed_raw[free["external_id"]] = dict(changed_raw[free["external_id"]], name="tampered")
        assert sql(readback_sql(staged, claims["held"], "new", changed_raw)).startswith("2|[")
        # Exercise the actual readback function against this isolated database,
        # replacing only its transport and synthetic target resolution.
        import cre_validate
        old_run = cre_validate._run_psql_script
        old_load = ingest.load_db_url
        old_fingerprint = ingest.database_target_fingerprint_from_url
        old_find = ingest.find_psql
        try:
            cre_validate._run_psql_script = lambda _psql, _url, query, **kw: subprocess.run(
                ["docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-A", "-F", "\t",
                 "-P", "footer=off", "-v", "ON_ERROR_STOP=1"], input=query, capture_output=True, text=True, check=False)
            ingest.load_db_url = lambda _env: ("synthetic", None)
            ingest.database_target_fingerprint_from_url = lambda _url: {"value": "b"*64}
            ingest.find_psql = lambda: "synthetic"
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "artifact.json"
                path.write_text(json.dumps({"listings": [value["raw_data"] for value in (row, conflict, free)],
                                            "brokers": [], "runMeta": {"finishedAt":"2026-11-04T00:01:55Z"}}))
                bound = dict(claims, binding={"artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                            "generation_id":"new", "staged_sha256":"c"*64})
                exact = run_readback(path, bound, expected_database_target_sha256="b"*64, env_file=None)
                assert exact["ok"] is True and exact["expected"] == 2 and exact["held"] == claims["held"]
                bound["binding"]["generation_id"] = "other"
                mismatch = run_readback(path, bound, expected_database_target_sha256="b"*64, env_file=None)
                assert mismatch["ok"] is False and len(mismatch["invalid"]) == 2
        finally:
            cre_validate._run_psql_script = old_run
            ingest.load_db_url = old_load
            ingest.database_target_fingerprint_from_url = old_fingerprint
            ingest.find_psql = old_find
        # The sealed probe cannot silently omit a now-conflicting identity.
        stale_claims = {"staged": staged, "held": []}
        try:
            sql(ingest.build_sql([row, conflict, free], job, "2026-11-04T00:01:55Z", set(),
                                 jll_partial_ids=ids, jll_partial_claims=stale_claims))
        except RuntimeError:
            pass
        else:
            raise AssertionError("stale identity partition accepted")
        forged_claims = {"staged": staged, "held": sorted(["investor:" + conflicting_id, "investor:" + free_id])}
        try:
            sql(ingest.build_sql([row, conflict, free], job, "2026-11-04T00:01:55Z", set(),
                                 jll_partial_ids=ids, jll_partial_claims=forged_claims))
        except RuntimeError as exc:
            assert "identity partition changed" in str(exc)
        else:
            raise AssertionError("forged held member accepted")
        new_id = "006" + "b" * 15
        item.update(id=new_id, url=source_url+"-new", canonicalUrl=source_url+"-new")
        item["jllInvestorDetail"]["id"] = new_id
        new = ingest.to_row(item, {}, "2026-11-04T00:02:00Z")
        sql(ingest.build_sql([new], job, "2026-11-04T00:02:00Z", set(),
                             jll_partial_ids=(new["external_id"],)))
        new_state = sql("SELECT COALESCE(status, '<null>') || '|' || COALESCE(transaction_type, '<null>') FROM credeals.cre_listings "
                        "WHERE external_id='investor:" + new_id + "';")
        assert new_state == "<null>|sale", new_state
        assert sql("SELECT count(*) FROM credeals.cre_source_index WHERE external_id='investor:" + new_id + "';") == "0"
        first_raw = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='investor:" + new_id + "';"))
        assert first_raw["latestJllPartialObservation"] == new["raw_data"]
        next_item = dict(full_item, id=new_id, url=source_url+"-new", canonicalUrl=source_url+"-new",
                         jllInvestorDetail={"id":new_id, "scrape":{"rawHtmlLength":0,"markdownLength":0,"linkCount":0}})
        next_full = ingest.to_row(next_item, {}, "2026-11-04T00:03:00Z")
        sql(ingest.build_sql([next_full], job, "2026-11-04T00:03:00Z", set()))
        next_raw = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='investor:" + new_id + "';"))
        assert next_raw["latestJllPartialObservation"] == new["raw_data"]

        # A prior failed top-level observation plus a later provider field
        # named detailError must not erase a valid partial observation.
        error_id = "006" + "e" * 15
        error_url = source_url + "-error-history"
        safe_detail = {"id":error_id, "scrape":{"rawHtmlLength":0,"markdownLength":0,"linkCount":0}}
        error_item = dict(item, id=error_id, url=error_url, canonicalUrl=error_url,
                          jllInvestorDetail=safe_detail)
        initial = ingest.to_row(error_item, {}, "2026-11-04T00:04:00Z")
        sql(ingest.build_sql([initial], job, "2026-11-04T00:04:00Z", set()))
        sql("UPDATE credeals.cre_listings SET raw_data="
            "'{\"sourceKey\":\"jll-investor\",\"detailError\":\"prior failure\"}'::jsonb "
            "WHERE external_id='investor:" + error_id + "';")
        partial = ingest.to_row(error_item, {}, "2026-11-04T00:05:00Z")
        sql(ingest.build_sql([partial], job, "2026-11-04T00:05:00Z", set(),
                             jll_partial_ids=(partial["external_id"],)))
        nested_detail = dict(safe_detail, customAttributes={"detailError":"provider field"})
        full_nested = ingest.to_row(dict(error_item, jllInvestorDetail=nested_detail), {}, "2026-11-04T00:06:00Z")
        sql(ingest.build_sql([full_nested], job, "2026-11-04T00:06:00Z", set()))
        after_nested = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='investor:" + error_id + "';"))
        assert after_nested.get("latestJllPartialObservation") == partial["raw_data"]
        assert after_nested["jllInvestorDetail"]["customAttributes"]["detailError"] == "provider field"
        before_clock = sql("SELECT scraped_at::text FROM credeals.cre_listings WHERE external_id='investor:" + error_id + "';")
        actual_failure = ingest.to_row(dict(error_item, detailError="actual failed detail"), {}, "2026-11-04T00:07:00Z")
        sql(ingest.build_sql([actual_failure], job, "2026-11-04T00:07:00Z", set()))
        failed_raw = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='investor:" + error_id + "';"))
        assert failed_raw["latestJllPartialObservation"] == partial["raw_data"]
        assert sql("SELECT scraped_at::text FROM credeals.cre_listings WHERE external_id='investor:" + error_id + "';") == before_clock
        # Unrelated sources do not gain JLL partial retention semantics.
        other = ingest.to_row({"sourceKey":"svn", "transactionMode":"sale", "id":"unrelated",
                               "url":"https://example.test/unrelated",
                               "latestJllPartialObservation":{"sourceKey":"jll-investor"}}, {}, "2026-11-04T00:08:00Z")
        other_job = [{"slug":"svn","discovered":1,"saved":1,"errors":0,"notes":"synthetic"}]
        sql(ingest.build_sql([other], other_job, "2026-11-04T00:08:00Z", set()))
        ordinary = ingest.to_row({"sourceKey":"svn", "transactionMode":"sale", "id":"unrelated",
                                  "url":"https://example.test/unrelated", "name":"ordinary"}, {}, "2026-11-04T00:09:00Z")
        sql(ingest.build_sql([ordinary], other_job, "2026-11-04T00:09:00Z", set()))
        unrelated_raw = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings WHERE external_id='unrelated';"))
        assert "latestJllPartialObservation" not in unrelated_raw
        print("JLL partial SQL building blocks passed")
    finally:
        subprocess.run(["docker", "stop", "--time", "2", name], capture_output=True, check=False)


if __name__ == "__main__":
    main()
