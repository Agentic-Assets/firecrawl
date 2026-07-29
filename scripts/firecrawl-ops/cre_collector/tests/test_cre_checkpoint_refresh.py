"""Pure, no-network contracts for cre_checkpoint_refresh.py."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import cre_checkpoint_refresh as refresh


ATTEMPT = "2026-07-29T12:00:00+00:00"


def listing(source="svn", index=1, tx="sale", **extra):
    return {
        "sourceKey": source,
        "transactionMode": tx,
        "id": f"id-{index}",
        "url": f"https://example.test/{source}/{index}",
        **extra,
    }


def artifact(source="svn", listings=None, **run_meta):
    rows = (
        listings
        if listings is not None
        else [listing(source, 1, "sale"), listing(source, 2, "lease")]
    )
    sale = sum(row["transactionMode"] == "sale" for row in rows)
    lease = sum(row["transactionMode"] == "lease" for row in rows)
    return {
        "runMeta": {
            "mode": "full",
            "transactions": ["sale", "lease"],
            "maxItemsPerSource": None,
            "startedAt": "2026-07-29T12:00:01+00:00",
            "finishedAt": "2026-07-29T12:01:00+00:00",
            **run_meta,
        },
        "sources": [
            {
                "sourceKey": source,
                "transaction": "sale",
                "supported": True,
                "listingsCollected": sale,
                "truncated": False,
            },
            {
                "sourceKey": source,
                "transaction": "lease",
                "supported": True,
                "listingsCollected": lease,
                "truncated": False,
            },
        ],
        "listings": rows,
        "brokers": [],
        "totalListings": len(rows),
    }


def write_artifact(tmp_path, payload):
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_registry_is_exactly_the_ingest_registry():
    assert refresh.SOURCE_KEYS == tuple(refresh.SOURCE_TO_BROKERAGE)
    assert len(refresh.SOURCE_KEYS) == 20


def test_collect_argv_is_single_source_full_unlimited(tmp_path):
    argv = refresh.build_collect_argv(
        "cbre", tmp_path / "cbre.json.tmp", page_cap=400, concurrency=3
    )
    assert "--source=cbre" in argv
    assert "--transaction=both" in argv
    assert "--max-items=0" in argv
    assert "--monitor" not in argv
    assert "--enrich-input" not in " ".join(argv)


def test_ingest_argv_is_additive_and_status_neutral(tmp_path):
    argv = refresh.build_ingest_argv(tmp_path / "source.json", "/tmp/equire.env")
    assert argv == [
        sys.executable,
        "cre_ingest.py",
        "--in",
        str(tmp_path / "source.json"),
        "--env-file",
        "/tmp/equire.env",
    ]
    assert not refresh.FORBIDDEN_INGEST_FLAGS.intersection(argv)


def test_dry_run_argv_builds_sql_without_live_flags(tmp_path):
    argv = refresh.build_ingest_dry_run_argv(
        tmp_path / "source.json", tmp_path / "sql"
    )
    assert "--dry-run" in argv
    assert "--keep-artifacts" in argv
    assert not refresh.FORBIDDEN_INGEST_FLAGS.intersection(argv)


def test_gate_reads_live_baseline_strictly_without_updating_it(tmp_path):
    argv = refresh.build_gate_argv(
        tmp_path / "source.json", tmp_path / "gate.json", "/tmp/equire.env"
    )
    assert "--apply" in argv
    assert "--strict" in argv
    assert "--update-baseline" not in argv


def test_safe_process_env_clears_status_activation():
    env = refresh.safe_process_env({"CRE_ACTIVATE_STATUS": "1", "PATH": "/bin"})
    assert "CRE_ACTIVATE_STATUS" not in env
    assert env["PATH"] == "/bin"


def test_fresh_env_for_buildout_uses_empty_resumable_run_cache(tmp_path):
    env, summary = refresh.fresh_source_env(
        "svn",
        tmp_path,
        {
            "BUILDOUT_CACHE_ONLY": "1",
            "BUILDOUT_ASSEMBLE_FROM_CACHE": "1",
            "BUILDOUT_USE_PAGE_CACHE": "1",
            "BUILDOUT_REFRESH_PAGE_CACHE": "1",
        },
    )
    assert "BUILDOUT_REFRESH_PAGE_CACHE" not in env
    assert "BUILDOUT_CACHE_ONLY" not in env
    assert "BUILDOUT_ASSEMBLE_FROM_CACHE" not in env
    assert "BUILDOUT_USE_PAGE_CACHE" not in env
    assert summary["BUILDOUT_CACHE_ONLY"] == "<unset>"
    assert str(tmp_path) in env["BUILDOUT_CACHE_DIR"]


@pytest.mark.parametrize(
    "source,key,value",
    [
        ("jll", "JLL_DETAIL_CACHE_DIR", "jll-detail"),
        ("jll-investor", "JLL_INVESTOR_SITEMAP_SCAN_LIMIT", "0"),
        ("avison-young", "AVISON_YOUNG_DETAIL_LIMIT", "1000000"),
        ("cushman-wakefield", "CUSHMAN_DETAIL_MODE", "full"),
        ("colliers-main", "COLLIERS_MAIN_MAX_FETCHES_PER_RUN", "2500"),
    ],
)
def test_fresh_env_source_profiles(tmp_path, source, key, value):
    env, _summary = refresh.fresh_source_env(source, tmp_path, {})
    assert value in env[key]


def test_valid_full_artifact_is_accepted(tmp_path):
    path = write_artifact(tmp_path, artifact())
    stats = refresh.validate_source_artifact(path, "svn", ATTEMPT)
    assert stats["flat_listings"] == 2
    assert stats["staged_unique"] == 2
    assert stats["rejected_by_ingest"] == 0
    assert len(stats["sha256"]) == 64


@pytest.mark.parametrize("mode", ["monitor", "enrich", None])
def test_wrong_artifact_mode_is_rejected(tmp_path, mode):
    payload = artifact(mode=mode)
    path = write_artifact(tmp_path, payload)
    with pytest.raises(refresh.ArtifactValidationError, match="mode"):
        refresh.validate_source_artifact(path, "svn", ATTEMPT)


def test_wrong_source_entry_is_rejected(tmp_path):
    payload = artifact()
    payload["sources"][0]["sourceKey"] = "cbre"
    path = write_artifact(tmp_path, payload)
    with pytest.raises(refresh.ArtifactValidationError, match="wrong sourceKey"):
        refresh.validate_source_artifact(path, "svn", ATTEMPT)


def test_wrong_listing_source_is_rejected(tmp_path):
    payload = artifact()
    payload["listings"][0]["sourceKey"] = "cbre"
    path = write_artifact(tmp_path, payload)
    with pytest.raises(refresh.ArtifactValidationError, match="wrong sourceKey"):
        refresh.validate_source_artifact(path, "svn", ATTEMPT)


def test_source_error_is_rejected(tmp_path):
    payload = artifact()
    payload["sources"][0]["error"] = "provider failed"
    path = write_artifact(tmp_path, payload)
    with pytest.raises(refresh.ArtifactValidationError, match="reported an error"):
        refresh.validate_source_artifact(path, "svn", ATTEMPT)


@pytest.mark.parametrize("truncated", [True, None])
def test_truncated_or_implicit_coverage_is_rejected(tmp_path, truncated):
    payload = artifact()
    if truncated is None:
        payload["sources"][0].pop("truncated")
    else:
        payload["sources"][0]["truncated"] = truncated
    path = write_artifact(tmp_path, payload)
    with pytest.raises(refresh.ArtifactValidationError, match="truncated=false"):
        refresh.validate_source_artifact(path, "svn", ATTEMPT)


def test_missing_transaction_entry_is_rejected(tmp_path):
    payload = artifact()
    payload["sources"] = payload["sources"][:1]
    path = write_artifact(tmp_path, payload)
    with pytest.raises(refresh.ArtifactValidationError, match="two source entries"):
        refresh.validate_source_artifact(path, "svn", ATTEMPT)


def test_count_mismatch_is_rejected(tmp_path):
    payload = artifact()
    payload["totalListings"] = 99
    path = write_artifact(tmp_path, payload)
    with pytest.raises(refresh.ArtifactValidationError, match="totalListings"):
        refresh.validate_source_artifact(path, "svn", ATTEMPT)


def test_ingest_rejected_listing_is_rejected(tmp_path):
    payload = artifact(listings=[listing(url=None)])
    path = write_artifact(tmp_path, payload)
    with pytest.raises(refresh.ArtifactValidationError, match="rejected by ingest"):
        refresh.validate_source_artifact(path, "svn", ATTEMPT)


def test_listing_detail_error_is_rejected(tmp_path):
    payload = artifact(listings=[listing(detailError="timeout")])
    path = write_artifact(tmp_path, payload)
    with pytest.raises(refresh.ArtifactValidationError, match="detailError"):
        refresh.validate_source_artifact(path, "svn", ATTEMPT)


def test_explicit_detail_unavailable_is_counted_when_children_are_preserved(tmp_path):
    payload = artifact(
        listings=[
            listing(
                detailUnavailable={"reason": "landing_not_setup"},
                preserveChildCollections=True,
                provisionalIdentity={"historyContinuity": "not_guaranteed"},
            )
        ]
    )
    path = write_artifact(tmp_path, payload)
    stats = refresh.validate_source_artifact(path, "svn", ATTEMPT)
    assert stats["detail_unavailable"] == 1
    assert stats["provisional_identities"] == 1
    assert stats["detail_errors"] == 0


def test_inventory_only_card_is_counted_without_canonical_staging(tmp_path):
    payload = artifact(
        source="cbre-dealflow",
        listings=[
            listing("cbre-dealflow", 1, "sale"),
            listing(
                "cbre-dealflow",
                2,
                "lease",
                url=None,
                id="card:abc123",
                preserveChildCollections=True,
                provisionalIdentity={"historyContinuity": "not_guaranteed"},
                inventoryOnly={
                    "reason": "no_provider_id_or_listing_url",
                    "indexUrl": "https://www.cbredealflow.com/",
                },
                detailUnavailable={"reason": "card_not_linked"},
            ),
        ],
    )
    path = write_artifact(tmp_path, payload)
    stats = refresh.validate_source_artifact(path, "cbre-dealflow", ATTEMPT)
    assert stats["staged_unique"] == 1
    assert stats["inventory_only"] == 1
    assert stats["rejected_by_ingest"] == 0


def test_empty_dealflow_full_snapshot_is_admitted_for_gated_watermark(tmp_path):
    payload = artifact(source="cbre-dealflow", listings=[])
    path = write_artifact(tmp_path, payload)
    stats = refresh.validate_source_artifact(path, "cbre-dealflow", ATTEMPT)
    assert stats["flat_listings"] == 0
    assert stats["staged_unique"] == 0
    assert stats["inventory_only"] == 0


def test_detail_unavailable_without_child_preservation_is_rejected(tmp_path):
    payload = artifact(
        listings=[listing(detailUnavailable={"reason": "landing_not_setup"})]
    )
    path = write_artifact(tmp_path, payload)
    with pytest.raises(refresh.ArtifactValidationError, match="child preservation"):
        refresh.validate_source_artifact(path, "svn", ATTEMPT)


def test_artifact_from_before_attempt_is_rejected(tmp_path):
    payload = artifact(
        startedAt="2026-07-29T11:00:00+00:00",
        finishedAt="2026-07-29T11:01:00+00:00",
    )
    path = write_artifact(tmp_path, payload)
    with pytest.raises(refresh.ArtifactValidationError, match="predates"):
        refresh.validate_source_artifact(path, "svn", ATTEMPT)


def test_atomic_manifest_write_replaces_complete_json(tmp_path):
    path = tmp_path / "manifest.json"
    refresh.atomic_write_json(path, {"value": 1})
    refresh.atomic_write_json(path, {"value": 2})
    assert json.loads(path.read_text()) == {"value": 2}
    assert not list(tmp_path.glob(".manifest.json.*.tmp"))


def test_resume_rejects_git_or_configuration_drift(tmp_path):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    path = run_dir / "manifest.json"
    refresh.atomic_write_json(path, manifest)
    with pytest.raises(refresh.RefreshError, match="Git SHA"):
        refresh.load_resume_manifest(
            path, git_sha="def", sources=("svn",), page_cap=400, concurrency=3
        )
    with pytest.raises(refresh.RefreshError, match="configuration"):
        refresh.load_resume_manifest(
            path, git_sha="abc", sources=("svn",), page_cap=401, concurrency=3
        )


def test_resume_checkpoint_requires_matching_artifact_hash(tmp_path):
    run_dir = tmp_path / "run"
    source_path = run_dir / "sources" / "svn.json"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(json.dumps(artifact()), encoding="utf-8")
    checkpoint = {
        "artifact": {
            "path": "sources/svn.json",
            "sha256": "0" * 64,
            "attempt_started_at": ATTEMPT,
        }
    }
    assert refresh._checkpoint_artifact_valid(run_dir, checkpoint, "svn") is None


def test_lock_refuses_live_owner(tmp_path):
    lock_dir = tmp_path / ".cre.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text(f"{os.getpid()} 1\n", encoding="utf-8")
    lock = refresh.SharedLock(lock_dir)
    with pytest.raises(refresh.LockHeldError, match="live owner"):
        lock.acquire()


def test_lock_reclaims_dead_owner(tmp_path):
    lock_dir = tmp_path / ".cre.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text("99999999 1\n", encoding="utf-8")
    lock = refresh.SharedLock(lock_dir)
    lock.acquire()
    try:
        assert refresh._lock_owner(lock_dir) == os.getpid()
    finally:
        lock.release()
    assert not lock_dir.exists()


def test_canonical_lock_uses_primary_checkout_for_worktree(tmp_path, monkeypatch):
    primary = tmp_path / "primary"
    common_git = primary / ".git"

    class Proc:
        stdout = str(common_git) + "\n"

    monkeypatch.setattr(refresh.subprocess, "run", lambda *_args, **_kwargs: Proc())
    assert refresh.canonical_shared_lock_dir(tmp_path / "worktree") == (
        primary
        / "scripts"
        / "firecrawl-ops"
        / "cre_collector"
        / "out"
        / "daily"
        / ".cre.lock"
    )


def test_gate_hold_is_recorded_without_being_infrastructure_failure(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    source_path = run_dir / "sources" / "svn.json"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(json.dumps(artifact()), encoding="utf-8")

    def fake_run(_argv, _log, **_kwargs):
        gate = {
            "per_source": {
                "svn": {
                    "verdict": "hold",
                    "reason": "below baseline",
                    "mark_missing_safe": False,
                }
            }
        }
        refresh.atomic_write_json(run_dir / "gates" / "svn.json", gate)
        return 2

    monkeypatch.setattr(refresh, "run_command", fake_run)
    refresh.gate_source(run_dir, manifest, "svn", source_path, None)
    assert manifest["sources"]["svn"]["state"] == "gated"
    assert manifest["sources"]["svn"]["gate"]["verdict"] == "hold"
    assert manifest["sources"]["svn"]["gate"]["mark_missing_safe"] is False


def test_gate_infrastructure_failure_is_fatal(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    run_dir.mkdir(parents=True)
    source_path = run_dir / "source.json"
    source_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(refresh, "run_command", lambda *_args, **_kwargs: 1)
    with pytest.raises(refresh.GlobalStageError, match="infrastructure"):
        refresh.gate_source(run_dir, manifest, "svn", source_path, None)


def test_aggregate_gate_hold_prevents_completion(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    artifact_path = run_dir / "sources" / "svn.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(json.dumps(artifact()), encoding="utf-8")
    manifest["sources"]["svn"]["artifact"] = {"path": "sources/svn.json"}

    def fake_run(_argv, _log, **_kwargs):
        refresh.atomic_write_json(
            run_dir / "aggregate-gate.json",
            {
                "per_source": {"svn": {"verdict": "hold"}},
                "summary": {
                    "hold_sources": ["svn"],
                    "mark_missing_safe_brokerages": [],
                }
            },
        )
        return 2

    monkeypatch.setattr(refresh, "run_command", fake_run)
    with pytest.raises(refresh.RefreshError, match="not established"):
        refresh.run_aggregate_gate(run_dir, manifest, None)
    assert manifest["aggregate_gate"]["hold_sources"] == ["svn"]


def test_aggregate_gate_first_seen_prevents_completion(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    artifact_path = run_dir / "sources" / "svn.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(json.dumps(artifact()), encoding="utf-8")
    manifest["sources"]["svn"]["artifact"] = {"path": "sources/svn.json"}

    def fake_run(_argv, _log, **_kwargs):
        refresh.atomic_write_json(
            run_dir / "aggregate-gate.json",
            {
                "per_source": {"svn": {"verdict": "first_seen"}},
                "summary": {
                    "hold_sources": [],
                    "mark_missing_safe_brokerages": [],
                },
            },
        )
        return 0

    monkeypatch.setattr(refresh, "run_command", fake_run)
    with pytest.raises(refresh.RefreshError, match="not established"):
        refresh.run_aggregate_gate(run_dir, manifest, None)
    assert manifest["aggregate_gate"]["non_ok_sources"] == ["svn"]


def test_aggregate_gate_missing_configured_source_prevents_completion(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    artifact_path = run_dir / "sources" / "svn.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(json.dumps(artifact()), encoding="utf-8")
    manifest["sources"]["svn"]["artifact"] = {"path": "sources/svn.json"}

    def fake_run(_argv, _log, **_kwargs):
        refresh.atomic_write_json(
            run_dir / "aggregate-gate.json",
            {
                "per_source": {},
                "summary": {
                    "hold_sources": [],
                    "mark_missing_safe_brokerages": [],
                },
            },
        )
        return 0

    monkeypatch.setattr(refresh, "run_command", fake_run)
    with pytest.raises(refresh.RefreshError, match="not established"):
        refresh.run_aggregate_gate(run_dir, manifest, None)
    assert manifest["aggregate_gate"]["non_ok_sources"] == ["svn"]


def test_dry_run_failure_prevents_ready_state(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    monkeypatch.setattr(refresh, "run_command", lambda *_args, **_kwargs: 9)
    ok = refresh.dry_run_source(
        run_dir, manifest, "svn", run_dir / "sources" / "svn.json"
    )
    assert ok is False
    assert manifest["sources"]["svn"]["state"] == "dry_run_failed"


def test_ingest_failure_remains_retryable(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    monkeypatch.setattr(refresh, "run_command", lambda *_args, **_kwargs: 7)
    with pytest.raises(refresh.GlobalStageError, match="additive ingest failed"):
        refresh.ingest_source(
            run_dir, manifest, "svn", run_dir / "sources" / "svn.json", None
        )
    assert manifest["sources"]["svn"]["state"] == "ingest_failed"
    assert manifest["sources"]["svn"]["ingest"]["additive"] is True


def test_validation_readback_requires_exact_staged_count(tmp_path):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    manifest["sources"]["svn"]["artifact"] = {
        "finished_at": "2026-07-29T12:01:00+00:00",
        "staged_unique": 2,
    }
    validation = {
        "queries": {
            "source_counts": [
                {
                    "source_key": "svn",
                    "latest_inventory_observed_at": "2026-07-29 12:01:00Z",
                    "latest_inventory_batch_active": "1",
                    "latest_scraped_at": "2026-07-28 12:01:00Z",
                    "latest_batch_active": "1",
                    "detail_unavailable": "1",
                }
            ],
            "inventory_only_index": [
                {
                    "source_key": "cbre-dealflow",
                    "active": "0",
                    "soft_deleted": "0",
                    "latest_batch_active": "0",
                    "latest_enumerated_at": "",
                    "scope_watermark_at": "",
                }
            ],
        }
    }
    result = refresh.verify_validation_readback(run_dir, manifest, validation)
    assert result == {"ok": False, "failed_sources": ["svn"]}
    assert manifest["sources"]["svn"]["readback"]["ok"] is False


def test_validation_readback_requires_exact_inventory_only_count(tmp_path):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("cbre-dealflow",),
        page_cap=400,
        concurrency=3,
    )
    manifest["sources"]["cbre-dealflow"]["artifact"] = {
        "finished_at": "2026-07-29T12:01:00+00:00",
        "staged_unique": 2,
        "inventory_only": 1,
    }
    validation = {
        "queries": {
            "source_counts": [
                {
                    "source_key": "cbre-dealflow",
                    "latest_inventory_observed_at": "2026-07-29 12:01:00Z",
                    "latest_inventory_batch_active": "2",
                    "latest_scraped_at": "2026-07-29 12:01:00Z",
                    "latest_batch_active": "2",
                    "detail_unavailable": "1",
                }
            ],
            "inventory_only_index": [
                {
                    "source_key": "cbre-dealflow",
                    "active": "2",
                    "soft_deleted": "0",
                    "latest_batch_active": "2",
                    "latest_enumerated_at": "2026-07-29 12:01:00Z",
                    "scope_watermark_at": "2026-07-29 12:01:00Z",
                }
            ],
        }
    }
    result = refresh.verify_validation_readback(run_dir, manifest, validation)
    assert result == {"ok": False, "failed_sources": ["cbre-dealflow"]}
    readback = manifest["sources"]["cbre-dealflow"]["readback"]
    assert readback["inventory_only"]["ok"] is False
    assert "active inventory-only 2 != expected 1" == readback["reason"]


def test_validation_quality_rejects_new_defects_and_child_collapse():
    before = {
        "queries": {
            "duplicates": [
                {
                    "check_name": "duplicate_external_id_groups",
                    "source_key": "",
                    "groups": "0",
                    "rows": "0",
                }
            ],
            "bad_child_urls": [{"check_name": "image_bad_url", "count": "0"}],
            "orphans": [{"child_type": "images", "orphan_rows": "0"}],
            "quality_by_source": [
                {"source_key": "svn", "bad_source_url": "0", "impossible_lat": "0"}
            ],
            "child_counts": [
                {"source_key": "svn", "child_type": "images", "count": "100"}
            ],
            "search_smoke": [{"smoke": "office_sale", "rows": "10"}],
        }
    }
    after = json.loads(json.dumps(before))
    after["queries"]["bad_child_urls"][0]["count"] = "1"
    after["queries"]["child_counts"][0]["count"] = "50"
    after["queries"]["search_smoke"][0]["rows"] = "0"
    result = refresh.compare_validation_quality(before, after)
    assert result["ok"] is False
    assert any("bad_child_urls" in failure for failure in result["failures"])
    assert any("fell more than 30%" in failure for failure in result["failures"])
    assert any("search_smoke" in failure for failure in result["failures"])


def test_scope_readback_counts_unsupported_active_rows(tmp_path):
    manifest = refresh.new_manifest(
        tmp_path / "run",
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    refresh.record_scope_from_validation(
        manifest,
        {
            "queries": {
                "source_counts": [
                    {"source_key": "svn", "active": "100"},
                    {"source_key": "legacy-broker", "active": "12"},
                ]
            }
        },
    )
    assert manifest["scope"]["unsupported_active_rows_before"] == 12


def test_report_contains_source_state_without_credentials(tmp_path):
    manifest = refresh.new_manifest(
        tmp_path / "run",
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    manifest["sources"]["svn"]["state"] = "ingested"
    report = refresh.render_report(manifest)
    assert "| svn | ingested |" in report
    assert "postgres://" not in report
    assert "password" not in report.lower()
