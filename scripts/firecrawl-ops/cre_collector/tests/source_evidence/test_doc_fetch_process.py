import subprocess
from types import SimpleNamespace

import pytest
from source_evidence import doc_fetch_process, document_fetch_worker, events


@pytest.fixture(autouse=True)
def owned_settlement(monkeypatch):
    calls = []
    monkeypatch.setattr(doc_fetch_process, "OwnedProcessGroup", SimpleNamespace(from_spawn=lambda child: child))
    def settle(groups, **kwargs):
        calls.append((groups, kwargs))
        return SimpleNamespace(settled=True)
    monkeypatch.setattr(doc_fetch_process, "settle_owned_groups", settle)
    return calls


def job():
    return {"schema_version": "cre-document-fetch-job/v1", "url": "https://docs.example.test/a",
            "allowed_hosts": ["docs.example.test"], "max_bytes": 100,
            "timeout_seconds": 10, "max_redirects": 1}


def receipt():
    return {"started_at": "2026-10-28T12:00:00.000000Z", "finished_at": "2026-10-28T12:00:01.000000Z",
            "request_url": "https://docs.example.test/a", "final_url": "https://docs.example.test/a",
            "http_status": 200, "hops": [{"url": "https://docs.example.test/a", "status": 200}],
            "outcome": "acquired", "failure_code": None, "body_sha256": events.sha256(b"pdf"),
            "body_bytes": 3, "body_representation": "http_entity_bytes_after_transfer_framing_identity_content_encoding",
            "completion": "complete", "deadline_enforcement": "external_worker_required"}


def test_worker_private_job_and_complete_readback(monkeypatch, tmp_path):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(document_fetch_worker.document_fetch, "fetch",
                        lambda *args, **kwargs: {"receipt": receipt(), "body": b"pdf"})
    calls = []
    def child(command, **kwargs):
        calls.append((command, kwargs))
        document_fetch_worker.main(command[3:])
        return SimpleNamespace(pid=1234, wait=lambda timeout: 0)
    monkeypatch.setattr(subprocess, "Popen", child)
    result = doc_fetch_process.run(job(), tmp_path / "job.json", tmp_path / "output")
    assert result["state"] == "completed"
    assert result["receipt"] == receipt()
    assert result["body_path"] == str(tmp_path / "output" / "document.bin")
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][1]["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    assert calls[0][1]["stdout"] == subprocess.DEVNULL
    assert (tmp_path / "job.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("exception,state", [(subprocess.TimeoutExpired(["worker"], 15), "fetch_outcome_unknown"),
                                              (OSError("private detail"), "worker_failed_ambiguous")])
def test_timeout_and_launch_failure_do_not_claim_no_request(monkeypatch, tmp_path, exception, state):
    tmp_path.chmod(0o700)
    def child(*args, **kwargs):
        if isinstance(exception, OSError):
            raise exception
        def wait(timeout):
            assert timeout == 15
            raise exception
        return SimpleNamespace(pid=1234, wait=wait)
    monkeypatch.setattr(subprocess, "Popen", child)
    result = doc_fetch_process.run(job(), tmp_path / "job.json", tmp_path / "output")
    assert result["state"] == state and "receipt" not in result


def test_interrupted_fetch_with_unknown_cleanup_propagates(monkeypatch, tmp_path):
    tmp_path.chmod(0o700)
    def wait(timeout):
        raise KeyboardInterrupt()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: SimpleNamespace(pid=1234, wait=wait))
    monkeypatch.setattr(doc_fetch_process, "settle_owned_groups", lambda *a, **k: SimpleNamespace(settled=False))
    with pytest.raises(doc_fetch_process.DocumentProcessCleanupUnknown) as error:
        doc_fetch_process.run(job(), tmp_path / "job.json", tmp_path / "output")
    assert isinstance(error.value.__cause__, KeyboardInterrupt)


def test_existing_job_or_output_never_overwritten(monkeypatch, tmp_path):
    tmp_path.chmod(0o700)
    (tmp_path / "job.json").write_text("existing")
    with pytest.raises(FileExistsError):
        doc_fetch_process.run(job(), tmp_path / "job.json", tmp_path / "output")
    assert (tmp_path / "job.json").read_text() == "existing"


def test_worker_refuses_wrong_hash_without_fetch(monkeypatch, tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "job.json"
    path.write_bytes(events.encoded(job()))
    path.chmod(0o600)
    def forbidden(*args, **kwargs):
        raise AssertionError("must not call fetch")
    monkeypatch.setattr(document_fetch_worker.document_fetch, "fetch", forbidden)
    with pytest.raises(ValueError):
        document_fetch_worker.main(["--job", str(path), "--job-sha256", "0" * 64,
                                    "--out-dir", str(tmp_path / "output")])


def test_worker_preserves_explicit_http_target_permitted_by_parent_policy():
    value = {**job(), 'url': 'http://docs.example.test/a'}
    assert document_fetch_worker.validate_job(value) is value
