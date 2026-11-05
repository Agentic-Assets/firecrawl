"""Small real-schema proof for the NAI post-identity transaction."""
import json
import re
import subprocess
import sys
import tempfile
import time
import uuid
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import cre_ingest as ingest  # noqa: E402
import cre_nai_post_admission as nai_admission  # noqa: E402
from cre_nai_post_admission import admit, candidate, ingest_subset_context  # noqa: E402
from cre_nai_post_partial import build_partial_sql, identity_probe_sql, partial_readback_sql, stage_post  # noqa: E402
from tests.test_nai_post_partial import post  # noqa: E402


def run(*args, text=None, check=True):
    return subprocess.run(args, input=text, text=True, capture_output=True, check=check, timeout=90)


def main():
    nai_admission._utc_now = lambda: datetime(2026, 11, 5, 10, 2, tzinfo=timezone.utc)
    name = "nai-post-proof-" + uuid.uuid4().hex[:12]
    tmp = tempfile.TemporaryDirectory(prefix="nai-post-fixture-")

    def sql(query):
        result = run("docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA",
                     "-v", "ON_ERROR_STOP=1", text=query, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip()

    try:
        run("docker", "run", "-d", "--rm", "--name", name, "--network=none", "--cpus=0.5",
            "--memory=512m", "--pids-limit=128", "--tmpfs", "/var/lib/postgresql/data:rw,size=256m",
            "-e", "POSTGRES_PASSWORD=synthetic-only", "postgres:17")
        for _ in range(100):
            logs = run("docker", "logs", name, check=False)
            if (run("docker", "exec", name, "pg_isready", "-U", "postgres", check=False).returncode == 0
                    and (logs.stdout + logs.stderr).count("database system is ready to accept connections") >= 2):
                break
            time.sleep(.2)
        else:
            raise RuntimeError("disposable postgres not ready")
        directory = HERE.parent.parent / "sql"
        master = (directory / "000_run_all.sql").read_text()
        master = re.sub(r"^\\i (\S+)\s*$", lambda m: (directory / m[1]).read_text(), master, flags=re.MULTILINE)
        schema = master + "\n" + "\n".join([
            "\\set CRE_APPROVE_LISTING_LIFECYCLE 1", "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-4284",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text()])
        sql(schema)
        sql("""
INSERT INTO credeals.cre_listings (brokerage_id,external_id,source_url,canonical_url,status,title,lat,scraped_at,raw_data)
SELECT id,'infabode:1','https://infabode.com/services/listings/1',NULL,'active','Existing title',45.123456789012345,
 '2026-11-05T09:00:00Z','{"sourceKey":"nai-global","id":"infabode:1","existing":true}'::jsonb
FROM credeals.cre_brokerages WHERE slug='nai-global';
INSERT INTO credeals.cre_listings (brokerage_id,external_id,source_url,canonical_url,status,title,raw_data)
SELECT id,'infabode:old','https://example.test/old',
 'https://www.naiglobal.com/listings/?propertyId=post-2','active','Conflict','{}'::jsonb
FROM credeals.cre_brokerages WHERE slug='nai-global';
INSERT INTO credeals.cre_listing_contacts(listing_id,name)
SELECT id,'Preserve me' FROM credeals.cre_listings WHERE external_id='infabode:1';
""")
        incomplete = post(3)
        incomplete.pop("publicPost")
        incomplete.pop("detailObservedAt")
        incomplete.pop("transactionMode")
        incomplete["feedRow"].pop("contentType")
        incomplete.update(latitude=45.123456789012345, longitude=-117.98765432109876)
        incomplete["highlights"] = ["Source tag one", "Source tag two"]
        root = Path(tmp.name) / "gen-1"
        (root / "sources").mkdir(parents=True)
        artifact = root / "sources" / "nai-global.json"
        data = {"runMeta": {"transactions": ["sale", "lease"], "mode": "full",
                            "startedAt": "2026-11-05T09:59:00Z", "finishedAt": "2026-11-05T10:01:00Z",
                            "freshness": {"generationId": "gen-1", "generationStartedAt": "2026-11-05T09:58:00Z"}},
                "sources": [{"sourceKey": "nai-global", "transaction": "sale", "truncated": False}],
                "brokers": [], "listings": [post(1), post(2), incomplete]}
        import hashlib
        raw = json.dumps(data).encode()
        artifact.write_bytes(raw)
        artifact.chmod(0o600)
        manifest = {"run_id": "gen-1", "started_at": "2026-11-05T09:58:00Z", "collector_git_sha": "a" * 40,
                    "collector_git_dirty": False, "scope": {}, "config": {"sources": ["nai-global"],
                    "additive": True, "mark_missing": False, "status_activation": False,
                    "transactions": ["sale", "lease"], "max_items": 0},
                    "preflight": {"database_target": {"algorithm": "sha256", "value": "b" * 64}},
                    "sources": {"nai-global": {"artifact": {"path": "sources/nai-global.json", "nai_post_partial": True,
                    "attempt_started_at": "2026-11-05T09:58:30Z", "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw), "freshness_generation_id": "gen-1", "freshness_generation_started_at": "2026-11-05T09:58:00Z",
                    "started_at": "2026-11-05T09:59:00Z", "finished_at": "2026-11-05T10:01:00Z"},
                    "attempts": [{"rc": 0, "error": None, "started_at": "2026-11-05T09:58:30Z",
                                  "finished_at": "2026-11-05T10:02:00Z"}]}}}
        binding, partition = candidate(artifact, manifest, code_sha="a" * 40, dirty=False)
        staged = partition["staged"]
        result = sql(identity_probe_sql(staged)).split("|", 3)
        assert result[:2] == ["3", "3"]
        held, before = json.loads(result[2]), json.loads(result[3], parse_float=Decimal)
        assert held == ["infabode:2"], held
        manifest["sources"]["nai-global"]["nai_post_identity_probe"] = {
            **binding, "held": held, "preimage": result[3], "preimage_sha256": hashlib.sha256(result[3].encode()).hexdigest()}
        report = {"summary": {"mode": "live", "torow_errors": 0, "baseline_updated": False,
                  "baseline_update_sources": [], "hold_sources": ["nai-global"]},
                  "generated_for_artifact": str(artifact), "per_source": {"nai-global": {
                  "current_active": 3, "verdict": "hold", "mark_missing_safe": False,
                  "reason": "current_active 3 below floor 100"}}}
        import copy
        proof = admit(artifact, manifest, report, code_sha="a" * 40, dirty=False, phase="source")
        manifest["sources"]["nai-global"]["gate"] = {"current_inventory_proof": proof}
        aggregate = copy.deepcopy(report)
        assert admit(artifact, manifest, aggregate, code_sha="a" * 40, dirty=False, phase="aggregate") == proof
        envelope = {"manifest": manifest, "source_report": report, "aggregate_report": aggregate,
                    "binding": proof, "attempt_id": "d" * 32}
        context = ingest_subset_context(artifact, envelope, code_sha="a" * 40, dirty=False,
                                        target_sha256="b" * 64, attempt_id="d" * 32)
        job_id = context["job_id"]
        sql(build_partial_sql(context["staged"], context["held"], context["preimage"], job_id,
                              "2026-11-05T09:59:00Z", "2026-11-05T10:01:00Z"))
        readback = partial_readback_sql(staged, held, before, job_id,
                                        "2026-11-05T09:59:00Z", "2026-11-05T10:01:00Z")
        assert sql(readback) == "1|3|0"
        # The previous partial deliberately leaves canonical_url NULL. Its
        # retained alias must still fence a different post on a later run.
        alias_peer = stage_post(post(4, post(3)["canonicalUrl"]), "gen-1")
        later = sql(identity_probe_sql([alias_peer])).split("|", 3)
        assert json.loads(later[2]) == ["infabode:4"]
        result = json.loads(sql("""
SELECT json_build_object(
 'existing_title',(SELECT title FROM credeals.cre_listings WHERE external_id='infabode:1'),
 'existing_history',(SELECT jsonb_array_length(raw_data#>'{naiPublicPostHistory,entries}')
    FROM credeals.cre_listings WHERE external_id='infabode:1'),
 'preserved_contacts',(SELECT count(*) FROM credeals.cre_listing_contacts c
    JOIN credeals.cre_listings l ON l.id=c.listing_id WHERE l.external_id='infabode:1'),
 'held_untouched',(SELECT count(*) FROM credeals.cre_listings WHERE external_id='infabode:2'),
 'new_rows',(SELECT count(*) FROM credeals.cre_listings WHERE external_id='infabode:3'
    AND status IS NULL AND transaction_type IS NULL AND canonical_url IS NULL AND scraped_at IS NULL
    AND highlights=ARRAY['Source tag one','Source tag two']::text[]
    AND raw_data->>'identityScope'='infabode_public_post'),
 'index_rows',(SELECT count(*) FROM credeals.cre_source_index si
    JOIN credeals.cre_brokerages b ON b.id=si.brokerage_id WHERE b.slug='nai-global'))::text;
"""))
        assert result == {"existing_title": "Existing title", "existing_history": 1,
                          "preserved_contacts": 1, "held_untouched": 0,
                          "new_rows": 1, "index_rows": 0}, result
        assert sql("BEGIN; UPDATE credeals.cre_listings SET status='active' WHERE external_id='infabode:3'; "
                   + readback + "; ROLLBACK;") == "1|3|1"
        retained = json.loads(sql("SELECT raw_data->'naiPublicPostHistory' FROM credeals.cre_listings "
                                  "WHERE external_id='infabode:1'"))
        full = post(1)
        jobs = [{"slug": "nai-global", "discovered": 1, "saved": 1, "errors": 0, "notes": "synthetic"}]
        def full_sql(value):
            return ingest.build_sql([ingest.to_row(value, {}, "2026-11-05T10:05:00Z")],
                                    jobs, "2026-11-05T10:05:00Z", set())
        sql(full_sql(full))
        assert json.loads(sql("SELECT raw_data->'naiPublicPostHistory' FROM credeals.cre_listings "
                              "WHERE external_id='infabode:1'")) == retained
        # A reserved incoming member cannot silently replace the retained history.
        assert run("docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA",
                   "-v", "ON_ERROR_STOP=1", text=full_sql({**full, "naiPublicPostHistory": {}}),
                   check=False).returncode != 0
        print("NAI post partial real-schema proof passed")
    finally:
        run("docker", "rm", "-f", name, check=False)
        tmp.cleanup()


if __name__ == "__main__":
    main()
