"""Exercise automatic append against an owned, disposable PostgreSQL cluster."""
import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

HERE = Path(__file__).resolve().parent
COLLECTOR = HERE.parents[1]
sys.path.insert(0, str(COLLECTOR))
sys.path.insert(0, str(HERE.parent / "observation_store"))
import cre_checkpoint_refresh as refresh
from buildout_observation_artifact import URL, pointer_component
from cre_ingest import database_target_fingerprint_from_url
from owned_observation_fixture_cluster import OwnedCluster, tool
from source_evidence import acquisition, automatic, enrichment_custody, guarded_evidence, guarded_fence, map_projection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-disposable-fixture", action="store_true", required=True)
    parser.add_argument("--saved-collector-artifact")
    parser.add_argument("--project-map-credentials", action="store_true")
    args = parser.parse_args()
    initdb, pg_ctl, psql = (tool(name) for name in ("initdb", "pg_ctl", "psql"))
    database = "cre_auto_" + uuid.uuid4().hex[:12]
    with OwnedCluster(initdb, pg_ctl, port=55445) as cluster:
        def sql(statement, *, target=database, user=None, check=True):
            result = subprocess.run([psql, "-X", "-qAt", "-h", str(cluster.root), "-p", str(cluster.port),
                                     "-d", target, "-v", "ON_ERROR_STOP=1"] + (["-U", user] if user else []),
                                    input=statement, capture_output=True, text=True, timeout=45, env=cluster.env, check=False)
            if check and result.returncode:
                raise AssertionError("disposable fixture SQL failed")
            return result
        sql("CREATE ROLE evidence_executor LOGIN CREATEROLE CREATEDB BYPASSRLS;", target="postgres")
        sql(f"CREATE DATABASE {database} OWNER evidence_executor;", target="postgres")
        sql("CREATE ROLE cre_source_evidence_owner NOLOGIN NOINHERIT; CREATE ROLE cre_source_evidence_writer NOLOGIN NOINHERIT; CREATE ROLE cre_source_evidence_reader NOLOGIN NOINHERIT;", user="evidence_executor")
        sql("CREATE SCHEMA credeals; CREATE TABLE credeals.sentinel(id integer, status text); INSERT INTO credeals.sentinel VALUES (1,'active');")
        migration = (HERE.parents[2] / "sql/019_cre_source_evidence.sql").read_text()
        sql("\\set CRE_SOURCE_EVIDENCE_APPROVE 1\n\\set CRE_SOURCE_EVIDENCE_OPERATOR cayman\n"
            "\\set CRE_SOURCE_EVIDENCE_APPROVAL_REF AGENTIC-2895\n"
            "\\set CRE_SOURCE_EVIDENCE_CONFIRM 'APPLY 019_cre_source_evidence'\n" + migration, user="evidence_executor")
        sql("CREATE ROLE evidence_auto LOGIN NOINHERIT; GRANT cre_source_evidence_writer TO evidence_auto WITH INHERIT FALSE, SET TRUE;")
        url = f"postgresql://evidence_auto@{quote(str(cluster.root), safe='')}:{cluster.port}/{database}"
        target = database_target_fingerprint_from_url(url)["value"]
        env = cluster.root / "writer.env"
        env.write_text("POSTGRES_URL=" + url + "\n")
        env.chmod(0o600)
        git = "a" * 40
        policy = {"schema_version": automatic.SCHEMA, "purpose": "private_unverified_evidence",
                  "data_scope": "public_listing_source", "format": "collector_artifact_v1",
                  "collector_git_sha": git, "target_sha256": target, "source_keys": ["faris-lee"],
                  "max_artifact_bytes": 16 * 1024 * 1024, "max_records": 10000}
        if args.project_map_credentials:
            policy.update(schema_version=automatic.SCHEMA_V2,
                          collector_projections={"faris-lee": map_projection.VERSION},
                          native_source_formats={}, max_native_pages=128)
        policy_path = cluster.root / "policy.json"
        raw_policy = json.dumps(policy).encode()
        policy_path.write_bytes(raw_policy)
        policy_path.chmod(0o600)
        config = automatic.configuration(str(policy_path), hashlib.sha256(raw_policy).hexdigest(), str(env), target, git, ["faris-lee"])
        run = cluster.root / "generation"
        run.mkdir(mode=0o700)
        artifact = {"runMeta": {"finishedAt": "2026-10-28T10:00:00Z"},
                    "sources": [{"sourceKey": "faris-lee", "truncated": True, "error": "partial"}],
                    "listings": [{"sourceKey": "faris-lee", "unknown": [None, i, i]} for i in range(78)]}
        if args.project_map_credentials:
            for i, record in enumerate(artifact["listings"]):
                record["photos"] = [f"https://maps.googleapis.com/maps/vt?pb=map{i}&key=FixtureKeySensitiveABC&token=987654321012345678"]
        artifact_bytes = json.dumps(artifact).encode()
        if args.saved_collector_artifact:
            artifact_bytes = Path(args.saved_collector_artifact).read_bytes()
            artifact = json.loads(artifact_bytes)
        else:
            needle = b'"unknown": [null, 1, 1]'
            assert artifact_bytes.count(needle) == 1
            artifact_bytes = artifact_bytes.replace(needle, b'"unknown": [null, 0.10000000000000000001, 1]')
        if args.project_map_credentials:
            original, original_literals = acquisition.decode_json(artifact_bytes)
            projected, projected_literals, projection = map_projection.prepare(artifact_bytes, source_key="faris-lee")
            assert original_literals == projected_literals
            assert projection["replacements"]
            map_urls = set()
            def find_map_urls(value):
                if isinstance(value, dict):
                    for child in value.values():
                        find_map_urls(child)
                elif isinstance(value, list):
                    for child in value:
                        find_map_urls(child)
                elif isinstance(value, str):
                    for match in URL.finditer(value):
                        candidate = match.group(0).rstrip(")]},.")
                        try:
                            parsed = urlsplit(candidate)
                            if parsed.netloc == "maps.googleapis.com" and parsed.path == "/maps/vt" and ("key=" in parsed.query or "token=" in parsed.query):
                                map_urls.add(candidate)
                        except ValueError:
                            pass
            find_map_urls(original)
            assert map_urls
            changed_paths = set()
            def assert_safe_diff(before, after, path=""):
                if isinstance(before, dict):
                    assert before.keys() == after.keys()
                    for key, value in before.items():
                        assert_safe_diff(value, after[key], path + "/" + pointer_component(key))
                elif isinstance(before, list):
                    assert len(before) == len(after)
                    for i, value in enumerate(before):
                        assert_safe_diff(value, after[i], path + "/" + str(i))
                elif before != after:
                    assert isinstance(before, str) and isinstance(after, str)
                    changed_paths.add(path)
                    def strip_credentials(match):
                        text = match.group(0)
                        token = text.rstrip(")]},.")
                        suffix = text[len(token):]
                        parsed = urlsplit(token)
                        if parsed.netloc != "maps.googleapis.com" or parsed.path != "/maps/vt":
                            return text
                        base, _, query = token.partition("?")
                        safe = [part for part in query.split("&") if part.partition("=")[0] not in ("key", "token")]
                        return base + ("?" + "&".join(safe) if safe else "") + suffix
                    assert URL.sub(strip_credentials, before) == after
            assert_safe_diff(original, projected)
            assert changed_paths == {entry["pointer"] for entry in projection["replacements"]}
        expected_count = len(artifact["listings"])
        assert 50 < expected_count <= 10000
        manifest = refresh.new_manifest(run, git_sha=git, git_dirty=False, sources=("faris-lee",),
                                        page_cap=400, concurrency=1, automatic_source_evidence=config)
        attempt, artifact_path, _, started = refresh._cohort_attempt(run, manifest, "faris-lee", collector_concurrency=1)
        artifact_path.write_bytes(artifact_bytes)
        item = refresh.CohortCollectionProcess("faris-lee", SimpleNamespace(poll=lambda: refresh.COHORT_SETTLED_FAILURE_EXIT),
                                              io.StringIO(), artifact_path, attempt, started, object())
        # Simulate a settled worker exit; evidence and database work remain real.
        refresh._require_completed_group_settlement = lambda *_: None
        refresh.git_identity = lambda: (git, False)
        assert not refresh._finalize_cohort_collection(run, manifest, item, candidate_only=True)
        assert attempt["source_evidence"]["state"] == "pending"
        os.environ["PSQL_BIN"] = psql
        refresh.pump_source_evidence(run, manifest)
        assert attempt["source_evidence"]["state"] == "appended"
        assert sql("SELECT count(*) FROM cre_source_evidence.events;").stdout.strip() == str(expected_count + 1)
        assert sql(f"SELECT count(*) FROM cre_source_evidence.events WHERE source_key='faris-lee' AND item_no BETWEEN 1 AND {expected_count};").stdout.strip() == str(expected_count)
        assert sql("SELECT status FROM credeals.sentinel WHERE id=1;").stdout.strip() == "active"
        plan = json.loads((run / attempt["source_evidence"]["path"]).read_bytes())
        if args.project_map_credentials:
            state = attempt["source_evidence"]
            assert state["input_representation"] == "projected_collector_observation_v1"
            sealed = (run / state["input_path"]).read_bytes()
            assert all(url.encode() not in sealed for url in map_urls)
            wrapped = json.loads(sealed)
            header = plan["events"][0]["facts"][0]["value"]
            assert header["provenance"]["projection"] == projection
            provenance_json = json.dumps(header["provenance"])
            assert hashlib.sha256(artifact_bytes).hexdigest() not in provenance_json
            assert map_projection.canonical_digest(original, original_literals or None) not in provenance_json
            assert header["provenance"]["originalArtifactSha256"] == projection["projectedCanonicalSha256"]
            assert header["provenance"]["automaticPolicy"]["artifactFileSha256"] == projection["projectedCanonicalSha256"]
            assert header["provenance"]["originalSourceHashesVerification"] == "omitted_due_to_projection"
            assert header["context"] == {k: v for k, v in projected.items() if k != "listings"}
            assert wrapped["records"] == projected["listings"]
            assert [event["facts"][0]["value"] for event in plan["events"][1:]] == projected["listings"]
            assert "originalRawSha256" not in projection and "originalCanonicalSha256" not in projection
            assert all("originalUrlSha256" not in entry for entry in projection["replacements"])
            if not args.saved_collector_artifact:
                assert b"FixtureKeySensitiveABC" not in sealed and b"987654321012345678" not in sealed
        if not args.saved_collector_artifact:
            assert plan["events"][0]["facts"][0]["value"]["provenance"]["numericLiterals"]["/listings/1/unknown/1"] == "0.10000000000000000001"
        result = sql("SELECT row_to_json(r) FROM (SELECT capture_id,item_no,source_key,external_id,observed_at,source_url,"
                     "provenance,facts,document_url,extractor_version,extracted_text,text_sha256 "
                     "FROM cre_source_evidence.events ORDER BY item_no) r;")
        actual = [json.loads(line) for line in result.stdout.splitlines()]
        if args.project_map_credentials:
            encoded_rows = json.dumps(actual, ensure_ascii=False).encode()
            assert all(url.encode() not in encoded_rows for url in map_urls)
            if not args.saved_collector_artifact:
                assert b"FixtureKeySensitiveABC" not in encoded_rows and b"987654321012345678" not in encoded_rows
        for observed, expected in zip(actual, plan["events"], strict=True):
            assert datetime.fromisoformat(observed.pop("observed_at").replace("Z", "+00:00")) == datetime.fromisoformat(expected["observed_at"].replace("Z", "+00:00"))
            assert observed == {k: v for k, v in expected.items() if k != "observed_at"}
        attempt["source_evidence"]["state"] = "pending"
        refresh.pump_source_evidence(run, manifest)
        assert attempt["source_evidence"]["state"] == "appended"
        assert sql("SELECT count(*) FROM cre_source_evidence.events;").stdout.strip() == str(expected_count + 1)
        # Real restricted append, simulated lost response, then exact receipt replay.
        targeted = cluster.root / "targeted"
        targeted.mkdir(mode=0o700)
        custody = enrichment_custody.mark(targeted, "faris-lee")
        (targeted / "enriched.json").write_bytes(artifact_bytes)
        (targeted / "enriched.json").chmod(0o600)
        receipt = {"tier": "enrich", "source": "faris-lee", "apply": True, "expected_sha": git,
                   "local_settlement": {"state": "confirmed"},
                   "enrichment_artifact": enrichment_custody.settle(targeted, custody),
                   "source_evidence": {"config": config, "status": "awaiting_worker"}}
        receipt_path = targeted / "receipt.json"
        def save_targeted():
            refresh.atomic_write_text(receipt_path, json.dumps(receipt) + "\n")
        save_targeted()
        lock_path = cluster.root / "targeted.lock"
        real_psql = automatic.cli.run_psql
        def lost_response(*values, **options):
            real_psql(*values, **options)
            raise RuntimeError("simulated lost response after commit")
        automatic.cli.run_psql = lost_response
        with refresh.SharedLock(lock_path, recovery_required=True) as owner:
            guarded_evidence.seal_and_append(targeted, receipt, owner, save_targeted)
            assert receipt["source_evidence"]["job"]["state"] == "outcome_unknown"
            owner.clear_recovery_requirement()
        targeted_count = int(sql("SELECT count(*) FROM cre_source_evidence.events;").stdout.strip())
        assert targeted_count == 2 * (expected_count + 1)
        assert guarded_fence.read(lock_path) is not None
        automatic.cli.run_psql = real_psql
        guarded_evidence.reconcile(receipt_path, lock_path, source="faris-lee", expected_sha=git)
        assert guarded_fence.read(lock_path) is None
        assert int(sql("SELECT count(*) FROM cre_source_evidence.events;").stdout.strip()) == targeted_count
        assert json.loads(receipt_path.read_text())["source_evidence"]["job"]["state"] == "appended"
        assert sql("SELECT status FROM credeals.sentinel WHERE id=1;").stdout.strip() == "active"
        print(f"Targeted receipt: {expected_count} records, lost response and exact idempotent replay passed")
        if args.project_map_credentials:
            sql("CREATE SCHEMA danger; GRANT USAGE ON SCHEMA danger TO PUBLIC; CREATE TABLE danger.items(id integer); GRANT INSERT ON danger.items TO PUBLIC;")
            refused = sql(automatic.append_sql(plan), user="evidence_auto", check=False)
            assert refused.returncode != 0 and "disallowed effective database capability" in refused.stderr
            sql("REVOKE INSERT ON danger.items FROM PUBLIC;")
        assert sql(automatic.append_sql(plan), check=False).returncode != 0  # superuser login refused
        sql("GRANT SELECT ON credeals.sentinel TO cre_source_evidence_writer;")
        assert sql(automatic.append_sql(plan), user="evidence_auto", check=False).returncode != 0
        sql("REVOKE SELECT ON credeals.sentinel FROM cre_source_evidence_writer; GRANT CREATE ON SCHEMA cre_source_evidence TO cre_source_evidence_writer;")
        assert sql(automatic.append_sql(plan), user="evidence_auto", check=False).returncode != 0
        sql("REVOKE CREATE ON SCHEMA cre_source_evidence FROM cre_source_evidence_writer; GRANT EXECUTE ON FUNCTION cre_source_evidence.append_event(text,integer,text,text,timestamptz,text,jsonb,jsonb,text,text,text,text) TO cre_source_evidence_writer WITH GRANT OPTION;")
        assert sql(automatic.append_sql(plan), user="evidence_auto", check=False).returncode != 0
        sql("REVOKE GRANT OPTION FOR EXECUTE ON FUNCTION cre_source_evidence.append_event(text,integer,text,text,timestamptz,text,jsonb,jsonb,text,text,text,text) FROM cre_source_evidence_writer;")
        sql("ALTER TABLE credeals.sentinel OWNER TO cre_source_evidence_writer;")
        assert sql(automatic.append_sql(plan), user="evidence_auto", check=False).returncode != 0
        sql("ALTER TABLE credeals.sentinel OWNER TO CURRENT_USER;")
        assert sql(automatic.append_sql(plan), user="evidence_auto", check=False).returncode == 0
        sql("CREATE ROLE unrelated_role NOLOGIN; GRANT unrelated_role TO evidence_auto;")
        assert sql(automatic.append_sql(plan), user="evidence_auto", check=False).returncode != 0
        sql("REVOKE unrelated_role FROM evidence_auto; ALTER ROLE evidence_auto INHERIT;")
        assert sql(automatic.append_sql(plan), user="evidence_auto", check=False).returncode != 0
        sql("ALTER ROLE evidence_auto NOINHERIT REPLICATION;")
        assert sql(automatic.append_sql(plan), user="evidence_auto", check=False).returncode != 0
        sql("ALTER ROLE evidence_auto NOREPLICATION; GRANT SELECT ON credeals.sentinel TO evidence_auto;")
        assert sql(automatic.append_sql(plan), user="evidence_auto", check=False).returncode != 0
        sql("REVOKE SELECT ON credeals.sentinel FROM evidence_auto; GRANT unrelated_role TO cre_source_evidence_writer;")
        assert sql(automatic.append_sql(plan), user="evidence_auto", check=False).returncode != 0
        sql("REVOKE unrelated_role FROM cre_source_evidence_writer; GRANT cre_source_evidence_writer TO evidence_auto WITH ADMIN TRUE, INHERIT FALSE, SET TRUE;")
        assert sql(automatic.append_sql(plan), user="evidence_auto", check=False).returncode != 0
        sql("GRANT cre_source_evidence_writer TO evidence_auto WITH ADMIN FALSE, INHERIT TRUE, SET TRUE;")
        assert sql(automatic.append_sql(plan), user="evidence_auto", check=False).returncode != 0
        sql("GRANT cre_source_evidence_writer TO evidence_auto WITH ADMIN FALSE, INHERIT FALSE, SET TRUE; ALTER ROLE cre_source_evidence_writer REPLICATION;")
        assert sql(automatic.append_sql(plan), user="evidence_auto", check=False).returncode != 0
        sql("ALTER ROLE cre_source_evidence_writer NOREPLICATION INHERIT;")
        assert sql(automatic.append_sql(plan), user="evidence_auto", check=False).returncode != 0
        sql("ALTER ROLE cre_source_evidence_writer NOINHERIT LOGIN;")
        assert sql(automatic.append_sql(plan), user="evidence_auto", check=False).returncode != 0
        sql("ALTER ROLE cre_source_evidence_writer NOLOGIN; GRANT cre_source_evidence_writer TO evidence_auto WITH ADMIN FALSE, INHERIT FALSE, SET FALSE;")
        assert sql(automatic.append_sql(plan), user="evidence_auto", check=False).returncode != 0
        print(f"Disposable automatic evidence: {expected_count} records, simulated failed-worker settlement, exact replay, role guard and canonical sentinel passed")


if __name__ == "__main__":
    main()
