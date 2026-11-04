"""Saved synthetic cache-audit integration; no live provider or database."""
import base64
import hashlib
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

import buildout_cache_observation_pipeline as pipeline
import buildout_observation_artifact as artifact
import buildout_observation_manifest as manifest


def audit_fixture(count=84, generation=None, page=None):
    rows = []
    for i in range(count):
        row = {"id": None if i % 2 else str(i), "name": "example", "address": None,
               "nested": {"unknown": [False, None, {"unit": "SF", "precise": "NUMERIC_LITERAL"}]},
               "index_attributes": [["# of Spaces", "1 Space"], ["# of Spaces", "2 Spaces"],
                                    ["Cap Rate", "6.5%"], ["Unknown", False], ["Unknown", None]]}
        if i % 3:
            row.pop("id")
        rows.append(row)
    cache = json.dumps({"page": page, "generationId": generation, "data": {"inventory": rows}}, separators=(",", ":"))
    cache_bytes = cache.replace('"NUMERIC_LITERAL"', '12345678901234567890.12345678901234567890').encode()
    audit = {"schema": "buildout-page-audit-v1", "scope": "selected_page_only", "authority": "none",
             "publisher_authenticated": False, "private_unreviewed": True, "coverage": "unknown",
             "status": "provenance_incomplete", "provenance_reasons": ["generation_missing_or_mismatch", "invalid_page"],
             "sourceKey": "faris-lee", "cacheFileSha256": hashlib.sha256(cache_bytes).hexdigest(),
             "input_byte_authority": "originalCacheBytesBase64", "originalCacheBytesBase64": base64.b64encode(cache_bytes).decode(),
             "data_fidelity": {"unsafeNumericValues": 84}, "observedRows": count,
             # Deliberately wrong JS-derived data must not supply source facts.
             "rows": [{"row": {"index_attributes": [["# of Spaces", "999 Spaces"]]}}]}
    return cache_bytes, audit


def scope(capture="capture-a", generation=None, page=None, transaction="unknown"):
    return {"sourceKey": "faris-lee", "generationId": generation, "captureId": capture,
            "page": page, "transaction": transaction}


def encode(value):
    return json.dumps(value, separators=(",", ":")).encode()


def test_eighty_four_cache_rows_use_original_bytes_and_mark_provenance_incomplete():
    cache, audit = audit_fixture()
    source = encode(audit)
    result, seal_bytes, projected, summary = pipeline.pipeline(source, scope=scope())
    doc = json.loads(result, parse_float=Decimal)
    seal = json.loads(seal_bytes)
    projection = json.loads(projected)
    assert len(doc["records"]) == 84 and summary["artifact"]["retainedRecords"] == 84
    assert doc["auditContext"]["cacheBytesSha256"] == hashlib.sha256(cache).hexdigest()
    assert doc["auditContext"]["status"] == "provenance_incomplete"
    assert doc["reportedProvenanceIncomplete"] is True
    assert summary["artifact"]["reportedProvenanceIncomplete"] is True
    assert projection["reportedProvenanceIncomplete"] is True
    assert summary["projection"]["reportedProvenanceIncomplete"] is True
    assert summary["projection"]["upstreamIncomplete"] is False
    assert doc["provenance"]["inputSha256"] == hashlib.sha256(source).hexdigest()
    assert doc["provenance"]["sourceCompleteness"] == "unverified"
    assert doc["records"][3]["sourceFields"]["id"] is None
    assert "id" not in doc["records"][1]["sourceFields"]
    assert doc["records"][0]["sourceFields"]["nested"]["unknown"][2]["precise"] == Decimal("12345678901234567890.12345678901234567890")
    assert doc["records"][0]["sourcePointer"] == "/data/inventory/0"
    assert doc["records"][0]["indexAttributes"][0]["literal"] == ["# of Spaces", "1 Space"]
    assert seal["scope"]["generationId"] is None and seal["scope"]["page"] is None
    assert seal["scopeResolution"] == "capture_scoped_unresolved"
    assert projection["auditProvenance"]["status"] == "provenance_incomplete"
    assert projection["promotion"] == "not_authorized" and projection["sourceCompleteness"] == "unverified"
    import available_space_count
    import buildout_observation_projection
    code = projection["projectionCode"]
    assert code["projectorSha256"] == hashlib.sha256(Path(buildout_observation_projection.__file__).read_bytes()).hexdigest()
    assert code["availableSpaceParserSha256"] == hashlib.sha256(Path(available_space_count.__file__).read_bytes()).hexdigest()
    assert code["fingerprintOrigin"] == "local_files_at_projection_time_not_authentication"
    assert projection["records"][0]["availableSpaceCount"]["state"] == "ambiguous"
    assert b"999 Spaces" not in result


def test_forged_audit_claim_is_not_verified_or_admitted():
    _, audit = audit_fixture(2)
    audit["status"] = "parsed_json_consistent_fidelity_unverified"
    audit["provenance_reasons"] = []
    raw, _, projected, _ = pipeline.pipeline(encode(audit), scope=scope())
    doc, projection = json.loads(raw), json.loads(projected)
    assert len(doc["records"]) == len(projection["records"]) == 2
    assert doc["auditContext"]["auditClaimsOrigin"] == "caller_reported"
    assert doc["auditContext"]["auditClaimsVerification"] == "not_evaluated"
    assert projection["auditProvenance"]["auditClaimsVerification"] == "not_evaluated"
    assert projection["promotion"] == "not_authorized"
    assert projection["sourceCompleteness"] == "unverified"


def test_embedded_hash_base64_duplicates_and_scope_mismatch_fail_closed():
    cache, audit = audit_fixture(2)
    bad = dict(audit, provenance_reasons=["password_secretvalue"])
    with pytest.raises(ValueError, match="invalid audit provenance status") as error:
        pipeline.pipeline(encode(bad), scope=scope())
    assert "password_secretvalue" not in str(error.value)
    bad = dict(audit, cacheFileSha256="0" * 64)
    with pytest.raises(ValueError, match="digest"):
        pipeline.pipeline(encode(bad), scope=scope())
    bad = dict(audit, originalCacheBytesBase64="%%%")
    with pytest.raises(ValueError, match="encoding"):
        pipeline.pipeline(encode(bad), scope=scope())
    bad = dict(audit, sourceKey="svn")
    with pytest.raises(ValueError, match="audit envelope"):
        pipeline.pipeline(encode(bad), scope=scope())
    duplicate = b'{"data":{"inventory":[],"inventory":[]}}'
    bad = dict(audit, originalCacheBytesBase64=base64.b64encode(duplicate).decode(),
               cacheFileSha256=hashlib.sha256(duplicate).hexdigest(), observedRows=0)
    with pytest.raises(ValueError, match="projection unsupported.*source availability is unverified"):
        pipeline.pipeline(encode(bad), scope=scope())
    assert cache


def test_cache_and_audit_limits_do_not_truncate(monkeypatch):
    _, audit = audit_fixture(2)
    monkeypatch.setattr(artifact, "MAX_CACHE_BYTES", 16)
    with pytest.raises(ValueError, match="embedded cache"):
        pipeline.pipeline(encode(audit), scope=scope())
    monkeypatch.setattr(artifact, "MAX_AUDIT_BYTES", 16)
    with pytest.raises(ValueError, match="input exceeds"):
        pipeline.pipeline(encode(audit), scope=scope())


def test_unknown_captures_remain_distinct_without_cross_capture_identity():
    _, first = audit_fixture(1)
    first["syntheticFixture"] = True
    _, second = audit_fixture(2)
    second["syntheticFixture"] = True
    model = manifest.FixtureReplayModel()
    for audit, s in ((first, scope("first")), (second, scope("second"))):
        original = encode(audit)
        output, sealed, _, _ = pipeline.pipeline(original, scope=s)
        assert model.apply(sealed, original, output, expected_scope=s) == "inserted"
    assert len(model.snapshot()) == 2


@pytest.mark.parametrize("generation,page", [(None, None), ("other", 0), ("g1", 1)])
def test_assertions_cannot_bind_different_unresolved_cache_captures(generation, page):
    model = manifest.FixtureReplayModel()
    for capture, count in (("first", 1), ("second", 2)):
        _, audit = audit_fixture(count, generation=generation, page=page)
        audit["syntheticFixture"] = True
        asserted = scope(capture, generation="g1", page=0)
        original = encode(audit)
        output, sealed, _, _ = pipeline.pipeline(original, scope=asserted)
        assert json.loads(sealed)["scopeResolution"] == "capture_scoped_unresolved"
        assert model.apply(sealed, original, output, expected_scope=asserted) == "inserted"
    assert len(model.snapshot()) == 2


def test_foreign_cache_and_forged_status_never_bind_distinct_captures():
    model = manifest.FixtureReplayModel()
    for capture, count, foreign in (("first", 1, False), ("second", 2, True)):
        cache, audit = audit_fixture(count, generation="g1", page=0)
        if foreign:
            data = json.loads(cache)
            data.update(pluginKey="foreign", queryFingerprint="foreign", observation={"endpoint": "foreign"})
            cache = encode(data)
            audit["originalCacheBytesBase64"] = base64.b64encode(cache).decode()
            audit["cacheFileSha256"] = hashlib.sha256(cache).hexdigest()
        audit.update(syntheticFixture=True, status="parsed_json_consistent_fidelity_unverified", provenance_reasons=[])
        asserted = scope(capture, generation="g1", page=0)
        original = encode(audit)
        output, sealed, _, _ = pipeline.pipeline(original, scope=asserted)
        assert json.loads(sealed)["scopeResolution"] == "capture_scoped_unresolved"
        assert model.apply(sealed, original, output, expected_scope=asserted) == "inserted"
    assert len(model.snapshot()) == 2


def test_same_capture_rejects_changed_content_and_changed_asserted_scope():
    model = manifest.FixtureReplayModel()
    _, audit = audit_fixture(1, generation="g1", page=0)
    audit["syntheticFixture"] = True
    asserted = scope("same", generation="g1", page=0)
    original = encode(audit)
    output, sealed, _, _ = pipeline.pipeline(original, scope=asserted)
    assert model.apply(sealed, original, output, expected_scope=asserted) == "inserted"
    assert model.apply(sealed, original, output, expected_scope=asserted) == "unchanged"
    _, changed = audit_fixture(2, generation="g1", page=0)
    changed["syntheticFixture"] = True
    new_input = encode(changed)
    new_output, new_seal, _, _ = pipeline.pipeline(new_input, scope=asserted)
    with pytest.raises(ValueError, match="divergent fixture capture binding"):
        model.apply(new_seal, new_input, new_output, expected_scope=asserted)
    other_scope = scope("same", generation="g2", page=1)
    other_output, other_seal, _, _ = pipeline.pipeline(original, scope=other_scope)
    with pytest.raises(ValueError, match="divergent fixture capture binding"):
        model.apply(other_seal, original, other_output, expected_scope=other_scope)
    assert len(model.snapshot()) == 1


def test_cli_outputs_are_private_exclusive_and_do_not_print_raw_data(tmp_path):
    _, audit = audit_fixture(2)
    audit_path, output = tmp_path / "audit.json", tmp_path / "result"
    audit_path.write_bytes(encode(audit))
    os.chmod(audit_path, 0o600)
    script = Path(pipeline.__file__)
    command = [sys.executable, str(script), "--audit", str(audit_path), "--out-dir", str(output),
               "--source-key", "faris-lee", "--capture-id", "local-test"]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert "999 Spaces" not in result.stdout and "originalCacheBytesBase64" not in result.stdout
    assert output.stat().st_mode & 0o077 == 0
    files = [output / name for name in ("artifact.json", "manifest.json", "projection.json")]
    assert all(path.is_file() and path.stat().st_mode & 0o077 == 0 for path in files)
    original = [path.read_bytes() for path in files]
    assert subprocess.run(command, capture_output=True, text=True, check=False).returncode != 0
    assert [path.read_bytes() for path in files] == original
