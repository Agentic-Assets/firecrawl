"""Runner terminal proof files must satisfy the consumer's private-file contract."""

from __future__ import annotations

import json
import os
import stat

import cre_repair_jll_images as repair
import run_jll_image_repair_postgres_contract as runner


def test_terminal_proof_is_private_and_readable_by_consumer(tmp_path):
    proof_dir = tmp_path / "proof"
    proof_dir.mkdir(mode=0o700)
    proof_dir.chmod(0o700)
    record = {"schema": "jll-image-repair-postgres-runner/v1", "exit_code": 0}

    previous_umask = os.umask(0o022)
    try:
        runner.write_terminal_proof(proof_dir, record)
    finally:
        os.umask(previous_umask)

    proof_path = proof_dir / "terminal-proof.json"
    with repair.private_input(proof_path, runner.sha256_file(proof_path)) as stream:
        assert json.load(stream) == record
    assert stat.S_IMODE(proof_path.stat().st_mode) == 0o600
