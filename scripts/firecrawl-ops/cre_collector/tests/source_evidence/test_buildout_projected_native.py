import copy
import hashlib
import json

import pytest
from source_evidence import (
    acquisition,
    buildout_pages,
    buildout_response,
    events,
    native_pages,
)
from test_buildout_native_pipeline import native_run
from test_buildout_pages import entry


@pytest.mark.parametrize("source,feed", [("svn", None), ("franklin-street", "franklin-street-sale"),
                                        ("franklin-street", "franklin-street-lease")])
def test_projected_page_binds_only_projected_content_and_replays(source, feed):
    body = (b'{"inventory":[{"id":1,"pdf_url":"https://buildout.com/docs/foo/123?token=private123",'
            b'"precise":0.1234567890123456789},{"id":2,"pdf_url":"https://example.org/flyer.pdf"}]}')
    raw, name = entry(body, source=source, feed_key=feed)
    original = json.loads(raw)["receipt"]
    result, reason = buildout_pages.decode_entry(raw, name, {"attempt": 1}, source_key=source,
                                                native_format=buildout_pages.PROJECTED_FORMAT)
    assert reason is None and result["schema"] == buildout_response.PROJECTED_SCHEMA
    assert result["records"][0]["pdf_url"] is None
    assert result["records"][1]["pdf_url"] == "https://example.org/flyer.pdf"
    assert result["projection"]["withheld"] == [{"pointer": "/inventory/0/pdf_url",
        "recordPointer": "/inventory/0", "reason": "credential-bearing-document-reference"}]
    output = events.encoded(result)
    assert b"private123" not in output and original["bodySha256"].encode() not in output
    assert hashlib.sha256(raw).hexdigest().encode() not in output
    assert "bodyBytes" not in result["receipt"] and "bodySha256" not in result["receipt"]
    assert result["receipt"].get("feedKey") == feed
    assert result["provenance"]["httpStatus"] == result["provenance"]["finalUrl"] == "unobserved"
    buildout_response.validate(result)
    restored = acquisition.encode_json(buildout_response.reconstruct(result), result["numericLiterals"])
    assert "0.1234567890123456789" in restored and "private123" not in restored
    forged = copy.deepcopy(result)
    forged["records"][0]["pdf_url"] = "https://example.org/changed.pdf"
    with pytest.raises(ValueError):
        buildout_response.validate(forged)


def test_formats_are_explicit_and_legacy_result_is_unchanged():
    raw, name = entry(b'{"inventory":[{"pdf_url":"https://buildout.com/docs/foo/1?token=abc"}]}', source="svn")
    old, _ = buildout_pages.decode_entry(raw, name, {}, source_key="svn")
    explicit, _ = native_pages._decode("svn", raw, name, {}, native_format=buildout_pages.FORMAT)
    assert old == explicit and old["receipt"] == json.loads(raw)["receipt"]
    projected, _ = native_pages._decode("svn", raw, name, {}, native_format=buildout_pages.PROJECTED_FORMAT)
    assert projected["schema"] == buildout_response.PROJECTED_SCHEMA
    with pytest.raises(ValueError):
        native_pages._decode("svn", raw, name, {}, native_format="unknown")
    assert not native_pages.format_supported("jll", buildout_pages.PROJECTED_FORMAT)


@pytest.mark.parametrize("body", [b'{"inventory":null}', b'{"inventory":[null]}',
                                   b'{"inventory":[{"pdf_url":123}]}', b'null'])
def test_projected_clean_unknown_shapes_remain_reconstructable(body):
    raw, name = entry(body, source="svn")
    result, reason = buildout_pages.decode_entry(raw, name, {}, source_key="svn",
                                                 native_format=buildout_pages.PROJECTED_FORMAT)
    assert reason is None and not result["projection"]["withheld"]
    assert json.loads(acquisition.encode_json(buildout_response.reconstruct(result), result["numericLiterals"])) == json.loads(body)


def test_private_original_file_is_rechecked_before_projection():
    raw, name = entry(b'{"inventory":[]}', source="svn")
    with pytest.raises(ValueError):
        buildout_pages.decode_entry(raw + b" ", name, {}, source_key="svn",
                                    native_format=buildout_pages.PROJECTED_FORMAT)


def test_unreviewed_sensitive_reference_is_rejected_by_normal_scanner():
    from source_evidence import automatic

    body = b'{"inventory":[{"other":"https://example.org/x?token=unreviewed"}]}'
    raw, name = entry(body, source="svn")
    result, reason = buildout_pages.decode_entry(raw, name, {}, source_key="svn",
                                                 native_format=buildout_pages.PROJECTED_FORMAT)
    assert reason is None and result["projection"]["withheld"] == []
    with pytest.raises(ValueError):
        automatic._refuse_credential_fields(result)


def test_projected_native_settlement_replays_without_exporting_original_hash(tmp_path):
    from source_evidence import automatic

    run, manifest, attempt, directory = native_run(tmp_path)
    config = manifest["config"]["automatic_source_evidence"]
    policy_path = config["policy_path"]
    from pathlib import Path
    policy = json.loads(Path(policy_path).read_text())
    policy["native_source_formats"]["fortis-net-lease"] = buildout_pages.PROJECTED_FORMAT
    policy_bytes = events.encoded(policy)
    Path(policy_path).write_bytes(policy_bytes)
    config = automatic.configuration(policy_path, events.sha256(policy_bytes), config["env_file"],
                                     config["target_sha256"], config["collector_git_sha"], ["fortis-net-lease"])
    manifest["config"]["automatic_source_evidence"] = config
    raw, name = entry(b'{"inventory":[{"pdf_url":"https://buildout.com/docs/foo/1?token=private123"}]}',
                      source="fortis-net-lease")
    path = directory / name
    path.write_bytes(raw)
    path.chmod(0o600)
    native_pages.settle(run, manifest, "fortis-net-lease", attempt, lambda *_: None)
    page = attempt["native_source_evidence"]["pages"][0]
    plan = native_pages.prepare_job(run, manifest, "fortis-net-lease", attempt, page)
    assert attempt["native_source_evidence"]["state"] == "settled"
    plan_bytes = events.encoded(plan)
    assert b"private123" not in plan_bytes and hashlib.sha256(raw).hexdigest().encode() not in plan_bytes
    assert json.loads(raw)["receipt"]["bodySha256"].encode() not in plan_bytes
    assert native_pages.prepare_job(run, manifest, "fortis-net-lease", attempt, page) == plan
    path.write_bytes(raw + b" ")
    with pytest.raises(ValueError):
        native_pages.prepare_job(run, manifest, "fortis-net-lease", attempt, page)
