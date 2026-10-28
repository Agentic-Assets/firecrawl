import json
import subprocess

import pytest
from source_evidence import doc_extract_process, events, private_export
from test_doc_fetch_process import job, receipt


def setup_fetch(tmp_path):
    tmp_path.chmod(0o700)
    raw = events.encoded(job())
    job_path = tmp_path / "job.json"
    job_path.write_bytes(raw)
    job_path.chmod(0o600)
    directory = tmp_path / "fetched"
    digest = events.sha256(raw)
    private_export.write(directory, [("receipt.json", events.encoded({"job_sha256": digest, "receipt": receipt()})),
                                     ("document.bin", b"pdf")])
    return {"state": "completed", "job_sha256": digest, "directory": str(directory),
            "receipt": receipt(), "body_path": str(directory / "document.bin")}, job_path


def artifact(status="extracted"):
    source = {"url": receipt()["final_url"], "observed_at": receipt()["finished_at"],
              "sha256": receipt()["body_sha256"], "byte_length": 3, "format": "pdf", "format_detection": "content"}
    text = "Extracted document" if status == "extracted" else None
    return {"schema_version": "cre-document-text/v1", "source": source,
            "parser": {"name": "@firecrawl/anydoc", "version": "0.2.4", "config": {"ocr": "disabled"}},
            "extraction": {"status": status, "coverage": "unknown", "page_count": None,
                           "ocr_needed_pages": [], "text_format": "markdown", "text": text,
                           "text_sha256": events.sha256(text.encode()) if text else None, "error_code": None}}


@pytest.mark.parametrize("status,code,state", [("extracted", 0, "success"), ("ocr_needed", 2, "ocr_needed"),
                                               ("failed", 2, "failed")])
def test_supervised_validated_artifact(monkeypatch, tmp_path, status, code, state):
    fetched, job_path = setup_fetch(tmp_path)
    commands = []
    def popen(command, **kwargs):
        commands.append((command, kwargs))
        path = tmp_path / "extracted" / "artifact.json"
        path.write_text(json.dumps(artifact(status)))
        path.chmod(0o600)
        return type("Child", (), {"pid": 1234, "wait": lambda self, timeout: code})()
    monkeypatch.setattr(subprocess, "Popen", popen)
    killed = []
    monkeypatch.setattr(doc_extract_process.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    result = doc_extract_process.run(fetched, job_path, tmp_path / "extracted", node_executable="/usr/bin/node",
                                     source_key="svn")
    assert result["state"] == state and result["artifact"] == artifact(status)
    assert commands[0][1]["start_new_session"] is True
    assert commands[0][1]["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    assert killed and commands[0][0][-3].endswith("document.bin")


def test_changed_body_refuses_before_spawn(monkeypatch, tmp_path):
    fetched, job_path = setup_fetch(tmp_path)
    (tmp_path / "fetched" / "document.bin").write_bytes(b"other")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    with pytest.raises(ValueError):
        doc_extract_process.run(fetched, job_path, tmp_path / "extracted", node_executable="/usr/bin/node", source_key="svn")


def test_timeout_kills_group_and_does_not_claim_artifact(monkeypatch, tmp_path):
    fetched, job_path = setup_fetch(tmp_path)
    class Child:
        pid = 1234
        calls = 0
        def wait(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("node", timeout)
            return -9
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Child())
    killed = []
    monkeypatch.setattr(doc_extract_process.os, "killpg", lambda pid, sig: killed.append(pid))
    result = doc_extract_process.run(fetched, job_path, tmp_path / "extracted", node_executable="/usr/bin/node", source_key="svn")
    assert result["state"] == "timeout" and killed == [1234]
    assert "artifact" not in result


def test_mismatched_artifact_is_ambiguous(monkeypatch, tmp_path):
    fetched, job_path = setup_fetch(tmp_path)
    def popen(*args, **kwargs):
        value = artifact()
        value["source"]["sha256"] = "0" * 64
        path = tmp_path / "extracted" / "artifact.json"
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return type("Child", (), {"pid": 1234, "wait": lambda self, timeout: 0})()
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(doc_extract_process.os, "killpg", lambda *args: None)
    result = doc_extract_process.run(fetched, job_path, tmp_path / "extracted", node_executable="/usr/bin/node", source_key="svn")
    assert result["state"] == "ambiguous"


@pytest.mark.parametrize("body,incorrect_length", [(b"x", True), (b"", False)])
def test_boolean_artifact_length_does_not_equal_numeric_receipt(monkeypatch, tmp_path, body, incorrect_length):
    fetched, job_path = setup_fetch(tmp_path)
    fetch_dir = tmp_path / "fetched"
    bound_receipt = dict(fetched["receipt"], body_bytes=len(body), body_sha256=events.sha256(body))
    fetched["receipt"] = bound_receipt
    (fetch_dir / "document.bin").write_bytes(body)
    (fetch_dir / "receipt.json").write_bytes(events.encoded({"job_sha256": fetched["job_sha256"],
                                                              "receipt": bound_receipt}))
    def popen(*args, **kwargs):
        value = artifact()
        value["source"]["sha256"] = bound_receipt["body_sha256"]
        value["source"]["byte_length"] = incorrect_length
        path = tmp_path / "extracted" / "artifact.json"
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return type("Child", (), {"pid": 1234, "wait": lambda self, timeout: 0})()
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(doc_extract_process.os, "killpg", lambda *args: None)
    result = doc_extract_process.run(fetched, job_path, tmp_path / "extracted", node_executable="/usr/bin/node", source_key="svn")
    assert result["state"] == "ambiguous" and "artifact" not in result
