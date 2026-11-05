"""Network-none disposable PG contract for Franklin partial SQL."""
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
from run_franklin_site_lifecycle_postgres import run
from test_franklin_site_partial import mixed

import cre_franklin_site_sql as partial
from cre_franklin_site_partial import derive


def item(identity, slug, stamp, text):
    attrs = {"data-listing-id": identity, "data-offer-type": "FOR_SALE"}
    return {"sourceKey": "franklin-street", "id": f"franklin-site:sale:{identity}",
            "url": f"https://franklinst.com/properties/for-sale/{slug}",
            "canonicalUrl": f"https://franklinst.com/properties/for-sale/{slug}",
            "transactionMode": "sale", "name": "First", "state": "TX", "latitude": "30.0",
            "longitude": "-97.0", "preserveChildCollections": True, "franklinSiteGenerationId": "synthetic-run",
            "franklinSiteObservation": {"coverage": "partial_unknown",
                "sourceUrl": "https://franklinst.com/properties/for-sale/",
                "observedAt": stamp, "decodedHtmlSha256": "a" * 64,
                "card": {"attributes": attrs, "html": text}}}


def binding(rows):
    return {"artifact_sha256": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
            "attempt_id": "a" * 32, "generation_id": "synthetic-run", "producer_sha": "b" * 40,
            "database_target_sha256": "c" * 64,
            "generation_started_at": "2026-11-04T11:59:00Z",
            "started_at": "2026-11-04T12:00:00Z", "finished_at": "2026-11-04T13:00:00Z"}


def write_sql(rows, held):
    return partial.build_sql(rows, held, binding(rows))


def readback_sql(rows, held):
    return partial.readback_sql(rows, held, binding(rows))


def main():
    name = "franklin-site-write-" + uuid.uuid4().hex[:10]
    def sql(statement):
        return run("docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA",
                   "-v", "ON_ERROR_STOP=1", input=statement)

    try:
        run("docker", "run", "-d", "--rm", "--name", name, "--network=none", "--cpus=0.5",
            "--memory=512m", "--pids-limit=128", "-e", "POSTGRES_PASSWORD=synthetic-only", "postgres:17")
        for _ in range(100):
            logs = subprocess.run(["docker", "logs", name], check=False, capture_output=True, text=True)
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"],
                                   check=False, capture_output=True).returncode == 0
            if ready and (logs.stdout + logs.stderr).count("database system is ready to accept connections") >= 2:
                break
            time.sleep(.2)
        else:
            raise RuntimeError("disposable postgres not ready")
        directory = HERE.parent.parent / "sql"
        master = (directory / "000_run_all.sql").read_text()
        master = re.sub(r"^\\i (\S+)\s*$", lambda m: (directory / m[1]).read_text(), master, flags=re.MULTILINE)
        sql(master + "\n" + "\n".join([
            "\\set CRE_APPROVE_LISTING_LIFECYCLE 1",
            "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-5257",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text()]))
        identity = "12345678-1234-5123-8123-123456789abc"
        first = item(identity, "a", "2026-11-04T12:00:01Z", "Subject to Offer")
        assert json.loads(sql(partial.identity_probe_sql([first]))) == {"staged": 1, "joined": 1, "held": []}
        assert json.loads(sql(write_sql([first], [])).splitlines()[-1])["held"] == []
        # Real schema defaults to active; a partial site card explicitly stores
        # unknown status and is excluded by the shipped on-market views.
        assert sql("SELECT status IS NULL FROM credeals.cre_listings WHERE external_id=" + partial.sql_lit(first["id"])) == "t"
        assert sql("SELECT count(*) FROM credeals.v_cre_active_for_sale WHERE external_id=" + partial.sql_lit(first["id"])) == "0"
        assert sql("SELECT count(*) FROM credeals.v_cre_active_for_lease WHERE external_id=" + partial.sql_lit(first["id"])) == "0"
        active = item("dddddddd-3333-5333-8333-333333333333", "explicit-active", "2026-11-04T12:00:01Z", "active evidence")
        active["franklinSiteObservation"]["card"]["attributes"].update({
            "data-status": "ACTIVE", "data-listing-address": "12921 Camino Real, Niederwald, TX, Kyle, TX 78640"})
        assert json.loads(sql(write_sql([active], [])).splitlines()[-1])["held"] == []
        active_row = json.loads(sql("SELECT json_build_object('status', status, 'address', address, 'city', city, 'zip', zip) FROM credeals.cre_listings WHERE external_id=" + partial.sql_lit(active["id"])))
        assert active_row == {"status": "active", "address": "12921 Camino Real, Niederwald, TX, Kyle, TX 78640", "city": None, "zip": None}
        assert sql("SELECT count(*) FROM credeals.v_cre_active_for_sale WHERE external_id=" + partial.sql_lit(active["id"])) == "1"
        changed_attempt = {**binding([first]), "attempt_id": "f" * 32}
        try:
            sql(partial.build_sql([first], [], changed_attempt))
        except RuntimeError as error:
            assert "Franklin partial job binding differs" in str(error)
        else:
            raise AssertionError("same artifact with different attempt was accepted")
        assert sql("SELECT count(*) FROM credeals.cre_scrape_jobs WHERE status='partial';") == "2"
        sql("""UPDATE credeals.cre_listings SET status='sold', title='Prior title', address='Prior address'
               WHERE external_id='franklin-site:sale:12345678-1234-5123-8123-123456789abc';
               INSERT INTO credeals.cre_listing_contacts (listing_id,name)
               SELECT id,'Prior contact' FROM credeals.cre_listings
               WHERE external_id='franklin-site:sale:12345678-1234-5123-8123-123456789abc';""")
        second = item(identity, "a", "2026-11-04T12:00:02Z", "Visible new field")
        second["franklinSiteObservation"]["card"]["attributes"].update(
            {"data-status": "ACTIVE", "data-listing-address": "New literal address"})
        assert json.loads(sql(write_sql([second], [])).splitlines()[-1])["held"] == []
        readback = json.loads(sql(readback_sql([second], [])))
        assert readback == {"expected": 1, "invalid": [], "matching_jobs": 1}
        assert json.loads(sql(readback_sql([first], [])))["invalid"] == [first["id"]]
        result = json.loads(sql("SELECT json_build_object('status',status,'title',title,'address',address,'raw',raw_data,'scraped',scraped_at) FROM credeals.cre_listings WHERE external_id='franklin-site:sale:12345678-1234-5123-8123-123456789abc';"))
        assert result["status"] == "sold" and result["title"] == "Prior title" and result["address"] == "Prior address" and result["scraped"] is None
        assert [x["card"]["html"] for x in result["raw"]["franklinSiteObservations"]] == ["Subject to Offer", "Visible new field"]
        assert sql("SELECT count(*) FROM credeals.cre_listing_contacts WHERE name='Prior contact';") == "1"
        repeat = item(identity, "a", "2026-11-04T12:00:04Z", "Visible new field")
        repeat["franklinSiteObservation"]["card"]["attributes"].update(
            {"data-status": "ACTIVE", "data-listing-address": "New literal address"})
        assert json.loads(sql(write_sql([repeat], [])).splitlines()[-1])["held"] == []
        intermediate = item(identity, "a", "2026-11-04T12:00:03Z", "stale new text")
        assert json.loads(sql(write_sql([intermediate], [first["id"]])).splitlines()[-1])["held"] == [first["id"]]
        stale = {**first, "fixtureArtifact": "different"}
        held = json.loads(sql(write_sql([stale], [first["id"]])).splitlines()[-1])["held"]
        assert held == [first["id"]]
        changed_url = item(identity, "different", "2026-11-04T12:00:03Z", "new")
        assert json.loads(sql(write_sql([changed_url], [first["id"]])).splitlines()[-1])["held"] == [first["id"]]
        cycle = item(identity, "a", "2026-11-04T12:00:05Z", "Subject to Offer")
        assert json.loads(sql(write_sql([cycle], [])).splitlines()[-1])["held"] == []
        assert sql("SELECT jsonb_array_length(raw_data->'franklinSiteObservations') FROM credeals.cre_listings WHERE external_id='franklin-site:sale:12345678-1234-5123-8123-123456789abc';") == "3"
        conflict_id = "87654321-4321-5432-8432-abcdefabcdef"
        clean_id = "aaaaaaaa-bbbb-5ccc-8ddd-eeeeeeeeeeee"
        conflict = item(conflict_id, "legacy", "2026-11-04T12:00:06Z", "conflict")
        clean = item(clean_id, "clean", "2026-11-04T12:00:06Z", "clean")
        sql("""INSERT INTO credeals.cre_listings (brokerage_id, external_id, source_url)
               SELECT id, 'legacy', 'https://franklinst.com/properties/for-sale/legacy'
               FROM credeals.cre_brokerages WHERE slug='franklin-street';""")
        assert json.loads(sql(partial.identity_probe_sql([conflict, clean]))) == {"staged": 2, "joined": 2, "held": [conflict["id"]]}
        try:
            sql(write_sql([conflict, clean], []))
        except RuntimeError as error:
            assert "Franklin identity partition changed" in str(error)
        else:
            raise AssertionError("stale identity partition was accepted")
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='franklin-site:sale:aaaaaaaa-bbbb-5ccc-8ddd-eeeeeeeeeeee';") == "0"
        result = json.loads(sql(write_sql([conflict, clean], [conflict["id"]])).splitlines()[-1])
        assert result["held"] == [conflict["id"]]
        assert json.loads(sql(readback_sql([conflict, clean], result["held"]))) == {"expected": 1, "invalid": [], "matching_jobs": 1}
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='franklin-site:sale:aaaaaaaa-bbbb-5ccc-8ddd-eeeeeeeeeeee';") == "1"
        # Existing same-broker canonical URLs and deleted parents remain
        # identity conflicts, even when their scraped source URL differs.
        canonical = item("bbbbbbbb-1111-5111-8111-111111111111", "canonical", "2026-11-04T12:00:06Z", "canonical conflict")
        deleted = item("cccccccc-2222-5222-8222-222222222222", "deleted", "2026-11-04T12:00:06Z", "deleted conflict")
        sql("""INSERT INTO credeals.cre_listings (brokerage_id, external_id, source_url, canonical_url, deleted_at)
               SELECT id, 'legacy-canonical', 'https://legacy.example/item',
                      'https://franklinst.com/properties/for-sale/canonical', NULL
               FROM credeals.cre_brokerages WHERE slug='franklin-street'
               UNION ALL
               SELECT id, 'legacy-deleted', 'https://franklinst.com/properties/for-sale/deleted',
                      NULL, now()
               FROM credeals.cre_brokerages WHERE slug='franklin-street';""")
        expected = sorted([canonical["id"], deleted["id"]])
        assert json.loads(sql(partial.identity_probe_sql([canonical, deleted])))["held"] == expected
        assert json.loads(sql(write_sql([canonical, deleted], expected)).splitlines()[-1])["held"] == expected
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id IN (" +
                   ",".join(partial.sql_lit(v) for v in expected) + ");") == "0"

        # Never mutate an existing site parent whose canonical URL drifted.
        sql("UPDATE credeals.cre_listings SET canonical_url='https://legacy.example/changed' WHERE external_id=" + partial.sql_lit(first["id"]))
        assert json.loads(sql(partial.identity_probe_sql([repeat])))["held"] == [first["id"]]
        sql("UPDATE credeals.cre_listings SET canonical_url=source_url WHERE external_id=" + partial.sql_lit(first["id"]))

        sql("""UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data, '{franklinSiteObservations}',
                 (SELECT jsonb_agg(jsonb_set(raw_data->'franklinSiteObservations'->-1,
                   '{card,html}', to_jsonb('distinct history ' || n))) FROM generate_series(1,128) AS n))
               WHERE external_id='franklin-site:sale:12345678-1234-5123-8123-123456789abc';""")
        over_limit = item(identity, "a", "2026-11-04T12:00:07Z", "new card past old cap")
        assert json.loads(sql(write_sql([over_limit], [])).splitlines()[-1])["held"] == []
        assert sql("SELECT jsonb_array_length(raw_data->'franklinSiteObservations') FROM credeals.cre_listings WHERE external_id=" + partial.sql_lit(first["id"])) == "129"
        same_at_limit = item(identity, "a", "2026-11-04T12:00:08Z", "new card past old cap")
        assert json.loads(sql(write_sql([same_at_limit], [])).splitlines()[-1])["held"] == []
        next_change = item(identity, "a", "2026-11-04T12:00:09Z", "130th distinct card")
        assert json.loads(sql(write_sql([next_change], [])).splitlines()[-1])["held"] == []
        assert sql("SELECT jsonb_array_length(raw_data->'franklinSiteObservations') FROM credeals.cre_listings WHERE external_id=" + partial.sql_lit(first["id"])) == "130"
        larger = item("eeeeeeee-4444-5444-8444-444444444444", "large", "2026-11-04T12:00:09Z", "A" * 70000)
        assert json.loads(sql(write_sql([larger], [])).splitlines()[-1])["held"] == []
        assert sql("SELECT length(raw_data->'franklinSiteObservations'->0->'card'->>'html') FROM credeals.cre_listings WHERE external_id=" + partial.sql_lit(larger["id"])) == "70000"
        sizes = sql("SELECT pg_column_size(raw_data) || '/' || octet_length(raw_data::text) FROM credeals.cre_listings WHERE external_id=" + partial.sql_lit(first["id"]))
        large_size = sql("SELECT pg_column_size(raw_data) || '/' || octet_length(raw_data::text) FROM credeals.cre_listings WHERE external_id=" + partial.sql_lit(larger["id"]))
        print("Synthetic raw_data sizes (stored/uncompressed JSON bytes): 130 distinct observations", sizes, "; 70k repetitive card", large_size)
        assert sql("SELECT count(*) FROM credeals.cre_source_index;") == "0"

        raw, original_binding = mixed()
        original = json.loads(raw)
        page = original["sources"][0]["retainedObservations"][0]
        before = "12345678-1234-5123-8123-123456789abc"
        after = "bbbbbbbb-cccc-5ddd-8eee-ffffffffffff"
        page["decodedHtml"] = page["decodedHtml"].replace(before, after).replace("/for-sale/a", "/for-sale/mixed-page")
        page["decodedHtmlSha256"] = hashlib.sha256(page["decodedHtml"].encode()).hexdigest()
        page["cards"][0]["html"] = page["cards"][0]["html"].replace(before, after).replace("/for-sale/a", "/for-sale/mixed-page")
        page["cards"][0]["attributes"]["data-listing-id"] = after
        raw = json.dumps(original).encode()
        original_binding["artifact_sha256"] = hashlib.sha256(raw).hexdigest()
        bound = {**original_binding, "attempt_id": "d" * 32, "producer_sha": "e" * 40,
                 "database_target_sha256": "f" * 64}
        derived = derive(raw, bound, {"PATH": os.environ["PATH"]})
        assert derived["runMeta"]["franklinSitePartial"]["buildoutPending"] == 1
        assert json.loads(raw)["listings"][0]["name"] == "private Buildout peer"
        probe = json.loads(sql(partial.identity_probe_sql(derived["listings"])))
        assert probe["held"] == []
        sql(partial.build_sql(derived["listings"], probe["held"], bound))
        assert json.loads(sql(partial.readback_sql(derived["listings"], probe["held"], bound))) == {
            "expected": 1, "invalid": [], "matching_jobs": 1}
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='buildout-1';") == "0"
        print("Disposable Franklin partial SQL proof passed")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


if __name__ == "__main__":
    main()