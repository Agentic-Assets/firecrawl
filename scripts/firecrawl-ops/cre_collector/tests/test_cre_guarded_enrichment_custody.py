"""Draft guarded-parent custody tests; no collector or database is invoked."""
import hashlib
import json

import pytest

import cre_checkpoint_refresh as checkpoint
import cre_guarded_tier as tier
from test_cre_guarded_tier import assert_fenced, execute, options, receipt


def test_applied_enrichment_persists_custody_intent_before_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint, "database_target_fingerprint", lambda _: {"value": "b" * 64})
    seen = []
    def runner(argv, log, *, env):
        run_dir = log.parent.parent
        current = json.loads((run_dir / "receipt.json").read_text())
        seen.append(current["enrichment_artifact"])
        assert current["enrichment_artifact"]["state"] == "awaiting_worker"
        assert argv[argv.index("--artifact-out") + 1] == str(run_dir / "enriched.json")
        path = run_dir / "enriched.json"
        path.write_bytes(b'{"runMeta":{"mode":"enrich"},"sources":[],"listings":[]}')
        path.chmod(0o600)
        return 0
    assert execute(options(apply=True), tmp_path, _runner=runner) == 0
    assert len(seen) == 1
    custody = receipt(tmp_path)["enrichment_artifact"]
    body = b'{"runMeta":{"mode":"enrich"},"sources":[],"listings":[]}'
    assert custody["state"] == "settled"
    assert custody["artifact"] == {"state": "retained", "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
    assert custody["database_retention"] == "not_performed"


def test_nonzero_worker_preserves_private_artifact_and_recovery_fence(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint, "database_target_fingerprint", lambda _: {"value": "b" * 64})
    def runner(argv, log, *, env):
        path = log.parent.parent / "enriched.json"
        path.write_bytes(b'{"runMeta":{"mode":"enrich"},"sources":[],"listings":[]}')
        path.chmod(0o600)
        return 1
    assert execute(options(apply=True), tmp_path, _runner=runner) != 0
    result = receipt(tmp_path)
    assert result["enrichment_artifact"]["state"] == "settled"
    assert result["enrichment_artifact"]["artifact"]["state"] == "retained"
    assert result["status"] == "failed_recovery_required"
    assert result["recovery_clear_eligible"] is False
    assert_fenced(tmp_path)


def test_unknown_worker_settlement_never_reads_candidate(tmp_path, monkeypatch):
    from source_evidence import enrichment_custody
    from source_evidence.document_process import DocumentProcessCleanupUnknown
    monkeypatch.setattr(checkpoint, "database_target_fingerprint", lambda _: {"value": "b" * 64})
    monkeypatch.setattr(enrichment_custody, "settle", lambda *args, **kwargs: pytest.fail("must not read while worker unknown"))
    def runner(argv, log, *, env):
        raise DocumentProcessCleanupUnknown("synthetic")
    assert execute(options(apply=True), tmp_path, _runner=runner) != 0
    result = receipt(tmp_path)
    assert result["enrichment_artifact"]["state"] == "awaiting_worker"
    assert result["recovery_clear_eligible"] is False
    assert_fenced(tmp_path)


def test_dry_run_does_not_create_artifact_custody_intent(tmp_path):
    assert execute(options(apply=False), tmp_path, _runner=lambda *_a, **_k: 0) == 0
    assert "enrichment_artifact" not in receipt(tmp_path)


def test_empty_queue_has_settled_absence_without_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint, "database_target_fingerprint", lambda _: {"value": "b" * 64})
    assert execute(options(apply=True), tmp_path, _runner=lambda *_a, **_k: 0) == 0
    custody = receipt(tmp_path)["enrichment_artifact"]
    assert custody["state"] == "settled"
    assert custody["artifact"] == {"state": "absent"} and custody["claims"] == {"state": "absent"}


def test_confirmed_settlement_exception_retains_artifact_but_keeps_fence(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint, "database_target_fingerprint", lambda _: {"value": "b" * 64})
    def runner(argv, log, *, env):
        path = log.parent.parent / "enriched.json"
        path.write_bytes(b"partial")
        path.chmod(0o600)
        error = RuntimeError("synthetic interruption")
        error.cre_local_settlement = {"state": "confirmed"}
        raise error
    assert execute(options(apply=True), tmp_path, _runner=runner) != 0
    result = receipt(tmp_path)
    assert result["enrichment_artifact"]["state"] == "settled"
    assert result["enrichment_artifact"]["artifact"]["state"] == "retained"
    assert result["recovery_clear_eligible"] is False
    assert_fenced(tmp_path)
