import hashlib
import json
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
    output = tmp_path / "receipt.json"
    assert redirect_candidate.collect_receipt(tmp_path, {}, "42734", None, output) == receipt
    assert json.loads(output.read_text()) == receipt
    assert len(calls) == 2
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        redirect_candidate.collect_receipt(tmp_path, {}, "42734", None, output)


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
    output = tmp_path / "receipt.json"
    with pytest.raises(ValueError, match="binding changed"):
        redirect_candidate.collect_receipt(tmp_path, {}, "42734", None, output)
    assert not output.exists()
