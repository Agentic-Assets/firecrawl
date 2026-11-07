"""Disposable, local-only PostgreSQL proof of a WIP SQL fragment.

Requires --execute-disposable-fixture; never accepts an external database URL.
Run from a standalone no-hardlinks clone. This does not test an ingest caller.
"""
import argparse
import json
import subprocess
import sys
import re
import time
import tempfile
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cre_ingest as ingest
from cre_ingest import artifact_run_identity_from_digests
from source_fields.buildout import prepare_inventory_observation_scope, qualify_prior_inventory_binding
from test_buildout_current_inventory import CODE, SOURCE, TARGET, setup
from source_fields.buildout_observation_sql import sql_fragment


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-disposable-fixture", action="store_true")
    if not parser.parse_args().execute_disposable_fixture:
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
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("fixture database failed to start")
        base = ["docker", "exec", "-i", name, "psql", "-h", "127.0.0.1", "-X", "-qAt", "-U", "postgres", "-v", "ON_ERROR_STOP=1"]
        def run(sql, *, check=True):
            result = subprocess.run(base, input=sql, cwd=migrations, text=True, capture_output=True, timeout=60)
            if check and result.returncode:
                raise RuntimeError(result.stderr[-4000:])
            return result
        try:
            schema = re.sub(r"^\\i (\S+)\s*$", lambda match: (migrations / match[1]).read_text(), (migrations / "000_run_all.sql").read_text(), flags=re.M)
            run(schema)
            run("\\set CRE_APPROVE_LISTING_LIFECYCLE 1\n\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman\n"
                "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-5337\n"
                "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'\n"
                + (migrations / "016_cre_listing_lifecycle.sql").read_text())
            run("\\set CRE_APPROVE_AVAILABLE_SPACE_COUNT 1\n\\set CRE_AVAILABLE_SPACE_COUNT_OPERATOR cayman\n\\set CRE_AVAILABLE_SPACE_COUNT_APPROVAL_REF AGENTIC-5337\n\\set CRE_AVAILABLE_SPACE_COUNT_CONFIRM 'APPLY 017_cre_available_space_count'\n" + (migrations / "017_cre_available_space_count.sql").read_text())
            run("\\set CRE_APPROVE_BUILDING_SOURCE_FACTS 1\n\\set CRE_BUILDING_SOURCE_FACTS_OPERATOR cayman\n\\set CRE_BUILDING_SOURCE_FACTS_APPROVAL_REF AGENTIC-5337\n\\set CRE_BUILDING_SOURCE_FACTS_CONFIRM 'APPLY 018_cre_building_source_facts'\n" + (migrations / "018_cre_building_source_facts.sql").read_text())
            source = SOURCE
            bid = run("SELECT id FROM credeals.cre_brokerages WHERE slug='faris-lee';").stdout.strip()
            proof_tmp = tempfile.TemporaryDirectory(prefix="cre-buildout-proof-")
            _, artifact_path, artifact, manifest, _ = setup(Path(proof_tmp.name))
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
            still_seen = create("closed-visible", "40")
            stale = create("changed-preimage", "88")
            create("duplicate-a", "77")
            create("duplicate-b", "77")
            run(f"UPDATE credeals.cre_listings SET title='user update',raw_data=raw_data || '{{\"url\":\"https://example.test/changed\"}}'::jsonb WHERE id='{stale['listing_id']}';")
            staged = {}
            for listing in artifact["listings"]:
                current = ingest.to_row(listing, {}, scope["observed_at"])
                key = current["external_id"]
                staged[key] = ingest.merge_rows(staged[key], current) if key in staged else current
            rows = list(staged.values())
            jobs = [{"slug": source, "discovered": len(artifact["listings"]), "saved": len(rows), "errors": 0, "notes": "synthetic", "finished_at": scope["observed_at"]}]
            tx = ingest.build_sql(rows, jobs, scope["observed_at"], set(), artifact_run_key=jobkey,
                                  finished_at=scope["observed_at"],
                                  buildout_observation_context={"scope": scope, "prior": None})
            with ThreadPoolExecutor(max_workers=2) as pool:
                attempts = [pool.submit(run, tx) for _ in range(2)]
                for attempt in attempts:
                    attempt.result()
            state = run("SELECT external_id || ':' || observation_present::text || ':' || presence_generation::text FROM credeals.cre_source_index ORDER BY external_id;").stdout.splitlines()
            assert state == ["99:false:1", "changed-preimage:true:0", "closed-visible:true:0", "dual:true:0", "duplicate-a:true:0", "duplicate-b:true:0", "other:true:0"], state
            events = run("SELECT event_type || ':' || source_value FROM credeals.cre_listing_events;").stdout.splitlines()
            assert events == ["disappeared:not_observed_in_complete_enumeration:faris-lee:" + scope["generation_id"]], events
            state_rows = run("SELECT external_id || ':' || status || ':' || (deleted_at IS NULL)::text FROM credeals.cre_listings ORDER BY external_id;").stdout.splitlines()
            assert state_rows[:3] == ["99:active:true", "changed-preimage:active:true", "closed-visible:active:true"], state_rows
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
            proof_tmp.cleanup()
            print("PASS: actual schema and ingest builder, absent and held peers, concurrent replay, wrong job and stale source job refused, preserved status, actual ingest reappearance")
        finally:
            pass
    finally:
        if created:
            subprocess.run(["docker", "rm", "-f", name], check=True, stdout=subprocess.DEVNULL, timeout=30)


if __name__ == "__main__":
    main()
