"""Network-none disposable PG contract for Franklin partial SQL."""
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from run_franklin_site_lifecycle_postgres import run

import cre_franklin_site_sql as partial


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
        changed_attempt = {**binding([first]), "attempt_id": "f" * 32}
        try:
            sql(partial.build_sql([first], [], changed_attempt))
        except RuntimeError as error:
            assert "Franklin partial job binding differs" in str(error)
        else:
            raise AssertionError("same artifact with different attempt was accepted")
        assert sql("SELECT count(*) FROM credeals.cre_scrape_jobs WHERE status='partial';") == "1"
        sql("""UPDATE credeals.cre_listings SET status='sold', title='Prior title';
               INSERT INTO credeals.cre_listing_contacts (listing_id,name)
               SELECT id,'Prior contact' FROM credeals.cre_listings;""")
        second = item(identity, "a", "2026-11-04T12:00:02Z", "Visible new field")
        assert json.loads(sql(write_sql([second], [])).splitlines()[-1])["held"] == []
        readback = json.loads(sql(readback_sql([second], [])))
        assert readback == {"expected": 1, "invalid": [], "matching_jobs": 1}
        assert json.loads(sql(readback_sql([first], [])))["invalid"] == [first["id"]]
        result = json.loads(sql("SELECT json_build_object('status',status,'title',title,'raw',raw_data,'scraped',scraped_at) FROM credeals.cre_listings;"))
        assert result["status"] == "sold" and result["title"] == "Prior title" and result["scraped"] is None
        assert [x["card"]["html"] for x in result["raw"]["franklinSiteObservations"]] == ["Subject to Offer", "Visible new field"]
        assert sql("SELECT count(*) FROM credeals.cre_listing_contacts WHERE name='Prior contact';") == "1"
        repeat = item(identity, "a", "2026-11-04T12:00:04Z", "Visible new field")
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
        assert sql("SELECT jsonb_array_length(raw_data->'franklinSiteObservations') FROM credeals.cre_listings;") == "3"
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
        sql("""UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data, '{franklinSiteObservations}',
                 (SELECT jsonb_agg((raw_data->'franklinSiteObservations'->-1) || jsonb_build_object('fixtureIndex', n)) FROM generate_series(1,128) AS n))
               WHERE external_id='franklin-site:sale:12345678-1234-5123-8123-123456789abc';""")
        over_limit = item(identity, "a", "2026-11-04T12:00:07Z", "new card past cap")
        assert json.loads(sql(write_sql([over_limit], [first["id"]])).splitlines()[-1])["held"] == [first["id"]]
        same_at_limit = item(identity, "a", "2026-11-04T12:00:07Z", "Subject to Offer")
        assert json.loads(sql(write_sql([same_at_limit], [])).splitlines()[-1])["held"] == []
        assert sql("SELECT count(*) FROM credeals.cre_source_index;") == "0"
        print("Disposable Franklin partial SQL proof passed")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


if __name__ == "__main__":
    main()