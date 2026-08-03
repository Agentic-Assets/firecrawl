"""No-network contracts for the read-only CRE freshness certificate."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import cre_freshness_certificate as certificate
import cre_source_policy as source_policy
import pytest


NOW = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(
    tmp_path,
    source_key,
    *,
    finished_at=NOW - timedelta(hours=1),
    name=None,
    mutate=None,
):
    policy = source_policy.load_source_policy()[source_key]
    run_dir = tmp_path / (name or f"run-{source_key}")
    artifact_path = run_dir / "sources" / f"{source_key}.json"
    artifact_path.parent.mkdir(parents=True)
    evidence_class = policy["evidence_class"]
    preserve_children = policy["child_contract"] == "preserve_existing_children"
    inventory_namespace = policy["inventory_only_namespace"]
    detail_scope = (
        "authoritative_inventory_feed"
        if evidence_class == "authoritative_inventory_feed"
        else "detail_page"
    )
    freshness = {
        "generationId": "certificate-test-generation",
        "generationStartedAt": (NOW - timedelta(hours=2)).isoformat(),
        "requireFreshDetails": evidence_class in {"strict_detail", "authoritative_inventory_feed"},
        "requireFreshPropertyDetails": evidence_class == "property_detail",
    }
    listing = {
        "sourceKey": source_key,
        "id": f"{source_key}-1",
        "inventoryObservedAt": (NOW - timedelta(hours=1)).isoformat(),
        "freshnessProvenance": {
            "detailScope": detail_scope,
            "generationId": "certificate-test-generation",
            "cacheDisposition": "live",
        },
    }
    if detail_scope == "detail_page":
        listing["detailObservedAt"] = (NOW - timedelta(hours=1)).isoformat()
    if preserve_children:
        listing["preserveChildCollections"] = True
    if evidence_class == "property_detail":
        # Avison may preserve existing contacts when its supplemental team feed
        # is unavailable, but only with an admitted current detail observation.
        listing["preserveChildCollections"] = True
        listing["detailObservedWithChildPreservation"] = True
    listings = [listing]
    if evidence_class == "inventory_only_namespace":
        listings = [
            {
                "sourceKey": source_key,
                "id": f"{source_key}-source-index-1",
                "inventoryOnly": {"reason": "provider card"},
                "provisionalIdentity": {"reason": "provider card is not canonical"},
            }
        ]
    elif inventory_namespace is not None:
        listings.append(
            {
                "sourceKey": source_key,
                "id": f"{source_key}-source-index-1",
                "inventoryOnly": {"reason": "provider card"},
                "provisionalIdentity": {"reason": "provider card is not canonical"},
            }
        )
    artifact_path.write_text(
        json.dumps({"runMeta": {"freshness": freshness}, "listings": listings}),
        encoding="utf-8",
    )
    artifact = {
        "path": str(artifact_path.relative_to(run_dir)),
        "sha256": _sha(artifact_path),
        "bytes": artifact_path.stat().st_size,
        "staged_unique": 1,
        "inventory_only": 1 if inventory_namespace is not None else 0,
        "strict_freshness": evidence_class in {"strict_detail", "authoritative_inventory_feed"},
        "property_detail_freshness": evidence_class == "property_detail",
    }
    manifest = {
        "schema_version": 2,
        "status": "supported_scope_complete",
        "error": None,
        "config": {
            "sources": [source_key],
            "transactions": ["sale", "lease"],
            "max_items": 0,
            "additive": True,
            "status_activation": False,
            "mark_missing": False,
            "admit_baseline_hold_additively": False,
        },
        "scope": {"kind": "collector_registry", "source_keys": [source_key]},
        "preflight": {"healthcheck_rc": 0, "validation_rc": 0},
        "aggregate_gate": {
            "rc": 0,
            "non_ok_sources": [],
            "hold_sources": [],
            "baseline_advisory_holds": [],
        },
        "validation": {
            "query_execution_ok": True,
            "quality_no_regression": True,
            "readback_ok": True,
            "absolute_quality_ok": True,
            "absolute_quality_failures": [],
            "failed_readback_sources": [],
            "quality_failures": [],
        },
        "sources": {
            source_key: {
                "state": "ingested",
                "artifact": artifact,
                "gate": {"rc": 0, "verdict": "ok", "raw_verdict": None},
                "dry_run": {"rc": 0},
                "ingest": {
                    "rc": 0,
                    "additive": True,
                    "status_activation": False,
                    "mark_missing": False,
                    "finished_at": finished_at.isoformat(),
                },
                "readback": {"ok": True},
            }
        },
    }
    if mutate:
        mutate(manifest, artifact_path)
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run_dir


def _valid_runs(tmp_path):
    return [_run(tmp_path, source_key) for source_key in certificate.SOURCE_KEYS]


def _codes(result):
    return {failure["code"] for failure in result["failures"]}


def test_certificate_accepts_exact_51_source_union_and_reports_policy_evidence(tmp_path):
    result = certificate.build_freshness_certificate(
        _valid_runs(tmp_path), max_source_age_hours=2, now=NOW
    )

    assert result["status"] == "valid"
    assert result["certified_source_count"] == 51
    assert result["failures"] == []
    evidence = {row["source_key"]: row["evidence"]["evidence_class"] for row in result["sources"]}
    assert evidence["jll"] == "strict_detail"
    assert evidence["avison-young"] == "property_detail"
    assert evidence["svn"] == "authoritative_inventory_feed"
    assert evidence["cbre-dealflow"] == "authoritative_inventory_feed"
    assert evidence["colliers"] == "strict_detail"


def test_certificate_rejects_subset_union(tmp_path):
    result = certificate.build_freshness_certificate(
        [_run(tmp_path, "jll")], max_source_age_hours=2, now=NOW
    )

    assert result["status"] == "invalid"
    assert "source_union" in _codes(result)


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (lambda manifest, _path: manifest["config"].update(additive=False), "non_additive"),
        (lambda manifest, _path: manifest["config"].update(status_activation=True), "status_activation"),
        (lambda manifest, _path: manifest["config"].update(mark_missing=True), "mark_missing"),
        (lambda manifest, _path: manifest["config"].update(transactions=["sale"]), "subset_scope"),
        (lambda manifest, _path: manifest["aggregate_gate"].update(hold_sources=["jll"]), "baseline_hold"),
        (lambda manifest, _path: manifest.update(status="additive_scope_complete_coverage_hold"), "run_not_terminal"),
    ],
)
def test_certificate_rejects_nonadmissible_run_contracts(tmp_path, mutation, expected):
    runs = _valid_runs(tmp_path)
    manifest_path = runs[0] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    mutation(manifest, runs[0] / "sources" / "cbre.json")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = certificate.build_freshness_certificate(runs, max_source_age_hours=2, now=NOW)

    assert result["status"] == "invalid"
    assert expected in _codes(result)


def test_certificate_rejects_artifact_integrity_failure(tmp_path):
    runs = _valid_runs(tmp_path)
    artifact = runs[0] / "sources" / "cbre.json"
    artifact.write_text('{"tampered":true}', encoding="utf-8")

    result = certificate.build_freshness_certificate(runs, max_source_age_hours=2, now=NOW)

    assert result["status"] == "invalid"
    assert "artifact_sha256_mismatch" in _codes(result)


def test_certificate_rejects_missing_absolute_quality_proof(tmp_path):
    runs = _valid_runs(tmp_path)
    manifest_path = runs[0] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["validation"]["absolute_quality_ok"] = False
    manifest["validation"]["absolute_quality_failures"] = ["duplicate canonical identity"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = certificate.build_freshness_certificate(runs, max_source_age_hours=2, now=NOW)

    assert result["status"] == "invalid"
    assert "absolute_quality" in _codes(result)


def test_certificate_checks_artifact_provenance_not_only_manifest_summary(tmp_path):
    runs = _valid_runs(tmp_path)
    jll_run = next(path for path in runs if path.name == "run-jll")
    artifact_path = jll_run / "sources" / "jll.json"
    artifact = json.loads(artifact_path.read_text())
    artifact["listings"][0]["freshnessProvenance"]["detailScope"] = "inventory_only"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    manifest_path = jll_run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sources"]["jll"]["artifact"].update(
        sha256=_sha(artifact_path), bytes=artifact_path.stat().st_size
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = certificate.build_freshness_certificate(runs, max_source_age_hours=2, now=NOW)

    assert result["status"] == "invalid"
    assert "artifact_evidence" in _codes(result)


def test_certificate_separates_mixed_canonical_and_provisional_rows(tmp_path, monkeypatch):
    policy = source_policy.load_source_policy()
    policy["colliers"] = {
        "evidence_class": "strict_detail",
        "canonical_claim": "canonical_listing",
        "detail_claim": "current_strict_detail",
        "child_contract": "replace_from_fresh_detail",
        "inventory_only_namespace": "salestracker:card:%",
        "lifecycle_reconciliation_eligible": True,
    }
    monkeypatch.setattr(source_policy, "load_source_policy", lambda: policy)
    monkeypatch.setattr(certificate, "load_source_policy", lambda: policy)

    result = certificate.build_freshness_certificate(
        _valid_runs(tmp_path), max_source_age_hours=2, now=NOW
    )

    assert result["status"] == "valid"
    colliers = next(row for row in result["sources"] if row["source_key"] == "colliers")
    assert colliers["evidence"]["canonical_claim"] == "canonical_listing"
    assert colliers["provisional_source_index_count"] == 1


def test_certificate_rejects_duplicate_terminal_source_and_stale_source(tmp_path):
    runs = _valid_runs(tmp_path)
    cbre_manifest_path = runs[0] / "manifest.json"
    cbre_manifest = json.loads(cbre_manifest_path.read_text())
    cbre_manifest["sources"]["cbre"]["ingest"]["finished_at"] = (
        NOW - timedelta(hours=4)
    ).isoformat()
    cbre_manifest_path.write_text(json.dumps(cbre_manifest), encoding="utf-8")
    runs.append(_run(tmp_path, "jll", name="run-jll-duplicate"))

    result = certificate.build_freshness_certificate(runs, max_source_age_hours=2, now=NOW)

    assert result["status"] == "invalid"
    assert {"duplicate_terminal_source", "source_age"} <= _codes(result)


def test_certificate_rejects_observations_older_than_the_row_freshness_slo(tmp_path):
    runs = _valid_runs(tmp_path)
    jll_run = next(path for path in runs if path.name == "run-jll")
    artifact_path = jll_run / "sources" / "jll.json"
    artifact = json.loads(artifact_path.read_text())
    artifact["listings"][0]["inventoryObservedAt"] = (
        NOW - timedelta(hours=3)
    ).isoformat()
    artifact["listings"][0]["detailObservedAt"] = (
        NOW - timedelta(hours=3)
    ).isoformat()
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    manifest_path = jll_run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sources"]["jll"]["artifact"].update(
        sha256=_sha(artifact_path), bytes=artifact_path.stat().st_size
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = certificate.build_freshness_certificate(
        runs,
        max_source_age_hours=2,
        max_observation_age_hours=2,
        now=NOW,
    )

    assert result["status"] == "invalid"
    assert "observation_age" in _codes(result)


def test_certificate_rejects_future_observations_even_when_they_clear_age_cutoff(tmp_path):
    runs = _valid_runs(tmp_path)
    jll_run = next(path for path in runs if path.name == "run-jll")
    artifact_path = jll_run / "sources" / "jll.json"
    artifact = json.loads(artifact_path.read_text())
    artifact["listings"][0]["inventoryObservedAt"] = (
        NOW + timedelta(minutes=6)
    ).isoformat()
    artifact["listings"][0]["detailObservedAt"] = (
        NOW + timedelta(minutes=6)
    ).isoformat()
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    manifest_path = jll_run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sources"]["jll"]["artifact"].update(
        sha256=_sha(artifact_path), bytes=artifact_path.stat().st_size
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = certificate.build_freshness_certificate(
        runs, max_source_age_hours=2, now=NOW
    )

    assert result["status"] == "invalid"
    assert "observation_age" in _codes(result)


def test_certificate_uses_fresh_revision_validation_for_cached_detail_evidence(tmp_path):
    runs = _valid_runs(tmp_path)
    jll_run = next(path for path in runs if path.name == "run-jll")
    artifact_path = jll_run / "sources" / "jll.json"
    artifact = json.loads(artifact_path.read_text())
    listing = artifact["listings"][0]
    listing["detailObservedAt"] = (NOW - timedelta(days=2)).isoformat()
    listing["freshnessProvenance"].update(
        cacheDisposition="source_revision_cache",
        validatedAt=(NOW - timedelta(hours=1)).isoformat(),
    )
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    manifest_path = jll_run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sources"]["jll"]["artifact"].update(
        sha256=_sha(artifact_path), bytes=artifact_path.stat().st_size
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = certificate.build_freshness_certificate(
        runs, max_source_age_hours=2, now=NOW
    )

    assert result["status"] == "valid"
