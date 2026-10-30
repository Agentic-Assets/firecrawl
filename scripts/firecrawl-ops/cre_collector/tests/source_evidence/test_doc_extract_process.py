import json
import subprocess
from types import SimpleNamespace

import pytest
from source_evidence import doc_extract_process, events, private_export
from test_doc_fetch_process import job, receipt


@pytest.fixture(autouse=True)
def owned_settlement(monkeypatch):
    calls = []
    monkeypatch.setattr(doc_extract_process, "OwnedProcessGroup", SimpleNamespace(from_spawn=lambda child: child))
    def settle(groups, **kwargs):
        calls.append((groups, kwargs))
        return SimpleNamespace(settled=True)
    monkeypatch.setattr(doc_extract_process, "settle_owned_groups", settle)
    return calls


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
def test_supervised_validated_artifact(monkeypatch, tmp_path, status, code, state, owned_settlement):
    fetched, job_path = setup_fetch(tmp_path)
    commands = []
    def popen(command, **kwargs):
        commands.append((command, kwargs))
        path = tmp_path / "extracted" / "artifact.json"
        path.write_text(json.dumps(artifact(status)))
        path.chmod(0o600)
        return type("Child", (), {"pid": 1234, "wait": lambda self, timeout: code})()
    monkeypatch.setattr(subprocess, "Popen", popen)
    result = doc_extract_process.run(fetched, job_path, tmp_path / "extracted", node_executable="/usr/bin/node",
                                     source_key="svn")
    assert result["state"] == state and result["artifact"] == artifact(status)
    assert commands[0][1]["start_new_session"] is True
    assert commands[0][1]["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    assert owned_settlement and commands[0][0][-3].endswith("document.bin")


def test_changed_body_refuses_before_spawn(monkeypatch, tmp_path):
    fetched, job_path = setup_fetch(tmp_path)
    (tmp_path / "fetched" / "document.bin").write_bytes(b"other")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    with pytest.raises(ValueError):
        doc_extract_process.run(fetched, job_path, tmp_path / "extracted", node_executable="/usr/bin/node", source_key="svn")


def test_timeout_kills_group_and_does_not_claim_artifact(monkeypatch, tmp_path, owned_settlement):
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
    result = doc_extract_process.run(fetched, job_path, tmp_path / "extracted", node_executable="/usr/bin/node", source_key="svn")
    assert result["state"] == "timeout" and owned_settlement
    assert "artifact" not in result


def test_interrupted_wait_with_unknown_group_propagates_cleanup_unknown(monkeypatch, tmp_path):
    fetched, job_path = setup_fetch(tmp_path)
    class Child:
        pid = 1234
        def wait(self, timeout):
            raise KeyboardInterrupt()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Child())
    monkeypatch.setattr(doc_extract_process, "settle_owned_groups", lambda *args, **kwargs: SimpleNamespace(settled=False))
    with pytest.raises(doc_extract_process.DocumentProcessCleanupUnknown) as error:
        doc_extract_process.run(fetched, job_path, tmp_path / "extracted", node_executable="/usr/bin/node", source_key="svn")
    assert isinstance(error.value.__cause__, KeyboardInterrupt)
    assert not (tmp_path / "extracted" / "conversion-complete.json").exists()


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
    result = doc_extract_process.run(fetched, job_path, tmp_path / "extracted", node_executable="/usr/bin/node", source_key="svn")
    assert result["state"] == "ambiguous" and "artifact" not in result


def settled_conversion(monkeypatch, tmp_path):
    fetched, job_path = setup_fetch(tmp_path)
    def popen(*args, **kwargs):
        path = tmp_path / "extracted" / "artifact.json"
        path.write_text(json.dumps(artifact()))
        path.chmod(0o600)
        return type("Child", (), {"pid": 1234, "wait": lambda self, timeout: 0})()
    monkeypatch.setattr(subprocess, "Popen", popen)
    result = doc_extract_process.run(fetched, job_path, tmp_path / "extracted", node_executable="/usr/bin/node", source_key="svn")
    assert result["state"] == "success"
    return fetched, job_path, result


def test_replay_revalidates_without_spawning(monkeypatch, tmp_path):
    fetched, job_path, result = settled_conversion(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("replay must not spawn"))
    replayed = doc_extract_process.replay(fetched, job_path, tmp_path / "extracted", source_key="svn")
    assert replayed == result


@pytest.mark.parametrize("mutation", ["missing", "artifact", "job", "source", "input", "exit"])
def test_replay_refuses_missing_or_tampered_custody(monkeypatch, tmp_path, mutation):
    fetched, job_path, _ = settled_conversion(monkeypatch, tmp_path)
    directory = tmp_path / "extracted"
    marker_path = directory / "conversion-complete.json"
    if mutation == "missing":
        marker_path.unlink()
    elif mutation == "artifact":
        (directory / "artifact.json").write_text("{}")
    elif mutation == "input":
        (directory / "document.bin").write_bytes(b"bad")
    else:
        marker = json.loads(marker_path.read_text())
        marker[{"job": "job_sha256", "source": "source_key", "exit": "exit_code"}[mutation]] = {
            "job": "0" * 64, "source": "other", "exit": True}[mutation]
        marker_path.write_text(json.dumps(marker))
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("replay must not spawn"))
    with pytest.raises((ValueError, OSError)):
        doc_extract_process.replay(fetched, job_path, directory, source_key="svn")


def test_hint_is_bound_through_private_conversion_and_no_spawn_replay(monkeypatch, tmp_path):
    fetched, job_path = setup_fetch(tmp_path)
    hint = {"schema": "cre-document-format-hint/v1", "status": "resolved", "format": "csv",
            "candidate_sha256": "a" * 64, "selection_sha256": "b" * 64,
            "assertions": [], "assertion_count": 0, "assertions_sha256": "c" * 64,
            "omitted_assertions": 0, "authority": "source_asserted_unverified"}
    def popen(*args, **kwargs):
        provenance = json.loads((tmp_path / "extracted" / "provenance.json").read_text())
        assert provenance["format_hint"] == hint
        value = artifact()
        value["source"]["format_hint"] = hint
        path = tmp_path / "extracted" / "artifact.json"
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return type("Child", (), {"pid": 1234, "wait": lambda self, timeout: 0})()
    monkeypatch.setattr(subprocess, "Popen", popen)
    result = doc_extract_process.run(fetched, job_path, tmp_path / "extracted", node_executable="/usr/bin/node",
                                     source_key="svn", format_hint=hint)
    assert result["state"] == "success"
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("replay must not spawn"))
    assert doc_extract_process.replay(fetched, job_path, tmp_path / "extracted", source_key="svn", format_hint=hint) == result
    with pytest.raises(ValueError):
        doc_extract_process.replay(fetched, job_path, tmp_path / "extracted", source_key="svn",
                                   format_hint={**hint, "candidate_sha256": "d" * 64})
    with pytest.raises(ValueError):
        doc_extract_process.replay(fetched, job_path, tmp_path / "extracted", source_key="svn")


def test_real_anydoc_reads_bound_extensionless_csv_and_replays(monkeypatch, tmp_path):
    import shutil
    node = shutil.which("node")
    from pathlib import Path
    anydoc = Path(__file__).resolve().parents[2] / "document_text" / "node_modules" / "@firecrawl" / "anydoc"
    if not node or not anydoc.is_dir():
        pytest.skip("Node and document_text npm ci --ignore-scripts are required")
    fetched, job_path = setup_fetch(tmp_path)
    body = b'item,price,flag\n"Test, A",0,false\n'
    fetch_dir = tmp_path / "fetched"
    bound = dict(fetched["receipt"], body_bytes=len(body), body_sha256=events.sha256(body))
    fetched["receipt"] = bound
    (fetch_dir / "document.bin").write_bytes(body)
    (fetch_dir / "receipt.json").write_bytes(events.encoded({"job_sha256": fetched["job_sha256"], "receipt": bound}))
    hint = {"schema": "cre-document-format-hint/v1", "status": "resolved", "format": "csv",
            "candidate_sha256": "a" * 64, "selection_sha256": "b" * 64,
            "assertions": [{"occurrence": 0, "field": "type", "format": "csv"}], "assertion_count": 1,
            "assertions_sha256": "c" * 64, "omitted_assertions": 0, "authority": "source_asserted_unverified"}
    result = doc_extract_process.run(fetched, job_path, tmp_path / "extracted", node_executable=node,
                                     source_key="svn", format_hint=hint)
    assert result["state"] == "success"
    assert result["artifact"]["source"]["format_detection"] == "source_hint"
    assert "Test, A" in result["artifact"]["extraction"]["text"]
    assert result["artifact"]["extraction"]["coverage"] == "unknown"
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("replay must not spawn"))
    assert doc_extract_process.replay(fetched, job_path, tmp_path / "extracted", source_key="svn", format_hint=hint) == result


def test_hint_comparison_is_typed_and_absence_is_not_null():
    base = artifact()
    hint = {"assertion_count": 1, "omitted_assertions": 0,
            "assertions": [{"occurrence": 0}]}
    base["source"]["format_hint"] = hint
    assert doc_extract_process.validate_artifact(base, receipt(), source_key="svn", exit_code=0, format_hint=hint) == "success"
    for mutated in ({**hint, "assertion_count": True}, {**hint, "omitted_assertions": False},
                    {**hint, "assertions": [{"occurrence": False}]}):
        with pytest.raises(ValueError):
            doc_extract_process.validate_artifact(base, receipt(), source_key="svn", exit_code=0, format_hint=mutated)
    with pytest.raises(ValueError):
        doc_extract_process.validate_artifact(base, receipt(), source_key="svn", exit_code=0)
    base["source"]["format_hint"] = None
    with pytest.raises(ValueError):
        doc_extract_process.validate_artifact(base, receipt(), source_key="svn", exit_code=0)
