"""Matthews targeted admission through real worker/ingest, no external IO."""

import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import cre_enrich as enrich
import cre_ingest as ingest


def artifact(source="matthews", *, strict=False):
    boundary = datetime.now(timezone.utc) - timedelta(seconds=10)
    observed = boundary + timedelta(seconds=5)
    data = {
        "runMeta": {
            "mode": "enrich",
            "startedAt": boundary.isoformat(),
            "finishedAt": observed.isoformat(),
        },
        "sources": [{"sourceKey": source, "requested": 1, "listingsCollected": 1}],
        "listings": [
            {
                "sourceKey": source,
                "id": "example-sale",
                "url": "https://www.matthews.com/properties/example-sale",
                "canonicalUrl": "https://www.matthews.com/properties/example-sale",
                "name": "Synthetic Property",
                "detailObservedAt": observed.isoformat(),
            }
        ],
        "brokers": [],
    }
    if strict:
        data["runMeta"]["freshness"] = {
            "requireFreshDetails": True,
            "generationId": "new-fixture-generation",
            "generationStartedAt": boundary.isoformat(),
        }
        data["listings"][0].update(
            {
                "inventoryObservedAt": observed.isoformat(),
                "freshnessProvenance": {
                    "generationId": "new-fixture-generation",
                    "detailScope": "detail_page",
                    "cacheDisposition": "live",
                    "method": "matthews_canonical_detail",
                },
            }
        )
    return data


def claims(data):
    return [
        {
            "id": "synthetic-claim",
            "source_key": row["sourceKey"],
            "external_id": row["id"],
            "url": row["url"],
            "reason": "changed",
            "attempts": 0,
        }
        for row in data["listings"]
    ]


def worker(monkeypatch, tmp_path, data, *, bound=False):
    claim_rows = claims(data)
    io = {"ingest": [], "queue": []}
    monkeypatch.setattr(enrich, "OUT_ENRICH_DIR", str(tmp_path))
    monkeypatch.setattr(
        enrich,
        "load_db_url",
        lambda _: ("postgresql://synthetic.invalid/example", "synthetic.env"),
    )
    monkeypatch.setattr(enrich, "assert_expected_database_target", lambda *_: None)
    monkeypatch.setattr(
        enrich,
        "_psql_query",
        lambda *_: [
            (
                row["id"],
                row["source_key"],
                row["external_id"],
                row["url"],
                row["reason"],
                "0",
            )
            for row in claim_rows
        ],
    )
    monkeypatch.setattr(enrich, "_psql_exec", lambda _db, sql: io["queue"].append(sql))

    def run_process(argv, **_kwargs):
        if "--enrich-input" in argv:
            with open(argv[argv.index("--out") + 1], "w") as output:
                json.dump(data, output)
        else:
            assert argv[1].endswith("cre_ingest.py")
            io["ingest"].append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(enrich.subprocess, "run", run_process)
    rc = enrich.run(
        SimpleNamespace(
            batch=1,
            source=None,
            dry_run=False,
            env_file=None,
            expected_db_target_sha256="a" * 64 if bound else None,
        )
    )
    return rc, io


@pytest.mark.parametrize("bound", [False, True])
def test_plain_matthews_worker_refuses_before_ingest_and_claim_completion(
    monkeypatch, tmp_path, bound
):
    rc, io = worker(monkeypatch, tmp_path, artifact(), bound=bound)
    assert rc == 1
    assert io["ingest"] == []
    assert len(io["queue"]) == 1
    sql = io["queue"][0]
    assert "claimed_at = NULL" in sql
    assert "DELETE FROM" not in sql
    assert "attempts = attempts +" not in sql


def test_plain_matthews_direct_cli_refuses_before_sql_credentials_or_outputs(
    monkeypatch, tmp_path
):
    path = tmp_path / "partial.json"
    path.write_text(json.dumps(artifact()))
    output = tmp_path / "generated"
    monkeypatch.setattr(
        sys,
        "argv",
        ["cre_ingest.py", "--in", str(path), "--keep-artifacts", str(output)],
    )

    def forbidden(*_args, **_kwargs):
        pytest.fail("Plain targeted artifact reached SQL or credential IO")

    monkeypatch.setattr(ingest, "build_sql", forbidden)
    monkeypatch.setattr(ingest, "load_db_url", forbidden)
    monkeypatch.setattr(ingest, "find_psql", forbidden)
    with pytest.raises(SystemExit, match="strict freshness"):
        ingest.main()
    assert not output.exists()


def test_plain_matthews_dry_run_is_also_refused_before_sql(monkeypatch, tmp_path):
    path = tmp_path / "partial.json"
    path.write_text(json.dumps(artifact()))
    monkeypatch.setattr(sys, "argv", ["cre_ingest.py", "--in", str(path), "--dry-run"])
    monkeypatch.setattr(
        ingest, "build_sql", lambda *_a, **_k: pytest.fail("Unsafe SQL was generated")
    )
    with pytest.raises(SystemExit, match="strict freshness"):
        ingest.main()


@pytest.mark.parametrize("source", ["marcus-millichap", "srs", "colliers-main"])
def test_other_plain_source_worker_behavior_is_unchanged(monkeypatch, tmp_path, source):
    rc, io = worker(monkeypatch, tmp_path, artifact(source))
    assert rc == 0
    assert len(io["ingest"]) == 1
    assert "--require-strict-freshness" not in io["ingest"][0]
    assert any("DELETE FROM" in sql for sql in io["queue"])


def test_other_plain_source_existing_validator_behavior_is_unchanged():
    data = artifact("srs")
    assert enrich.validate_enriched_artifact(claims(data), data) == data["listings"]
    ingest.validate_strict_artifact_freshness(data)


def test_strict_matthews_reuses_existing_validator_without_rewriting_clocks():
    data = artifact(strict=True)
    before = copy.deepcopy(data)
    ingest.validate_strict_artifact_freshness(data)
    assert enrich.validate_enriched_artifact(claims(data), data) == data["listings"]
    assert data == before


def test_full_matthews_legacy_behavior_remains_unchanged():
    data = artifact()
    data["runMeta"]["mode"] = "full"
    ingest.validate_strict_artifact_freshness(data)


def test_empty_matthews_batch_has_no_ingest_and_retains_retry_accounting(
    monkeypatch, tmp_path
):
    data = artifact()
    original_claims = claims(data)
    data["listings"] = []
    # The actual worker must claim an item even when its source adapter omits it.
    monkeypatch.setattr(enrich, "OUT_ENRICH_DIR", str(tmp_path))
    monkeypatch.setattr(
        enrich,
        "load_db_url",
        lambda _: ("postgresql://synthetic.invalid/example", "synthetic.env"),
    )
    monkeypatch.setattr(
        enrich,
        "_psql_query",
        lambda *_: [
            (r["id"], r["source_key"], r["external_id"], r["url"], r["reason"], "0")
            for r in original_claims
        ],
    )
    writes = []
    monkeypatch.setattr(enrich, "_psql_exec", lambda _db, sql: writes.append(sql))

    def collect_only(argv, **_kwargs):
        assert "--enrich-input" in argv
        with open(argv[argv.index("--out") + 1], "w") as output:
            json.dump(data, output)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(enrich.subprocess, "run", collect_only)
    assert (
        enrich.run(
            SimpleNamespace(batch=1, source="matthews", dry_run=False, env_file=None)
        )
        == 0
    )
    assert any("attempts = attempts + 1" in sql for sql in writes)
    assert not any("DELETE FROM" in sql for sql in writes)


@pytest.mark.parametrize(
    "mutation",
    [
        "envelope_false",
        "envelope_missing",
        "generation_missing",
        "generation_empty",
        "generation_wrong_type",
        "boundary_missing",
        "boundary_invalid",
        "provenance_missing",
        "generation_mismatch",
        "inventory_missing",
        "inventory_invalid",
        "inventory_stale",
        "inventory_future",
        "detail_missing",
        "detail_invalid",
        "detail_stale",
        "detail_future",
        "detail_error",
        "preserve_children",
        "preserve_contacts",
        "scope_missing",
        "scope_inventory_feed",
        "scope_authoritative_inventory_feed",
        "scope_invalid",
        "inventory_only",
    ],
)
def test_matthews_targeted_strict_faults_refuse_at_both_boundaries(mutation):
    data = artifact(strict=True)
    row = data["listings"][0]
    fresh = data["runMeta"]["freshness"]
    if mutation == "envelope_false":
        fresh["requireFreshDetails"] = False
    elif mutation == "envelope_missing":
        data["runMeta"].pop("freshness")
    elif mutation == "generation_missing":
        fresh.pop("generationId")
    elif mutation == "generation_empty":
        fresh["generationId"] = ""
    elif mutation == "generation_wrong_type":
        fresh["generationId"] = []
    elif mutation == "boundary_missing":
        fresh.pop("generationStartedAt")
    elif mutation == "boundary_invalid":
        fresh["generationStartedAt"] = "not a time"
    elif mutation == "provenance_missing":
        row.pop("freshnessProvenance")
    elif mutation == "generation_mismatch":
        row["freshnessProvenance"]["generationId"] = "previous-full-generation"
    elif mutation in {"inventory_missing", "detail_missing"}:
        row.pop(
            "inventoryObservedAt"
            if mutation.startswith("inventory")
            else "detailObservedAt"
        )
    elif mutation in {"inventory_invalid", "detail_invalid"}:
        row[
            "inventoryObservedAt"
            if mutation.startswith("inventory")
            else "detailObservedAt"
        ] = "not a time"
    elif mutation in {"inventory_stale", "detail_stale"}:
        row[
            "inventoryObservedAt"
            if mutation.startswith("inventory")
            else "detailObservedAt"
        ] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    elif mutation in {"inventory_future", "detail_future"}:
        row[
            "inventoryObservedAt"
            if mutation.startswith("inventory")
            else "detailObservedAt"
        ] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    elif mutation == "detail_error":
        row["detailError"] = "Synthetic failure"
    elif mutation == "preserve_children":
        row["preserveChildCollections"] = True
    elif mutation == "preserve_contacts":
        row["preserveContactCollections"] = True
    elif mutation == "scope_missing":
        row["freshnessProvenance"].pop("detailScope")
    elif mutation == "scope_inventory_feed":
        row["freshnessProvenance"]["detailScope"] = "inventory_feed"
    elif mutation == "scope_authoritative_inventory_feed":
        row["freshnessProvenance"]["detailScope"] = "authoritative_inventory_feed"
    elif mutation == "scope_invalid":
        row["freshnessProvenance"]["detailScope"] = []
    elif mutation == "inventory_only":
        row["inventoryOnly"] = {"namespace": "fake"}
    with pytest.raises(ValueError):
        ingest.validate_strict_artifact_freshness(data)
    with pytest.raises(ValueError):
        enrich.validate_enriched_artifact(claims(data), data)


def test_mixed_batch_cannot_dispatch_unsafe_matthews_with_other_source(
    monkeypatch, tmp_path
):
    data = artifact("srs")
    data["listings"].extend(artifact()["listings"])
    rc, io = worker(monkeypatch, tmp_path, data)
    assert rc == 1
    assert not io["ingest"]
    assert not any("DELETE FROM" in sql for sql in io["queue"])


def test_other_plain_source_direct_cli_still_reaches_existing_sql_builder(
    monkeypatch, tmp_path
):
    path = tmp_path / "srs.json"
    path.write_text(json.dumps(artifact("srs")))
    monkeypatch.setattr(sys, "argv", ["cre_ingest.py", "--in", str(path), "--dry-run"])

    class ReachedBuilder(Exception):
        pass

    def builder(*_args, **_kwargs):
        raise ReachedBuilder

    monkeypatch.setattr(ingest, "build_sql", builder)
    monkeypatch.setattr(
        ingest, "load_db_url", lambda *_: pytest.fail("Dry-run attempted DB load")
    )
    with pytest.raises(ReachedBuilder):
        ingest.main()


def test_second_input_unsafe_matthews_blocks_the_entire_direct_ingest(
    monkeypatch, tmp_path
):
    first = tmp_path / "srs.json"
    second = tmp_path / "matthews.json"
    first.write_text(json.dumps(artifact("srs")))
    second.write_text(json.dumps(artifact()))
    monkeypatch.setattr(
        sys, "argv", ["cre_ingest.py", "--in", str(first), "--in", str(second)]
    )
    monkeypatch.setattr(
        ingest, "build_sql", lambda *_a, **_k: pytest.fail("Mixed input reached SQL")
    )
    monkeypatch.setattr(
        ingest, "load_db_url", lambda *_: pytest.fail("Mixed input reached DB load")
    )
    with pytest.raises(SystemExit, match="strict freshness"):
        ingest.main()


def test_valid_strict_partial_does_not_acquire_full_source_authority():
    data = artifact(strict=True)
    before = copy.deepcopy(data)
    ingest.validate_strict_artifact_freshness(data)
    with pytest.raises(ValueError, match="full"):
        ingest.strict_full_source_scope(data, "matthews")
    assert data == before


@pytest.mark.parametrize("detail", [None, "stale"])
def test_matthews_cannot_substitute_cache_validation_for_actual_detail(detail):
    data = artifact(strict=True)
    row = data["listings"][0]
    row["freshnessProvenance"].update(
        {
            "cacheDisposition": "source_revision_cache",
            "validatedAt": row["detailObservedAt"],
        }
    )
    if detail is None:
        row.pop("detailObservedAt")
    else:
        row["detailObservedAt"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
    with pytest.raises(ValueError):
        ingest.validate_strict_artifact_freshness(data)
    with pytest.raises(ValueError):
        enrich.validate_enriched_artifact(claims(data), data)


@pytest.mark.parametrize("scope", [None, False, {}, "source_native_public_record"])
def test_matthews_non_detail_page_scopes_refuse(scope):
    data = artifact(strict=True)
    data["listings"][0]["freshnessProvenance"]["detailScope"] = scope
    with pytest.raises(ValueError):
        ingest.validate_strict_artifact_freshness(data)
    with pytest.raises(ValueError):
        enrich.validate_enriched_artifact(claims(data), data)


def test_matthews_false_inventory_only_flag_cannot_skip_detail_admission():
    data = artifact(strict=True)
    data["listings"][0]["inventoryOnly"] = False
    with pytest.raises(ValueError, match="canonical detail"):
        ingest.validate_strict_artifact_freshness(data)
    with pytest.raises(ValueError, match="canonical detail"):
        enrich.validate_enriched_artifact(claims(data), data)


@pytest.mark.parametrize(
    "disposition",
    [None, False, {}, "generation_cache", "source_revision_cache", "unknown"],
)
def test_matthews_targeted_requires_its_actual_live_detail_contract(disposition):
    data = artifact(strict=True)
    data["listings"][0]["freshnessProvenance"]["cacheDisposition"] = disposition
    with pytest.raises(ValueError):
        ingest.validate_strict_artifact_freshness(data)
    with pytest.raises(ValueError):
        enrich.validate_enriched_artifact(claims(data), data)


@pytest.mark.parametrize(
    "flag", ["preserveChildCollections", "preserveContactCollections"]
)
@pytest.mark.parametrize("marker", [True, "true", [True], ["true"], [[True]]])
@pytest.mark.parametrize("nested", [False, True])
def test_recursive_preservation_markers_cannot_reach_worker_ingest_or_cli_sql(
    monkeypatch, tmp_path, flag, marker, nested
):
    data = artifact(strict=True)
    if nested:
        data["listings"][0]["syntheticMeta"] = [{"nested": {flag: marker}}]
    else:
        data["listings"][0][flag] = marker
    with pytest.raises(ValueError):
        ingest.validate_strict_artifact_freshness(data)
    rc, io = worker(monkeypatch, tmp_path, data)
    assert rc == 1 and io["ingest"] == []
    assert not any("DELETE FROM" in sql for sql in io["queue"])
    path = tmp_path / "marker.json"
    path.write_text(json.dumps(data))
    monkeypatch.setattr(sys, "argv", ["cre_ingest.py", "--in", str(path)])
    monkeypatch.setattr(
        ingest,
        "build_sql",
        lambda *_a, **_k: pytest.fail("Preservation marker reached SQL"),
    )
    monkeypatch.setattr(
        ingest, "load_db_url", lambda *_: pytest.fail("Preservation marker reached DB")
    )
    with pytest.raises(SystemExit, match="strict freshness"):
        ingest.main()


@pytest.mark.parametrize(
    "flag", ["preserveChildCollections", "preserveContactCollections"]
)
@pytest.mark.parametrize(
    "marker", [False, "false", 1, None, [False], ["false"], "TRUE"]
)
def test_nonmatching_preservation_scalars_keep_existing_admission(flag, marker):
    data = artifact(strict=True)
    data["listings"][0]["syntheticMeta"] = [{flag: marker, "unrelated": True}]
    before = copy.deepcopy(data)
    ingest.validate_strict_artifact_freshness(data)
    assert enrich.validate_enriched_artifact(claims(data), data) == data["listings"]
    assert data == before
