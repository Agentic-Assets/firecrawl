import copy
import hashlib
import json
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from nai_partial.admission import admit, candidate, ingest_subset_context, run_identity_probe
from tests.test_nai_post_partial import post


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr("nai_partial.admission._utc_now", lambda: datetime(2026, 11, 5, 10, 2, tzinfo=timezone.utc))


def fixture(tmp_path):
    root = tmp_path / "gen-1"
    source = root / "sources"
    source.mkdir(parents=True)
    path = source / "nai-global.json"
    data = {"runMeta": {"transactions": ["sale", "lease"], "mode": "full",
                        "startedAt": "2026-11-05T09:59:00Z", "finishedAt": "2026-11-05T10:00:02Z",
                        "freshness": {"generationId": "gen-1", "generationStartedAt": "2026-11-05T09:58:00Z"}}, "listings": [post()],
            "sources": [{"sourceKey": "nai-global", "transaction": "sale", "truncated": False}], "brokers": []}
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    sha = "a" * 40
    manifest = {"run_id": "gen-1", "started_at": "2026-11-05T09:58:00Z", "collector_git_sha": sha,
                "collector_git_dirty": False, "config": {"sources": ["nai-global"], "additive": True,
                "mark_missing": False, "status_activation": False, "transactions": ["sale", "lease"],
                "max_items": 0}, "preflight": {"database_target": {"algorithm": "sha256", "value": "b" * 64}},
                "sources": {"nai-global": {"artifact": {"path": "sources/nai-global.json", "nai_post_partial": True,
                "attempt_started_at": "2026-11-05T09:58:30Z", "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw), "freshness_generation_id": "gen-1",
                "freshness_generation_started_at": "2026-11-05T09:58:00Z",
                "started_at": "2026-11-05T09:59:00Z", "finished_at": "2026-11-05T10:00:02Z"}, "attempts": [{"rc": 0,
                "error": None, "started_at": "2026-11-05T09:58:30Z", "finished_at": "2026-11-05T10:00:03Z"}]}}}
    return path, manifest, sha


def test_bound_artifact_partition(tmp_path):
    path, manifest, sha = fixture(tmp_path)
    binding, partition = candidate(path, manifest, code_sha=sha, dirty=False)
    assert binding["source_count"] == 1 and binding["staged_count"] == 1
    assert partition["staged"][0]["external_id"] == "infabode:1"


def test_active_checkpoint_must_match_claims_and_live_write_attempt(tmp_path, monkeypatch):
    import cre_checkpoint_refresh as refresh
    import cre_tier_dispatch as dispatch
    from nai_partial.admission import _require_active_checkpoint

    path, manifest, sha = fixture(tmp_path)
    lock_path = tmp_path / ".cre.lock"
    monkeypatch.setattr(refresh, "canonical_shared_lock_dir", lambda: lock_path)
    monkeypatch.setattr(dispatch, "verify_inherited_lock", lambda **kwargs: None)
    monkeypatch.setenv("CRE_TIER_LOCK_AUTHORITY_FD", "4")
    monkeypatch.setenv("CRE_TIER_LOCK_SYNC_FD", "5")
    binding, _ = candidate(path, manifest, code_sha=sha, dirty=False)
    root = path.parent.parent
    claims_path = root / "claims.json"
    raw = b"{}"
    claims_path.write_bytes(raw)
    checkpoint = manifest["sources"]["nai-global"]
    checkpoint.update(state="ingesting", subset_ingest_attempt_id="d" * 32,
        ingest={"attempt_id": "d" * 32, "rc": None}, gate={"current_inventory_proof": binding},
        nai_post_claims={"path": str(claims_path), "sha256": hashlib.sha256(raw).hexdigest(), "attempt_id": "d" * 32})
    manifest["preflight"]["shared_lock"] = str(lock_path)
    (root / "manifest.json").write_text(json.dumps(manifest))
    args = SimpleNamespace(inputs=[str(path)], nai_post_claims=str(claims_path), dry_run=False,
                           checkpoint_ingest_attempt="d" * 32, expected_db_target_sha256="b" * 64)
    envelope = {"manifest": manifest, "binding": binding}
    assert _require_active_checkpoint(args, raw, envelope) == (4, 5)
    checkpoint["ingest"]["attempt_id"] = "e" * 32
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="write attempt differs"):
        _require_active_checkpoint(args, raw, envelope)
    checkpoint["ingest"]["attempt_id"] = "d" * 32
    checkpoint["nai_post_claims"]["sha256"] = "0" * 64
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="checkpoint binding differs"):
        _require_active_checkpoint(args, raw, envelope)


def test_inventory_recovery_rederives_exact_original_and_rejects_tampering(tmp_path):
    from nai_partial.inventory_recovery import derive
    from tests.test_nai_inventory_recovery import recovery_source

    path, manifest, sha = fixture(tmp_path)
    original_data = recovery_source()
    original_raw = json.dumps(original_data, separators=(",", ":")).encode()
    rejected = path.parent.parent / "rejected"
    rejected.mkdir()
    original = rejected / "nai-global-attempt-1.json"
    original.write_bytes(original_raw)
    original.chmod(0o600)
    candidate_raw, _ = derive(original_raw, "gen-1", "2026-11-05T09:58:00Z", "2026-11-05T09:58:30Z",
                              "2026-11-05T10:02:00Z", now=datetime(2026, 11, 5, 10, 2, tzinfo=timezone.utc))
    path.write_bytes(candidate_raw)
    info = manifest["sources"]["nai-global"]["artifact"]
    info.update(nai_inventory_recovery=True, bytes=len(candidate_raw), sha256=hashlib.sha256(candidate_raw).hexdigest(),
                finished_at="2026-11-05T10:01:00Z")
    manifest["sources"]["nai-global"]["attempts"][-1].update(number=1,
        error="nai-global/sale freshness metric detailErrors must be zero", finished_at="2026-11-05T10:02:00Z",
        rejected_artifact="rejected/nai-global-attempt-1.json", rejected_artifact_sha256=hashlib.sha256(original_raw).hexdigest())
    binding, selected = candidate(path, manifest, code_sha=sha, dirty=False)
    assert binding["source_count"] == 2 and len(selected["staged"]) == 2
    original.write_bytes(original_raw + b" ")
    with pytest.raises(ValueError, match="original digest"):
        candidate(path, manifest, code_sha=sha, dirty=False)


def test_trusted_ancestor_symlink_does_not_break_platform_paths(tmp_path):
    path, manifest, sha = fixture(tmp_path)
    alias = tmp_path / "host-alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    alternate = alias / "gen-1" / "sources" / path.name
    binding, _ = candidate(alternate, manifest, code_sha=sha, dirty=False)
    assert binding["artifact_sha256"] == manifest["sources"]["nai-global"]["artifact"]["sha256"]


def test_symlinked_sources_directory_is_refused(tmp_path):
    path, manifest, sha = fixture(tmp_path)
    directory = path.parent
    relocated = directory.with_name("relocated")
    directory.rename(relocated)
    directory.symlink_to(relocated, target_is_directory=True)
    with pytest.raises(ValueError, match="path invalid"):
        candidate(path, manifest, code_sha=sha, dirty=False)


def test_clock_unqualified_post_is_held_while_peer_proceeds(tmp_path):
    path, manifest, sha = fixture(tmp_path)
    data = json.loads(path.read_bytes())
    bad = post(2)
    bad["inventoryObservedAt"] = "2026-11-05T09:00:00Z"
    data["listings"].append(bad)
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    manifest["sources"]["nai-global"]["artifact"].update(
        sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
    binding, partition = candidate(path, manifest, code_sha=sha, dirty=False)
    assert binding["source_count"] == 2 and binding["staged_count"] == 1
    assert partition["held"] == [{"index": 1, "reason": "unqualified_source_post"}]


def test_probe_keeps_numeric_preimage_as_exact_json_text(tmp_path, monkeypatch):
    import cre_checkpoint_refresh as refresh
    import cre_ingest as ingest
    import cre_validate as validate

    path, manifest, sha = fixture(tmp_path)
    before = ('[{"external_id":"infabode:1","listing":{"lat":45.1234567890123456789},'
              '"source_index":null,"children":{}}]')
    monkeypatch.setattr(refresh, "git_identity", lambda: (sha, False))
    monkeypatch.setattr(ingest, "load_db_url", lambda _: ("fixture-only", None))
    monkeypatch.setattr(ingest, "database_target_fingerprint_from_url", lambda _: {"value": "b" * 64})
    monkeypatch.setattr(ingest, "find_psql", lambda: "psql")
    monkeypatch.setattr(validate, "_run_psql_script", lambda *a, **k: SimpleNamespace(returncode=0, stdout="fixture"))
    monkeypatch.setattr(validate, "parse_tsv", lambda _: [{"staged": "1", "joined": "1", "held": "[]", "preimage": before}])
    result = run_identity_probe(path, manifest, None)
    assert result["preimage"] == before
    assert result["preimage_sha256"] == hashlib.sha256(before.encode()).hexdigest()
    assert json.loads(json.dumps(result))["preimage"] == before


def test_only_count_hold_is_source_partial_and_preserves_failed_peer(tmp_path):
    path, manifest, sha = fixture(tmp_path)
    manifest["scope"] = {}
    manifest["config"].update(pipeline=True, sources=["nai-global", "peer"])
    manifest["sources"]["peer"] = {"artifact": {"path": "sources/peer.json"}}
    binding, _ = candidate(path, manifest, code_sha=sha, dirty=False)
    preimage = ('[{"external_id":"infabode:1","listing":null,"source_index":null,'
                '"children":{}}]')
    manifest["sources"]["nai-global"]["nai_post_identity_probe"] = {
        **binding, "held": [], "preimage": preimage,
        "preimage_sha256": hashlib.sha256(preimage.encode()).hexdigest()}
    report = {"summary": {"mode": "live", "torow_errors": 0, "baseline_updated": False,
              "baseline_update_sources": [], "hold_sources": ["nai-global", "peer"],
              "mark_missing_safe_sources": []},
              "generated_for_artifact": [str(path), str(path.parent / "peer.json")], "per_source": {
              "nai-global": {"current_active": 1, "verdict": "hold", "mark_missing_safe": False,
                             "reason": "current_active 1 below floor 100"},
              "peer": {"verdict": "hold", "reason": "provider error"}}}
    proof = admit(path, manifest, report, code_sha=sha, dirty=False, phase="source")
    assert proof["emitted_count"] == 1 and proof["whole_source_coverage"] is False
    assert report["summary"]["hold_sources"] == ["peer"]
    assert report["per_source"]["peer"]["reason"] == "provider error"
    manifest["sources"]["nai-global"]["gate"] = {"current_inventory_proof": proof}
    aggregate = copy.deepcopy(report)
    assert admit(path, manifest, aggregate, code_sha=sha, dirty=False, phase="aggregate") == proof
    attempt = "d" * 32
    envelope = {"manifest": manifest, "source_report": report, "aggregate_report": aggregate,
                "binding": proof, "attempt_id": attempt}
    context = ingest_subset_context(path, envelope, code_sha=sha, dirty=False,
                                    target_sha256="b" * 64, attempt_id=attempt)
    assert context["held"] == [] and context["attempt_id"] == attempt
    with pytest.raises(ValueError):
        ingest_subset_context(path, envelope, code_sha=sha, dirty=False,
                              target_sha256="c" * 64, attempt_id=attempt)
    bad = copy.deepcopy(report)
    bad["per_source"]["nai-global"].update(verdict="hold", reason="source pass error: access denied")
    with pytest.raises(ValueError, match="coverage-only"):
        admit(path, manifest, bad, code_sha=sha, dirty=False, phase="source")


@pytest.mark.parametrize("change", [
    lambda m: m["sources"]["nai-global"]["attempts"][-1].update(rc=1),
    lambda m: m["sources"]["nai-global"]["attempts"][-1].update(finished_at=None),
    lambda m: m["sources"]["nai-global"]["artifact"].update(sha256="0" * 64),
    lambda m: m["config"].update(mark_missing=True),
    lambda m: m["preflight"]["database_target"].update(value="bad"),
    lambda m: m.update(run_id="other"),
])
def test_unbound_source_refused(tmp_path, change):
    path, manifest, sha = fixture(tmp_path)
    altered = copy.deepcopy(manifest)
    change(altered)
    with pytest.raises((ValueError, KeyError)):
        candidate(path, altered, code_sha=sha, dirty=False)


def test_stale_or_future_claim_is_not_replayed(tmp_path, monkeypatch):
    path, manifest, sha = fixture(tmp_path)
    monkeypatch.setattr("nai_partial.admission._utc_now", lambda: datetime(2026, 11, 6, 12, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="no qualified posts"):
        candidate(path, manifest, code_sha=sha, dirty=False)
    monkeypatch.setattr("nai_partial.admission._utc_now", lambda: datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="freshness bound"):
        candidate(path, manifest, code_sha=sha, dirty=False)
