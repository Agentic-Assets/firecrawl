import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from cre_nai_post_admission import admit, candidate, run_identity_probe
from tests.test_nai_post_partial import post


def fixture(tmp_path):
    root = tmp_path / "gen-1"
    source = root / "sources"
    source.mkdir(parents=True)
    path = source / "nai-global.json"
    data = {"runMeta": {"transactions": ["sale", "lease"], "mode": "full",
                        "startedAt": "2026-11-05T09:59:00Z", "finishedAt": "2026-11-05T10:00:02Z",
                        "freshness": {"generationId": "gen-1"}}, "listings": [post()]}
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    sha = "a" * 40
    manifest = {"run_id": "gen-1", "started_at": "2026-11-05T09:58:00Z", "collector_git_sha": sha,
                "collector_git_dirty": False, "config": {"sources": ["nai-global"], "additive": True,
                "mark_missing": False, "status_activation": False, "transactions": ["sale", "lease"],
                "max_items": 0}, "preflight": {"database_target": {"algorithm": "sha256", "value": "b" * 64}},
                "sources": {"nai-global": {"artifact": {"path": "sources/nai-global.json",
                "attempt_started_at": "2026-11-05T09:58:30Z", "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw), "freshness_generation_id": "gen-1"}, "attempts": [{"rc": 0,
                "error": None, "started_at": "2026-11-05T09:58:30Z", "finished_at": "2026-11-05T10:00:03Z"}]}}}
    return path, manifest, sha


def test_bound_artifact_partition(tmp_path):
    path, manifest, sha = fixture(tmp_path)
    binding, partition = candidate(path, manifest, code_sha=sha, dirty=False)
    assert binding["source_count"] == 1 and binding["staged_count"] == 1
    assert partition["staged"][0]["external_id"] == "infabode:1"


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


def test_only_count_hold_is_source_partial_and_preserves_failed_peer(tmp_path, monkeypatch):
    import cre_gate
    path, manifest, sha = fixture(tmp_path)
    manifest["scope"] = {}
    binding, _ = candidate(path, manifest, code_sha=sha, dirty=False)
    preimage = ('[{"external_id":"infabode:1","listing":null,"source_index":null,'
                '"children":{}}]')
    manifest["sources"]["nai-global"]["nai_post_identity_probe"] = {
        **binding, "held": [], "preimage": preimage,
        "preimage_sha256": hashlib.sha256(preimage.encode()).hexdigest()}
    monkeypatch.setattr(cre_gate, "count_artifacts", lambda *a, **k: ({"nai-global": 1}, {}, {"nai-global"}, None, 0))
    # Exercise aggregate member handling without manufacturing a second artifact.
    monkeypatch.setattr("cre_current_inventory_admission.validate_report_scope", lambda *a: None)
    report = {"summary": {"mode": "live", "torow_errors": 0, "baseline_updated": False,
              "baseline_update_sources": [], "hold_sources": ["nai-global", "peer"],
              "mark_missing_safe_sources": []}, "per_source": {
              "nai-global": {"current_active": 1, "verdict": "hold", "mark_missing_safe": False,
                             "reason": "current_active 1 below floor 100"},
              "peer": {"verdict": "hold", "reason": "provider error"}}}
    proof = admit(path, manifest, report, code_sha=sha, dirty=False, phase="source")
    assert proof["emitted_count"] == 1 and proof["whole_source_coverage"] is False
    assert report["summary"]["hold_sources"] == ["peer"]
    assert report["per_source"]["peer"]["reason"] == "provider error"
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
