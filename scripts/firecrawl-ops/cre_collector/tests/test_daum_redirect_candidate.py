import hashlib
import json
import shutil
from types import SimpleNamespace

import pytest

from daum import redirect_candidate


def test_receipt_persists_only_after_identical_candidate_recheck(tmp_path, monkeypatch):
    envelope = {
        "producer_sha": "a" * 40,
        "candidate": {"oldUrl": "old", "currentUrl": "current"},
    }
    page = "<html>evidence</html>"
    receipt = {
        "schema": "daum-redirect-observation/v1",
        "write_authority": False,
        "candidate": envelope,
        "redirect": {"status": 301, "oldUrl": "old", "newUrl": "current"},
        "page": {
            "url": "current",
            "shortlink": "https://daumcommercial.com/?p=42734",
            "html": page,
            "sha256": hashlib.sha256(page.encode()).hexdigest(),
        },
    }
    calls = []
    monkeypatch.setattr(redirect_candidate, "probe_candidate", lambda *args: calls.append(args) or envelope)
    monkeypatch.setattr(redirect_candidate.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps(receipt).encode()
    ))
    import cre_checkpoint_refresh
    monkeypatch.setattr(cre_checkpoint_refresh, "git_identity", lambda: ("a" * 40, False))
    output = tmp_path / "daum-redirect-42734.json"
    node = shutil.which("node")
    assert node is not None
    assert redirect_candidate.collect_receipt(tmp_path, {}, "42734", None, tmp_path, node_executable=node) == receipt
    assert json.loads(output.read_text()) == receipt
    assert len(calls) == 2
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        redirect_candidate.collect_receipt(tmp_path, {}, "42734", None, tmp_path, node_executable=node)


def test_changed_candidate_refuses_receipt(tmp_path, monkeypatch):
    first = {"producer_sha": "a" * 40, "candidate": {"oldUrl": "old", "currentUrl": "current"}}
    items = iter([first, {**first, "producer_sha": "b" * 40}])
    monkeypatch.setattr(redirect_candidate, "probe_candidate", lambda *args: next(items))
    page = "evidence"
    receipt = {
        "schema": "daum-redirect-observation/v1", "write_authority": False,
        "candidate": first,
        "redirect": {"status": 301, "oldUrl": "old", "newUrl": "current"},
        "page": {"url": "current", "shortlink": "https://daumcommercial.com/?p=42734",
                 "html": page, "sha256": hashlib.sha256(page.encode()).hexdigest()},
    }
    monkeypatch.setattr(redirect_candidate.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps(receipt).encode()
    ))
    import cre_checkpoint_refresh
    monkeypatch.setattr(cre_checkpoint_refresh, "git_identity", lambda: ("a" * 40, False))
    output = tmp_path / "daum-redirect-42734.json"
    node = shutil.which("node")
    assert node is not None
    with pytest.raises(ValueError, match="binding changed"):
        redirect_candidate.collect_receipt(tmp_path, {}, "42734", None, tmp_path, node_executable=node)
    assert not output.exists()


def test_rejects_missing_and_relative_node_before_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(redirect_candidate, "probe_candidate", lambda *args: pytest.fail("probe ran"))
    with pytest.raises(ValueError, match="absolute"):
        redirect_candidate.collect_receipt(tmp_path, {}, "42734", None, tmp_path, node_executable="node")
    with pytest.raises(FileNotFoundError):
        redirect_candidate.collect_receipt(tmp_path, {}, "42734", None, tmp_path, node_executable="/missing/node")
    with pytest.raises(ValueError, match="unavailable"):
        redirect_candidate.collect_receipt(tmp_path, {}, "42734", None, tmp_path, node_executable="/bin/sh")


def test_evidence_directory_refuses_unsafe_or_changed_paths(tmp_path):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private"):
        redirect_candidate._open_evidence_directory(unsafe)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(private, target_is_directory=True)
    with pytest.raises(OSError):
        redirect_candidate._open_evidence_directory(alias)
    path, descriptors = redirect_candidate._open_evidence_directory(private)
    try:
        private.chmod(0o755)
        with pytest.raises(ValueError, match="private"):
            redirect_candidate._check_evidence_directory(path, descriptors)
        private.chmod(0o700)
        private.rename(tmp_path / "renamed")
        private.mkdir(mode=0o700)
        with pytest.raises(ValueError, match="changed"):
            redirect_candidate._check_evidence_directory(path, descriptors)
    finally:
        for descriptor in reversed(descriptors):
            import os
            os.close(descriptor)
