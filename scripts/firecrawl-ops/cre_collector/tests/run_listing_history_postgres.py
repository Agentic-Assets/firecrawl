"""Disposable network-none PostgreSQL proof for generated listing ingest."""
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


def run(*args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr}")
    return result.stdout.strip()


def main():
    name = "listing-history-proof-" + uuid.uuid4().hex[:10]
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
        stamp = "2026-11-07T00:00:00Z"
        job = [{"slug": "svn", "discovered": 1, "saved": 1, "errors": 0, "notes": "synthetic"}]
        item = {"sourceKey": "svn", "id": "history-fixture", "url": "https://example.test/listing", "name": "old", "latitude": 26.2, "detailObservedAt": stamp}
        def apply(value, key):
            return sql(ingest.build_sql([ingest.to_row(value, {}, stamp)], job, stamp, set(), artifact_run_key=key))
        first = apply(item, "test-initial")
        assert "store_unavailable" in first and sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='history-fixture';") == "1"
        from cre_listing_history_sql import read_receipt
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "ingest.log"
            log.write_text(first + "\n")
            parsed = read_receipt(log, "test-initial")
            assert parsed["state"] == "reported_omissions"
            assert parsed["outcomes"]["svn:observation:store_unavailable"] == 1
            assert read_receipt(log, "wrong-key")["state"] == "unknown"
        sql("CREATE ROLE anon NOLOGIN; CREATE ROLE authenticated NOLOGIN; CREATE ROLE service_role NOLOGIN BYPASSRLS;")
        migration = (directory / "022_cre_listing_snapshots.sql").read_text()
        refused = subprocess.run(["docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-v", "ON_ERROR_STOP=1"], input=migration, capture_output=True, text=True)
        assert refused.returncode != 0
        assert sql("SELECT to_regclass('credeals.cre_listing_snapshots') IS NULL;") == "t"
        sql("\n".join(["\\set CRE_APPROVE_LISTING_SNAPSHOTS 1", "\\set CRE_LISTING_SNAPSHOTS_OPERATOR cayman", "\\set CRE_LISTING_SNAPSHOTS_APPROVAL_REF AGENTIC-5255", "\\set CRE_LISTING_SNAPSHOTS_CONFIRM 'APPLY 022_cre_listing_snapshots'", migration]))
        sql("""INSERT INTO credeals.cre_listing_contacts(listing_id,name,title) SELECT id,'Agent','Old contact' FROM credeals.cre_listings WHERE external_id='history-fixture';
INSERT INTO credeals.cre_listing_documents(listing_id,doc_type,title,url) SELECT id,'brochure','Old document','https://example.test/a.pdf' FROM credeals.cre_listings WHERE external_id='history-fixture';
INSERT INTO credeals.cre_listing_images(listing_id,url,alt_text) SELECT id,'https://example.test/a.jpg','Old image' FROM credeals.cre_listings WHERE external_id='history-fixture';
INSERT INTO credeals.cre_listing_media(listing_id,media_type,url,title) SELECT id,'video','https://example.test/v','Old video' FROM credeals.cre_listings WHERE external_id='history-fixture';
INSERT INTO credeals.cre_listing_links(listing_id,link_type,url,rel) SELECT id,'other','https://example.test/link','old' FROM credeals.cre_listings WHERE external_id='history-fixture';""")
        next_item = {**item, "name": "new", "latitude": None, "longitude": -80.1, "detailObservedAt": "2026-11-07T00:01:00Z"}
        receipt = apply(next_item, "test-second")
        assert "captured" in receipt, receipt
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "captured.log"
            log.write_text(receipt + "\n")
            assert read_receipt(log, "test-second")["state"] == "reported_no_omissions"
            assert read_receipt(log, "wrong-key")["state"] == "unknown"
        assert sql("SELECT payload->'parent'->>'title' FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-second' AND phase='preimage';") == "old"
        assert sql("SELECT string_agg(jsonb_array_length(payload->'children'->k)::text,',' ORDER BY k) FROM credeals.cre_listing_snapshots CROSS JOIN LATERAL unnest(ARRAY['cre_listing_contacts','cre_listing_documents','cre_listing_images','cre_listing_media','cre_listing_links']) k WHERE artifact_run_key='test-second' AND phase='preimage';") == "1,1,1,1,1"
        assert sql("SELECT payload->'raw_data'->>'name' FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-second' AND phase='observation';") == "new"
        assert sql("SELECT payload->'parent'->>'title' FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-second' AND phase='postimage';") == "new"
        assert sql("SELECT has_table_privilege('public','credeals.cre_listing_snapshots','SELECT')::text;") == "false"
        assert sql("SELECT relrowsecurity::text FROM pg_class WHERE oid='credeals.cre_listing_snapshots'::regclass;") == "true"
        assert sql("SELECT has_table_privilege('anon','credeals.cre_listing_snapshots','SELECT')::text || '|' || has_table_privilege('authenticated','credeals.cre_listing_snapshots','SELECT')::text || '|' || has_table_privilege('service_role','credeals.cre_listing_snapshots','INSERT')::text || '|' || has_table_privilege('service_role','credeals.cre_listing_snapshots','DELETE')::text;") == "false|false|true|false"
        third = {**item, "name": "third", "latitude": 26.3, "longitude": None,
                 "detailObservedAt": "2026-11-07T00:02:00Z"}
        apply(third, "test-third")
        assert sql("SELECT payload->'raw_data'->>'longitude' FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-second' AND phase='observation';") == "-80.1"
        assert sql("SELECT payload->'raw_data'->>'latitude' FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-third' AND phase='observation';") == "26.3"
        assert sql("SELECT payload->'raw_data'->>'detailObservedAt' FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-third' AND phase='observation';") == "2026-11-07T00:02:00Z"
        replay = apply(next_item, "test-second")
        assert "svn:observation:duplicate" in replay
        assert sql("SELECT count(*) FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-second' AND phase='observation';") == "1"
        # A failed snapshot insert is isolated from the canonical listing write.
        sql("""CREATE FUNCTION credeals.test_fail_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic'; END $$;
CREATE TRIGGER test_fail_snapshot BEFORE INSERT ON credeals.cre_listing_snapshots FOR EACH ROW EXECUTE FUNCTION credeals.test_fail_snapshot();""")
        failure = apply({**next_item, "name": "captured without history"}, "test-refused")
        assert "capture_error" in failure
        assert sql("SELECT title FROM credeals.cre_listings WHERE external_id='history-fixture';") == "captured without history"
        sql("DROP TRIGGER test_fail_snapshot ON credeals.cre_listing_snapshots; DROP FUNCTION credeals.test_fail_snapshot();")
        # A failed canonical transaction rolls back all history captured in it.
        rollback_sql = ingest.build_sql([ingest.to_row({**next_item, "name": "rollback"}, {}, stamp)], job, stamp, set(), artifact_run_key="test-rollback")
        rollback_sql = rollback_sql.replace("\nCOMMIT;\n", "\nDO $$ BEGIN RAISE EXCEPTION 'synthetic rollback'; END $$;\nCOMMIT;\n")
        failed = subprocess.run(["docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-v", "ON_ERROR_STOP=1"], input=rollback_sql, text=True, capture_output=True)
        assert failed.returncode != 0
        assert sql("SELECT count(*) FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-rollback';") == "0"
        assert sql("SELECT title FROM credeals.cre_listings WHERE external_id='history-fixture';") == "captured without history"
        # An independent SQL child writer is not covered by the lifecycle
        # advisory lock. Verify real row-lock interleavings in separate sessions.
        sql("""INSERT INTO credeals.cre_listings(brokerage_id,external_id,source_url,title,status)
SELECT id,'race-fixture','https://example.test/race','Race parent','active' FROM credeals.cre_brokerages WHERE slug='svn';
INSERT INTO credeals.cre_listing_contacts(listing_id,name,title)
SELECT id,'Race agent','Original title' FROM credeals.cre_listings WHERE external_id='race-fixture';""")
        race_item = {"sourceKey": "svn", "id": "race-fixture", "url": "https://example.test/race",
                     "name": "Race parent", "preserveChildCollections": True}
        import cre_listing_history_sql as history
        race_sql = ingest.build_sql([ingest.to_row(race_item, {}, stamp)], job, stamp, set(), artifact_run_key="test-race-held")
        assert race_sql.count(history.BEFORE) == 1
        race_sql = race_sql.replace(history.BEFORE, history.BEFORE + "\nSELECT pg_sleep(5);\n")
        def background(application, statement):
            proc = subprocess.Popen(["docker", "exec", "-i", "-e", f"PGAPPNAME={application}", name,
                                     "psql", "-U", "postgres", "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1"],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            proc.stdin.write(statement)
            proc.stdin.close()
            return proc
        def wait_for_sleep(application):
            for _ in range(100):
                if sql(f"SELECT count(*) FROM pg_stat_activity WHERE application_name='{application}' AND wait_event='PgSleep';") == "1":
                    return
                time.sleep(.05)
            raise AssertionError("synthetic concurrent session did not enter sleep")
        racing_ingest = background("history-race-ingest", race_sql)
        wait_for_sleep("history-race-ingest")
        blocked = subprocess.run(["docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-v", "ON_ERROR_STOP=1"],
                                 input="SET statement_timeout='200ms'; UPDATE credeals.cre_listing_contacts SET title='Should not commit' WHERE name='Race agent';",
                                 capture_output=True, text=True)
        assert blocked.returncode != 0 and "statement timeout" in blocked.stderr
        blocked_delete = subprocess.run(["docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-v", "ON_ERROR_STOP=1"],
                                        input="SET statement_timeout='200ms'; DELETE FROM credeals.cre_listing_contacts WHERE name='Race agent';",
                                        capture_output=True, text=True)
        assert blocked_delete.returncode != 0 and "statement timeout" in blocked_delete.stderr
        # backfill_media_from_raw_data writes additive child INSERTs without the
        # lifecycle advisory lock; its real FK still needs a parent key-share lock.
        blocked_backfill = subprocess.run(["docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-v", "ON_ERROR_STOP=1"],
                                          input="SET statement_timeout='200ms'; INSERT INTO credeals.cre_listing_media(listing_id,media_type,url) SELECT id,'video','https://example.test/race-video' FROM credeals.cre_listings WHERE external_id='race-fixture';",
                                          capture_output=True, text=True)
        assert blocked_backfill.returncode != 0 and "statement timeout" in blocked_backfill.stderr
        assert racing_ingest.wait(timeout=15) == 0, racing_ingest.stderr.read()
        assert sql("SELECT payload->'children'->'cre_listing_contacts'->0->>'title' FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-race-held' AND phase='preimage';") == "Original title"
        # When an independent writer already holds the child row, history
        # reports an omission while a child-preserving valid refresh commits.
        racing_writer = background("history-race-writer", "BEGIN; UPDATE credeals.cre_listing_contacts SET title='Writer title' WHERE name='Race agent'; SELECT pg_sleep(5); COMMIT;")
        wait_for_sleep("history-race-writer")
        race_receipt = sql(ingest.build_sql([ingest.to_row(race_item, {}, stamp)], job, stamp, set(), artifact_run_key="test-race-busy"))
        assert "child_lock_unavailable" in race_receipt
        assert racing_writer.poll() is None
        assert racing_writer.wait(timeout=15) == 0, racing_writer.stderr.read()
        assert sql("SELECT title FROM credeals.cre_listing_contacts WHERE name='Race agent';") == "Writer title"
        # Buildout preserves existing contacts when the staged contact set is
        # empty even while other child types refresh. This row lock is extra.
        empty_writer = background("history-race-empty", "BEGIN; UPDATE credeals.cre_listing_contacts SET title='Empty contact witness' WHERE name='Race agent'; SELECT pg_sleep(5); COMMIT;")
        wait_for_sleep("history-race-empty")
        empty_item = {key: value for key, value in race_item.items() if key != "preserveChildCollections"}
        empty_receipt = sql(ingest.build_sql([ingest.to_row(empty_item, {}, stamp)], job, stamp, set(), artifact_run_key="test-race-empty"))
        assert "child_lock_unavailable" in empty_receipt
        assert empty_writer.poll() is None
        assert empty_writer.wait(timeout=15) == 0, empty_writer.stderr.read()
        assert sql("SELECT title FROM credeals.cre_listing_contacts WHERE name='Race agent';") == "Empty contact witness"
        deleting_writer = background("history-race-delete", "BEGIN; DELETE FROM credeals.cre_listing_contacts WHERE name='Race agent'; SELECT pg_sleep(5); COMMIT;")
        wait_for_sleep("history-race-delete")
        delete_receipt = sql(ingest.build_sql([ingest.to_row(race_item, {}, stamp)], job, stamp, set(), artifact_run_key="test-race-delete"))
        assert "child_lock_unavailable" in delete_receipt
        assert deleting_writer.poll() is None
        assert deleting_writer.wait(timeout=15) == 0, deleting_writer.stderr.read()
        assert sql("SELECT count(*) FROM credeals.cre_listing_contacts WHERE name='Race agent';") == "0"
        # A normal replacement must acquire the child lock anyway. Do not
        # introduce the short optional-history timeout on this path: wait for
        # the existing writer and capture its committed version before DELETE.
        sql("""INSERT INTO credeals.cre_listings(brokerage_id,external_id,source_url,title,status)
SELECT id,'replace-fixture','https://example.test/replace','Replace parent','active' FROM credeals.cre_brokerages WHERE slug='svn';
INSERT INTO credeals.cre_listing_contacts(listing_id,name,title)
SELECT id,'Replace agent','Prior title' FROM credeals.cre_listings WHERE external_id='replace-fixture';""")
        replacing_writer = background("history-replace-writer", "BEGIN; UPDATE credeals.cre_listing_contacts SET title='Committed witness' WHERE name='Replace agent'; SELECT pg_sleep(2); COMMIT;")
        wait_for_sleep("history-replace-writer")
        replacement_item = {"sourceKey": "svn", "id": "replace-fixture", "url": "https://example.test/replace", "name": "Replace parent", "contactsDetailed": [{"name": "New agent"}]}
        replace_receipt = sql(ingest.build_sql([ingest.to_row(replacement_item, {}, stamp)], job, stamp, set(), artifact_run_key="test-replacement-wait"))
        assert replacing_writer.wait(timeout=15) == 0, replacing_writer.stderr.read()
        assert "child_lock_unavailable" not in replace_receipt
        assert sql("SELECT payload->'children'->'cre_listing_contacts'->0->>'title' FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-replacement-wait' AND phase='preimage';") == "Committed witness"
        assert sql("SELECT count(*) FROM credeals.cre_listing_contacts WHERE name='Replace agent';") == "0"
        # Explicit source-complete retirement captures both sides while retaining children.
        sql("""INSERT INTO credeals.cre_listings(brokerage_id,external_id,source_url,title,status)
SELECT id,'retire-fixture','https://example.test/retire','Retained listing','active' FROM credeals.cre_brokerages WHERE slug='svn';
INSERT INTO credeals.cre_listing_images(listing_id,url,alt_text)
SELECT id,'https://example.test/retire.jpg','Retained image' FROM credeals.cre_listings WHERE external_id='retire-fixture';""")
        retirement = ingest.build_sql([ingest.to_row(next_item, {}, stamp)], job, "2026-11-07T00:10:00Z", {"svn"}, artifact_run_key="test-retirement")
        retirement_receipt = sql(retirement)
        assert "retired_preimage:captured" in retirement_receipt
        assert "retired_postimage:captured" in retirement_receipt
        assert sql("SELECT payload->'parent'->>'status' FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-retirement' AND external_id='retire-fixture' AND phase='preimage';") == "active"
        assert sql("SELECT payload->'parent'->>'status' FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-retirement' AND external_id='retire-fixture' AND phase='postimage';") == "inactive"
        assert sql("SELECT jsonb_array_length(payload->'children'->'cre_listing_images') FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-retirement' AND external_id='retire-fixture' AND phase='postimage';") == "1"
        secret_item = {**next_item, "name": "valid peer", "sourceEvidence": {"token": "synthetic-secret"}}
        receipt = apply(secret_item, "test-credential")
        assert "captured_with_omissions" in receipt
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "redacted.log"
            log.write_text(receipt + "\n")
            parsed = read_receipt(log, "test-credential")
            assert parsed["state"] == "reported_omissions"
            assert parsed["outcomes"]["svn:observation:captured_with_omissions"] == 1
            assert read_receipt(log, "wrong-key")["state"] == "unknown"
        assert sql("SELECT payload->'raw_data'->>'name' FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-credential' AND phase='observation';") == "valid peer"
        assert sql("SELECT title FROM credeals.cre_listings WHERE external_id='history-fixture';") == "valid peer"
        assert sql("SELECT count(*) FROM credeals.cre_listing_snapshots WHERE payload::text LIKE '%synthetic-secret%';") == "0"
        for idx, extra in enumerate([
            {"sourceEvidence": "{\"access_token\":\"synthetic-secret\"}"},
            {"sourceEvidence": "https://example.test/a?X-Amz-Credential=synthetic-secret"},
            {"sourceEvidence": "https://example.test/a?%74oken=synthetic-secret"},
            {"sourceEvidence": "https://user:synthetic-secret@example.test/a"},
            {"sourceEvidence": {"cookie": "synthetic-secret", "safe": "retained"}},
            {"sourceEvidence": {"session_id": "synthetic-secret", "safe": "retained"}},
            {"sourceEvidence": {"private_key": "synthetic-secret", "safe": "retained"}},
            {"sourceEvidence": "https://example.test/a?session=synthetic-secret"},
        ]):
            if idx in (4, 5, 6, 7):
                from source_evidence.automatic import _refuse_credential_fields
                try:
                    _refuse_credential_fields(extra)
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"existing admission did not flag synthetic case {idx}")
            response = apply({**next_item, **extra, "name": f"valid-{idx}"}, f"test-sensitive-{idx}")
            assert "svn:observation:captured_with_omissions" in response, (idx, response)
            assert sql(f"SELECT count(*) FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-sensitive-{idx}' AND phase='observation';") == "1"
            omissions_result = sql(f"SELECT jsonb_array_length(omissions)::text FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-sensitive-{idx}' AND phase='observation';")
            assert int(omissions_result) > 0, (idx, omissions_result)
            if idx in (4, 5, 6):
                assert sql(f"SELECT payload->'raw_data'->'sourceEvidence'->>'safe' FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-sensitive-{idx}' AND phase='observation';") == "retained"
        assert sql("SELECT count(*) FROM credeals.cre_listing_snapshots WHERE payload::text LIKE '%synthetic-secret%';") == "0"
        # Explicit object-key regressions from the independent c3a726b review.
        key_bundle = {"refresh_token": "synthetic-credential-refresh",
                      "security_token": "synthetic-credential-security",
                      "signature": "synthetic-credential-signature",
                      "sig": "synthetic-credential-sig",
                      "key": "synthetic-credential-key",
                      "nested": {"refreshToken": "synthetic-credential-nested", "safe": "nested retained"},
                      "encoded": '{"sig":"synthetic-credential-encoded","safe":"inside string"}',
                      "safe": "retained peer"}
        key_result = apply({**next_item, "name": "key bundle", "sourceEvidence": key_bundle}, "test-sensitive-key-bundle")
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "key-bundle.log"
            log.write_text(key_result + "\n")
            parsed = read_receipt(log, "test-sensitive-key-bundle")
            assert parsed["state"] == "reported_omissions"
            assert parsed["outcomes"]["svn:observation:captured_with_omissions"] == 1
        assert sql("SELECT payload->'raw_data'->'sourceEvidence'->>'safe' FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-sensitive-key-bundle' AND phase='observation';") == "retained peer"
        assert sql("SELECT payload->'raw_data'->'sourceEvidence'->'nested'->>'safe' FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-sensitive-key-bundle' AND phase='observation';") == "nested retained"
        assert int(sql("SELECT jsonb_array_length(omissions) FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-sensitive-key-bundle' AND phase='observation';")) >= 7
        assert sql("SELECT count(*) FROM credeals.cre_listing_snapshots WHERE payload::text LIKE '%synthetic-credential-%';") == "0"
        key_replay = apply({**next_item, "name": "key bundle", "sourceEvidence": key_bundle}, "test-sensitive-key-bundle")
        assert "svn:observation:duplicate_with_omissions" in key_replay
        assert sql("SELECT count(*) FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-sensitive-key-bundle' AND phase='observation';") == "1"
        identity_item = {**next_item, "id": "https://example.test/a?token=synthetic-secret", "name": "valid identity peer"}
        identity_result = apply(identity_item, "test-sensitive-identity")
        assert "credential_identity_refused" in identity_result
        assert sql("SELECT count(*) FROM credeals.cre_listing_snapshots WHERE external_id LIKE '%synthetic-secret%';") == "0"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id LIKE '%synthetic-secret%';") == "1"
        allowed = apply({**next_item, "sourceEvidence": "https://example.test/a?listing=42", "name": "allowed-query"}, "test-allowed-query")
        assert "svn:observation:captured" in allowed
        assert sql("SELECT count(*) FROM credeals.cre_listing_snapshots WHERE artifact_run_key='test-allowed-query' AND phase='observation';") == "1"
        print("listing history: receipt, redaction, rollback, access and concurrent child locks passed")
    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()