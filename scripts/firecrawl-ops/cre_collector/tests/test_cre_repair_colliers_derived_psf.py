"""Fail-closed contracts for the one-row Colliers derived-PSF repair."""

from __future__ import annotations

import copy

import pytest

import cre_repair_colliers_derived_psf as repair


def state(*, psf=repair.EXPECTED_PSF):
    return {
        "brokerage_slug": "colliers",
        "source_key": "colliers",
        "generation": repair.EXPECTED_GENERATION,
        "row": {
            "id": repair.EXPECTED_ID,
            "brokerage_id": repair.EXPECTED_BROKERAGE_ID,
            "external_id": repair.EXPECTED_EXTERNAL_ID,
            "title": repair.EXPECTED_TITLE,
            "status": "active",
            "deleted_at": None,
            "sale_price_usd": repair.EXPECTED_PRICE,
            "size_sf": repair.EXPECTED_SIZE,
            "sale_price_per_sf": psf,
            "updated_at": "2026-09-10T04:39:37.880242+00:00",
            "raw_data": {"sourceKey": "colliers"},
        },
    }


def test_validate_state_accepts_only_exact_before_and_after_shapes():
    repair.validate_state(state(), repaired=False)
    repair.validate_state(state(psf=None), repaired=True)
    drifted = copy.deepcopy(state())
    drifted["row"]["size_sf"] = 194
    with pytest.raises(ValueError, match="size_sf drifted"):
        repair.validate_state(drifted, repaired=False)


def test_apply_sql_changes_only_derived_psf_and_is_exactly_guarded():
    sql = repair.mutation_sql(state(), commit=True)
    assert "hashtextextended('credeals:listing-lifecycle:v1', 0)" in sql
    assert "SET sale_price_per_sf=NULL" in sql
    assert "status=" not in sql.split("SET sale_price_per_sf=NULL", 1)[1].split("WHERE", 1)[0]
    assert "deleted_at=" not in sql.split("SET sale_price_per_sf=NULL", 1)[1].split("WHERE", 1)[0]
    assert f"l.sale_price_usd={repair.EXPECTED_PRICE}::numeric" in sql
    assert f"l.size_sf={repair.EXPECTED_SIZE}::numeric" in sql
    assert f"l.sale_price_per_sf={repair.EXPECTED_PSF}::numeric" in sql
    assert "l.status='active'" in sql
    assert "l.deleted_at IS NULL" in sql
    assert "l.raw_data->>'sourceKey'='colliers'" in sql
    assert "l.raw_data=" in sql
    assert "COMMIT;" in sql


def test_verification_sql_rolls_back_and_asserts_one_changed_row():
    sql = repair.mutation_sql(state(), commit=False)
    assert "changed_count <> 1" in sql
    assert "ROLLBACK;" in sql
    assert "COMMIT;" not in sql


def test_rollback_sql_requires_repaired_precondition_and_restores_only_psf():
    sql = repair.mutation_sql(state(psf=None), commit=True, rollback=True)
    assert f"SET sale_price_per_sf={repair.EXPECTED_PSF}::numeric" in sql
    assert "l.sale_price_per_sf IS NULL" in sql
    assert "COMMIT;" in sql
    standalone = repair.mutation_sql(
        state(psf=None), commit=True, rollback=True, guard_updated_at=False
    )
    assert "AND l.updated_at=" not in standalone
    assert "l.raw_data=" in standalone


def test_private_preimage_bytes_are_bounded():
    payload = {
        "db_target_sha256": repair.EXPECTED_DB_TARGET_SHA256,
        "state": state(),
    }
    assert len(repair.private_json_bytes(payload)) < repair.MAX_PREIMAGE_BYTES


def test_private_preimage_supports_new_and_existing_secure_parents(tmp_path):
    payload = {
        "db_target_sha256": repair.EXPECTED_DB_TARGET_SHA256,
        "state": state(),
    }
    first = tmp_path / "new-parent" / "preimage.json"
    assert len(repair.write_private_preimage(first, payload)) == 64
    existing = tmp_path / "existing-parent"
    existing.mkdir(mode=0o700)
    second = existing / "preimage.json"
    assert len(repair.write_private_preimage(second, payload)) == 64


def test_reserved_postimage_can_be_replaced_atomically(tmp_path):
    path = tmp_path / "evidence" / "postimage.json"
    repair.write_private_bytes(path, b'{"status":"pending"}\n')
    digest = repair.replace_private_bytes(path, b'{"status":"applied"}\n')
    assert len(digest) == 64
    assert path.read_text() == '{"status":"applied"}\n'


def test_evidence_directory_can_be_fsynced_before_commit(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    repair.fsync_directory(evidence)


def test_cli_private_path_rejects_symlink_components(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        repair.cli_private_path(link / "preimage.json")
