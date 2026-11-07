"""Run generated JLL ingest against an isolated disposable PostgreSQL container."""
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import cre_ingest as ingest
from test_pyramid_rollback_result import BINDING


def run(*args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr}")
    return result.stdout.strip()


def main():
    name = "jll-guard-proof-" + uuid.uuid4().hex[:10]
    def sql(statement):
        return run("docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1", input=statement)
    try:
        run("docker", "run", "-d", "--rm", "--name", name, "--network=none", "--cpus=0.5", "--memory=512m", "--pids-limit=128", "--tmpfs", "/var/lib/postgresql/data:rw", "-e", "POSTGRES_PASSWORD=synthetic-only", "postgres:17")
        for _ in range(100):
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"], capture_output=True).returncode == 0
            if ready and (logs.stdout + logs.stderr).count("database system is ready to accept connections") >= 2:
                break
            time.sleep(.2)
        else:
            raise RuntimeError("disposable PostgreSQL not ready")
        directory = HERE.parent.parent / "sql"
        master = (directory / "000_run_all.sql").read_text()
        master = re.sub(r"^\\i (\S+)\s*$", lambda m: (directory / m[1]).read_text(), master, flags=re.M)
        sql(master + "\n" + "\n".join(["\\set CRE_APPROVE_LISTING_LIFECYCLE 1", "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman", "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-5246", "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'", (directory / "016_cre_listing_lifecycle.sql").read_text()]))
        item = {"sourceKey":"jll", "id":"synthetic-jll", "url":"https://example.test/jll", "name":"Synthetic", "transactionType":"sale", "photos":[f"https://example.test/{n}.jpg" for n in range(12)]}
        job = [{"slug":"jll", "discovered":1, "saved":1, "errors":0, "notes":"synthetic"}]
        row = ingest.to_row(item, {}, "2026-11-04T00:00:00Z")
        sql(ingest.build_sql([row], job, "2026-11-04T00:00:00Z", set()))
        retained = ingest.to_row({**item, "jllPublicFactsRetention": {"schema": "jll-public-facts-retention/v2"},
                                  "salePriceUsd": 1000000, "description": "Asking $1,000,000",
                                  "jllSearchResult": {"priceWithholdingControl": "withheld"}},
                                 {}, "2026-11-04T00:00:30Z")
        sql(ingest.build_sql([retained], job, "2026-11-04T00:00:30Z", set()))
        assert sql("SELECT sale_price_usd::text || '|' || description || '|' || (raw_data#>>'{jllSearchResult,priceWithholdingControl}') FROM credeals.cre_listings WHERE external_id='synthetic-jll';") == "1000000|Asking $1,000,000|withheld"
        # A legacy withheld/unknown observation is sparse; an old mixed marker
        # must not erase populated values or existing child labels.
        sql("""UPDATE credeals.cre_listings SET cap_rate=0.05, noi=50000, markdown='Price $1,000,000', raw_data=raw_data || jsonb_build_object('priorSentinel','retained') WHERE external_id='synthetic-jll';
                INSERT INTO credeals.cre_listing_contacts(listing_id,name,title)
                SELECT id,'Agent','Price $1,000,000' FROM credeals.cre_listings
                WHERE external_id='synthetic-jll';
                INSERT INTO credeals.cre_listing_documents(listing_id,doc_type,title,url)
                SELECT id,'brochure','Price $1,000,000','https://example.test/brochure.pdf'
                FROM credeals.cre_listings WHERE external_id='synthetic-jll';""")
        for control in (True, "unknown"):
            legacy = ingest.to_row({**item, "hidePrice": control, "photos": [],
                                    "salePriceUsd": None, "description": None}, {},
                                   "2026-11-04T00:00:40Z")
            sql(ingest.build_sql([legacy], job, "2026-11-04T00:00:40Z", set()))
            assert sql("SELECT sale_price_usd::text || '|' || description FROM credeals.cre_listings WHERE external_id='synthetic-jll';") == "1000000|Asking $1,000,000"
            assert sql("SELECT title FROM credeals.cre_listing_contacts WHERE name='Agent';") == "Price $1,000,000"
            assert sql("SELECT title FROM credeals.cre_listing_documents WHERE url='https://example.test/brochure.pdf';") == "Price $1,000,000"
            assert sql("SELECT count(*) FROM credeals.cre_listing_images;") == "12"
            assert sql("SELECT (cap_rate=0.05 AND noi=50000 AND markdown='Price $1,000,000' AND raw_data->>'priorSentinel'='retained')::text FROM credeals.cre_listings WHERE external_id='synthetic-jll';") == "true"
        mixed = ingest.to_row({**item, "jllPublicFactsRetention": {"schema": "jll-public-facts-retention/v2"},
                               "jllPriceWithheld": True, "salePriceUsd": None,
                               "description": None}, {},
                              "2026-11-04T00:00:50Z")
        sql(ingest.build_sql([mixed], job, "2026-11-04T00:00:50Z", set()))
        assert sql("SELECT sale_price_usd::text || '|' || description FROM credeals.cre_listings WHERE external_id='synthetic-jll';") == "1000000|Asking $1,000,000"
        assert sql("SELECT title FROM credeals.cre_listing_contacts WHERE name='Agent';") == "Price $1,000,000"
        assert sql("SELECT title FROM credeals.cre_listing_documents WHERE url='https://example.test/brochure.pdf';") == "Price $1,000,000"
        for reverse in (False, True):
            v2 = ingest.to_row({**item, "jllPublicFactsRetention": {"schema": "jll-public-facts-retention/v2"},
                                "salePriceUsd": 1100000}, {},
                               "2026-11-04T00:00:55Z")
            legacy = ingest.to_row({**item, "hidePrice": True, "salePriceUsd": None}, {}, "2026-11-04T00:00:55Z")
            combined = ingest.merge_rows(legacy, v2) if reverse else ingest.merge_rows(v2, legacy)
            assert "preserveChildCollections" not in json.dumps(combined["raw_data"])
            sql(ingest.build_sql([combined], job, "2026-11-04T00:00:55Z", set()))
            assert sql("SELECT sale_price_usd::text FROM credeals.cre_listings WHERE external_id='synthetic-jll';") == "1100000"
            assert sql("SELECT title FROM credeals.cre_listing_contacts WHERE name='Agent';") == "Price $1,000,000"
            assert sql("SELECT title FROM credeals.cre_listing_documents WHERE url='https://example.test/brochure.pdf';") == "Price $1,000,000"
            assert sql("SELECT count(*) FROM credeals.cre_listing_images;") == "12"
        before = sql("SELECT count(*) FROM credeals.cre_listing_images;")
        assert int(before) == 12, before
        jobs_before = sql("SELECT count(*) FROM credeals.cre_scrape_jobs;")
        reduced = ingest.to_row({**item, "name":"Should roll back", "photos":["https://example.test/replacement.jpg"]}, {}, "2026-11-04T00:01:00Z")
        output = sql(ingest.build_sql([reduced], job, "2026-11-04T00:01:00Z", set(), artifact_run_key=BINDING["artifact_run_key"], jll_guard_binding=BINDING))
        assert '"outcome":"image_loss_refused"' in output, output
        assert sql("SELECT count(*) FROM credeals.cre_listing_images;") == before
        assert sql("SELECT count(*) FROM credeals.cre_scrape_jobs;") == jobs_before
        assert sql("SELECT title FROM credeals.cre_listings WHERE external_id='synthetic-jll';") == "Synthetic"
        unchanged = ingest.to_row(item, {}, "2026-11-04T00:02:00Z")
        output = sql(ingest.build_sql([unchanged], job, "2026-11-04T00:02:00Z", set(), artifact_run_key=BINDING["artifact_run_key"], jll_guard_binding=BINDING))
        assert '"outcome":"committed"' in output, output
        # Legacy-only visible and withheld passes retain each pass's evidence.
        # Exercise both first insert and updates to an existing parent/children.
        for reverse in (False, True):
            for existing in (False, True):
                identity = f"legacy-{int(reverse)}-{int(existing)}"
                item_legacy = {"sourceKey": "jll", "id": identity,
                               "url": f"https://example.test/{identity}", "name": "Legacy"}
                if existing:
                    original = ingest.to_row({**item_legacy, "salePriceUsd": 100,
                                              "description": "Prior description",
                                              "photos": [f"https://example.test/{identity}-old.jpg"],
                                              "documents": [{"url": f"https://example.test/{identity}-old.pdf", "title": "Prior document"}],
                                              "contactsDetailed": [{"name": f"Old {identity}", "title": "Prior title"}]},
                                             {}, "2026-11-04T00:00:00Z")
                    sql(ingest.build_sql([original], job, "2026-11-04T00:00:00Z", set()))
                visible = ingest.to_row({**item_legacy, "transactionType": "sale", "hidePrice": False,
                                         "salePriceUsd": 200, "description": "Visible source description",
                                         "photos": [f"https://example.test/{identity}-new.jpg"],
                                         "documents": [{"url": f"https://example.test/{identity}-new.pdf", "title": "Visible document"}],
                                         "contactsDetailed": [{"name": f"New {identity}", "title": "Visible title"}]},
                                        {}, "2026-11-04T00:00:01Z")
                withheld = ingest.to_row({**item_legacy, "transactionType": "lease", "hidePrice": True,
                                          "leaseRateText": "$99/SF"}, {}, "2026-11-04T00:00:01Z")
                merged = ingest.merge_rows(withheld, visible) if reverse else ingest.merge_rows(visible, withheld)
                sql(ingest.build_sql([merged], job, "2026-11-04T00:00:01Z", set()))
                record = json.loads(sql(f"SELECT json_build_object('price',sale_price_usd,'description',description,'lease',lease_rate_min,'raw',raw_data) FROM credeals.cre_listings WHERE external_id='{identity}';"))
                assert record["price"] == 200 and record["description"] == "Visible source description"
                assert record["lease"] is None
                observed = record["raw"]["latestJllLegacyObservation"] if existing else record["raw"]
                assert observed["jllPriceWithheld"] is True
                assert observed["secondary_pass" if reverse else "primary"]["salePriceUsd"] == 200
                hidden = observed["primary" if reverse else "secondary_pass"]
                assert hidden["jllPriceWithheld"] is True and "leaseRateText" not in hidden
                assert hidden["jllLegacySourceControls"]["listing"] == {"hidePrice": True}
                count = sql(f"SELECT count(*) FROM credeals.cre_listing_contacts c JOIN credeals.cre_listings l ON l.id=c.listing_id WHERE l.external_id='{identity}';")
                assert int(count) == (2 if existing else 1)
                count = sql(f"SELECT count(*) FROM credeals.cre_listing_images c JOIN credeals.cre_listings l ON l.id=c.listing_id WHERE l.external_id='{identity}';")
                assert int(count) == (2 if existing else 1)
                count = sql(f"SELECT count(*) FROM credeals.cre_listing_documents c JOIN credeals.cre_listings l ON l.id=c.listing_id WHERE l.external_id='{identity}';")
                assert int(count) == (2 if existing else 1)
        print("JLL generated ingest: image guard explicitly rolled back, retained original rows, and normal success committed")
    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
