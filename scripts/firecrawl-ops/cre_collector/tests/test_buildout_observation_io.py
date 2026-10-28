"""Stable-descriptor input tests; no provider, network, database or scheduler."""
import os
import sys
from pathlib import Path

import pytest

import buildout_observation_artifact as artifact
import buildout_observation_io as io
import buildout_observation_manifest as manifest


def test_regular_file_and_bounded_extra_byte(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"abcdef")
    assert io.read_consistent(source, 10) == b"abcdef"
    assert io.read_consistent(source, 3) == b"abcd"
    assert manifest.read_bounded(source, 3) == b"abcd"


def test_rejects_final_symlink_and_nonregular_file(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"value")
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(OSError):
        io.read_consistent(link, 100)
    with pytest.raises(ValueError, match="regular file"):
        io.read_consistent(tmp_path, 100)
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        with pytest.raises(ValueError, match="regular file"):
            io.read_consistent(fifo, 100)


def test_same_size_in_place_rewrite_with_restored_mtime_fails(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"A" * (io.READ_CHUNK_BYTES * 2))
    original = source.stat()
    native_read = os.read
    called = False

    def racing_read(fd, count):
        nonlocal called
        part = native_read(fd, count)
        if part and not called:
            called = True
            source.write_bytes(b"B" * original.st_size)
            os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
            assert os.fstat(fd).st_ctime_ns != original.st_ctime_ns
        return part

    monkeypatch.setattr(io.os, "read", racing_read)
    with pytest.raises(ValueError, match="changed during read"):
        io.read_consistent(source, original.st_size)
    assert called


def test_path_replacement_after_first_chunk_fails(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"A" * (io.READ_CHUNK_BYTES * 2))
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"B" * (io.READ_CHUNK_BYTES * 2))
    native_read = os.read
    called = False

    def racing_read(fd, count):
        nonlocal called
        part = native_read(fd, count)
        if part and not called:
            called = True
            os.replace(replacement, source)
        return part

    monkeypatch.setattr(io.os, "read", racing_read)
    with pytest.raises(ValueError, match="changed during read"):
        io.read_consistent(source, io.READ_CHUNK_BYTES * 2)
    assert called


def test_builder_never_creates_output_after_input_changes(tmp_path, monkeypatch):
    source, output = tmp_path / "source.json", tmp_path / "artifact.json"
    source.write_text('{"inventory":[],"padding":"' + "A" * (io.READ_CHUNK_BYTES * 2) + '"}')
    native_read = os.read
    called = False

    def racing_read(fd, count):
        nonlocal called
        part = native_read(fd, count)
        if part and not called:
            called = True
            source.write_bytes(b"B" * source.stat().st_size)
        return part

    monkeypatch.setattr(io.os, "read", racing_read)
    monkeypatch.setattr(sys, "argv", ["builder", "--input", str(source), "--input-kind", "buildout-page",
                                       "--source-key", "fortis-net-lease", "--out", str(output)])
    with pytest.raises(ValueError, match="changed during read"):
        artifact.main()
    assert called and not output.exists()


def test_builder_reader_and_registry_fingerprints_are_pinned(tmp_path, monkeypatch):
    original = Path(io.__file__)
    first = manifest.builder_fingerprint()
    replacement = tmp_path / "io.py"
    replacement.write_bytes(original.read_bytes() + b"\n# changed reader\n")
    monkeypatch.setattr(io, "__file__", str(replacement))
    assert manifest.builder_fingerprint()["readerModuleSha256"] != first["readerModuleSha256"]


def test_projector_never_creates_output_after_artifact_changes(tmp_path, monkeypatch):
    import json

    import buildout_observation_projection as projection

    original = json.dumps({"inventory": [{"index_attributes": [["# of Spaces", "1 Space"]],
                                          "large": "A" * (io.READ_CHUNK_BYTES * 2)}]}).encode()
    artifact_bytes, _ = artifact.build_artifact(original, source_key="fortis-net-lease", input_kind="buildout-page")
    scope = {"sourceKey": "fortis-net-lease", "generationId": "g1", "captureId": "c1",
             "page": 0, "transaction": "unknown"}
    seal = manifest.seal(original, artifact_bytes, scope=scope, input_kind="buildout-page")
    source, prior, seal_path, output = (tmp_path / name for name in ("artifact.json", "original.json", "seal.json", "projection.json"))
    source.write_bytes(artifact_bytes)
    prior.write_bytes(original)
    seal_path.write_bytes(manifest.canonical(seal))
    native_read = os.read
    called = False

    def racing_read(fd, count):
        nonlocal called
        part = native_read(fd, count)
        if part and not called:
            called = True
            source.write_bytes(b"B" * source.stat().st_size)
        return part

    monkeypatch.setattr(io.os, "read", racing_read)
    monkeypatch.setattr(sys, "argv", ["projector", "--input", str(source), "--original", str(prior),
        "--manifest", str(seal_path), "--source-key", "fortis-net-lease", "--generation-id", "g1",
        "--capture-id", "c1", "--page", "0", "--transaction", "unknown", "--out", str(output)])
    with pytest.raises(ValueError, match="changed during read"):
        projection.main()
    assert called and not output.exists()
