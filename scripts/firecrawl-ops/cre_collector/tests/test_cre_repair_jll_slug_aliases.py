import hashlib
import json

import cre_repair_jll_slug_aliases as repair
import pytest

URL = "https://property.jll.com/listings/example"
LEGACY = "11111111-1111-1111-1111-111111111111"
NUMERIC = "22222222-2222-2222-2222-222222222222"


def row(identifier, external_id, *, generation=None):
    raw = {"sourceKey": "jll"}
    if generation:
        raw["freshnessProvenance"] = {"generationId": generation}
    return {
        "id": identifier,
        "brokerage_id": "33333333-3333-3333-3333-333333333333",
        "external_id": external_id,
        "source_url": URL,
        "status": "active",
        "deleted_at": None,
        "updated_at": "2026-07-31T00:00:00+00:00",
        "raw_data": raw,
        "generation": generation,
        "references": {
            name: 0 for name in repair.REFERENCE_TABLES + repair.SOFT_REFERENCE_TABLES
        }
        | {"sourceIndex": 0, "queue": 0},
        "parent": {"id": identifier, "external_id": external_id},
    }


def state():
    return {
        "rows": [
            row(LEGACY, "old-jll-slug"),
            row(NUMERIC, "123", generation=repair.EXPECTED_GENERATION),
        ],
        "duplicate_url_groups": 1,
        "fk_surfaces": sorted(repair.EXPECTED_FK_SURFACES),
        "soft_reference_surfaces": sorted(repair.EXPECTED_SOFT_REFERENCE_SURFACES),
    }


def one_pair(monkeypatch):
    monkeypatch.setattr(repair, "EXPECTED_PAIRS", 1)
    return repair.build_plan(state(), {URL: "123"})


def test_build_plan_is_exact_numeric_current_and_childless(monkeypatch):
    plan = one_pair(monkeypatch)
    assert plan[0]["legacy_id"] == LEGACY
    assert plan[0]["numeric_id"] == NUMERIC
    assert plan[0]["numeric_external_id"] == "123"


def test_build_plan_refuses_extra_duplicate_or_reference(monkeypatch):
    monkeypatch.setattr(repair, "EXPECTED_PAIRS", 1)
    bad = state()
    bad["rows"].append(row("44444444-4444-4444-4444-444444444444", "other"))
    with pytest.raises(ValueError, match="outside the reviewed artifact plan"):
        repair.build_plan(bad, {URL: "123"})
    bad = state()
    bad["rows"][0]["references"]["cre_listing_om_facts_archive"] = 1
    with pytest.raises(ValueError, match="dependent"):
        repair.build_plan(bad, {URL: "123"})


def test_build_plan_refuses_unreviewed_surfaces_and_stale_survivor(monkeypatch):
    monkeypatch.setattr(repair, "EXPECTED_PAIRS", 1)
    bad = state()
    bad["soft_reference_surfaces"] = ["credeals.cre_listing_archive"]
    with pytest.raises(ValueError, match="soft-reference"):
        repair.build_plan(bad, {URL: "123"})
    bad = state()
    bad["rows"][1]["generation"] = "old"
    with pytest.raises(ValueError, match="reviewed generation"):
        repair.build_plan(bad, {URL: "123"})


def test_apply_sql_is_serializable_locks_and_only_updates_parents(monkeypatch):
    plan = one_pair(monkeypatch)
    sql = repair.mutation_sql(plan, commit=False)
    assert "BEGIN ISOLATION LEVEL SERIALIZABLE" in sql
    assert f"pg_advisory_xact_lock({repair.ADVISORY_LOCK_KEY})" in sql
    assert "FOR UPDATE" in sql
    assert "JLL FK surface drifted" in sql
    assert "JLL legacy alias has dependent references" in sql
    assert "cre_scrape_log" in sql
    assert "cre_listing_om_facts_archive" in sql
    assert "UPDATE credeals.cre_listings a" in sql
    assert "DELETE FROM" not in sql
    assert "UPDATE credeals.cre_listing_" not in sql
    assert "'verify_apply_rollback'" in sql
    assert sql.rstrip().endswith("ROLLBACK;")


def test_preimage_is_owner_only_digest_bound_and_contains_full_parent(
    monkeypatch, tmp_path
):
    plan = one_pair(monkeypatch)
    payload = repair.preimage_from_plan(plan, artifact_geometry="a" * 64)
    repair.validate_preimage(payload)
    folder = tmp_path / "private"
    folder.mkdir(mode=0o700)
    target = folder / "preimage.json"
    actual = repair.atomic_private_json(target.resolve(), payload)
    assert target.stat().st_mode & 0o077 == 0
    assert actual == hashlib.sha256(target.read_bytes()).hexdigest()
    loaded, found = repair.load_private_preimage(target.resolve(), actual)
    assert found == actual and loaded == payload
    with pytest.raises(FileExistsError):
        repair.atomic_private_json(target.resolve(), payload)


def test_rollback_restores_only_bound_alias_fields_after_post_state_guard(monkeypatch):
    plan = one_pair(monkeypatch)
    payload = repair.preimage_from_plan(plan, artifact_geometry="a" * 64)
    sql = repair.rollback_sql(payload)
    assert "BEGIN ISOLATION LEVEL SERIALIZABLE" in sql
    assert "JLL rollback refused: post-repair state drifted" in sql
    assert "JLL rollback refused: dependent references" in sql
    assert "JLL rollback refused: legacy soft references" in sql
    assert "SET external_id=p.legacy_external_id,status=p.legacy_status" in sql
    assert "updated_at=p.legacy_updated_at" not in sql
    assert "updated_at IS DISTINCT FROM transaction_timestamp()" in sql
    assert "DELETE FROM" not in sql
    assert "UPDATE credeals.cre_listing_" not in sql
    assert sql.rstrip().endswith("COMMIT;")


def test_artifact_plan_rejects_non_numeric_or_url_collision(monkeypatch, tmp_path):
    monkeypatch.setattr(repair, "EXPECTED_ARTIFACT_ROWS", 2)
    monkeypatch.setattr(repair, "EXPECTED_ARTIFACT_TARGETS", 1)
    content = {
        "runMeta": {"freshness": {"generationId": repair.EXPECTED_GENERATION}},
        "listings": [
            {"sourceKey": "jll", "id": "123", "url": URL},
            {"sourceKey": "jll", "id": "456", "url": URL},
        ],
    }
    target = tmp_path / "artifact.json"
    target.write_text(json.dumps(content))
    monkeypatch.setattr(
        repair,
        "EXPECTED_ARTIFACT_SHA256",
        hashlib.sha256(target.read_bytes()).hexdigest(),
    )
    with pytest.raises(ValueError, match="multiple provider"):
        repair.load_artifact(target)


def test_rollback_cli_does_not_require_artifact(monkeypatch, tmp_path):
    monkeypatch.setattr(repair, "EXPECTED_PAIRS", 1)
    preimage = repair.preimage_from_plan(
        one_pair(monkeypatch), artifact_geometry="a" * 64
    )
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "preimage.json"
    digest = repair.atomic_private_json(path.resolve(), preimage)
    seen = {}

    class Lock:
        def __init__(self, _path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(
        repair, "load_db_url", lambda _path: ("postgres://db", "/safe/env")
    )
    monkeypatch.setattr(repair, "assert_expected_database_target", lambda *_args: None)
    monkeypatch.setattr(repair, "canonical_shared_lock_dir", lambda: tmp_path / "lock")
    monkeypatch.setattr(repair, "SharedLock", Lock)
    monkeypatch.setattr(
        repair, "run_psql", lambda _db, sql: seen.update(sql=sql) or {"ok": True}
    )
    assert (
        repair.main(
            [
                "--env-file",
                "/safe/env",
                "--rollback-preimage",
                str(path.resolve()),
                "--expected-preimage-sha256",
                digest,
            ]
        )
        == 0
    )
    assert "rollback_applied" in seen["sql"]
