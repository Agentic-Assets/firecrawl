"""Coverage-held candidate retains original and cannot use generic ingestion."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import cre_checkpoint_refresh as refresh
from nai_partial.admission import candidate
from nai_partial.coverage_candidate import ORIGINAL, SCHEMA, prepare_owned
from tests.test_nai_post_admission import fixture


def test_owned_marked_candidate_binds_original_and_refuses_unclaimed_cli(tmp_path, monkeypatch):
    path, manifest, sha = fixture(tmp_path)
    lock_path = tmp_path / ".cre.lock"
    original = path.read_bytes()
    monkeypatch.setattr(refresh, "canonical_shared_lock_dir", lambda: lock_path)
    monkeypatch.setattr(refresh, "save_manifest", lambda *_: None)
    with refresh.SharedLock(lock_path, recovery_required=True) as owner:
        try:
            prepare_owned(path.parent.parent, manifest, owner)
        finally:
            owner.clear_recovery_requirement()
    info = manifest["sources"]["nai-global"]["artifact"]
    assert (path.parent.parent / ORIGINAL).read_bytes() == original
    assert info["nai_coverage_original_sha256"] == hashlib.sha256(original).hexdigest()
    assert json.loads(path.read_bytes())["schema"] == SCHEMA
    binding, staged = candidate(path, manifest, code_sha=sha, dirty=False)
    assert binding["staged_count"] == 1 and len(staged["staged"]) == 1
    cli = Path(__file__).resolve().parents[1] / "cre_ingest.py"
    proc = subprocess.run([sys.executable, str(cli), "--in", str(path), "--dry-run"],
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode != 0
    assert "refusing NAI partial artifact without guarded checkpoint admission" in proc.stderr
    (path.parent.parent / ORIGINAL).write_bytes(original + b" ")
    with pytest.raises(ValueError, match="original binding differs"):
        candidate(path, manifest, code_sha=sha, dirty=False)


def test_failed_publication_preserves_original_source(tmp_path, monkeypatch):
    path, manifest, _ = fixture(tmp_path)
    lock_path = tmp_path / ".cre.lock"
    original = path.read_bytes()
    monkeypatch.setattr(refresh, "canonical_shared_lock_dir", lambda: lock_path)
    monkeypatch.setattr(refresh, "save_manifest", lambda *_: None)
    import nai_partial.coverage_candidate as coverage
    with refresh.SharedLock(lock_path, recovery_required=True) as owner:
        try:
            with monkeypatch.context() as scoped:
                scoped.setattr(coverage.os, "replace", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic stop")))
                with pytest.raises(OSError, match="synthetic stop"):
                    prepare_owned(path.parent.parent, manifest, owner)
            assert path.read_bytes() == original
            assert (path.parent.parent / ORIGINAL).read_bytes() == original
            assert not manifest["sources"]["nai-global"]["artifact"].get("nai_coverage_partial")
        finally:
            owner.clear_recovery_requirement()
