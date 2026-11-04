"""Exercise a saved TS-produced NAI page against an owned disposable PG17."""
import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parent / "observation_store"))
import cre_checkpoint_refresh as refresh
from cre_ingest import database_target_fingerprint_from_url
from owned_observation_fixture_cluster import OwnedCluster, tool
from source_evidence import acquisition, automatic, events, native_pages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-disposable-fixture", action="store_true", required=True)
    parser.add_argument("--saved-native-page", required=True)
    args = parser.parse_args()
    initdb, pg_ctl, psql = (tool(name) for name in ("initdb", "pg_ctl", "psql"))
    database = "cre_native_" + uuid.uuid4().hex[:12]
    with OwnedCluster(initdb, pg_ctl, port=55449) as cluster:
        def sql(statement, *, target=database, user=None):
            result = subprocess.run([psql, "-X", "-qAt", "-h", str(cluster.root), "-p", str(cluster.port),
                                     "-d", target, "-v", "ON_ERROR_STOP=1"] + (["-U", user] if user else []),
                                    input=statement, capture_output=True, text=True, timeout=45, env=cluster.env, check=False)
            if result.returncode:
                raise AssertionError("fixture SQL failed: " + result.stderr)
            return result.stdout
        sql("CREATE ROLE evidence_executor LOGIN CREATEROLE CREATEDB BYPASSRLS;", target="postgres")
        sql(f"CREATE DATABASE {database} OWNER evidence_executor;", target="postgres")
        sql("CREATE ROLE cre_source_evidence_owner NOLOGIN NOINHERIT; CREATE ROLE cre_source_evidence_writer NOLOGIN NOINHERIT; CREATE ROLE cre_source_evidence_reader NOLOGIN NOINHERIT;", user="evidence_executor")
        migration = (HERE.parents[2] / "sql/019_cre_source_evidence.sql").read_text()
        sql("\\set CRE_SOURCE_EVIDENCE_APPROVE 1\n\\set CRE_SOURCE_EVIDENCE_OPERATOR cayman\n"
            "\\set CRE_SOURCE_EVIDENCE_APPROVAL_REF AGENTIC-2895\n"
            "\\set CRE_SOURCE_EVIDENCE_CONFIRM 'APPLY 019_cre_source_evidence'\n" + migration,
            user="evidence_executor")
        sql("CREATE ROLE evidence_auto LOGIN NOINHERIT; GRANT cre_source_evidence_writer TO evidence_auto WITH INHERIT FALSE, SET TRUE;")
        url = f"postgresql://evidence_auto@{quote(str(cluster.root), safe='')}:{cluster.port}/{database}"
        target = database_target_fingerprint_from_url(url)["value"]
        env = cluster.root / "writer.env"
        env.write_text("POSTGRES_URL=" + url + "\n")
        env.chmod(0o600)
        git = "a" * 40
        policy = {"schema_version": automatic.SCHEMA_V2, "purpose": "private_unverified_evidence",
                  "data_scope": "public_listing_source", "format": "collector_artifact_v1",
                  "collector_git_sha": git, "target_sha256": target, "source_keys": ["nai-global"],
                  "max_artifact_bytes": 16 * 1024 * 1024, "max_records": 1000,
                  "collector_projections": {}, "native_source_formats": {"nai-global": "nai_public_posts_v1"},
                  "max_native_pages": 4}
        raw = events.encoded(policy)
        policy_path = cluster.root / "policy.json"
        policy_path.write_bytes(raw)
        policy_path.chmod(0o600)
        config = automatic.configuration(str(policy_path), hashlib.sha256(raw).hexdigest(), str(env), target, git, ["nai-global"])
        run = cluster.root / "generation"
        run.mkdir(mode=0o700)
        manifest = refresh.new_manifest(run, git_sha=git, git_dirty=False, sources=("nai-global",), page_cap=4,
                                        concurrency=1, automatic_source_evidence=config)
        attempt = {"number": 1, "source_evidence": {"state": "no_artifact"}}
        manifest["sources"]["nai-global"]["attempts"].append(attempt)
        directory = native_pages.mark(run, manifest, "nai-global", attempt)
        source = Path(args.saved_native_page)
        page_path = directory / source.name
        page_path.write_bytes(source.read_bytes())
        page_path.chmod(0o600)
        error_entry = json.loads(source.read_bytes())
        error_body = b'{"errors":[{"message":"temporarily unavailable"}],"number":0.10000000000000000001}'
        error_entry["receipt"].update(status=503, bodySha256=hashlib.sha256(error_body).hexdigest(), bodyBytes=len(error_body))
        error_entry["responseBodyBase64"] = base64.b64encode(error_body).decode()
        error_raw = (json.dumps(error_entry, separators=(",", ":")) + "\n").encode()
        error_path = directory / f"page-0-{hashlib.sha256(error_raw).hexdigest()}.json"
        error_path.write_bytes(error_raw)
        error_path.chmod(0o600)
        native_pages.settle(run, manifest, "nai-global", attempt, lambda *_: None)
        pages = attempt["native_source_evidence"]["pages"]
        assert len(pages) == 2 and all(page["state"] == "pending" for page in pages)
        plans = [native_pages.prepare_job(run, manifest, "nai-global", attempt, page) for page in pages]
        assert sorted(len(plan["events"]) for plan in plans) == [1, 101]
        os.environ["PSQL_BIN"] = psql
        automatic.pump(run, manifest, lambda *_: None)
        assert all(page["state"] == "appended" for page in pages)
        actual = [json.loads(line, parse_float=__import__("decimal").Decimal) for line in sql(
            "SELECT row_to_json(r) FROM (SELECT capture_id,item_no,source_key,external_id,observed_at,source_url,"
            "provenance,facts,document_url,extractor_version,extracted_text,text_sha256 "
            "FROM cre_source_evidence.events ORDER BY capture_id,item_no) r;").splitlines()]
        expected_rows = sorted((event for plan in plans for event in plan["events"]), key=lambda event: (event["capture_id"], event["item_no"]))
        expected = json.loads(events.encoded(expected_rows), parse_float=__import__("decimal").Decimal)
        for observed, wanted in zip(actual, expected, strict=True):
            assert datetime.fromisoformat(observed.pop("observed_at").replace("Z", "+00:00")) == datetime.fromisoformat(wanted.pop("observed_at").replace("Z", "+00:00"))
            assert observed == wanted
        for page in pages:
            page["state"] = "pending"
        automatic.pump(run, manifest, lambda *_: None)
        assert all(page["state"] == "appended" for page in pages)
        assert int(sql("SELECT count(*) FROM cre_source_evidence.events;")) == len(expected)
        observation, reason = native_pages.decode_entry(source.read_bytes(), source.name, {})
        assert not reason and acquisition.reconstruct_json(observation)
        print("Disposable PG17 native NAI: 100 records plus HTTP error context; all fields and exact replay passed")


if __name__ == "__main__":
    main()
