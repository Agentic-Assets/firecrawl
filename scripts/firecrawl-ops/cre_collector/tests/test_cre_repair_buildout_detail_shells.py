import hashlib
import json
from pathlib import Path

import pytest

import cre_repair_buildout_detail_shells as repair


ROW_ID = "11111111-1111-1111-1111-111111111111"
BROKERAGE_ID = "22222222-2222-2222-2222-222222222222"


def preimage_payload() -> dict:
    return {
        "source": "svn",
        "count": 1,
        "digest": "c" * 32,
        "db_target_sha256": repair.EXPECTED_DB_TARGET_SHA256,
        "captured_at": "2026-07-30T18:30:00+00:00",
        "column_shell_count": 1,
        "root_shell_count": 1,
        "broad_marker_count": 1,
        "unexpected_marker_count": 0,
        "nested_marker_count": 0,
        "invalid_raw_data_count": 0,
        "rows": [
            {
                "id": ROW_ID,
                "brokerage_id": BROKERAGE_ID,
                "status": "active",
                "deleted_at": None,
                "markdown": "Listing not found",
                "raw_data": {"markdown": "Listing not found", "sourceKey": "svn"},
                "raw_data_is_sql_null": False,
            }
        ],
    }


def test_source_is_frozen_to_svn_and_exact_hashes_are_used():
    assert repair.validate_source("svn") == "svn"
    with pytest.raises(ValueError):
        repair.validate_source("lee-associates")
    predicate = repair.exact_shell_predicate("l.markdown")
    for digest in repair.EXACT_SHELL_MD5:
        assert digest in predicate
    assert "LIKE" not in predicate


def test_preflight_is_locked_by_main_read_only_and_audits_variants():
    sql = repair.preflight_sql(include_rows=True)
    assert "BEGIN READ ONLY" in sql
    assert sql.rstrip().endswith("ROLLBACK;")
    assert "b.slug = 'svn'" in sql
    assert "l.status = 'active'" in sql
    assert "l.deleted_at IS NULL" in sql
    assert "'unexpected_marker_count'" in sql
    assert "'nested_marker_count'" in sql
    assert "'invalid_raw_data_count'" in sql
    assert "(l.raw_data - 'markdown')::text" in sql
    assert "'brokerage_id'" in sql
    assert "'raw_data_is_sql_null'" in sql


def test_validate_audit_report_refuses_scope_disagreement():
    base = {
        "source": "svn",
        "count": 1,
        "digest": "a" * 32,
        "column_shell_count": 1,
        "root_shell_count": 0,
        "broad_marker_count": 1,
        "unexpected_marker_count": 0,
        "nested_marker_count": 0,
        "invalid_raw_data_count": 0,
    }
    repair.validate_audit_report(base)
    for key in (
        "unexpected_marker_count",
        "nested_marker_count",
        "invalid_raw_data_count",
    ):
        bad = dict(base, **{key: 1})
        with pytest.raises(RuntimeError):
            repair.validate_audit_report(bad)
    with pytest.raises(RuntimeError):
        repair.validate_audit_report(dict(base, broad_marker_count=2))


def test_apply_is_exact_scoped_and_verifies_derived_post_state():
    sql = repair.apply_sql(705, "a" * 32, commit=True)
    assert "BEGIN ISOLATION LEVEL SERIALIZABLE" in sql
    assert f"pg_advisory_xact_lock({repair.ADVISORY_LOCK_KEY})" in sql
    assert "actual_count <> 705" in sql
    assert "THEN NULL" in sql
    assert "l.raw_data - 'markdown'" in sql
    assert "l.status IS DISTINCT FROM s.status" in sql
    assert "l.deleted_at IS DISTINCT FROM s.deleted_at" in sql
    assert "updated_count <> 705" in sql
    assert "remaining_broad" in sql
    assert "remaining_nested" in sql
    assert "SET status =" not in sql
    assert "SET deleted_at =" not in sql
    assert sql.rstrip().endswith("COMMIT;")


def test_verify_apply_rolls_back():
    sql = repair.apply_sql(1, "b" * 32, commit=False)
    assert "'verify_apply_rollback'" in sql
    assert sql.rstrip().endswith("ROLLBACK;")


def test_rollback_recomputes_digest_and_refuses_scope_or_post_state_drift():
    sql = repair.rollback_sql(preimage_payload())
    assert repair.digest_sql("p") in sql
    assert "invalid_preimage" in sql
    assert "b.slug <> 'svn'" in sql
    assert "l.status IS DISTINCT FROM p.status" in sql
    assert "raw_data_is_sql_null" in sql
    assert "changed since repair" not in sql
    assert "drifted" in sql
    assert "SET markdown = p.markdown, raw_data = p.raw_data" in sql
    assert "SET status =" not in sql
    assert "SET deleted_at =" not in sql


def test_private_preimage_is_exclusive_owner_only_and_hash_checked(tmp_path):
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    target = private_dir / "preimage.json"
    payload = preimage_payload()
    written_sha = repair.atomic_private_json(target, payload)
    assert target.stat().st_mode & 0o077 == 0
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    assert written_sha == expected
    loaded, actual = repair.load_private_preimage(target.resolve(), expected)
    assert loaded == payload
    assert actual == expected
    with pytest.raises(FileExistsError):
        repair.atomic_private_json(target, payload)
    with pytest.raises(ValueError):
        repair.load_private_preimage(target.resolve(), "0" * 64)


def test_private_preimage_rejects_group_readable_and_symlink(tmp_path):
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    target = private_dir / "preimage.json"
    target.write_text(json.dumps(preimage_payload()))
    target.chmod(0o640)
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        repair.load_private_preimage(target.resolve(), expected)

    target.chmod(0o600)
    link = private_dir / "preimage-link.json"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        repair.load_private_preimage(link, expected)


def test_private_preimage_refuses_oversized_payload_before_creation(
    monkeypatch, tmp_path
):
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    target = private_dir / "preimage.json"
    monkeypatch.setattr(repair, "MAX_PREIMAGE_BYTES", 8)
    with pytest.raises(ValueError):
        repair.atomic_private_json(target, preimage_payload())
    assert not target.exists()


def test_preimage_validation_rejects_wrong_source_and_lifecycle():
    with pytest.raises(ValueError):
        repair.validate_preimage(dict(preimage_payload(), source="lee-associates"))
    bad = preimage_payload()
    bad["rows"][0]["status"] = "inactive"
    with pytest.raises(ValueError):
        repair.validate_preimage(bad)
    bad = preimage_payload()
    bad["rows"][0].pop("raw_data_is_sql_null")
    with pytest.raises(ValueError):
        repair.validate_preimage(bad)
    bad = preimage_payload()
    bad["rows"][0]["raw_data_is_sql_null"] = True
    with pytest.raises(ValueError):
        repair.validate_preimage(bad)


def test_apply_holds_canonical_lock_through_capture_and_transaction(
    monkeypatch, tmp_path
):
    state = {"locked": False, "calls": 0}

    class FakeLock:
        def __init__(self, path):
            assert path == Path("/canonical/.cre.lock")

        def __enter__(self):
            state["locked"] = True

        def __exit__(self, *_args):
            state["locked"] = False

    captured = preimage_payload()
    captured.pop("db_target_sha256")
    captured.pop("captured_at")

    def fake_psql(_db_url, sql):
        assert state["locked"]
        state["calls"] += 1
        if "BEGIN READ ONLY" in sql:
            return captured
        return {"ok": True, "mode": "applied", "source": "svn", "repaired": 1}

    monkeypatch.setattr(
        repair, "load_db_url", lambda _path: ("postgres://db", "/safe/env")
    )
    monkeypatch.setattr(
        repair, "assert_expected_database_target", lambda *_args: None
    )
    monkeypatch.setattr(
        repair, "canonical_shared_lock_dir", lambda: Path("/canonical/.cre.lock")
    )
    monkeypatch.setattr(repair, "SharedLock", FakeLock)
    monkeypatch.setattr(repair, "run_psql", fake_psql)

    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    preimage = private_dir / "preimage.json"
    rc = repair.main(
        [
            "--source",
            "svn",
            "--env-file",
            "/safe/env",
            "--expected-count",
            "1",
            "--expected-digest",
            "c" * 32,
            "--preimage",
            str(preimage),
            "--apply",
        ]
    )
    assert rc == 0
    assert state == {"locked": False, "calls": 2}
    assert preimage.stat().st_mode & 0o077 == 0


def test_preflight_main_holds_canonical_lock(monkeypatch):
    state = {"locked": False}

    class FakeLock:
        def __init__(self, _path):
            pass

        def __enter__(self):
            state["locked"] = True

        def __exit__(self, *_args):
            state["locked"] = False

    report = {
        "source": "svn",
        "count": 0,
        "digest": hashlib.md5(b"").hexdigest(),
        "column_shell_count": 0,
        "root_shell_count": 0,
        "broad_marker_count": 0,
        "unexpected_marker_count": 0,
        "nested_marker_count": 0,
        "invalid_raw_data_count": 0,
    }
    monkeypatch.setattr(
        repair, "load_db_url", lambda _path: ("postgres://db", "/safe/env")
    )
    monkeypatch.setattr(
        repair, "assert_expected_database_target", lambda *_args: None
    )
    monkeypatch.setattr(
        repair, "canonical_shared_lock_dir", lambda: Path("/canonical/.cre.lock")
    )
    monkeypatch.setattr(repair, "SharedLock", FakeLock)

    def fake_psql(_db_url, _sql):
        assert state["locked"]
        return report

    monkeypatch.setattr(repair, "run_psql", fake_psql)
    assert repair.main(["--env-file", "/safe/env"]) == 0
    assert state["locked"] is False
