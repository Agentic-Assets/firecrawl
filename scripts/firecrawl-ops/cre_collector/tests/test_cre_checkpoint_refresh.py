"""Pure, no-network contracts for cre_checkpoint_refresh.py."""

from __future__ import annotations

import json
import io
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import cre_checkpoint_refresh as refresh


ATTEMPT = "2026-07-29T12:00:00+00:00"


def listing(source="svn", index=1, tx="sale", **extra):
    host = (
        "www.cushmanwakefield.com"
        if source == "cushman-wakefield"
        else "example.test"
    )
    return {
        "sourceKey": source,
        "transactionMode": tx,
        "id": f"id-{index}",
        "url": f"https://{host}/{source}/{index}",
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


def strict_artifact(
    source="svn",
    detail_scope="authoritative_inventory_feed",
    *,
    generation="refresh-generation-1",
    generation_started_at="2026-07-29T12:00:00+00:00",
    preserve_children=None,
):
    if preserve_children is None:
        preserve_children = (
            source in refresh.CHILD_PRESERVING_AUTHORITATIVE_FEED_SOURCE_KEYS
        )
    payload = artifact(source=source)
    observed = "2026-07-29T12:00:30+00:00"
    payload["runMeta"]["freshness"] = {
        "generationId": generation,
        "generationStartedAt": generation_started_at,
        "requireFreshDetails": True,
    }
    for row in payload["listings"]:
        row["inventoryObservedAt"] = observed
        row["freshnessProvenance"] = {
            "detailScope": detail_scope,
            "generationId": generation,
            "method": "test",
            "cacheDisposition": "live",
        }
        if detail_scope != "authoritative_inventory_feed":
            row["detailObservedAt"] = observed
        if preserve_children:
            row["preserveChildCollections"] = True
    for entry in payload["sources"]:
        count = entry["listingsCollected"]
        entry["freshness"] = {
            "listings": count,
            "inventoryObserved": count,
            "detailObserved": 0 if detail_scope == "authoritative_inventory_feed" else count,
            "authoritativeInventoryFeed": (
                count if detail_scope == "authoritative_inventory_feed" else 0
            ),
            "detailErrors": 0,
            "childPreservationRows": count if preserve_children else 0,
            "staleInventoryObservations": 0,
            "staleDetailObservations": 0,
        }
    return payload


def avison_property_detail_artifact():
    payload = artifact(source="avison-young")
    generation = "refresh-generation-1"
    observed = "2026-07-29T12:00:30+00:00"
    payload["runMeta"]["freshness"] = {
        "generationId": generation,
        "generationStartedAt": "2026-07-29T12:00:00+00:00",
        "requireFreshDetails": False,
        "requireFreshPropertyDetails": True,
    }
    for row in payload["listings"]:
        row.update(
            {
                "inventoryObservedAt": observed,
                "detailObservedAt": observed,
                "preserveChildCollections": True,
                "detailObservedWithChildPreservation": True,
                "freshnessProvenance": {
                    "detailScope": "detail_page",
                    "generationId": generation,
                    "method": "avison_young_detail",
                    "cacheDisposition": "live",
                },
            }
        )
    for entry in payload["sources"]:
        count = entry["listingsCollected"]
        entry["freshness"] = {
            "listings": count,
            "inventoryObserved": count,
            "detailObserved": count,
            "authoritativeInventoryFeed": 0,
            "detailErrors": 0,
            "childPreservationRows": count,
            "staleInventoryObservations": 0,
            "staleDetailObservations": 0,
        }
    return payload


def strict_artifact_info(staged=2):
    return {
        "finished_at": "2026-07-29T12:01:00+00:00",
        "staged_unique": staged,
        "inventory_only": 0,
        "strict_freshness": True,
        "freshness_generation_id": "refresh-generation-1",
        "freshness_generation_started_at": "2026-07-29T12:00:00+00:00",
    }


def freshness_generation_row(source="svn", active=2, **overrides):
    policy = refresh.load_source_policy()[source]
    evidence_class = policy["evidence_class"]
    requires_detail = evidence_class in {"strict_detail", "property_detail"}
    row = {
        "source_key": source,
        "evidence_class": evidence_class,
        "detail_claim": policy["detail_claim"],
        "generation_id": "refresh-generation-1",
        "detail_scopes": [
            "authoritative_inventory_feed"
            if evidence_class == "authoritative_inventory_feed"
            else "detail_page"
        ],
        "cache_dispositions": ["live"],
        "active": str(active),
        "persisted_inventory_observed": str(active),
        "persisted_detail_observed": str(active if requires_detail else 0),
        "missing_persisted_detail_proof": "0",
        "earliest_inventory_observed_at": "2026-07-29 12:00:10Z",
        "latest_inventory_observed_at": "2026-07-29 12:00:40Z",
        "earliest_detail_observed_at": (
            "2026-07-29 12:00:15Z" if requires_detail else None
        ),
        "latest_detail_observed_at": (
            "2026-07-29 12:00:50Z" if requires_detail else None
        ),
    }
    row.update(overrides)
    return row


def absolute_quality_report(source="svn", **quality_overrides):
    quality = {
        "source_key": source,
        "bad_source_url": "0",
        "missing_canonical_url": "0",
        "bad_canonical_url": "0",
        "invalid_state": "0",
        "impossible_lat": "0",
        "impossible_lng": "0",
        "sale_price_flags": "0",
        "sale_psf_flags": "0",
        "lease_rate_min_flags": "0",
        "lease_rate_max_flags": "0",
        "cap_rate_flags": "0",
        # Coordinates are intentionally allowed to be sparse.
        "missing_coords": "99",
    }
    quality.update(quality_overrides)
    return {
        "ok": True,
        "queries": {
            "duplicates": [
                {
                    "check_name": "duplicate_external_id_groups",
                    "source_key": None,
                    "groups": "0",
                    "rows": "0",
                }
            ],
            "bad_child_urls": [
                {"check_name": check_name, "count": "0"}
                for check_name in refresh.ABSOLUTE_BAD_CHILD_URL_CHECKS
            ],
            "primary_child_conflicts": [],
            "orphans": [
                {"child_type": child_type, "orphan_rows": "0"}
                for child_type in refresh.ABSOLUTE_ORPHAN_CHILD_TYPES
            ],
            "quality_by_source": [quality],
        },
    }


def test_registry_is_exactly_the_ingest_registry():
    assert refresh.SOURCE_KEYS == tuple(refresh.SOURCE_TO_BROKERAGE)
    assert len(refresh.SOURCE_KEYS) == 51


def test_authoritative_feed_admission_does_not_expand_inventory_only_storage():
    assert set(refresh.INVENTORY_ONLY_SOURCE_DEFINITIONS) == {
        "cbre-dealflow",
        "colliers",
    }
    assert (
        refresh.CHILD_PRESERVING_AUTHORITATIVE_FEED_SOURCE_KEYS
        & set(refresh.INVENTORY_ONLY_SOURCE_DEFINITIONS)
        == {"cbre-dealflow"}
    )


def test_collect_argv_is_single_source_full_unlimited(tmp_path):
    argv = refresh.build_collect_argv(
        "cbre", tmp_path / "cbre.json.tmp", page_cap=400, concurrency=3
    )
    assert "--source=cbre" in argv
    assert "--transaction=both" in argv
    assert "--max-items=0" in argv
    assert "--monitor" not in argv
    assert "--enrich-input" not in " ".join(argv)


def test_collect_argv_can_select_one_additive_transaction_scope(tmp_path):
    argv = refresh.build_collect_argv(
        "savills",
        tmp_path / "savills.json.tmp",
        transactions=("lease",),
    )
    assert "--source=savills" in argv
    assert "--transaction=lease" in argv
    assert "--transaction=both" not in argv
    assert "--max-items=0" in argv


def test_parse_transactions_accepts_only_canonical_scopes():
    assert refresh.parse_transactions("both") == ("sale", "lease")
    assert refresh.parse_transactions("sale") == ("sale",)
    assert refresh.parse_transactions("lease") == ("lease",)
    with pytest.raises(ValueError, match="sale, lease, or both"):
        refresh.parse_transactions("lease,sale")


def test_single_transaction_artifact_is_valid_only_for_its_bound_scope(tmp_path):
    payload = strict_artifact(source="svn")
    payload["runMeta"]["transactions"] = ["lease"]
    payload["sources"] = [
        entry for entry in payload["sources"] if entry["transaction"] == "lease"
    ]
    payload["listings"] = [
        row for row in payload["listings"] if row["transactionMode"] == "lease"
    ]
    payload["totalListings"] = len(payload["listings"])
    path = write_artifact(tmp_path, payload)

    stats = refresh.validate_source_artifact(
        path,
        "svn",
        ATTEMPT,
        require_strict_freshness=True,
        expected_generation_id="refresh-generation-1",
        expected_generation_started_at="2026-07-29T12:00:00+00:00",
        expected_transactions=("lease",),
        now=datetime(2026, 7, 29, 13, 0, tzinfo=timezone.utc),
    )
    assert stats["flat_listings"] == 1

    with pytest.raises(refresh.ArtifactValidationError, match="runMeta.transactions"):
        refresh.validate_source_artifact(
            path,
            "svn",
            ATTEMPT,
            require_strict_freshness=True,
            now=datetime(2026, 7, 29, 13, 0, tzinfo=timezone.utc),
        )


def test_strict_artifact_rejects_observations_older_than_completion_slo(tmp_path):
    payload = strict_artifact(source="svn")
    payload["runMeta"]["finishedAt"] = "2026-07-30T12:01:00+00:00"
    path = write_artifact(tmp_path, payload)

    with pytest.raises(
        refresh.ArtifactValidationError,
        match="inventory observation exceeds the 24-hour artifact freshness SLO",
    ):
        refresh.validate_source_artifact(
            path,
            "svn",
            ATTEMPT,
            require_strict_freshness=True,
            expected_generation_id="refresh-generation-1",
            expected_generation_started_at="2026-07-29T12:00:00+00:00",
            now=datetime(2026, 7, 30, 13, tzinfo=timezone.utc),
        )


def test_subset_manifest_records_non_full_scope(tmp_path):
    manifest = refresh.new_manifest(
        tmp_path / "run",
        git_sha="abc",
        git_dirty=False,
        sources=("savills",),
        transactions=("lease",),
        page_cap=400,
        concurrency=3,
    )
    assert manifest["scope"]["kind"] == "collector_registry_transaction_subset"
    assert manifest["config"]["transactions"] == ["lease"]


def test_full_manifest_can_bind_explicit_additive_coverage_hold_mode(tmp_path):
    manifest = refresh.new_manifest(
        tmp_path / "run",
        git_sha="abc",
        git_dirty=False,
        sources=("nai-global",),
        page_cap=400,
        concurrency=3,
        admit_baseline_hold_additively=True,
    )
    assert manifest["config"]["transactions"] == ["sale", "lease"]
    assert manifest["config"]["admit_baseline_hold_additively"] is True


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


def test_database_child_argv_carries_expected_target_fingerprint(tmp_path):
    expected = "a" * 64
    ingest = refresh.build_ingest_argv(
        tmp_path / "source.json",
        "/tmp/equire.env",
        expected_db_target_sha256=expected,
    )
    gate = refresh.build_gate_argv(
        tmp_path / "source.json",
        tmp_path / "gate.json",
        "/tmp/equire.env",
        expected_db_target_sha256=expected,
    )
    validate = refresh.build_validate_argv(
        tmp_path / "validation.json",
        "/tmp/equire.env",
        expected_db_target_sha256=expected,
    )

    for argv in (ingest, gate, validate):
        assert argv[argv.index("--expected-db-target-sha256") + 1] == expected


def test_strict_ingest_argv_passes_explicit_freshness_requirement(tmp_path):
    argv = refresh.build_ingest_argv(
        tmp_path / "source.json",
        None,
        require_strict_freshness=True,
    )
    assert "--require-strict-freshness" in argv


def test_dry_run_argv_builds_sql_without_live_flags(tmp_path):
    argv = refresh.build_ingest_dry_run_argv(
        tmp_path / "source.json", tmp_path / "sql"
    )
    assert "--dry-run" in argv
    assert "--keep-artifacts" in argv
    assert not refresh.FORBIDDEN_INGEST_FLAGS.intersection(argv)


def test_strict_dry_run_argv_passes_explicit_freshness_requirement(tmp_path):
    argv = refresh.build_ingest_dry_run_argv(
        tmp_path / "source.json",
        tmp_path / "sql",
        require_strict_freshness=True,
    )
    assert "--require-strict-freshness" in argv


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


def test_run_command_interrupt_terminates_its_process_group(tmp_path, monkeypatch):
    spawned = {}
    signals = []

    class FakeProcess:
        pid = 4321

        def __init__(self):
            self.wait_calls = 0

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise KeyboardInterrupt
            return -2

    process = FakeProcess()

    def fake_popen(argv, **kwargs):
        spawned["argv"] = argv
        spawned["kwargs"] = kwargs
        return process

    monkeypatch.setattr(refresh.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        refresh.os, "killpg", lambda pid, sig: signals.append((pid, sig))
    )

    with pytest.raises(KeyboardInterrupt):
        refresh.run_command(["node", "collect.ts"], tmp_path / "command.log", env={})

    assert spawned["kwargs"]["start_new_session"] is True
    assert signals == [(4321, refresh.signal.SIGINT)]
    assert process.wait_calls == 2
    assert "terminating process group 4321" in (tmp_path / "command.log").read_text()


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
    assert env["CRE_REFRESH_GENERATION"] == tmp_path.name
    assert env["CRE_REQUIRE_FRESH_DETAILS"] == "1"


def test_buildout_retry_refreshes_live_pages_instead_of_reusing_failed_snapshot(
    tmp_path,
):
    env, summary = refresh.fresh_source_env(
        "lee-associates",
        tmp_path,
        {},
        attempt_number=2,
    )
    assert env["BUILDOUT_REFRESH_PAGE_CACHE"] == "1"
    assert summary["BUILDOUT_REFRESH_PAGE_CACHE"] == "1"


@pytest.mark.parametrize("source", sorted(refresh.BUILDOUT_SOURCE_KEYS))
def test_every_buildout_source_gets_exact_fresh_cache_environment(tmp_path, source):
    env, summary = refresh.fresh_source_env(
        source,
        tmp_path,
        {
            "BUILDOUT_CACHE_ONLY": "1",
            "BUILDOUT_ASSEMBLE_FROM_CACHE": "1",
            "BUILDOUT_USE_PAGE_CACHE": "1",
        },
        attempt_number=2,
    )
    assert env["BUILDOUT_REFRESH_PAGE_CACHE"] == "1"
    assert env["BUILDOUT_CACHE_DIR"] == str(tmp_path / "cache" / "buildout")
    assert "BUILDOUT_CACHE_ONLY" not in env
    assert "BUILDOUT_ASSEMBLE_FROM_CACHE" not in env
    assert "BUILDOUT_USE_PAGE_CACHE" not in env
    assert summary["BUILDOUT_CACHE_ONLY"] == "<unset>"


@pytest.mark.parametrize(
    "source,key,value",
    [
        ("jll", "JLL_DETAIL_CACHE_DIR", "jll-detail"),
        ("jll-investor", "JLL_INVESTOR_SITEMAP_SCAN_LIMIT", "0"),
        ("avison-young", "AVISON_YOUNG_DETAIL_LIMIT", "1000000"),
        ("cushman-wakefield", "CUSHMAN_DETAIL_MODE", "base"),
        ("colliers-main", "COLLIERS_MAIN_MAX_FETCHES_PER_RUN", "2500"),
        ("colliers-main", "COLLIERS_MAIN_DETAIL_CONCURRENCY", "1"),
    ],
)
def test_fresh_env_source_profiles(tmp_path, source, key, value):
    env, _summary = refresh.fresh_source_env(source, tmp_path, {})
    assert value in env[key]


@pytest.mark.parametrize("source", sorted(refresh.STRICT_FRESHNESS_SOURCE_KEYS))
def test_fresh_env_requires_provenance_for_strict_sources(tmp_path, source):
    env, _summary = refresh.fresh_source_env(source, tmp_path, {})
    assert env["CRE_REQUIRE_FRESH_DETAILS"] == "1"


def test_fresh_env_enriches_all_avison_details_without_claiming_strict_contacts(
    tmp_path,
):
    env, _summary = refresh.fresh_source_env("avison-young", tmp_path, {})
    assert env["AVISON_YOUNG_DETAIL_LIMIT"] == "1000000"
    assert env["AVISON_YOUNG_DETAIL_TRANSPORT"] == "direct"
    assert env["CRE_REQUIRE_FRESH_PROPERTY_DETAILS"] == "1"
    assert "CRE_REQUIRE_FRESH_DETAILS" not in env
    assert env["CRE_REFRESH_GENERATION"] == tmp_path.name


def _colliers_chunk_artifact(*, complete: bool):
    payload = artifact(source="colliers-main")
    for entry in payload["sources"]:
        entry["truncated"] = not complete
    return payload


def test_colliers_cache_progress_ignores_interrupted_final_json_line(tmp_path):
    cache_path = tmp_path / "cache" / "colliers-main" / "detail-cache.jsonl"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text('{"id":"main:1"}\n{"id":', encoding="utf-8")

    assert refresh._cache_line_count(cache_path) == 1


def test_colliers_dependency_preflight_fails_before_consuming_source_attempt(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "checkpoint"
    monkeypatch.setattr(refresh, "utc_now", lambda: "2026-07-31T12:00:00+00:00")
    monkeypatch.setattr(
        refresh,
        "collector_runtime_dependency_error",
        lambda: "collector runtime dependencies are unavailable: cheerio",
    )
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("colliers-main",),
        page_cap=400,
        concurrency=3,
    )

    result = refresh.collect_source(
        run_dir,
        manifest,
        "colliers-main",
        transactions=("sale", "lease"),
        page_cap=400,
        concurrency=3,
        attempts_this_run=3,
    )

    assert result is None
    checkpoint = manifest["sources"]["colliers-main"]
    assert checkpoint["state"] == "collect_infrastructure_failed"
    assert checkpoint["attempts"] == []
    assert checkpoint["collection_preflight"]["ok"] is False


def test_colliers_main_chunks_continue_until_complete_artifact(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache" / "colliers-main" / "detail-cache.jsonl"
    chunks = []
    calls = []

    def fake_run(argv, log_path, *, env):
        calls.append((argv, log_path, env))
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"id": f"main:{len(calls)}"}) + "\n")
        out = Path(next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--out=")))
        out.write_text(
            json.dumps(_colliers_chunk_artifact(complete=len(calls) == 3)),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(refresh, "run_command", fake_run)
    rc, error = refresh.collect_colliers_main_chunks(
        tmp_path,
        output=tmp_path / "sources" / "colliers-main.json.tmp",
        attempt_number=1,
        page_cap=400,
        concurrency=2,
        transactions=("sale", "lease"),
        env={"COLLIERS_MAIN_DETAIL_CACHE_PATH": str(cache_path)},
        on_chunk=chunks.append,
    )

    assert (rc, error) == (0, None)
    assert len(calls) == 3
    assert [chunk["cache_rows_before"] for chunk in chunks] == [0, 1, 2]
    assert [chunk["cache_rows_after"] for chunk in chunks] == [1, 2, 3]
    assert [chunk["artifact_complete"] for chunk in chunks] == [False, False, True]
    assert all("--source=colliers-main" in argv for argv, _log, _env in calls)


def test_colliers_main_chunks_resume_from_run_local_cache(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache" / "colliers-main" / "detail-cache.jsonl"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("\n".join('{"id":"main:old"}' for _ in range(2500)) + "\n")
    chunks = []

    def fake_run(argv, _log_path, *, env):
        assert env["COLLIERS_MAIN_DETAIL_CACHE_PATH"] == str(cache_path)
        with cache_path.open("a", encoding="utf-8") as handle:
            handle.write('{"id":"main:new"}\n')
        out = Path(next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--out=")))
        out.write_text(json.dumps(_colliers_chunk_artifact(complete=True)), encoding="utf-8")
        return 0

    monkeypatch.setattr(refresh, "run_command", fake_run)
    rc, error = refresh.collect_colliers_main_chunks(
        tmp_path,
        output=tmp_path / "sources" / "colliers-main.json.tmp",
        attempt_number=2,
        page_cap=400,
        concurrency=2,
        transactions=("sale", "lease"),
        env={"COLLIERS_MAIN_DETAIL_CACHE_PATH": str(cache_path)},
        on_chunk=chunks.append,
    )

    assert (rc, error) == (0, None)
    assert chunks == [
        {
            "number": 1,
            "rc": 0,
            "log": "logs/colliers-main-collect-attempt-2-chunk-1.log",
            "cache_rows_before": 2500,
            "cache_rows_after": 2501,
            "artifact_complete": True,
        }
    ]


def test_colliers_main_chunks_fail_closed_when_partial_artifact_stalls(
    tmp_path, monkeypatch
):
    cache_path = tmp_path / "cache" / "colliers-main" / "detail-cache.jsonl"
    chunks = []

    def fake_run(argv, _log_path, *, env):
        assert env["COLLIERS_MAIN_DETAIL_CACHE_PATH"] == str(cache_path)
        out = Path(next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--out=")))
        out.write_text(json.dumps(_colliers_chunk_artifact(complete=False)), encoding="utf-8")
        return 0

    monkeypatch.setattr(refresh, "run_command", fake_run)
    rc, error = refresh.collect_colliers_main_chunks(
        tmp_path,
        output=tmp_path / "sources" / "colliers-main.json.tmp",
        attempt_number=1,
        page_cap=400,
        concurrency=2,
        transactions=("sale", "lease"),
        env={"COLLIERS_MAIN_DETAIL_CACHE_PATH": str(cache_path)},
        on_chunk=chunks.append,
    )

    assert rc == 75
    assert error == "colliers-main incomplete artifact made no durable detail-cache progress"
    assert chunks[0]["artifact_complete"] is False
    assert len(chunks) == 1


def test_collect_source_routes_colliers_main_through_chunk_protocol(tmp_path, monkeypatch):
    run_dir = tmp_path / "checkpoint"
    monkeypatch.setattr(refresh, "utc_now", lambda: "2026-07-29T12:00:00+00:00")
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("colliers-main",),
        page_cap=400,
        concurrency=2,
    )
    calls = []

    def fake_chunks(_run_dir, *, output, on_chunk, **kwargs):
        calls.append(kwargs)
        on_chunk(
            {
                "number": 1,
                "rc": 0,
                "log": "logs/colliers-main-collect-attempt-1-chunk-1.log",
                "cache_rows_before": 0,
                "cache_rows_after": 2,
                "artifact_complete": True,
            }
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                strict_artifact(
                    source="colliers-main",
                    detail_scope="detail_page",
                    generation=run_dir.name,
                    generation_started_at=manifest["started_at"],
                )
            ),
            encoding="utf-8",
        )
        return 0, None

    monkeypatch.setattr(refresh, "collect_colliers_main_chunks", fake_chunks)
    result = refresh.collect_source(
        run_dir,
        manifest,
        "colliers-main",
        transactions=("sale", "lease"),
        page_cap=400,
        concurrency=2,
        attempts_this_run=1,
    )

    assert result is not None
    assert len(calls) == 1
    assert manifest["sources"]["colliers-main"]["state"] == "validated"
    assert manifest["sources"]["colliers-main"]["attempts"][0]["chunks"][0][
        "artifact_complete"
    ]


def test_valid_full_artifact_is_accepted(tmp_path):
    path = write_artifact(tmp_path, artifact())
    stats = refresh.validate_source_artifact(path, "svn", ATTEMPT)
    assert stats["flat_listings"] == 2
    assert stats["staged_unique"] == 2
    assert stats["rejected_by_ingest"] == 0
    assert len(stats["sha256"]) == 64


def test_strict_buildout_artifact_accepts_current_authoritative_feed(tmp_path):
    path = write_artifact(tmp_path, strict_artifact())
    stats = refresh.validate_source_artifact(path, "svn", ATTEMPT)
    assert stats["staged_unique"] == 2


def test_source_artifact_rejects_future_run_timestamp_beyond_clock_skew(tmp_path):
    payload = strict_artifact()
    payload["runMeta"]["finishedAt"] = "2026-07-29T12:05:01+00:00"
    path = write_artifact(tmp_path, payload)

    with pytest.raises(refresh.ArtifactValidationError, match="clock-skew"):
        refresh.validate_source_artifact(
            path,
            "svn",
            ATTEMPT,
            now=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize("field", ["inventoryObservedAt", "detailObservedAt"])
def test_source_artifact_rejects_future_listing_observation_beyond_clock_skew(
    tmp_path, field
):
    payload = strict_artifact(source="jll", detail_scope="detail_page")
    payload["listings"][0][field] = "2026-07-29T12:05:01+00:00"
    path = write_artifact(tmp_path, payload)

    with pytest.raises(refresh.ArtifactValidationError, match="clock-skew"):
        refresh.validate_source_artifact(
            path,
            "jll",
            ATTEMPT,
            require_strict_freshness=True,
            expected_generation_id="refresh-generation-1",
            expected_generation_started_at="2026-07-29T12:00:00Z",
            now=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
        )


def test_avison_nonstrict_artifact_proves_current_property_details(tmp_path):
    path = write_artifact(tmp_path, avison_property_detail_artifact())
    stats = refresh.validate_source_artifact(
        path,
        "avison-young",
        ATTEMPT,
        expected_generation_id="refresh-generation-1",
        expected_generation_started_at="2026-07-29T12:00:00Z",
    )
    assert stats["staged_unique"] == 2
    assert stats["strict_freshness"] is False
    assert stats["property_detail_freshness"] is True
    assert stats["freshness_generation_id"] == "refresh-generation-1"


def test_avison_nonstrict_artifact_rejects_incomplete_detail_identity(tmp_path):
    payload = avison_property_detail_artifact()
    payload["listings"][0].pop("detailObservedAt")
    path = write_artifact(tmp_path, payload)
    with pytest.raises(
        refresh.ArtifactValidationError,
        match="detailObservedAt",
    ):
        refresh.validate_source_artifact(
            path,
            "avison-young",
            ATTEMPT,
            expected_generation_id="refresh-generation-1",
            expected_generation_started_at="2026-07-29T12:00:00Z",
        )


def test_avison_property_detail_artifact_requires_explicit_live_contract(tmp_path):
    payload = avison_property_detail_artifact()
    payload["runMeta"]["freshness"].pop("requireFreshPropertyDetails")
    path = write_artifact(tmp_path, payload)
    with pytest.raises(
        refresh.ArtifactValidationError,
        match="requireFreshPropertyDetails=true",
    ):
        refresh.validate_source_artifact(
            path,
            "avison-young",
            ATTEMPT,
            expected_generation_id="refresh-generation-1",
            expected_generation_started_at="2026-07-29T12:00:00Z",
        )


def test_avison_property_detail_artifact_rejects_cached_detail_proof(tmp_path):
    payload = avison_property_detail_artifact()
    payload["listings"][0]["freshnessProvenance"]["cacheDisposition"] = (
        "generation_cache"
    )
    path = write_artifact(tmp_path, payload)
    with pytest.raises(
        refresh.ArtifactValidationError,
        match="property detail was not observed live",
    ):
        refresh.validate_source_artifact(
            path,
            "avison-young",
            ATTEMPT,
            expected_generation_id="refresh-generation-1",
            expected_generation_started_at="2026-07-29T12:00:00Z",
        )


def test_avison_nonstrict_preservation_requires_current_detail_proof(tmp_path):
    payload = avison_property_detail_artifact()
    payload["listings"][0].pop("detailObservedWithChildPreservation")
    path = write_artifact(tmp_path, payload)
    with pytest.raises(
        refresh.ArtifactValidationError,
        match="inconsistent child-preservation detail proof",
    ):
        refresh.validate_source_artifact(
            path,
            "avison-young",
            ATTEMPT,
            expected_generation_id="refresh-generation-1",
            expected_generation_started_at="2026-07-29T12:00:00Z",
        )


def test_strict_cbre_artifact_accepts_current_authoritative_feed(tmp_path):
    path = write_artifact(tmp_path, strict_artifact("cbre"))
    stats = refresh.validate_source_artifact(
        path,
        "cbre",
        ATTEMPT,
        require_strict_freshness=True,
    )
    assert stats["staged_unique"] == 2


@pytest.mark.parametrize(
    "source",
    [
        "svn",
        "lee-associates",
        "franklin-street",
        "cushman-wakefield",
        "srs",
        "hanley",
        "kidder-mathews",
        "newmark",
    ],
)
def test_strict_child_preserving_feed_artifact_is_accepted(tmp_path, source):
    path = write_artifact(
        tmp_path,
        strict_artifact(source, preserve_children=True),
    )
    stats = refresh.validate_source_artifact(
        path,
        source,
        ATTEMPT,
        require_strict_freshness=True,
    )
    assert stats["staged_unique"] == 2


@pytest.mark.parametrize(
    "source",
    ["cbre"],
)
def test_strict_nonpreserving_feed_rejects_preservation_rows(tmp_path, source):
    path = write_artifact(
        tmp_path,
        strict_artifact(source, preserve_children=True),
    )
    with pytest.raises(
        refresh.ArtifactValidationError,
        match="must not preserve child collections",
    ):
        refresh.validate_source_artifact(
            path,
            source,
            ATTEMPT,
            require_strict_freshness=True,
        )


@pytest.mark.parametrize(
    "source",
    [
        "svn",
        "lee-associates",
        "franklin-street",
        "cushman-wakefield",
        "srs",
        "hanley",
        "kidder-mathews",
        "newmark",
    ],
)
def test_strict_child_preserving_feed_requires_preservation_rows(tmp_path, source):
    path = write_artifact(
        tmp_path,
        strict_artifact(source, preserve_children=False),
    )
    with pytest.raises(
        refresh.ArtifactValidationError,
        match="must preserve every child collection",
    ):
        refresh.validate_source_artifact(
            path,
            source,
            ATTEMPT,
            require_strict_freshness=True,
        )


def test_strict_detail_source_rejects_preservation_rows(tmp_path):
    path = write_artifact(
        tmp_path,
        strict_artifact("jll", "detail_page", preserve_children=True),
    )
    with pytest.raises(
        refresh.ArtifactValidationError,
        match="must not preserve child collections",
    ):
        refresh.validate_source_artifact(
            path,
            "jll",
            ATTEMPT,
            require_strict_freshness=True,
        )


def test_strict_detail_artifact_rejects_stale_observation(tmp_path):
    payload = strict_artifact("jll", "detail_page")
    payload["listings"][0]["detailObservedAt"] = "2026-07-29T11:59:59+00:00"
    path = write_artifact(tmp_path, payload)
    with pytest.raises(refresh.ArtifactValidationError, match="predates"):
        refresh.validate_source_artifact(path, "jll", ATTEMPT)


def test_runner_policy_rejects_strict_source_artifact_that_opts_out(tmp_path):
    path = write_artifact(tmp_path, artifact(source="jll"))
    with pytest.raises(
        refresh.ArtifactValidationError,
        match="requires runMeta.freshness.requireFreshDetails=true",
    ):
        refresh.validate_source_artifact(
            path,
            "jll",
            ATTEMPT,
            require_strict_freshness=True,
        )


def test_strict_artifact_must_match_expected_generation_id(tmp_path):
    path = write_artifact(tmp_path, strict_artifact())
    with pytest.raises(
        refresh.ArtifactValidationError,
        match="generationId does not match",
    ):
        refresh.validate_source_artifact(
            path,
            "svn",
            ATTEMPT,
            require_strict_freshness=True,
            expected_generation_id="different-generation",
            expected_generation_started_at="2026-07-29T12:00:00Z",
        )


def test_strict_artifact_must_match_normalized_expected_generation_start(tmp_path):
    path = write_artifact(
        tmp_path,
        strict_artifact(generation_started_at="2026-07-29T08:00:00-04:00"),
    )
    stats = refresh.validate_source_artifact(
        path,
        "svn",
        ATTEMPT,
        require_strict_freshness=True,
        expected_generation_id="refresh-generation-1",
        expected_generation_started_at="2026-07-29T12:00:00Z",
    )
    assert stats["freshness_generation_started_at"] == "2026-07-29T12:00:00+00:00"


def test_strict_artifact_rejects_wrong_expected_generation_start(tmp_path):
    path = write_artifact(tmp_path, strict_artifact())
    with pytest.raises(
        refresh.ArtifactValidationError,
        match="generationStartedAt does not match",
    ):
        refresh.validate_source_artifact(
            path,
            "svn",
            ATTEMPT,
            require_strict_freshness=True,
            expected_generation_id="refresh-generation-1",
            expected_generation_started_at="2026-07-29T11:59:59Z",
        )


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


def test_invalid_inventory_only_identity_is_rejected_before_gate(tmp_path):
    payload = artifact(
        source="colliers",
        listings=[
            listing(
                "colliers",
                1,
                "sale",
                id="wrong-prefix",
                inventoryOnly={
                    "reason": "card_not_linked",
                    "indexUrl": "https://sales.colliers.com/",
                },
            )
        ],
    )
    path = write_artifact(tmp_path, payload)
    with pytest.raises(
        refresh.ArtifactValidationError,
        match="invalid inventoryOnly identity",
    ):
        refresh.validate_source_artifact(path, "colliers", ATTEMPT)


def test_colliers_duplicate_canonical_project_id_is_rejected(tmp_path):
    payload = artifact(
        source="colliers",
        listings=[
            listing("colliers", 1, "sale", id="123", name="Property A"),
            listing(
                "colliers",
                2,
                "sale",
                id="123",
                name="Unrelated Property B",
                url="https://example.test/colliers/unrelated",
            ),
        ],
    )
    path = write_artifact(tmp_path, payload)
    with pytest.raises(
        refresh.ArtifactValidationError,
        match="duplicate canonical ProjectId",
    ):
        refresh.validate_source_artifact(path, "colliers", ATTEMPT)


def test_newmark_duplicate_canonical_slug_is_rejected(tmp_path):
    payload = artifact(
        source="newmark",
        listings=[
            listing("newmark", 1, "sale", id="same-slug"),
            listing("newmark", 2, "lease", id="same-slug"),
        ],
    )
    path = write_artifact(tmp_path, payload)
    with pytest.raises(
        refresh.ArtifactValidationError,
        match="newmark artifact contains duplicate canonical identity",
    ):
        refresh.validate_source_artifact(path, "newmark", ATTEMPT)


def test_colliers_duplicate_inventory_identity_is_rejected(tmp_path):
    inventory = {
        "url": None,
        "id": "salestracker:card:123",
        "preserveChildCollections": True,
        "provisionalIdentity": {"historyContinuity": "not_guaranteed"},
        "inventoryOnly": {
            "reason": "card_not_linked",
            "indexUrl": "https://sales.colliers.com/",
        },
        "detailUnavailable": {"reason": "card_not_linked"},
    }
    payload = artifact(
        source="colliers",
        listings=[
            listing("colliers", 1, "sale", **inventory),
            listing("colliers", 2, "sale", **inventory),
        ],
    )
    path = write_artifact(tmp_path, payload)
    with pytest.raises(
        refresh.ArtifactValidationError,
        match="duplicate provisional inventory identity",
    ):
        refresh.validate_source_artifact(path, "colliers", ATTEMPT)


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


def test_database_target_fingerprint_ignores_credentials_but_binds_target(tmp_path):
    first = tmp_path / "first.env"
    second = tmp_path / "second.env"
    other = tmp_path / "other.env"
    first.write_text(
        "POSTGRES_URL=postgresql://user-one:secret-one@db.example.test:5432/cre\n",
        encoding="utf-8",
    )
    second.write_text(
        "POSTGRES_URL=postgresql://user-two:secret-two@db.example.test/cre\n",
        encoding="utf-8",
    )
    other.write_text(
        "POSTGRES_URL=postgresql://user-one:secret-one@other.example.test:5432/cre\n",
        encoding="utf-8",
    )

    first_target = refresh.database_target_fingerprint(str(first))
    assert first_target == refresh.database_target_fingerprint(str(second))
    assert first_target != refresh.database_target_fingerprint(str(other))
    assert "secret" not in json.dumps(first_target)
    assert "user-" not in json.dumps(first_target)


@pytest.mark.parametrize(
    "url,error",
    [
        (
            "postgresql://user:secret@db.example.test/cre?host=other.example.test",
            "query parameters may override",
        ),
        (
            "postgresql://user:secret@db.example.test/cre?port=6432",
            "query parameters may override",
        ),
        (
            "postgresql://user:secret@db.example.test/cre?dbname=other",
            "query parameters may override",
        ),
        (
            "postgresql://user:secret@db1.example.test,db2.example.test/cre",
            "multi-host",
        ),
        (
            "postgresql://user:secret@db1.example.test:5432,db2.example.test:5432/cre",
            "multi-host",
        ),
    ],
)
def test_database_target_fingerprint_rejects_ambiguous_libpq_targets(
    tmp_path, url, error
):
    env_file = tmp_path / "target.env"
    env_file.write_text(f"POSTGRES_URL={url}\n", encoding="utf-8")

    with pytest.raises(refresh.RefreshError, match=error):
        refresh.database_target_fingerprint(str(env_file))


def test_resume_rejects_database_target_drift(tmp_path):
    run_dir = tmp_path / "run"
    original_target = {"algorithm": "sha256", "value": "a" * 64}
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
        database_target=original_target,
    )
    path = run_dir / "manifest.json"
    refresh.atomic_write_json(path, manifest)

    loaded = refresh.load_resume_manifest(
        path,
        git_sha="abc",
        sources=("svn",),
        page_cap=400,
        concurrency=3,
        database_target=original_target,
    )
    assert loaded["preflight"]["database_target"] == original_target

    with pytest.raises(refresh.RefreshError, match="different database target"):
        refresh.load_resume_manifest(
            path,
            git_sha="abc",
            sources=("svn",),
            page_cap=400,
            concurrency=3,
            database_target={"algorithm": "sha256", "value": "b" * 64},
        )


def test_resume_rejects_generation_older_than_configured_maximum(tmp_path):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    manifest["started_at"] = "2026-07-28T11:59:59+00:00"
    path = run_dir / "manifest.json"
    refresh.atomic_write_json(path, manifest)

    with pytest.raises(refresh.RefreshError, match="older than 24 hours"):
        refresh.load_resume_manifest(
            path,
            git_sha="abc",
            sources=("svn",),
            page_cap=400,
            concurrency=3,
            now=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
        )


def test_resume_accepts_generation_at_24_hour_boundary(tmp_path):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    manifest["started_at"] = "2026-07-28T12:00:00+00:00"
    path = run_dir / "manifest.json"
    refresh.atomic_write_json(path, manifest)

    loaded = refresh.load_resume_manifest(
        path,
        git_sha="abc",
        sources=("svn",),
        page_cap=400,
        concurrency=3,
        now=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
    )
    assert loaded["run_id"] == "run"


def test_resume_rejects_future_generation_start(tmp_path):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    manifest["started_at"] = "2026-07-29T12:00:01+00:00"
    path = run_dir / "manifest.json"
    refresh.atomic_write_json(path, manifest)

    with pytest.raises(refresh.RefreshError, match="starts in the future"):
        refresh.load_resume_manifest(
            path,
            git_sha="abc",
            sources=("svn",),
            page_cap=400,
            concurrency=3,
            now=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize("max_age_hours", [0, -1, float("inf"), float("nan")])
def test_resume_rejects_unsafe_maximum_age(tmp_path, max_age_hours):
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

    with pytest.raises(refresh.RefreshError, match="finite and positive"):
        refresh.load_resume_manifest(
            path,
            git_sha="abc",
            sources=("svn",),
            page_cap=400,
            concurrency=3,
            max_age_hours=max_age_hours,
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


def test_resume_checkpoint_rejects_artifact_from_another_generation(tmp_path):
    run_dir = tmp_path / "expected-generation"
    source_path = run_dir / "sources" / "svn.json"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        json.dumps(strict_artifact(generation="foreign-generation")),
        encoding="utf-8",
    )
    checkpoint = {
        "artifact": {
            "path": "sources/svn.json",
            "sha256": refresh.sha256_file(source_path),
            "attempt_started_at": ATTEMPT,
        }
    }

    assert (
        refresh._checkpoint_artifact_valid(
            run_dir,
            checkpoint,
            "svn",
            generation_started_at="2026-07-29T12:00:00Z",
        )
        is None
    )


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


def test_lock_release_preserves_replaced_same_pid_lease(tmp_path):
    lock_dir = tmp_path / ".cre.lock"
    lock = refresh.SharedLock(lock_dir)
    lock.acquire()
    original_lease = lock.lease_token
    assert original_lease

    shutil.rmtree(lock_dir)
    lock_dir.mkdir()
    (lock_dir / "pid").write_text(f"{os.getpid()} 1\n", encoding="utf-8")
    (lock_dir / "lease").write_text("replacement-lease\n", encoding="utf-8")

    lock.release()

    assert lock_dir.is_dir()
    assert refresh._lock_owner(lock_dir) == os.getpid()
    assert refresh._lock_lease(lock_dir) == "replacement-lease"


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


def test_checkpoint_lock_dir_rejects_noncanonical_override(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical" / ".cre.lock"
    monkeypatch.setattr(refresh, "canonical_shared_lock_dir", lambda: canonical)

    with pytest.raises(refresh.RefreshError, match="canonical shared CRE lock"):
        refresh.checkpoint_lock_dir(str(tmp_path / "split" / ".cre.lock"))


def test_checkpoint_lock_dir_accepts_canonical_override_for_test_injection(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical" / ".cre.lock"
    monkeypatch.setattr(refresh, "canonical_shared_lock_dir", lambda: canonical)

    assert refresh.checkpoint_lock_dir(str(canonical)) == canonical.resolve()


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


def test_subset_gate_can_admit_additive_rows_but_never_mark_missing(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("savills",),
        transactions=("lease",),
        page_cap=400,
        concurrency=3,
    )
    source_path = run_dir / "sources" / "savills.json"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("{}", encoding="utf-8")

    def fake_run(_argv, _log, **_kwargs):
        refresh.atomic_write_json(
            run_dir / "gates" / "savills.json",
            {
                "per_source": {
                        "savills": {
                            "verdict": "hold",
                            "reason": "current_active 3 below floor 100",
                            "mark_missing_safe": False,
                        }
                },
                "summary": {
                    "hold_sources": ["savills"],
                    "mark_missing_safe_brokerages": ["savills"],
                    "torow_errors": 0,
                },
            },
        )
        return 2

    monkeypatch.setattr(refresh, "run_command", fake_run)
    refresh.gate_source(run_dir, manifest, "savills", source_path, None)
    recorded = manifest["sources"]["savills"]["gate"]
    assert recorded["verdict"] == "ok_additive_subset"
    assert recorded["transaction_scope"] == ["lease"]
    assert recorded["mark_missing_safe"] is False
    durable_gate = json.loads(
        (run_dir / "gates" / "savills.json").read_text(encoding="utf-8")
    )
    assert durable_gate["scope"]["whole_source_coverage"] is False
    assert durable_gate["per_source"]["savills"]["raw_verdict"] == "hold"
    assert durable_gate["per_source"]["savills"]["mark_missing_safe"] is False
    assert durable_gate["summary"]["mark_missing_safe_brokerages"] == []
    assert durable_gate["summary"]["baseline_advisory_holds"] == ["savills"]
    assert durable_gate["summary"]["hold_sources"] == []
    assert (
        durable_gate["per_source"]["savills"]["admission_scope"]
        == "additive_transaction_subset"
    )


def test_subset_gate_admission_accepts_only_clean_or_baseline_only_holds():
    assert refresh.subset_gate_can_admit({"verdict": "ok", "reason": None})
    assert refresh.subset_gate_can_admit(
        {
            "verdict": "hold",
            "reason": "current_active 3 below floor 100",
        }
    )
    assert refresh.subset_gate_can_admit(
        {
            "verdict": "hold",
            "reason": (
                "current_active 60 below 70% of baseline median 100 "
                "(threshold 70)"
            ),
        }
    )
    assert not refresh.subset_gate_can_admit(
        {
            "verdict": "first_seen",
            "reason": "no baseline row; cannot gate (first sight)",
        }
    )
    assert not refresh.subset_gate_can_admit(
        {
            "verdict": "hold",
            "reason": "source pass error: timeout",
        }
    )


def test_full_additive_coverage_hold_is_admitted_without_lifecycle_claim(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("nai-global",),
        page_cap=400,
        concurrency=3,
        admit_baseline_hold_additively=True,
    )
    source_path = run_dir / "sources" / "nai-global.json"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("{}", encoding="utf-8")

    def fake_run(_argv, _log, **_kwargs):
        refresh.atomic_write_json(
            run_dir / "gates" / "nai-global.json",
            {
                "per_source": {
                    "nai-global": {
                        "verdict": "hold",
                        "reason": (
                            "current_active 408 below 70% of baseline median "
                            "1080 (threshold 756)"
                        ),
                        "mark_missing_safe": False,
                    }
                },
                "summary": {
                    "hold_sources": ["nai-global"],
                    "mark_missing_safe_brokerages": [],
                    "torow_errors": 0,
                },
            },
        )
        return 2

    monkeypatch.setattr(refresh, "run_command", fake_run)
    refresh.gate_source(run_dir, manifest, "nai-global", source_path, None)
    recorded = manifest["sources"]["nai-global"]["gate"]
    assert recorded["verdict"] == "ok_additive_coverage_hold"
    assert recorded["raw_verdict"] == "hold"
    assert recorded["admission_scope"] == "additive_coverage_hold"
    assert recorded["mark_missing_safe"] is False
    assert refresh.gate_verdict_is_admitted(manifest, recorded["verdict"])
    assert (
        manifest["scope"]["kind"]
        == "collector_registry_additive_coverage_hold"
    )
    durable_gate = json.loads(
        (run_dir / "gates" / "nai-global.json").read_text(encoding="utf-8")
    )
    assert durable_gate["scope"]["whole_source_coverage"] is False
    assert durable_gate["summary"]["baseline_advisory_holds"] == ["nai-global"]
    assert durable_gate["summary"]["mark_missing_safe_brokerages"] == []


def test_full_additive_hold_mode_does_not_admit_first_seen():
    manifest = refresh.new_manifest(
        Path("/tmp/run"),
        git_sha="abc",
        git_dirty=False,
        sources=("nai-global",),
        page_cap=400,
        concurrency=3,
        admit_baseline_hold_additively=True,
    )
    assert not refresh.gate_verdict_is_admitted(manifest, "first_seen")
    assert not refresh.gate_verdict_is_admitted(manifest, "hold")
    assert refresh.gate_verdict_is_admitted(
        manifest, "ok_additive_coverage_hold"
    )


def test_subset_first_seen_gate_remains_blocked(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("savills",),
        transactions=("lease",),
        page_cap=400,
        concurrency=3,
    )
    source_path = run_dir / "sources" / "savills.json"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("{}", encoding="utf-8")

    def fake_run(_argv, _log, **_kwargs):
        refresh.atomic_write_json(
            run_dir / "gates" / "savills.json",
            {
                "per_source": {
                    "savills": {
                        "verdict": "first_seen",
                        "reason": "no baseline row; cannot gate (first sight)",
                        "mark_missing_safe": False,
                    }
                },
                "summary": {
                    "hold_sources": [],
                    "mark_missing_safe_brokerages": [],
                    "torow_errors": 0,
                },
            },
        )
        return 0

    monkeypatch.setattr(refresh, "run_command", fake_run)
    refresh.gate_source(run_dir, manifest, "savills", source_path, None)
    recorded = manifest["sources"]["savills"]["gate"]
    assert recorded["verdict"] == "first_seen"
    assert recorded["admission_scope"] == "subset_admission_blocked"
    assert recorded["mark_missing_safe"] is False


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


def test_subset_aggregate_gate_strips_whole_source_missing_safe_claim(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("savills",),
        transactions=("lease",),
        page_cap=400,
        concurrency=3,
    )
    artifact_path = run_dir / "sources" / "savills.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("{}", encoding="utf-8")
    manifest["sources"]["savills"]["artifact"] = {
        "path": "sources/savills.json"
    }

    def fake_run(_argv, _log, **_kwargs):
        refresh.atomic_write_json(
            run_dir / "aggregate-gate.json",
            {
                "per_source": {
                    "savills": {
                        "verdict": "ok",
                        "mark_missing_safe": True,
                    }
                },
                "summary": {
                    "hold_sources": [],
                    "mark_missing_safe_brokerages": ["savills"],
                    "torow_errors": 0,
                },
            },
        )
        return 0

    monkeypatch.setattr(refresh, "run_command", fake_run)
    refresh.run_aggregate_gate(run_dir, manifest, None)
    assert manifest["aggregate_gate"]["mark_missing_safe_brokerages"] == []
    durable_gate = json.loads(
        (run_dir / "aggregate-gate.json").read_text(encoding="utf-8")
    )
    assert durable_gate["scope"]["whole_source_coverage"] is False
    assert durable_gate["summary"]["mark_missing_safe_brokerages"] == []
    assert durable_gate["per_source"]["savills"]["mark_missing_safe"] is False


def test_full_aggregate_gate_can_admit_only_explicit_baseline_hold_additively(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("nai-global",),
        page_cap=400,
        concurrency=3,
        admit_baseline_hold_additively=True,
    )
    artifact_path = run_dir / "sources" / "nai-global.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("{}", encoding="utf-8")
    manifest["sources"]["nai-global"]["artifact"] = {
        "path": "sources/nai-global.json"
    }

    def fake_run(_argv, _log, **_kwargs):
        refresh.atomic_write_json(
            run_dir / "aggregate-gate.json",
            {
                "per_source": {
                    "nai-global": {
                        "verdict": "hold",
                        "reason": "current_active 408 below floor 500",
                        "mark_missing_safe": False,
                    }
                },
                "summary": {
                    "hold_sources": ["nai-global"],
                    "mark_missing_safe_brokerages": [],
                    "torow_errors": 0,
                },
            },
        )
        return 2

    monkeypatch.setattr(refresh, "run_command", fake_run)
    refresh.run_aggregate_gate(run_dir, manifest, None)
    aggregate = manifest["aggregate_gate"]
    assert aggregate["non_ok_sources"] == []
    assert aggregate["baseline_advisory_holds"] == ["nai-global"]
    assert aggregate["mark_missing_safe_brokerages"] == []
    durable_gate = json.loads(
        (run_dir / "aggregate-gate.json").read_text(encoding="utf-8")
    )
    assert (
        durable_gate["per_source"]["nai-global"]["verdict"]
        == "ok_additive_coverage_hold"
    )
    assert durable_gate["scope"]["whole_source_coverage"] is False


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


def test_nonzero_ingest_result_requires_exact_readback_and_never_retries(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    manifest["sources"]["svn"]["artifact"] = strict_artifact_info()
    validation = {
        "queries": {
            "source_counts": [
                {
                    "source_key": "svn",
                    "latest_inventory_observed_at": "2026-07-29 12:01:00Z",
                    "latest_inventory_batch_active": "1",
                }
            ],
            "freshness_generations": [freshness_generation_row(active=1)],
            "inventory_only_index": [],
        }
    }
    calls = []

    def fake_run(argv, _log, **_kwargs):
        calls.append(argv)
        if len(calls) == 1:
            return 7
        output = Path(argv[argv.index("--out") + 1])
        refresh.atomic_write_json(output, validation)
        return 0

    monkeypatch.setattr(refresh, "run_command", fake_run)
    with pytest.raises(refresh.GlobalStageError, match="manual recovery"):
        refresh.ingest_source(
            run_dir, manifest, "svn", run_dir / "sources" / "svn.json", None
        )
    checkpoint = manifest["sources"]["svn"]
    assert len(calls) == 2
    assert checkpoint["state"] == "ingest_recovery_required"
    assert checkpoint["ingest"]["rc"] == 7
    assert checkpoint["ingest_recovery"]["reason"] == "nonzero_live_ingest_result"
    assert checkpoint["ingest_recovery"]["subprocess_rc"] == 7


def test_nonzero_ingest_result_is_accepted_only_after_exact_readback(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    manifest["sources"]["svn"]["artifact"] = strict_artifact_info()
    validation = {
        "queries": {
            "source_counts": [
                {
                    "source_key": "svn",
                    "latest_inventory_observed_at": "2026-07-29 12:01:00Z",
                    "latest_inventory_batch_active": "2",
                    "latest_scraped_at": "2026-07-29 12:01:00Z",
                    "latest_batch_active": "2",
                    "detail_unavailable": "0",
                }
            ],
            "freshness_generations": [freshness_generation_row()],
            "inventory_only_index": [],
        }
    }
    calls = []

    def fake_run(argv, _log, **_kwargs):
        calls.append(argv)
        if len(calls) == 1:
            return 7
        output = Path(argv[argv.index("--out") + 1])
        refresh.atomic_write_json(output, validation)
        return 0

    monkeypatch.setattr(refresh, "run_command", fake_run)
    refresh.ingest_source(
        run_dir, manifest, "svn", run_dir / "sources" / "svn.json", None
    )

    checkpoint = manifest["sources"]["svn"]
    assert len(calls) == 2
    assert checkpoint["state"] == "ingested"
    assert checkpoint["ingest"]["rc"] == 7
    assert checkpoint["ingest"]["recovered_from_exact_readback"] is True
    assert checkpoint["ingest_recovery"]["readback_ok"] is True


def test_nonzero_ingest_result_with_invalid_readback_persists_recovery_required(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    manifest["sources"]["svn"]["artifact"] = strict_artifact_info()
    return_codes = iter((7, 0))
    monkeypatch.setattr(
        refresh,
        "run_command",
        lambda *_args, **_kwargs: next(return_codes),
    )

    with pytest.raises(refresh.GlobalStageError, match="readback is invalid"):
        refresh.ingest_source(
            run_dir, manifest, "svn", run_dir / "sources" / "svn.json", None
        )

    checkpoint = manifest["sources"]["svn"]
    assert checkpoint["state"] == "ingest_recovery_required"
    assert checkpoint["ingest_recovery"]["readback_ok"] is False
    assert checkpoint["ingest_recovery"]["reason"] == "nonzero_live_ingest_result"


def test_ingest_persists_in_progress_state_before_subprocess(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )

    def fake_run(*_args, **_kwargs):
        checkpoint = manifest["sources"]["svn"]
        assert checkpoint["state"] == "ingesting"
        assert checkpoint["ingest"]["rc"] is None
        assert checkpoint["ingest"]["finished_at"] is None
        return 0

    monkeypatch.setattr(refresh, "run_command", fake_run)
    refresh.ingest_source(
        run_dir, manifest, "svn", run_dir / "sources" / "svn.json", None
    )
    assert manifest["sources"]["svn"]["state"] == "ingested"
    assert manifest["sources"]["svn"]["ingest"]["rc"] == 0


@pytest.mark.parametrize(
    "verdict,expected_state",
    [("first_seen", "baseline_seed_required"), ("hold", "gate_blocked")],
)
def test_advance_source_blocks_non_ok_gate_before_dry_run_or_ingest(
    tmp_path, monkeypatch, verdict, expected_state
):
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

    monkeypatch.setattr(
        refresh,
        "_checkpoint_artifact_valid",
        lambda *_args: (artifact_path, {"staged_unique": 2}),
    )

    def gate(*_args):
        manifest["sources"]["svn"]["gate"] = {"verdict": verdict}
        manifest["sources"]["svn"]["state"] = "gated"

    monkeypatch.setattr(refresh, "gate_source", gate)
    monkeypatch.setattr(
        refresh,
        "dry_run_source",
        lambda *_args: pytest.fail("dry run ran for a non-ok source gate"),
    )
    monkeypatch.setattr(
        refresh,
        "ingest_source",
        lambda *_args: pytest.fail("live ingest ran for a non-ok source gate"),
    )

    assert not refresh.advance_source(
        run_dir,
        manifest,
        "svn",
        page_cap=400,
        concurrency=3,
        attempts_this_run=1,
        env_file=None,
    )
    assert manifest["sources"]["svn"]["state"] == expected_state


def test_advance_source_prepares_without_live_ingest(tmp_path, monkeypatch):
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

    monkeypatch.setattr(
        refresh,
        "_checkpoint_artifact_valid",
        lambda *_args: (artifact_path, {"staged_unique": 2}),
    )

    def gate(*_args):
        manifest["sources"]["svn"]["gate"] = {"verdict": "ok"}
        manifest["sources"]["svn"]["state"] = "gated"

    def dry_run(*_args):
        manifest["sources"]["svn"]["state"] = "dry_run_passed"
        return True

    monkeypatch.setattr(refresh, "gate_source", gate)
    monkeypatch.setattr(refresh, "dry_run_source", dry_run)
    monkeypatch.setattr(
        refresh,
        "ingest_source",
        lambda *_args: pytest.fail("live ingest ran before aggregate admission"),
    )

    assert refresh.advance_source(
        run_dir,
        manifest,
        "svn",
        page_cap=400,
        concurrency=3,
        attempts_this_run=1,
        env_file=None,
    )
    assert manifest["sources"]["svn"]["state"] == "dry_run_passed"


def test_prepare_sources_stops_before_later_source_after_gate_block(
    tmp_path, monkeypatch
):
    manifest = refresh.new_manifest(
        tmp_path / "run",
        git_sha="abc",
        git_dirty=False,
        sources=("svn", "cbre"),
        page_cap=400,
        concurrency=3,
    )
    calls = []

    def advance(_run_dir, _manifest, source, **_kwargs):
        calls.append(source)
        return False

    monkeypatch.setattr(refresh, "advance_source", advance)
    failures = refresh.prepare_sources(
        tmp_path / "run",
        manifest,
        ("svn", "cbre"),
        page_cap=400,
        concurrency=3,
        attempts_this_run=1,
        env_file=None,
    )
    assert failures == ["svn"]
    assert calls == ["svn"]


def test_cohort_scheduler_respects_exclusive_and_provider_lanes():
    assert refresh.select_cohort_sources(
        ("colliers-main", "svn"), (), source_workers=3
    ) == ["colliers-main"]
    assert refresh.select_cohort_sources(
        ("svn", "jll", "jll-investor", "nai-global", "svn"),
        (),
        source_workers=4,
    ) == ["svn", "jll", "nai-global"]
    assert refresh.select_cohort_sources(
        ("jll-investor", "svn"), ("jll",), source_workers=3
    ) == ["svn"]
    assert refresh.select_cohort_sources(
        ("cbre", "cbre-dealflow", "svn"), (), source_workers=3
    ) == ["cbre", "svn"]
    buildout = tuple(sorted(refresh.BUILDOUT_SOURCE_KEYS))
    assert len(buildout) >= 2
    assert refresh.select_cohort_sources(
        buildout, (), source_workers=4
    ) == [buildout[0]]


def test_cohort_worker_argv_carries_only_collection_inputs(tmp_path):
    argv = refresh.build_cohort_collect_worker_argv(
        "svn",
        tmp_path / "source.json.tmp",
        run_dir=tmp_path / "run",
        attempt_log=tmp_path / "attempt.log",
        attempt_number=2,
        generation_started_at="2026-07-31T12:00:00+00:00",
        page_cap=400,
        concurrency=3,
        transactions=("sale", "lease"),
    )
    assert "--_cohort-collect-source" in argv
    assert "--_cohort-generation-started-at" in argv
    assert "--env-file" not in argv
    assert "--mark-missing" not in argv
    assert "cre_ingest.py" not in argv


def test_one_source_worker_preserves_the_serial_preparation_path(
    tmp_path, monkeypatch
):
    manifest = refresh.new_manifest(
        tmp_path / "run",
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    observed = {}

    def serial(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return ["svn"]

    monkeypatch.setattr(refresh, "prepare_sources", serial)
    assert refresh.prepare_sources_cohort(
        tmp_path / "run",
        manifest,
        ("svn",),
        page_cap=400,
        concurrency=3,
        attempts_this_run=2,
        env_file=None,
        source_workers=1,
    ) == ["svn"]
    assert observed["kwargs"]["attempts_this_run"] == 2


def test_resume_accepts_legacy_serial_manifest_without_source_workers(tmp_path):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    del manifest["config"]["source_workers"]
    path = run_dir / "manifest.json"
    refresh.atomic_write_json(path, manifest)
    loaded = refresh.load_resume_manifest(
        path,
        git_sha="abc",
        sources=("svn",),
        page_cap=400,
        concurrency=3,
        source_workers=1,
    )
    assert "source_workers" not in loaded["config"]


def test_cohort_prepares_all_artifacts_before_any_gate_or_dry_run(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn", "cbre", "jll", "jll-investor"),
        page_cap=400,
        concurrency=3,
        source_workers=2,
    )
    events = []

    class CompletedProcess:
        def __init__(self, source):
            self.pid = 5000 + len(events)
            self.source = source

        def poll(self):
            return 0

    def start(_run_dir, _manifest, source, **_kwargs):
        events.append(("start", source))
        return refresh.CohortCollectionProcess(
            source=source,
            process=CompletedProcess(source),
            log_handle=io.StringIO(),
            tmp_artifact=run_dir / "sources" / f"{source}.tmp",
            attempt={"number": 1},
            attempt_started_at=ATTEMPT,
        )

    def finalize(_run_dir, current_manifest, item):
        events.append(("finalize", item.source))
        current_manifest["sources"][item.source]["artifact"] = {
            "path": f"sources/{item.source}.json"
        }
        current_manifest["sources"][item.source]["state"] = "validated"
        return True

    def artifact_valid(_run_dir, current_manifest, source):
        if current_manifest["sources"][source].get("artifact"):
            return run_dir / "sources" / f"{source}.json", {"staged_unique": 1}
        return None

    def advance(_run_dir, _manifest, source, **_kwargs):
        events.append(("advance", source))
        assert len([item for item in events if item[0] == "finalize"]) == 4
        return True

    monkeypatch.setattr(refresh, "_start_cohort_collection", start)
    monkeypatch.setattr(refresh, "_finalize_cohort_collection", finalize)
    monkeypatch.setattr(refresh, "_manifest_checkpoint_artifact_valid", artifact_valid)
    monkeypatch.setattr(refresh, "advance_source", advance)

    assert refresh.prepare_sources_cohort(
        run_dir,
        manifest,
        tuple(manifest["config"]["sources"]),
        page_cap=400,
        concurrency=3,
        attempts_this_run=1,
        env_file=None,
        source_workers=2,
    ) == []
    assert [source for kind, source in events if kind == "finalize"] == [
        "svn",
        "cbre",
        "jll",
        "jll-investor",
    ]
    assert [source for kind, source in events if kind == "advance"] == [
        "svn",
        "cbre",
        "jll",
        "jll-investor",
    ]


def test_cohort_interrupt_terminates_every_active_child_before_returning(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn", "cbre"),
        page_cap=400,
        concurrency=3,
        source_workers=2,
    )
    signals = []

    class RunningProcess:
        def __init__(self, pid):
            self.pid = pid
            self.done = False

        def poll(self):
            return 0 if self.done else None

        def wait(self, timeout=None):
            self.done = True
            return -2

    def start(_run_dir, _manifest, source, **_kwargs):
        return refresh.CohortCollectionProcess(
            source=source,
            process=RunningProcess(6000 + len(signals) + (1 if source == "cbre" else 0)),
            log_handle=io.StringIO(),
            tmp_artifact=run_dir / "sources" / f"{source}.tmp",
            attempt={"number": 1},
            attempt_started_at=ATTEMPT,
        )

    monkeypatch.setattr(refresh, "_start_cohort_collection", start)
    monkeypatch.setattr(refresh.time, "sleep", lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(
        refresh.os, "killpg", lambda pid, sig: signals.append((pid, sig))
    )

    with pytest.raises(KeyboardInterrupt):
        refresh.prepare_sources_cohort(
            run_dir,
            manifest,
            ("svn", "cbre"),
            page_cap=400,
            concurrency=3,
            attempts_this_run=1,
            env_file=None,
            source_workers=2,
        )
    assert {pid for pid, _signal in signals} == {6000, 6001}
    assert {signal for _pid, signal in signals} == {refresh.signal.SIGINT}


def test_two_resumes_preserve_ingest_then_refresh_first_seen_without_reingest(
    tmp_path, monkeypatch
):
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
    manifest["sources"]["svn"].update(
        {
            "artifact": {"path": "sources/svn.json"},
            "gate": {"verdict": "first_seen"},
            "state": "ingested",
        }
    )
    monkeypatch.setattr(
        refresh,
        "_checkpoint_artifact_valid",
        lambda *_args: (artifact_path, {"staged_unique": 2}),
    )

    verdicts = iter(["first_seen", "ok"])

    def gate(*_args):
        manifest["sources"]["svn"]["gate"] = {"verdict": next(verdicts)}
        manifest["sources"]["svn"]["state"] = "gated"

    monkeypatch.setattr(refresh, "gate_source", gate)
    monkeypatch.setattr(
        refresh,
        "dry_run_source",
        lambda *_args: pytest.fail("completed ingest should not dry-run again"),
    )
    monkeypatch.setattr(
        refresh,
        "ingest_source",
        lambda *_args: pytest.fail("completed ingest should not run again"),
    )

    assert not refresh.advance_source(
        run_dir,
        manifest,
        "svn",
        page_cap=400,
        concurrency=3,
        attempts_this_run=1,
        env_file=None,
    )
    assert manifest["sources"]["svn"]["state"] == "ingested"
    assert manifest["sources"]["svn"]["admission_state"] == "baseline_seed_required"

    assert refresh.advance_source(
        run_dir,
        manifest,
        "svn",
        page_cap=400,
        concurrency=3,
        attempts_this_run=1,
        env_file=None,
    )
    assert manifest["sources"]["svn"]["state"] == "ingested"
    assert "admission_state" not in manifest["sources"]["svn"]
    assert manifest["sources"]["svn"]["gate"]["verdict"] == "ok"


def test_ingest_admitted_sources_requires_prepared_state(tmp_path):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    with pytest.raises(refresh.GlobalStageError, match="not prepared"):
        refresh.ingest_admitted_sources(run_dir, manifest, None)


def test_ingest_admitted_sources_ingests_prepared_artifact(tmp_path, monkeypatch):
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
    manifest["sources"]["svn"].update(
        {
            "artifact": {"path": "sources/svn.json"},
            "state": "dry_run_passed",
        }
    )
    monkeypatch.setattr(
        refresh,
        "_checkpoint_artifact_valid",
        lambda *_args: (artifact_path, {"staged_unique": 2}),
    )
    calls = []
    monkeypatch.setattr(
        refresh,
        "ingest_source",
        lambda *_args: calls.append("svn"),
    )
    refresh.ingest_admitted_sources(run_dir, manifest, None)
    assert calls == ["svn"]


def test_advance_source_recovers_ingesting_state_before_preparation(
    tmp_path, monkeypatch
):
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
    manifest["sources"]["svn"].update(
        {
            "artifact": {"path": "sources/svn.json"},
            "gate": {"verdict": "ok"},
            "ingest": {"rc": None},
            "state": "ingesting",
        }
    )
    monkeypatch.setattr(
        refresh,
        "_checkpoint_artifact_valid",
        lambda *_args: (artifact_path, {"staged_unique": 2}),
    )

    calls = []

    def recover(_run_dir, _manifest, source, _env_file):
        calls.append("recovery")
        manifest["sources"][source]["state"] = "ingested"

    monkeypatch.setattr(refresh, "recover_interrupted_ingest", recover)
    monkeypatch.setattr(
        refresh,
        "dry_run_source",
        lambda *_args: pytest.fail("recovered ingest should not dry-run again"),
    )
    monkeypatch.setattr(
        refresh,
        "ingest_source",
        lambda *_args: pytest.fail("recovered ingest should not replay"),
    )
    failures = refresh.prepare_sources(
        run_dir,
        manifest,
        ("svn",),
        page_cap=400,
        concurrency=3,
        attempts_this_run=1,
        env_file=None,
    )
    assert failures == []
    monkeypatch.setattr(
        refresh,
        "run_aggregate_gate",
        lambda *_args: calls.append("aggregate"),
    )
    refresh.run_aggregate_gate(run_dir, manifest, None)
    refresh.ingest_admitted_sources(run_dir, manifest, None)
    assert calls == ["recovery", "aggregate"]


def test_advance_source_never_auto_retries_ambiguous_ingest_recovery(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    manifest["sources"]["svn"]["state"] = "ingest_recovery_required"
    with pytest.raises(refresh.GlobalStageError, match="reviewed ingest recovery"):
        refresh.advance_source(
            run_dir,
            manifest,
            "svn",
            page_cap=400,
            concurrency=3,
            attempts_this_run=1,
            env_file=None,
        )


def test_ingesting_with_invalid_artifact_never_recollects_or_replays(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    manifest["sources"]["svn"].update(
        {
            "artifact": {"path": "sources/missing.json"},
            "ingest": {"rc": None},
            "state": "ingesting",
        }
    )
    monkeypatch.setattr(refresh, "_checkpoint_artifact_valid", lambda *_args: None)
    monkeypatch.setattr(
        refresh,
        "collect_source",
        lambda *_args, **_kwargs: pytest.fail("ambiguous ingest recollected"),
    )
    monkeypatch.setattr(
        refresh,
        "gate_source",
        lambda *_args: pytest.fail("ambiguous ingest re-gated"),
    )
    monkeypatch.setattr(
        refresh,
        "dry_run_source",
        lambda *_args: pytest.fail("ambiguous ingest dry-ran"),
    )
    monkeypatch.setattr(
        refresh,
        "ingest_source",
        lambda *_args: pytest.fail("ambiguous ingest replayed"),
    )
    with pytest.raises(refresh.GlobalStageError, match="invalid or missing artifact"):
        refresh.advance_source(
            run_dir,
            manifest,
            "svn",
            page_cap=400,
            concurrency=3,
            attempts_this_run=1,
            env_file=None,
        )
    checkpoint = manifest["sources"]["svn"]
    assert checkpoint["state"] == "ingest_recovery_required"
    assert checkpoint["ingest_recovery"]["reason"] == "invalid_or_missing_artifact"


def test_recover_interrupted_ingest_accepts_only_exact_readback(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    manifest["sources"]["svn"].update(
        {
            "artifact": strict_artifact_info(),
            "ingest": {"rc": None},
            "state": "ingesting",
        }
    )
    validation = {
        "queries": {
            "source_counts": [
                {
                    "source_key": "svn",
                    "latest_inventory_observed_at": "2026-07-29 12:01:00Z",
                    "latest_inventory_batch_active": "2",
                    "latest_scraped_at": "2026-07-29 12:01:00Z",
                    "latest_batch_active": "2",
                    "detail_unavailable": "0",
                }
            ],
            "freshness_generations": [freshness_generation_row()],
            "inventory_only_index": [],
        }
    }

    def fake_run(argv, _log, **_kwargs):
        output = Path(argv[argv.index("--out") + 1])
        refresh.atomic_write_json(output, validation)
        return 0

    monkeypatch.setattr(refresh, "run_command", fake_run)
    refresh.recover_interrupted_ingest(run_dir, manifest, "svn", None)
    checkpoint = manifest["sources"]["svn"]
    assert checkpoint["state"] == "ingested"
    assert checkpoint["ingest"]["recovered_from_exact_readback"] is True
    assert checkpoint["ingest_recovery"]["readback_ok"] is True


def test_recover_interrupted_ingest_never_replays_on_mismatch(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    manifest["sources"]["svn"].update(
        {
            "artifact": strict_artifact_info(),
            "ingest": {"rc": None},
            "state": "ingesting",
        }
    )
    validation = {
        "queries": {
            "source_counts": [
                {
                    "source_key": "svn",
                    "latest_inventory_observed_at": "2026-07-29 12:01:00Z",
                    "latest_inventory_batch_active": "1",
                }
            ],
            "freshness_generations": [freshness_generation_row(active=1)],
            "inventory_only_index": [],
        }
    }

    def fake_run(argv, _log, **_kwargs):
        output = Path(argv[argv.index("--out") + 1])
        refresh.atomic_write_json(output, validation)
        return 0

    monkeypatch.setattr(refresh, "run_command", fake_run)
    with pytest.raises(refresh.GlobalStageError, match="manual recovery"):
        refresh.recover_interrupted_ingest(run_dir, manifest, "svn", None)
    assert manifest["sources"]["svn"]["state"] == "ingest_recovery_required"


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
    manifest["sources"]["svn"]["artifact"] = strict_artifact_info()
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
            "freshness_generations": [freshness_generation_row(active=1)],
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
    assert "generation batch 1 != staged unique 2" == (
        manifest["sources"]["svn"]["readback"]["reason"]
    )


def test_strict_readback_accepts_observations_within_artifact_freshness_slo(
    tmp_path,
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("jll",),
        page_cap=400,
        concurrency=3,
    )
    artifact_info = strict_artifact_info()
    artifact_info["finished_at"] = "2026-07-29T18:00:00+00:00"
    manifest["sources"]["jll"]["artifact"] = artifact_info
    validation = {
        "queries": {
            "source_counts": [
                {
                    "source_key": "jll",
                    "latest_inventory_observed_at": "2026-07-29 12:00:40Z",
                    "latest_inventory_batch_active": "1",
                    "latest_scraped_at": "2026-07-29 12:00:50Z",
                    "latest_batch_active": "1",
                    "detail_unavailable": "0",
                }
            ],
            "freshness_generations": [
                freshness_generation_row(source="jll", active=2)
            ],
            "inventory_only_index": [],
        }
    }

    result = refresh.verify_validation_readback(run_dir, manifest, validation)

    assert result == {"ok": True, "failed_sources": []}
    readback = manifest["sources"]["jll"]["readback"]
    assert readback["generation_id"] == "refresh-generation-1"
    assert readback["latest_inventory_batch_active"] == 2
    assert readback["latest_detail_batch_active"] == 2


def test_strict_readback_rejects_observations_older_than_artifact_freshness_slo(
    tmp_path,
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("jll",),
        page_cap=400,
        concurrency=3,
    )
    artifact_info = strict_artifact_info()
    artifact_info["finished_at"] = "2026-07-30T12:01:00+00:00"
    manifest["sources"]["jll"]["artifact"] = artifact_info
    validation = {
        "queries": {
            "source_counts": [],
            "freshness_generations": [
                freshness_generation_row(source="jll", active=2)
            ],
            "inventory_only_index": [],
        }
    }

    result = refresh.verify_validation_readback(run_dir, manifest, validation)

    assert result == {"ok": False, "failed_sources": ["jll"]}
    assert (
        manifest["sources"]["jll"]["readback"]["reason"]
        == "generation inventory observation exceeds artifact freshness SLO"
    )


def test_strict_readback_rejects_future_observation_beyond_clock_skew(tmp_path):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("jll",),
        page_cap=400,
        concurrency=3,
    )
    manifest["sources"]["jll"]["artifact"] = strict_artifact_info()
    validation = {
        "queries": {
            "source_counts": [],
            "freshness_generations": [
                freshness_generation_row(
                    source="jll",
                    latest_detail_observed_at="2026-07-29 12:05:01Z",
                )
            ],
            "inventory_only_index": [],
        }
    }

    result = refresh.verify_validation_readback(
        run_dir,
        manifest,
        validation,
        now=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
    )

    assert result == {"ok": False, "failed_sources": ["jll"]}
    assert "clock-skew" in manifest["sources"]["jll"]["readback"]["reason"]


def test_avison_nonstrict_readback_uses_property_detail_generation(tmp_path):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("avison-young",),
        page_cap=400,
        concurrency=3,
    )
    artifact_info = strict_artifact_info()
    artifact_info.update(
        {
            "strict_freshness": False,
            "property_detail_freshness": True,
            "finished_at": "2026-07-29T18:00:00+00:00",
        }
    )
    manifest["sources"]["avison-young"]["artifact"] = artifact_info
    validation = {
        "queries": {
            "source_counts": [],
            "freshness_generations": [
                freshness_generation_row(source="avison-young", active=2)
            ],
            "inventory_only_index": [],
        }
    }

    result = refresh.verify_validation_readback(run_dir, manifest, validation)

    assert result == {"ok": True, "failed_sources": []}
    assert (
        manifest["sources"]["avison-young"]["readback"]["generation_id"]
        == "refresh-generation-1"
    )


def test_strict_readback_rejects_generation_observation_before_start(tmp_path):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("jll",),
        page_cap=400,
        concurrency=3,
    )
    manifest["sources"]["jll"]["artifact"] = strict_artifact_info()
    validation = {
        "queries": {
            "source_counts": [],
            "freshness_generations": [
                freshness_generation_row(
                    source="jll",
                    earliest_detail_observed_at="2026-07-29 11:59:59Z",
                )
            ],
            "inventory_only_index": [],
        }
    }

    result = refresh.verify_validation_readback(run_dir, manifest, validation)

    assert result == {"ok": False, "failed_sources": ["jll"]}
    assert (
        manifest["sources"]["jll"]["readback"]["reason"]
        == "generation detail observation predates generation start"
    )


def test_strict_readback_uses_persisted_detail_proof_not_source_count_scrape_time(
    tmp_path,
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("jll",),
        page_cap=400,
        concurrency=3,
    )
    manifest["sources"]["jll"]["artifact"] = strict_artifact_info()
    validation = {
        "queries": {
            # The legacy scrape timestamp is intentionally absent.  The
            # generation report is the sole detail-freshness proof.
            "freshness_generations": [freshness_generation_row(source="jll")],
            "inventory_only_index": [],
        }
    }

    assert refresh.verify_validation_readback(run_dir, manifest, validation) == {
        "ok": True,
        "failed_sources": [],
    }
    readback = manifest["sources"]["jll"]["readback"]
    assert readback["detail_scopes"] == ["detail_page"]
    assert readback["cache_dispositions"] == ["live"]
    assert readback["latest_detail_observed_at"] == "2026-07-29 12:00:50Z"


def test_jll_current_generation_cache_is_accepted_as_persisted_detail_evidence(
    tmp_path,
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("jll",),
        page_cap=400,
        concurrency=3,
    )
    manifest["sources"]["jll"]["artifact"] = strict_artifact_info()
    validation = {
        "queries": {
            "freshness_generations": [
                freshness_generation_row(
                    source="jll", cache_dispositions=["generation_cache"]
                )
            ],
            "inventory_only_index": [],
        }
    }

    assert refresh.verify_validation_readback(run_dir, manifest, validation) == {
        "ok": True,
        "failed_sources": [],
    }


def test_jll_generation_with_live_and_current_cache_evidence_is_aggregated_once(
    tmp_path,
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("jll",),
        page_cap=400,
        concurrency=3,
    )
    manifest["sources"]["jll"]["artifact"] = strict_artifact_info()
    validation = {
        "queries": {
            "freshness_generations": [
                freshness_generation_row(
                    source="jll",
                    cache_dispositions='["generation_cache", "live"]',
                )
            ],
            "inventory_only_index": [],
        }
    }

    assert refresh.verify_validation_readback(run_dir, manifest, validation) == {
        "ok": True,
        "failed_sources": [],
    }
    assert manifest["sources"]["jll"]["readback"]["cache_dispositions"] == [
        "generation_cache",
        "live",
    ]


def test_strict_readback_rejects_missing_persisted_detail_proof(tmp_path):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("jll",),
        page_cap=400,
        concurrency=3,
    )
    manifest["sources"]["jll"]["artifact"] = strict_artifact_info()
    validation = {
        "queries": {
            "freshness_generations": [
                freshness_generation_row(
                    source="jll", missing_persisted_detail_proof="1"
                )
            ],
            "inventory_only_index": [],
        }
    }

    result = refresh.verify_validation_readback(run_dir, manifest, validation)

    assert result == {"ok": False, "failed_sources": ["jll"]}
    assert manifest["sources"]["jll"]["readback"]["reason"] == (
        "generation is missing persisted detail proof"
    )


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "detail_scopes",
            ["inventory_only"],
            "persisted generation has an unaccepted detail scope",
        ),
        (
            "cache_dispositions",
            ["unrecognized_cache"],
            "persisted generation has an unaccepted cache disposition",
        ),
    ],
)
def test_strict_readback_rejects_unaccepted_persisted_scope_or_cache(
    tmp_path, field, value, reason
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("jll",),
        page_cap=400,
        concurrency=3,
    )
    manifest["sources"]["jll"]["artifact"] = strict_artifact_info()
    validation = {
        "queries": {
            "freshness_generations": [
                freshness_generation_row(source="jll", **{field: value})
            ],
            "inventory_only_index": [],
        }
    }

    result = refresh.verify_validation_readback(run_dir, manifest, validation)

    assert result == {"ok": False, "failed_sources": ["jll"]}
    assert manifest["sources"]["jll"]["readback"]["reason"] == reason


def test_authoritative_inventory_readback_requires_persisted_inventory_evidence(
    tmp_path,
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    manifest["sources"]["svn"]["artifact"] = strict_artifact_info()
    validation = {
        "queries": {
            "freshness_generations": [
                freshness_generation_row(source="svn", persisted_inventory_observed="1")
            ],
            "inventory_only_index": [],
        }
    }

    result = refresh.verify_validation_readback(run_dir, manifest, validation)

    assert result == {"ok": False, "failed_sources": ["svn"]}
    assert manifest["sources"]["svn"]["readback"]["reason"] == (
        "persisted inventory observations 1 != staged unique 2"
    )


def test_avison_property_detail_readback_does_not_make_contact_freshness_claim(
    tmp_path,
):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("avison-young",),
        page_cap=400,
        concurrency=3,
    )
    artifact_info = strict_artifact_info()
    artifact_info.update(
        {"strict_freshness": False, "property_detail_freshness": True}
    )
    manifest["sources"]["avison-young"]["artifact"] = artifact_info
    validation = {
        "queries": {
            # Contact counts are deliberately not part of this source policy.
            "freshness_generations": [
                freshness_generation_row(source="avison-young")
            ],
            "inventory_only_index": [],
        }
    }

    assert refresh.verify_validation_readback(run_dir, manifest, validation) == {
        "ok": True,
        "failed_sources": [],
    }


def test_validation_readback_proves_mixed_canonical_and_inventory_only_lanes(tmp_path):
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
        **strict_artifact_info(),
        "inventory_only": 1,
    }
    validation = {
        "queries": {
            "freshness_generations": [
                freshness_generation_row(source="cbre-dealflow")
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
    assert readback["reason"] == "active inventory-only 2 != expected 1"
    assert readback["latest_inventory_batch_active"] == 2


def test_colliers_all_inventory_only_snapshot_needs_no_canonical_row(tmp_path):
    run_dir = tmp_path / "run"
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("colliers",),
        page_cap=400,
        concurrency=3,
    )
    manifest["sources"]["colliers"]["artifact"] = {
        "finished_at": "2026-07-29T12:01:00+00:00",
        "staged_unique": 0,
        "inventory_only": 2,
    }
    validation = {
        "queries": {
            "source_counts": [
                {
                    "source_key": "colliers",
                    "latest_inventory_observed_at": "2026-07-28 12:01:00Z",
                    "latest_inventory_batch_active": "100",
                    "latest_scraped_at": "2026-07-28 12:01:00Z",
                    "latest_batch_active": "100",
                    "detail_unavailable": "0",
                }
            ],
            "inventory_only_index": [
                {
                    "source_key": "colliers",
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
    assert result == {"ok": True, "failed_sources": []}
    readback = manifest["sources"]["colliers"]["readback"]
    assert readback["ok"] is True
    assert readback["latest_inventory_batch_active"] == 0
    assert readback["inventory_only"]["active"] == 2


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


def test_absolute_validation_quality_allows_sparse_coordinates_but_rejects_hard_defects():
    zero = absolute_quality_report()
    assert refresh.verify_absolute_validation_quality(zero) == {
        "ok": True,
        "failures": [],
    }

    defects = absolute_quality_report(
        bad_source_url="1",
        missing_canonical_url="1",
        bad_canonical_url="1",
        invalid_state="1",
        impossible_lat="1",
        impossible_lng="1",
        sale_price_flags="1",
        sale_psf_flags="1",
        lease_rate_min_flags="1",
        lease_rate_max_flags="1",
        cap_rate_flags="1",
    )
    defects["queries"]["duplicates"][0]["groups"] = "1"
    next(
        row
        for row in defects["queries"]["bad_child_urls"]
        if row["check_name"] == "image_bad_url"
    )["count"] = "1"
    defects["queries"]["primary_child_conflicts"] = [
        {"child_type": "images", "listings": "1"}
    ]
    next(
        row
        for row in defects["queries"]["orphans"]
        if row["child_type"] == "images"
    )["orphan_rows"] = "1"

    result = refresh.verify_absolute_validation_quality(defects)
    assert result["ok"] is False
    for expected in (
        "duplicates/duplicate_external_id_groups/all/groups",
        "bad_child_urls/image_bad_url/count",
        "primary_child_conflicts/images/listings",
        "orphans/images/orphan_rows",
        "quality_by_source/svn/missing_canonical_url",
        "quality_by_source/svn/lease_rate_max_flags",
    ):
        assert any(expected in failure for failure in result["failures"])


def test_absolute_validation_quality_requires_mixed_source_canonical_url_coverage():
    result = refresh.verify_absolute_validation_quality(
        absolute_quality_report(
            source="cbre-dealflow",
            missing_canonical_url="1",
            bad_canonical_url="1",
        )
    )
    assert result == {
        "ok": False,
        "failures": [
            "quality_by_source/cbre-dealflow/missing_canonical_url is nonzero: 1",
            "quality_by_source/cbre-dealflow/bad_canonical_url is nonzero: 1",
        ],
    }


def test_final_validation_fails_closed_on_preexisting_absolute_defect(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    (run_dir / "logs").mkdir(parents=True)
    report = absolute_quality_report(missing_canonical_url="1")
    (run_dir / "pre-validation.json").write_text(json.dumps(report), encoding="utf-8")
    manifest = refresh.new_manifest(
        run_dir,
        git_sha="abc",
        git_dirty=False,
        sources=("svn",),
        page_cap=400,
        concurrency=3,
    )
    manifest["preflight"]["validation_path"] = "pre-validation.json"

    def write_final_validation(argv, _log, env):
        assert "CRE_ACTIVATE_STATUS" not in env
        Path(argv[argv.index("--out") + 1]).write_text(
            json.dumps(report), encoding="utf-8"
        )
        return 0

    monkeypatch.setattr(refresh, "run_command", write_final_validation)
    monkeypatch.setattr(
        refresh,
        "verify_validation_readback",
        lambda *_args: {"ok": True, "failed_sources": []},
    )

    with pytest.raises(refresh.GlobalStageError, match="final validation"):
        refresh.run_final_validation(run_dir, manifest, None)
    assert manifest["validation"]["quality_no_regression"] is True
    assert manifest["validation"]["absolute_quality_ok"] is False
    assert any(
        "missing_canonical_url" in failure
        for failure in manifest["validation"]["absolute_quality_failures"]
    )


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
    assert "Transaction scope: `['sale', 'lease']`" in report
    assert "postgres://" not in report
    assert "password" not in report.lower()


def test_subset_report_disclaims_whole_source_coverage(tmp_path):
    manifest = refresh.new_manifest(
        tmp_path / "run",
        git_sha="abc",
        git_dirty=False,
        sources=("savills",),
        transactions=("lease",),
        page_cap=400,
        concurrency=3,
    )
    report = refresh.render_report(manifest)
    assert "Transaction scope: `['lease']`" in report
    assert "no whole-source coverage or lifecycle claim" in report


def test_additive_coverage_hold_report_retains_lifecycle_warning(tmp_path):
    manifest = refresh.new_manifest(
        tmp_path / "run",
        git_sha="abc",
        git_dirty=False,
        sources=("nai-global",),
        page_cap=400,
        concurrency=3,
        admit_baseline_hold_additively=True,
    )
    manifest["aggregate_gate"] = {
        "baseline_advisory_holds": ["nai-global"]
    }
    report = refresh.render_report(manifest)
    assert "retained historical coverage hold" in report
    assert "no lifecycle deletion or whole-source freshness claim" in report
