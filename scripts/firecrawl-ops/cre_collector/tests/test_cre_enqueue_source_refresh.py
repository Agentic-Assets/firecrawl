from pathlib import Path

import pytest

import cre_enqueue_source_refresh as enqueue
import cre_enrich


def test_validate_source_accepts_canonical_key():
    assert enqueue.validate_source("svn") == "svn"
    assert enqueue.validate_source("lee-associates") == "lee-associates"


def test_queue_mutation_lock_matches_worker_claim_lock():
    assert (
        enqueue.QUEUE_MUTATION_ADVISORY_LOCK
        == cre_enrich.QUEUE_MUTATION_ADVISORY_LOCK
    )


@pytest.mark.parametrize("source", ["", "SVN", "svn;drop", "../svn", "svn key"])
def test_validate_source_rejects_unsafe_key(source):
    with pytest.raises(ValueError, match="lowercase source key"):
        enqueue.validate_source(source)


def test_scope_sql_is_read_only_and_source_bound():
    sql = enqueue.source_scope_sql("svn")
    assert "source_key = 'svn'" in sql
    assert "count(*)" in sql
    assert "INSERT INTO" not in sql
    assert "DELETE FROM" not in sql


def test_enqueue_sql_is_exact_scoped_and_fail_closed():
    sql = enqueue.enqueue_sql("svn", 5832)
    assert (
        f"SELECT pg_advisory_xact_lock("
        f"{enqueue.QUEUE_MUTATION_ADVISORY_LOCK});"
    ) in sql
    assert "actual <> 5832" in sql
    assert "invalid <> 0" in sql
    assert "WHERE q.source_key = 'svn'" in sql
    assert "claimed <> 0" in sql
    assert "attempts < 5" in sql
    assert "missing <> 0 OR extra <> 0" in sql
    assert "'changed', 20" in sql
    assert "source_key = 'svn'" in sql
    assert "COMMIT;" in sql
    assert "--mark-missing" not in sql
    assert "deleted_at =" not in sql


@pytest.mark.parametrize("expected", [0, -1])
def test_enqueue_sql_rejects_nonpositive_expected_count(expected):
    with pytest.raises(ValueError, match="positive"):
        enqueue.enqueue_sql("svn", expected)


def test_apply_holds_canonical_lock_through_preflight_apply_and_readback(
    monkeypatch,
):
    state = {"locked": False, "calls": 0}

    class FakeLock:
        def __init__(self, path):
            assert path == Path("/canonical/.cre.lock")

        def __enter__(self):
            state["locked"] = True
            return self

        def __exit__(self, *_args):
            state["locked"] = False

    def fake_psql(_db_url, sql):
        assert state["locked"] is True
        state["calls"] += 1
        if "CREATE TEMP TABLE _source_refresh_scope" in sql:
            return [["5971", "5971", "0"]]
        if state["calls"] == 1:
            return [["5971", "0", "355", "355", "0"]]
        return [["5971", "0", "5971", "5971", "0"]]

    monkeypatch.setattr(
        enqueue, "load_db_url", lambda _path: ("postgres://db", "/safe/env")
    )
    monkeypatch.setattr(
        enqueue, "assert_expected_database_target", lambda *_args: None
    )
    monkeypatch.setattr(
        enqueue, "canonical_shared_lock_dir", lambda: Path("/canonical/.cre.lock")
    )
    monkeypatch.setattr(enqueue, "SharedLock", FakeLock)
    monkeypatch.setattr(enqueue, "run_psql", fake_psql)

    rc = enqueue.main(
        [
            "--source",
            "svn",
            "--expected-db-target-sha256",
            "a" * 64,
            "--expected-active-count",
            "5971",
            "--apply",
        ]
    )

    assert rc == 0
    assert state == {"locked": False, "calls": 3}


def test_apply_refuses_claimed_rows_before_replacing_queue(monkeypatch):
    state = {"apply_called": False}

    class FakeLock:
        def __init__(self, _path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    def fake_psql(_db_url, sql):
        if "CREATE TEMP TABLE _source_refresh_scope" in sql:
            state["apply_called"] = True
        return [["5971", "0", "355", "355", "1"]]

    monkeypatch.setattr(
        enqueue, "load_db_url", lambda _path: ("postgres://db", "/safe/env")
    )
    monkeypatch.setattr(
        enqueue, "assert_expected_database_target", lambda *_args: None
    )
    monkeypatch.setattr(
        enqueue, "canonical_shared_lock_dir", lambda: Path("/canonical/.cre.lock")
    )
    monkeypatch.setattr(enqueue, "SharedLock", FakeLock)
    monkeypatch.setattr(enqueue, "run_psql", fake_psql)

    with pytest.raises(RuntimeError, match="claimed"):
        enqueue.main(
            [
                "--source",
                "svn",
                "--expected-db-target-sha256",
                "a" * 64,
                "--expected-active-count",
                "5971",
                "--apply",
            ]
        )

    assert state["apply_called"] is False
