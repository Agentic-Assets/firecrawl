import os

import pytest

from cre_partial_cursor import advance_cursor, read_cursor


def test_cursor_requires_bound_settlement_and_is_idempotent(tmp_path):
    lock = tmp_path / ".cre.lock"
    target, artifact, attempt = "a" * 64, "b" * 64, "c" * 32
    proof = {"ok": True, "artifact_sha256": artifact, "generation_id": "gen",
             "attempt_id": attempt, "database_target_sha256": target, "outcome": "exact_commit"}
    assert read_cursor(lock, "colliers", target) == (1, None)
    with pytest.raises(ValueError, match="settlement"):
        advance_cursor(lock, "colliers", target, 1, 5, generation="gen", artifact_sha256=artifact,
                       attempt_id=attempt, outcome="exact_commit", proof={**proof, "ok": False}, owner_check=lambda: None)
    assert read_cursor(lock, "colliers", target) == (1, None)
    result = advance_cursor(lock, "colliers", target, 1, 5, generation="gen", artifact_sha256=artifact,
                            attempt_id=attempt, outcome="exact_commit", proof=proof, owner_check=lambda: None)
    assert result["position"] == 5
    assert read_cursor(lock, "colliers", target)[0] == 5
    assert advance_cursor(lock, "colliers", target, 1, 5, generation="gen", artifact_sha256=artifact,
                          attempt_id=attempt, outcome="exact_commit", proof=proof, owner_check=lambda: None) == result
    file = next(tmp_path.glob(".partial-cursor-colliers-*.json"))
    assert file.stat().st_mode & 0o077 == 0
    assert read_cursor(lock, "colliers", "d" * 64) == (1, None)
    with pytest.raises(ValueError, match="changed"):
        advance_cursor(lock, "colliers", target, 1, 7, generation="next", artifact_sha256=artifact,
                       attempt_id=attempt, outcome="exact_commit", proof={**proof, "generation_id": "next"},
                       owner_check=lambda: None)


def test_lost_owner_before_or_after_cursor_publication_is_not_silent(tmp_path):
    lock = tmp_path / ".cre.lock"
    target, artifact, attempt = "a" * 64, "b" * 64, "c" * 32
    proof = {"ok": True, "artifact_sha256": artifact, "generation_id": "gen",
             "attempt_id": attempt, "database_target_sha256": target, "outcome": "exact_commit"}
    for fail_at, expected_position in [(2, 1), (3, 5)]:
        calls = 0
        def owner(fail_at=fail_at):
            nonlocal calls
            calls += 1
            if calls == fail_at:
                raise RuntimeError("owner lost")
        with pytest.raises(RuntimeError, match="owner lost"):
            advance_cursor(lock, "colliers", target, 1, 5, generation="gen", artifact_sha256=artifact,
                           attempt_id=attempt, outcome="exact_commit", proof=proof, owner_check=owner)
        assert read_cursor(lock, "colliers", target)[0] == expected_position
    assert not any(path.name.endswith(".tmp") for path in tmp_path.iterdir())


def test_cursor_refuses_symlink_and_unsafe_source(tmp_path):
    lock = tmp_path / ".cre.lock"
    with pytest.raises(ValueError):
        read_cursor(lock, "../colliers", "a" * 64)
    name = ".partial-cursor-colliers-" + "a" * 64 + ".json"
    os.symlink(tmp_path / "missing", tmp_path / name)
    with pytest.raises(OSError):
        read_cursor(lock, "colliers", "a" * 64)
