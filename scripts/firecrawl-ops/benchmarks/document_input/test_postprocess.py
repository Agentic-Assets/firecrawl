"""Fenced-output normalization is explicit and never edits the original."""
import json

import postprocess
import pytest


def record(raw, **extra):
    return json.dumps({"document_id": "one", "model": "example/model", "arm": "native_pdf",
                       "status": "incomplete", "invalid_json": True, "finish_reason": "stop",
                       "raw_output": raw, **extra}).encode()


VALID = '{"transcription":"a", "facts":[], "tables":[]}'


def test_only_complete_outer_json_fence_is_recovered():
    result = postprocess.derive(record(f"```json\n{VALID}\n```"))
    assert result["original_invalid_json"] is True
    assert result["original_status"] == "incomplete"
    assert result["removed_outer_json_fence"] is True
    assert result["normalized_valid"] is True
    assert result["normalized_content"]["transcription"] == "a"


@pytest.mark.parametrize("text", [f"prefix\n```json\n{VALID}\n```", f"```json\n{VALID}\n```\nsuffix",
                                 f"```text\n{VALID}\n```", f"```json\n{VALID}\n```\n```json\n{VALID}\n```",
                                 '```json\n{"transcription":"x", "facts":["bad"], "tables":[]}\n```'])
def test_refuses_extra_content_or_invalid_shape(text):
    result = postprocess.derive(record(text))
    assert result["removed_outer_json_fence"] is False
    assert result["normalized_valid"] is False


def test_output_is_private_bound_and_original_unchanged(tmp_path):
    source, destination = tmp_path / "original", tmp_path / "normalized"
    source.mkdir(mode=0o700)
    old = record(f"```json\n{VALID}\n```", finish_reason="length", truncated=True)
    target = source / "one.result.json"
    target.write_bytes(old)
    target.chmod(0o600)
    summary = postprocess.run(source, destination)
    result = json.loads((destination / target.name).read_bytes())
    assert summary == {"records": 1, "fence_removed": 1, "normalized_valid": 1}
    assert target.read_bytes() == old
    assert result["original_sha256"] == postprocess.runner.digest(old)
    assert result["truncated"] is True and result["finish_reason"] == "length"
    assert (destination.stat().st_mode & 0o777) == 0o700
    assert ((destination / target.name).stat().st_mode & 0o777) == 0o600
    with pytest.raises(FileExistsError):
        postprocess.run(source, destination)
