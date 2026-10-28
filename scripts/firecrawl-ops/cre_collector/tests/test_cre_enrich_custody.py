"""Draft private custody regressions; no live queue or database."""

import hashlib
import os
from pathlib import Path

import pytest

import cre_enrich
from source_evidence import enrichment_custody


def test_owned_marker_and_settlement_retain_only_metadata(tmp_path):
    tmp_path.chmod(0o700)
    marker = enrichment_custody.mark(tmp_path, "svn")
    assert marker["state"] == "awaiting_worker"
    assert marker["database_retention"] == "not_performed"
    claim = tmp_path / "claims.json"
    claim.write_bytes(b"private claims")
    claim.chmod(0o600)
    artifact = tmp_path / "enriched.json"
    artifact.write_bytes(b"private evidence")
    artifact.chmod(0o600)
    settled = enrichment_custody.settle(tmp_path, marker)
    assert settled["state"] == "settled"
    assert settled["artifact"] == {"state": "retained", "bytes": 16,
                                   "sha256": hashlib.sha256(b"private evidence").hexdigest()}
    assert "private evidence" not in str(settled)


def test_absent_and_unsafe_destinations_refuse_or_report(tmp_path):
    tmp_path.chmod(0o700)
    marker = enrichment_custody.mark(tmp_path, "svn")
    assert enrichment_custody.settle(tmp_path, marker)["artifact"] == {"state": "absent"}
    (tmp_path / "enriched.json").symlink_to(Path("missing"))
    with pytest.raises((OSError, ValueError)):
        enrichment_custody.mark(tmp_path, "svn")
    with pytest.raises((OSError, ValueError)):
        enrichment_custody.settle(tmp_path, marker)


def test_private_file_must_be_single_link_and_owner_only(tmp_path):
    tmp_path.chmod(0o700)
    marker = enrichment_custody.mark(tmp_path, "svn")
    artifact = tmp_path / "enriched.json"
    artifact.write_bytes(b"x")
    artifact.chmod(0o600)
    os.link(artifact, tmp_path / "other")
    with pytest.raises(ValueError):
        enrichment_custody.settle(tmp_path, marker)


def test_worker_refuses_existing_output_before_credentials_or_claim(monkeypatch, tmp_path):
    from argparse import Namespace

    tmp_path.chmod(0o700)
    (tmp_path / "claims.json").write_text("already present")
    args = Namespace(batch=1, source="svn", dry_run=False, env_file=None,
                     artifact_out=str(tmp_path / "enriched.json"),
                     matthews_targeted_refresh=False, expected_db_target_sha256=None)
    monkeypatch.setattr(cre_enrich, "load_db_url", lambda *_: pytest.fail("must refuse before credentials"))
    monkeypatch.setattr(cre_enrich, "_psql_query", lambda *_: pytest.fail("must refuse before claim"))
    with pytest.raises(ValueError):
        cre_enrich.run(args)


def test_settlement_does_not_report_retained_when_file_fsync_fails(monkeypatch, tmp_path):
    tmp_path.chmod(0o700)
    marker = enrichment_custody.mark(tmp_path, "svn")
    artifact = tmp_path / "enriched.json"
    artifact.write_bytes(b"private")
    artifact.chmod(0o600)
    def fail(_fd):
        raise OSError("synthetic fsync failure")
    monkeypatch.setattr(enrichment_custody.os, "fsync", fail)
    with pytest.raises(OSError):
        enrichment_custody.settle(tmp_path, marker)


def test_leaf_appearing_after_absence_observation_refuses_custody(monkeypatch, tmp_path):
    tmp_path.chmod(0o700)
    marker = enrichment_custody.mark(tmp_path, "svn")
    original_stat = os.stat
    appeared = False

    def changing_stat(path, *args, **kwargs):
        nonlocal appeared
        if path == "enriched.json" and kwargs.get("dir_fd") is not None and not appeared:
            appeared = True
            (tmp_path / "enriched.json").write_bytes(b"newly appeared")
            (tmp_path / "enriched.json").chmod(0o600)
            raise FileNotFoundError()
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(enrichment_custody.os, "stat", changing_stat)
    with pytest.raises(ValueError, match="changed"):
        enrichment_custody.settle(tmp_path, marker)
