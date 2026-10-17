"""Exact execution authorization with real private files and staged CSVs."""

from __future__ import annotations

import os
from pathlib import Path

import cre_repair_jll_images as repair
import pytest
from test_cre_repair_jll_images import fixture_evidence

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX private evidence contract"
)
COLLECTOR = "a" * 40


@pytest.fixture
def contract(tmp_path):
    tmp_path.chmod(0o700)
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    evidence = fixture_evidence(stage)
    files = (evidence.parents_csv, evidence.images_csv, evidence.candidates_csv)
    for path in files:
        path.chmod(0o600)
    repair.write_json(
        stage / "manifest.json",
        {
            "pins": evidence.pins,
            "image_columns": evidence.image_columns,
            "files": {path.name: repair.file_hash(path) for path in files},
        },
    )
    executor = repair.file_hash(Path(repair.__file__), private=False)
    tests = Path(repair.__file__).parent / "tests"
    terminal_path = tmp_path / "postgres-terminal-proof.json"
    repair.write_json(
        terminal_path,
        {
            "schema": "jll-image-repair-postgres-runner/v1",
            "exit_code": 0,
            "error": None,
            "container_removed_or_absent": True,
            "container_cleanup": "removed",
            "source_sha256": executor,
            "test_sha256": repair.file_hash(
                tests / "test_cre_repair_jll_images.py", private=False
            ),
            "runner_sha256": repair.file_hash(
                tests / "run_jll_image_repair_postgres_contract.py", private=False
            ),
        },
    )
    verification = {
        "status": "passed",
        "isolated_postgres": True,
        "executor_sha256": executor,
        "evidence_pins": evidence.pins,
        "postgres_terminal_proof_path": str(terminal_path),
        "postgres_terminal_proof_sha256": repair.file_hash(terminal_path),
    }
    verification_path = tmp_path / "verification.json"
    repair.write_json(verification_path, verification)
    approval = {
        "schema_version": 1,
        "issue": "AGENTIC-4371",
        "approved": True,
        "approved_by": "cayman",
        "approval_scope": "production_delete",
        "approval_reference": "https://linear.app/agenticassets/issue/AGENTIC-4371#comment-12345678-1234-1234-1234-123456789abc",
        "executor_sha256": executor,
        "collector_sha": COLLECTOR,
        "evidence_pins": evidence.pins,
        "staged_manifest_sha256": repair.verify_stage(evidence),
        "verification_receipt_sha256": repair.file_hash(verification_path),
    }
    return tmp_path, evidence, approval, verification, verification_path


def verify(contract, *, approval_change=None, verification_change=None):
    private_dir, evidence, approval, verification, verification_path = contract
    if verification_change:
        verification.update(verification_change)
        verification_path.unlink()
        repair.write_json(verification_path, verification)
        # Rebind the outer hash so the verification's own content is examined.
        approval["verification_receipt_sha256"] = repair.file_hash(verification_path)
    approval.update(approval_change or {})
    approval_path = private_dir / "approval.json"
    repair.write_json(approval_path, approval)
    return repair.verify_approval(
        approval_path,
        verification_path,
        evidence,
        operation="delete",
        collector_sha=COLLECTOR,
    ), approval_path


def test_exact_private_execution_contract_passes(contract):
    accepted_hash, approval_path = verify(contract)
    assert accepted_hash == repair.file_hash(approval_path)


@pytest.mark.parametrize(
    "changed",
    [
        {"executor_sha256": "b" * 64},
        {"collector_sha": "b" * 40},
        {"evidence_pins": {}},
        {"staged_manifest_sha256": "b" * 64},
        {"verification_receipt_sha256": "b" * 64},
        {"approval_scope": "production_restore"},
        {"approval_reference": "https://example.com/approval"},
        {"approved": False},
    ],
)
def test_changed_approval_binding_refuses(contract, changed):
    with pytest.raises(ValueError):
        verify(contract, approval_change=changed)


@pytest.mark.parametrize(
    "changed",
    [
        {"status": "failed"},
        {"isolated_postgres": False},
        {"executor_sha256": "b" * 64},
        {"evidence_pins": {}},
    ],
)
def test_rehashed_but_unqualified_verification_refuses(contract, changed):
    with pytest.raises(ValueError):
        verify(contract, verification_change=changed)


def test_changed_staged_csv_refuses_even_with_original_manifest(contract):
    _, evidence, *_ = contract
    with evidence.images_csv.open("a") as stream:
        stream.write("unexpected row\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify(contract)


def test_apply_without_approval_refuses_before_dispatch(tmp_path, monkeypatch):
    def unexpected_dispatch(*_args, **_kwargs):
        pytest.fail("unauthorized invocation reached database dispatch")

    monkeypatch.setattr(repair, "run_psql", unexpected_dispatch)
    with pytest.raises(ValueError, match="authorization is OFF"):
        repair.main(
            [
                "--apply",
                "--proposal",
                str(tmp_path / "absent-proposal"),
                "--candidates",
                str(tmp_path / "absent-candidates"),
                "--preimage",
                str(tmp_path / "absent-preimage"),
                "--work-dir",
                str(tmp_path / "new-stage"),
                "--env-file",
                str(tmp_path / "absent-env"),
                "--expected-sha",
                COLLECTOR,
            ]
        )
    assert not (tmp_path / "new-stage").exists()
