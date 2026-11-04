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
        if "jsonb_agg(to_jsonb(q)" in statement:
            return [row]
        assert json.loads((tmp_path / "snapshot.json").read_text())["rows"] == [row]
        return {"applied": [row_id]}
    monkeypatch.setattr(mod, "query", query)
    result = mod.run_redrive([row_id], "a" * 40, "b" * 64, "synthetic", apply=True)
    assert result == tmp_path
    assert json.loads((tmp_path / "result.json").read_text())["applied"] == [row_id]
    assert lock.cleared


def test_unknown_write_leaves_recovery_fence_armed(monkeypatch, tmp_path):
    import cre_exact_redrive as mod
    row_id = "11111111-1111-4111-8111-111111111111"
    row = {"id": row_id, "source_key": "svn", "brokerage_id": "b", "external_id": "e",
           "url": "https://x", "reason": "changed", "priority": 100, "enqueued_at": "old",
           "claimed_at": None, "done_at": None, "attempts": 5, "last_error": "failure"}
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
        mod.run_redrive([row_id], "a" * 40, "b" * 64, "synthetic", apply=True)
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
        mod.run_redrive([row_id], "a" * 40, "b" * 64, "synthetic", apply=True)


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
