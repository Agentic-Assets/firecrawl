"""Authorization contracts bind verifiable PostgreSQL proof and prior apply receipts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import cre_repair_jll_images as repair


STAGE_SHA256 = "9" * 64
COLLECTOR_SHA = "d" * 40
COMMENT_ID = "a72b1d10-42c7-4d90-a79a-428767a894d0"


def _private_dir(tmp_path: Path) -> Path:
    path = tmp_path / "private"
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _write_private(path: Path, value: dict) -> str:
    repair.write_json(path, value)
    return repair.file_hash(path)


def _evidence(private_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        work_dir=private_dir,
        candidate_count=3,
        pins={"candidate_count": 3, "candidate_ids_sha256": "1" * 64},
    )


def _terminal_proof() -> dict:
    tests = Path(repair.__file__).parent / "tests"
    return {
        "schema": "jll-image-repair-postgres-runner/v1",
        "exit_code": 0,
        "error": None,
        "container_removed_or_absent": True,
        "container_cleanup": "removed",
        "source_sha256": repair.file_hash(Path(repair.__file__), private=False),
        "test_sha256": repair.file_hash(
            tests / "test_cre_repair_jll_images.py", private=False
        ),
        "runner_sha256": repair.file_hash(
            tests / "run_jll_image_repair_postgres_contract.py", private=False
        ),
    }


def _execution_files(private_dir: Path, evidence: SimpleNamespace, proof: dict):
    terminal_path = private_dir / "postgres-terminal-proof.json"
    terminal_sha = _write_private(terminal_path, proof)
    verification = {
        "status": "passed",
        "isolated_postgres": True,
        "executor_sha256": repair.file_hash(Path(repair.__file__), private=False),
        "evidence_pins": evidence.pins,
        "postgres_terminal_proof_path": str(terminal_path),
        "postgres_terminal_proof_sha256": terminal_sha,
    }
    verification_path = private_dir / "verification-receipt.json"
    verification_sha = _write_private(verification_path, verification)
    return verification_path, verification_sha


def _approval(
    evidence: SimpleNamespace,
    verification_sha: str,
    *,
    operation: str,
    applied_receipt_sha: str | None = None,
) -> dict:
    approval = {
        "schema_version": 1,
        "issue": "AGENTIC-4371",
        "approved": True,
        "approved_by": "cayman",
        "approval_scope": f"production_{operation}",
        "approval_reference": f"https://linear.app/agenticassets/issue/AGENTIC-4371#comment-{COMMENT_ID}",
        "executor_sha256": repair.file_hash(Path(repair.__file__), private=False),
        "collector_sha": COLLECTOR_SHA,
        "evidence_pins": evidence.pins,
        "staged_manifest_sha256": STAGE_SHA256,
        "verification_receipt_sha256": verification_sha,
    }
    if applied_receipt_sha is not None:
        approval["applied_receipt_sha256"] = applied_receipt_sha
    return approval


def _verify(
    private_dir: Path,
    evidence: SimpleNamespace,
    *,
    operation: str,
    proof: dict | None = None,
    applied_receipt: Path | None = None,
    applied_receipt_sha: str | None = None,
) -> str:
    verification_path, verification_sha = _execution_files(
        private_dir, evidence, proof or _terminal_proof()
    )
    approval_path = private_dir / "approval.json"
    _write_private(
        approval_path,
        _approval(
            evidence,
            verification_sha,
            operation=operation,
            applied_receipt_sha=applied_receipt_sha,
        ),
    )
    call_args = {
        "operation": operation,
        "collector_sha": COLLECTOR_SHA,
    }
    if applied_receipt is not None:
        call_args["applied_receipt"] = applied_receipt
    return repair.verify_approval(
        approval_path, verification_path, evidence, **call_args
    )


@pytest.fixture(autouse=True)
def pinned_staged_manifest(monkeypatch):
    monkeypatch.setattr(repair, "verify_stage", lambda _evidence: STAGE_SHA256)


def test_delete_approval_binds_a_hash_checked_current_postgres_terminal_proof(tmp_path):
    private_dir = _private_dir(tmp_path)
    evidence = _evidence(private_dir)

    approval_hash = _verify(private_dir, evidence, operation="delete")

    assert len(approval_hash) == 64


@pytest.mark.parametrize(
    "proof_change",
    [
        {"schema": "unknown"},
        {"exit_code": 1},
        {"error": "PostgreSQL verification failed"},
        {"container_removed_or_absent": False},
        {"container_cleanup": "cleanup_uncertain"},
        {"source_sha256": "0" * 64},
        {"test_sha256": "0" * 64},
        {"runner_sha256": "0" * 64},
    ],
)
def test_delete_approval_rejects_failed_unsettled_or_drifted_terminal_proof(
    tmp_path, proof_change
):
    private_dir = _private_dir(tmp_path)
    evidence = _evidence(private_dir)
    proof = _terminal_proof() | proof_change

    with pytest.raises((ValueError, OSError)):
        _verify(private_dir, evidence, operation="delete", proof=proof)


def test_delete_approval_rejects_a_four_field_verification_receipt(tmp_path):
    private_dir = _private_dir(tmp_path)
    evidence = _evidence(private_dir)
    verification_path = private_dir / "verification-receipt.json"
    verification_sha = _write_private(
        verification_path,
        {
            "status": "passed",
            "isolated_postgres": True,
            "executor_sha256": repair.file_hash(Path(repair.__file__), private=False),
            "evidence_pins": evidence.pins,
        },
    )
    approval_path = private_dir / "approval.json"
    _write_private(
        approval_path,
        _approval(evidence, verification_sha, operation="delete"),
    )

    with pytest.raises((ValueError, OSError)):
        repair.verify_approval(
            approval_path,
            verification_path,
            evidence,
            operation="delete",
            collector_sha=COLLECTOR_SHA,
        )


def test_delete_approval_rejects_terminal_proof_bytes_changed_after_receipt(tmp_path):
    private_dir = _private_dir(tmp_path)
    evidence = _evidence(private_dir)
    verification_path, verification_sha = _execution_files(
        private_dir, evidence, _terminal_proof()
    )
    approval_path = private_dir / "approval.json"
    _write_private(
        approval_path,
        _approval(evidence, verification_sha, operation="delete"),
    )

    terminal_path = private_dir / "postgres-terminal-proof.json"
    terminal_path.write_text(terminal_path.read_text() + " ", encoding="utf-8")

    with pytest.raises((ValueError, OSError)):
        repair.verify_approval(
            approval_path,
            verification_path,
            evidence,
            operation="delete",
            collector_sha=COLLECTOR_SHA,
        )


def _prior_apply_receipt(evidence: SimpleNamespace) -> dict:
    return {
        "status": "applied",
        "operation": "delete",
        "evidence_pins": evidence.pins,
        "staged_manifest_sha256": STAGE_SHA256,
        "database_target_sha256": repair.EXPECTED_DB_TARGET_SHA256,
        "executor_sha256": "2" * 64,
        "collector_sha": "3" * 40,
        "approval_sha256": "4" * 64,
        "local_client_settled": True,
        "remote_sql_rollback_claim": False,
        "result": {
            "operation": "delete",
            "committed": True,
            "matched": evidence.candidate_count,
            "status": "passed",
        },
        "completed_at": "2026-10-16T12:34:56+00:00",
    }


def test_restore_approval_binds_the_exact_positive_prior_apply_receipt(tmp_path):
    private_dir = _private_dir(tmp_path)
    evidence = _evidence(private_dir)
    receipt_path = private_dir / "prior-apply-receipt.json"
    receipt_sha = _write_private(receipt_path, _prior_apply_receipt(evidence))

    approval_hash = _verify(
        private_dir,
        evidence,
        operation="restore",
        applied_receipt=receipt_path,
        applied_receipt_sha=receipt_sha,
    )

    assert len(approval_hash) == 64


@pytest.mark.parametrize(
    "receipt_change",
    [
        {"result": {"operation": "delete", "committed": False, "matched": 3, "status": "passed"}},
        {"result": {"operation": "restore", "committed": True, "matched": 3, "status": "passed"}},
        {"local_client_settled": False},
        {"completed_at": "not-a-timestamp"},
        {"executor_sha256": "invalid"},
    ],
)
def test_restore_approval_rejects_unproven_prior_receipt_result(tmp_path, receipt_change):
    private_dir = _private_dir(tmp_path)
    evidence = _evidence(private_dir)
    receipt_path = private_dir / "prior-apply-receipt.json"
    receipt_sha = _write_private(
        receipt_path, _prior_apply_receipt(evidence) | receipt_change
    )

    with pytest.raises((ValueError, OSError)):
        _verify(
            private_dir,
            evidence,
            operation="restore",
            applied_receipt=receipt_path,
            applied_receipt_sha=receipt_sha,
        )


def test_restore_approval_rejects_four_field_receipt_and_changed_receipt_hash(tmp_path):
    private_dir = _private_dir(tmp_path)
    evidence = _evidence(private_dir)
    receipt_path = private_dir / "prior-apply-receipt.json"
    fabricated = {
        "status": "applied",
        "evidence_pins": evidence.pins,
        "staged_manifest_sha256": STAGE_SHA256,
        "database_target_sha256": repair.EXPECTED_DB_TARGET_SHA256,
    }
    actual_sha = _write_private(receipt_path, fabricated)

    with pytest.raises((ValueError, OSError)):
        _verify(
            private_dir,
            evidence,
            operation="restore",
            applied_receipt=receipt_path,
            applied_receipt_sha=actual_sha,
        )

    receipt_path.unlink()
    receipt_sha = _write_private(receipt_path, _prior_apply_receipt(evidence))
    with pytest.raises((ValueError, OSError)):
        _verify(
            private_dir,
            evidence,
            operation="restore",
            applied_receipt=receipt_path,
            applied_receipt_sha="0" * 64 if receipt_sha != "0" * 64 else "1" * 64,
        )
