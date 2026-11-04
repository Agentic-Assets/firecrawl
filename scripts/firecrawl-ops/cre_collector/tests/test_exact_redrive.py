import pytest
from cre_exact_redrive import build_apply_sql, validate_selection


def test_selection_is_bounded_unique_uuid_and_source_scoped():
    valid = "11111111-1111-4111-8111-111111111111"
    assert validate_selection("svn", [valid]) == [valid]
    for source, rows in [("newmark", [valid]), ("svn", []), ("svn", [valid, valid]), ("svn", ["not-uuid"])]:
        with pytest.raises(ValueError):
            validate_selection(source, rows)


def test_refuses_to_prepare_write_without_exact_snapshot_and_target_binding():
    with pytest.raises(ValueError):
        build_apply_sql([], "0" * 64)
    with pytest.raises(ValueError):
        build_apply_sql([{"id": "11111111-1111-4111-8111-111111111111"}], "invalid")


def test_private_receipt_is_exclusive_and_preserves_exact_prior_state(tmp_path):
    import json
    import stat

    from cre_exact_redrive import write_private_receipt
    payload = {"attempts": 7, "last_error": "prior error"}
    write_private_receipt(tmp_path, "snapshot.json", payload)
    path = tmp_path / "snapshot.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == payload
    with pytest.raises(FileExistsError):
        write_private_receipt(tmp_path, "snapshot.json", {"attempts": 0})
    assert json.loads(path.read_text()) == payload


def test_redrive_publishes_snapshot_before_apply_and_records_independent_results(monkeypatch, tmp_path):
    import json

    import cre_exact_redrive as mod
    row_id = "11111111-1111-4111-8111-111111111111"
    row = {"id": row_id, "source_key": "svn", "brokerage_id": "b", "external_id": "e",
           "url": "https://x", "reason": "changed", "priority": 100, "enqueued_at": "old",
           "claimed_at": None, "done_at": None, "attempts": 5, "last_error": "failure"}
    preview = tmp_path / "preview"
    preview.mkdir(mode=0o700)
    snapshot = {"schema": "cre-exact-redrive-snapshot/v1", "source": "svn", "expected_sha": "a" * 40,
                "database_target_sha256": "b" * 64, "requested_ids": [row_id], "rows": [row]}
    digest = mod.write_private_receipt(preview, "snapshot.json", snapshot)
    class Lock:
        path = tmp_path
        cleared = False
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def clear_recovery_requirement(self): self.cleared = True
    lock = Lock()
    monkeypatch.setattr(mod, "verify_exact_checkout", lambda *_: None)
    monkeypatch.setattr(mod, "load_db_url", lambda _: ("postgres://synthetic", "unused"))
    monkeypatch.setattr(mod, "assert_expected_database_target", lambda *_: None)
    monkeypatch.setattr(mod, "canonical_shared_lock_dir", lambda: tmp_path / "lock")
    monkeypatch.setattr(mod, "SharedLock", lambda *_, **__: lock)
    monkeypatch.setattr(mod, "require_clear", lambda _: None)
    monkeypatch.setattr(mod, "allocate_directory", lambda _: tmp_path)
    def query(_, statement):
        assert "jsonb_agg(to_jsonb(q)" not in statement
        assert '"last_error":"failure"' in statement
        assert json.loads((tmp_path / "snapshot.json").read_text())["rows"] == [row]
        return {"applied": [row_id]}
    monkeypatch.setattr(mod, "query", query)
    result = mod.run_redrive([row_id], "a" * 40, "b" * 64, "synthetic", apply=True, preview_path=preview / "snapshot.json", preview_sha256=digest)
    assert result == tmp_path
    assert json.loads((tmp_path / "result.json").read_text())["applied"] == [row_id]
    assert lock.cleared


def test_unknown_write_leaves_recovery_fence_armed(monkeypatch, tmp_path):
    import cre_exact_redrive as mod
    row_id = "11111111-1111-4111-8111-111111111111"
    row = {"id": row_id, "source_key": "svn", "brokerage_id": "b", "external_id": "e",
           "url": "https://x", "reason": "changed", "priority": 100, "enqueued_at": "old",
           "claimed_at": None, "done_at": None, "attempts": 5, "last_error": "failure"}
    preview = tmp_path / "preview"
    preview.mkdir(mode=0o700)
    snapshot = {"schema": "cre-exact-redrive-snapshot/v1", "source": "svn", "expected_sha": "a" * 40,
                "database_target_sha256": "b" * 64, "requested_ids": [row_id], "rows": [row]}
    digest = mod.write_private_receipt(preview, "snapshot.json", snapshot)
    class Lock:
        path = tmp_path
        cleared = False
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def clear_recovery_requirement(self): self.cleared = True
    lock = Lock()
    monkeypatch.setattr(mod, "canonical_shared_lock_dir", lambda: tmp_path / "lock")
    monkeypatch.setattr(mod, "SharedLock", lambda *_, **__: lock)
    monkeypatch.setattr(mod, "verify_exact_checkout", lambda *_: None)
    monkeypatch.setattr(mod, "load_db_url", lambda _: ("postgres://synthetic", "unused"))
    monkeypatch.setattr(mod, "assert_expected_database_target", lambda *_: None)
    monkeypatch.setattr(mod, "require_clear", lambda _: None)
    monkeypatch.setattr(mod, "allocate_directory", lambda _: tmp_path)
    def query(_, statement):
        if "jsonb_agg(to_jsonb(q)" in statement: return [row]
        raise RuntimeError("uncertain commit")
    monkeypatch.setattr(mod, "query", query)
    with pytest.raises(RuntimeError):
        mod.run_redrive([row_id], "a" * 40, "b" * 64, "synthetic", apply=True, preview_path=preview / "snapshot.json", preview_sha256=digest)
    assert (tmp_path / "snapshot.json").exists()
    assert not (tmp_path / "result.json").exists()
    assert not lock.cleared


def test_wrong_database_target_never_acquires_owner_or_mutates(monkeypatch):
    import cre_exact_redrive as mod
    row_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(mod, "verify_exact_checkout", lambda *_: None)
    monkeypatch.setattr(mod, "load_db_url", lambda _: ("postgres://synthetic", "unused"))
    def wrong(*_):
        raise SystemExit("target mismatch")
    monkeypatch.setattr(mod, "assert_expected_database_target", wrong)
    def forbidden(*_, **__):
        raise AssertionError("owner or mutation reached")
    monkeypatch.setattr(mod, "SharedLock", forbidden)
    with pytest.raises(SystemExit):
        mod.run_redrive([row_id], "a" * 40, "b" * 64, "synthetic", apply=True, preview_path="/unused/snapshot.json", preview_sha256="c" * 64)


def test_checkout_requires_selected_main_not_another_origin_ref(monkeypatch):
    import subprocess

    import cre_exact_redrive as mod
    sha = "a" * 40
    class Result:
        returncode = 0
        def __init__(self, output): self.stdout = output
    def run(argv, **_):
        if argv[-2:] == ["rev-parse", "HEAD"]: return Result(sha + "\n")
        if "status" in argv: return Result("")
        if "refs/remotes/origin/main" in argv: return Result("b" * 40 + "\n")
        return Result(sha + "\n")
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError): mod.verify_exact_checkout(sha)


def test_preview_saves_private_snapshot_without_mutation(monkeypatch, tmp_path):
    import json

    import cre_exact_redrive as mod
    row_id = "11111111-1111-4111-8111-111111111111"
    row = {"id": row_id, "source_key": "svn", "attempts": 5, "last_error": "failure"}
    class Lock:
        path = tmp_path
        cleared = False
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def clear_recovery_requirement(self): self.cleared = True
    lock = Lock()
    monkeypatch.setattr(mod, "canonical_shared_lock_dir", lambda: tmp_path / "lock")
    monkeypatch.setattr(mod, "SharedLock", lambda *_, **__: lock)
    monkeypatch.setattr(mod, "verify_exact_checkout", lambda *_: None)
    monkeypatch.setattr(mod, "load_db_url", lambda _: ("postgres://synthetic", "unused"))
    monkeypatch.setattr(mod, "assert_expected_database_target", lambda *_: None)
    monkeypatch.setattr(mod, "require_clear", lambda _: None)
    monkeypatch.setattr(mod, "allocate_directory", lambda _: tmp_path)
    def query(_, statement):
        assert "jsonb_agg(to_jsonb(q)" in statement
        return [row]
    monkeypatch.setattr(mod, "query", query)
    mod.run_redrive([row_id], "a" * 40, "b" * 64, "synthetic", apply=False)
    assert json.loads((tmp_path / "snapshot.json").read_text())["rows"] == [row]
    assert not (tmp_path / "result.json").exists()
    assert lock.cleared


def test_apply_requires_a_bound_preview():
    import cre_exact_redrive as mod
    row_id = "11111111-1111-4111-8111-111111111111"
    with pytest.raises(ValueError):
        mod.run_redrive([row_id], "a" * 40, "b" * 64, "synthetic", apply=True)


def test_preview_digest_rejects_modified_content(tmp_path):
    import hashlib

    import cre_exact_redrive as mod
    content = b'{"source":"svn"}'
    receipt = tmp_path / "snapshot.json"
    receipt.write_bytes(content)
    receipt.chmod(0o600)
    digest = hashlib.sha256(content).hexdigest()
    receipt.write_bytes(b'{"source":"newmark"}')
    with pytest.raises(ValueError):
        mod.read_preview(receipt, digest)


def test_preview_rejects_rows_outside_selected_ids():
    from cre_exact_redrive import validate_preview_binding
    selected = "11111111-1111-4111-8111-111111111111"
    foreign = "22222222-2222-4222-8222-222222222222"
    snapshot = {"schema": "cre-exact-redrive-snapshot/v1", "source": "svn", "expected_sha": "a" * 40,
                "database_target_sha256": "b" * 64, "requested_ids": [selected], "rows": [{"id": foreign}]}
    with pytest.raises(ValueError):
        validate_preview_binding(snapshot, [selected], "a" * 40, "b" * 64)


def test_selection_accepts_200_distinct_ids_without_a_count_cap():
    import uuid
    ids = [str(uuid.UUID(int=value)) for value in range(1, 201)]
    assert validate_selection("svn", ids) == ids


def test_oversized_receipt_is_refused_before_publication(monkeypatch, tmp_path):
    import cre_exact_redrive as mod
    monkeypatch.setattr(mod, "MAX_RECEIPT_BYTES", 128)
    with pytest.raises(ValueError):
        mod.write_private_receipt(tmp_path, "snapshot.json", {"contents": "x" * 256})
    assert not (tmp_path / "snapshot.json").exists()


def test_oversized_apply_preimage_never_dispatches_write(monkeypatch, tmp_path):
    import cre_exact_redrive as mod
    row_id = "11111111-1111-4111-8111-111111111111"
    snapshot = {"schema": "cre-exact-redrive-snapshot/v1", "source": "svn", "expected_sha": "a" * 40,
                "database_target_sha256": "b" * 64, "requested_ids": [row_id],
                "rows": [{"id": row_id, "last_error": "x" * 512}]}
    class Lock:
        path = tmp_path
        cleared = False
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def clear_recovery_requirement(self): self.cleared = True
    lock = Lock()
    monkeypatch.setattr(mod, "MAX_RECEIPT_BYTES", 128)
    monkeypatch.setattr(mod, "verify_exact_checkout", lambda *_: None)
    monkeypatch.setattr(mod, "load_db_url", lambda _: ("postgres://synthetic", "unused"))
    monkeypatch.setattr(mod, "assert_expected_database_target", lambda *_: None)
    monkeypatch.setattr(mod, "canonical_shared_lock_dir", lambda: tmp_path / "lock")
    monkeypatch.setattr(mod, "SharedLock", lambda *_, **__: lock)
    monkeypatch.setattr(mod, "require_clear", lambda _: None)
    monkeypatch.setattr(mod, "allocate_directory", lambda _: tmp_path)
    monkeypatch.setattr(mod, "read_preview", lambda *_: snapshot)
    def forbidden(*_):
        raise AssertionError("write dispatched")
    monkeypatch.setattr(mod, "query", forbidden)
    with pytest.raises(ValueError, match="receipt exceeds"):
        mod.run_redrive([row_id], "a" * 40, "b" * 64, "synthetic", apply=True,
                        preview_path="/synthetic/snapshot.json", preview_sha256="c" * 64)
    assert not (tmp_path / "snapshot.json").exists()
    assert lock.cleared


def test_large_selection_preserves_requested_and_result_order(monkeypatch, tmp_path):
    import json
    import uuid

    import cre_exact_redrive as mod
    ids = [str(uuid.UUID(int=value)) for value in range(1, 2001)]
    rows = [{"id": item, "source_key": "svn", "brokerage_id": "b", "external_id": item,
             "url": "https://x", "reason": "changed", "priority": 100, "enqueued_at": "old",
             "claimed_at": None, "done_at": None, "attempts": 5, "last_error": "old"} for item in ids]
    snapshot = {"schema": "cre-exact-redrive-snapshot/v1", "source": "svn", "expected_sha": "a" * 40,
                "database_target_sha256": "b" * 64, "requested_ids": ids, "rows": rows}
    assert mod.validate_preview_binding(snapshot, ids, "a" * 40, "b" * 64) == rows
    with pytest.raises(ValueError):
        mod.validate_preview_binding(snapshot, list(reversed(ids)), "a" * 40, "b" * 64)
    preview = tmp_path / "preview"
    preview.mkdir(mode=0o700)
    digest = mod.write_private_receipt(preview, "snapshot.json", snapshot)
    class Lock:
        path = tmp_path
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def clear_recovery_requirement(self): pass
    monkeypatch.setattr(mod, "canonical_shared_lock_dir", lambda: tmp_path / "lock")
    monkeypatch.setattr(mod, "SharedLock", lambda *_, **__: Lock())
    monkeypatch.setattr(mod, "verify_exact_checkout", lambda *_: None)
    monkeypatch.setattr(mod, "load_db_url", lambda _: ("postgres://synthetic", "unused"))
    monkeypatch.setattr(mod, "assert_expected_database_target", lambda *_: None)
    monkeypatch.setattr(mod, "require_clear", lambda _: None)
    monkeypatch.setattr(mod, "allocate_directory", lambda _: tmp_path)
    applied = list(reversed(ids[::2]))
    monkeypatch.setattr(mod, "query", lambda *_: {"applied": applied})
    mod.run_redrive(ids, "a" * 40, "b" * 64, "synthetic", apply=True,
                    preview_path=preview / "snapshot.json", preview_sha256=digest)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["applied"] == applied
    assert result["held"] == ids[1::2]
