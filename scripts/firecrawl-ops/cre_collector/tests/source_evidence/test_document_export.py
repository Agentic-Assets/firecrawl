import copy
import json
import os

import pytest
from source_evidence import document_export, events, private_export


def capture(text="A retained document\nSecond line"):
    artifact = {"schema_version": "cre-document-text/v1",
                "source": {"url": "https://example.test/vanished.pdf", "observed_at": "2026-10-28T12:00:00Z"},
                "parser": {"name": "anydoc", "version": "1"},
                "extraction": {"status": "extracted", "coverage": "unknown", "text": text,
                               "text_sha256": events.sha256(text.encode())}}
    return events.plan(artifact, source_key="svn")["events"]


def test_export_exact_text_and_private_metadata(tmp_path):
    text = "π" * 30001 + "\nLast line without newline"
    rows = capture(text)
    for row in rows:
        row["observed_at"] = "2026-10-28T08:00:00-04:00"
    destination = tmp_path / "document"
    metadata = document_export.export(rows, destination)
    assert (destination / "document.md").read_bytes() == text.encode()
    assert os.stat(destination).st_mode & 0o777 == 0o700
    assert all(os.stat(destination / name).st_mode & 0o777 == 0o600
               for name in ("document.md", "metadata.json"))
    written = json.loads((destination / "metadata.json").read_bytes())
    assert written == metadata and written["coverage"] == "unknown"
    assert written["publisher_identity"] == "unverified"
    assert "text" not in written["document"]["extraction"]
    assert written["chunk_count"] == len(rows) - 1
    with pytest.raises(FileExistsError):
        document_export.export(rows, destination)


@pytest.mark.parametrize("mutation", ["missing", "reorder", "duplicate", "tamper", "digest", "time", "extra"])
def test_incomplete_or_inconsistent_rows_refuse(mutation):
    rows = capture("a" * 60005)
    if mutation == "missing":
        rows.pop()
    elif mutation == "reorder":
        rows[1], rows[2] = rows[2], rows[1]
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(rows[-1]))
    elif mutation == "tamper":
        rows[1]["extracted_text"] += "x"
    elif mutation == "digest":
        rows[0]["facts"][0]["value"]["extraction"]["text_sha256"] = "0" * 64
    elif mutation == "time":
        rows[1]["observed_at"] = "2026-10-28T12:00:01Z"
    else:
        rows[1]["received_at"] = "unexpected"
    with pytest.raises(ValueError):
        document_export.reconstruct(rows)


def test_limits_and_non_document_refuse():
    with pytest.raises(ValueError):
        document_export.reconstruct([])
    with pytest.raises(ValueError):
        document_export.reconstruct([{}] * 72)
    rows = capture()
    rows[0]["facts"][0]["value"]["extraction"]["status"] = "failed"
    with pytest.raises(ValueError):
        document_export.reconstruct(rows)


def test_failure_cleans_owned_directory_without_touching_existing(tmp_path, monkeypatch):
    rows = capture()
    out = tmp_path / "new"
    monkeypatch.setattr(private_export.os, "fsync", lambda *_: (_ for _ in ()).throw(OSError("fixture")))
    with pytest.raises(OSError):
        document_export.export(rows, out)
    assert not out.exists()
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep").write_text("unchanged")
    with pytest.raises(FileExistsError):
        document_export.export(rows, existing)
    assert (existing / "keep").read_text() == "unchanged"


def test_symlink_destination_refused(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(FileExistsError):
        document_export.export(capture(), alias)
    assert not list(target.iterdir())


def test_missing_all_text_chunks_refuses_without_creating_output(tmp_path):
    rows = capture("retained nonempty document")[:1]
    out = tmp_path / "incomplete-document"
    with pytest.raises(ValueError):
        document_export.export(rows, out)
    assert not out.exists()


def test_directory_replacement_is_never_removed_during_failure(tmp_path, monkeypatch):
    out = tmp_path / "export"
    displaced = tmp_path / "displaced"

    def replace_during_publish(_fd):
        os.rename(out, displaced)
        out.mkdir()
        raise OSError("fixture interrupted publication")

    monkeypatch.setattr(private_export.os, "fsync", replace_during_publish)
    with pytest.raises(OSError):
        document_export.export(capture(), out)
    assert out.is_dir() and not list(out.iterdir())
    assert displaced.is_dir() and not list(displaced.iterdir())
