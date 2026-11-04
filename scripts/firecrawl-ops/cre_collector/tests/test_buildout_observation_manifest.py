"""Offline manifest and process-local replay model; synthetic fixtures only."""
import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import buildout_observation_artifact as builder
import buildout_observation_manifest as manifest


def page(index, count=45):
    return {"syntheticFixture": True, "providerPage": index, "inventory": [
        {"id": None if i % 2 else f"{index}-{i}", "address": None if i % 2 else "unverified",
         "nested": {"facts": [["Rate", "$10/SF"], ["Rate", "$12/SF"], ["Unknown", False], ["Unknown", None]]},
         "index_attributes": [["Area", {"amount": 500 + i, "unit": "SF"}], ["other", False], ["other", None]],
         **({"Authorization": "Bearer dummy"} if i == 0 else {})}
        for i in range(count)
    ]}


def scope(page_number=0, **changes):
    value = {"sourceKey": "fortis-net-lease", "generationId": "gen-1", "captureId": "capture-1",
             "page": page_number, "transaction": "unknown"}
    return {**value, **changes}


def bundle(index=0, *, scope_value=None, input_page=None):
    raw = json.dumps(input_page if input_page is not None else page(index)).encode()
    scoped = scope_value if scope_value is not None else scope(index)
    artifact, _ = builder.build_artifact(raw, source_key=scoped["sourceKey"], input_kind="buildout-page")
    doc = manifest.seal(raw, artifact, scope=scoped, input_kind="buildout-page")
    return raw, artifact, manifest.canonical(doc) + b"\n", doc


def test_two_pages_bind_90_partial_records_without_canonical_identity():
    model = manifest.FixtureReplayModel()
    for page_index in range(2):
        raw, artifact, sealed, doc = bundle(page_index)
        assert manifest.verify(sealed, raw, artifact, expected_scope=scope(page_index)) == doc
        assert doc["sourceAuthorization"] == "not_evaluated" and doc["sourceCompleteness"] == "unverified"
        assert doc["scopeOrigin"] == "operator_asserted_unverified"
        assert doc["summary"]["quarantinedItems"] == 1 and doc["summary"]["status"] == "partial"
        assert model.apply(sealed, raw, artifact, expected_scope=scope(page_index)) == "inserted"
    snapshot = model.snapshot()
    assert len(snapshot) == 2 and sum(len(item[1]) for item in snapshot.values()) == 90
    first = json.loads(snapshot[tuple(scope(0)[key] for key in manifest.SLOT_KEYS)][1][1][1])
    assert first["identityResolution"] == "unresolved"
    assert first["sourceFields"]["id"] is None and first["sourceFields"]["address"] is None
    assert first["sourceFields"]["nested"]["facts"][:2] == [["Rate", "$10/SF"], ["Rate", "$12/SF"]]
    assert "Bearer dummy" not in json.dumps(first)


def test_exact_retry_and_atomic_conflict_across_threads():
    raw, artifact, sealed, _ = bundle()
    model = manifest.FixtureReplayModel()
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: model.apply(sealed, raw, artifact, expected_scope=scope()), range(12)))
    assert outcomes.count("inserted") == 1 and outcomes.count("unchanged") == 11
    before = model.snapshot()
    other = page(0)
    other["inventory"][1]["nested"]["facts"][0][1] = "$999/SF"
    raw2, artifact2, sealed2, _ = bundle(input_page=other)
    with pytest.raises(ValueError, match="divergent fixture (page binding|replay)"):
        model.apply(sealed2, raw2, artifact2, expected_scope=scope())
    assert model.snapshot() == before


def test_new_generation_retains_conflicting_observations_separately():
    raw, artifact, sealed, _ = bundle()
    model = manifest.FixtureReplayModel()
    model.apply(sealed, raw, artifact, expected_scope=scope())
    other = page(0)
    other["inventory"][1]["nested"]["facts"][0][1] = "$999/SF"
    next_scope = scope(generationId="gen-2")
    raw2, artifact2, sealed2, _ = bundle(scope_value=next_scope, input_page=other)
    assert model.apply(sealed2, raw2, artifact2, expected_scope=next_scope) == "inserted"
    assert len(model.snapshot()) == 2


@pytest.mark.parametrize("field", ["sourceKey", "generationId", "captureId", "page", "transaction"])
def test_expected_scope_mismatch_never_authorizes_or_inserts(field):
    raw, artifact, sealed, _ = bundle()
    altered = scope()
    altered[field] = {"sourceKey": "svn", "generationId": "gen-2", "captureId": "capture-2", "page": 1,
                      "transaction": "lease"}[field]
    model = manifest.FixtureReplayModel()
    with pytest.raises(ValueError, match="scope"):
        model.apply(sealed, raw, artifact, expected_scope=altered)
    assert not model.snapshot()


def test_byte_hashes_and_rebuild_refuse_tampering():
    raw, artifact, sealed, doc = bundle()
    with pytest.raises(ValueError, match="artifact"):
        manifest.verify(sealed, raw + b" ", artifact, expected_scope=scope())
    with pytest.raises(ValueError, match="artifact"):
        manifest.verify(sealed, raw, artifact + b" ", expected_scope=scope())
    doc["summary"]["retainedRecords"] += 1
    with pytest.raises(ValueError, match="manifest"):
        manifest.verify(manifest.canonical(doc), raw, artifact, expected_scope=scope())


def test_invalid_scope_duplicate_json_and_unlabelled_fixture_fail_closed():
    raw, artifact, sealed, _ = bundle()
    for altered in (scope(page=True), scope(page=-1), scope(transaction="other"), scope(generationId="bad space")):
        with pytest.raises(ValueError):
            manifest.verify(sealed, raw, artifact, expected_scope=altered)
    with pytest.raises(ValueError, match="duplicate"):
        manifest.verify(b'{"scope":{},"scope":{}}', raw, artifact, expected_scope=scope())
    real = page(0)
    real.pop("syntheticFixture")
    raw2, artifact2, sealed2, _ = bundle(input_page=real)
    with pytest.raises(ValueError, match="synthetic"):
        manifest.FixtureReplayModel().apply(sealed2, raw2, artifact2, expected_scope=scope())


def test_cli_private_exclusive_output_and_independent_verification(tmp_path):
    raw, artifact, _, _ = bundle()
    source, output, sealed = tmp_path / "input.json", tmp_path / "artifact.json", tmp_path / "manifest.json"
    source.write_bytes(raw)
    output.write_bytes(artifact)
    script = Path(manifest.__file__)
    common = ["--input", str(source), "--artifact", str(output), "--manifest", str(sealed),
              "--source-key", "fortis-net-lease", "--generation-id", "gen-1", "--capture-id", "capture-1",
              "--page", "0", "--transaction", "unknown", "--input-kind", "buildout-page"]
    command = [sys.executable, str(script), "seal", *common]
    result = subprocess.run(command, text=True, capture_output=True, check=True)
    assert "Bearer dummy" not in result.stdout and "not_evaluated" in result.stdout
    assert sealed.stat().st_mode & 0o077 == 0
    before = sealed.read_bytes()
    assert subprocess.run(command, text=True, capture_output=True, check=False).returncode != 0
    assert sealed.read_bytes() == before
    assert subprocess.run([sys.executable, str(script), "verify", *common], text=True, capture_output=True, check=False).returncode == 0
    link = tmp_path / "link.json"
    link.symlink_to(sealed)
    with pytest.raises(FileExistsError):
        builder.write_private(link, b"replacement")
    assert sealed.read_bytes() == before


def test_unbounded_numeric_token_fails_before_model_mutation():
    raw, artifact, _, _ = bundle()
    malicious = b'{"page":' + b'9' * 1001 + b'}'
    model = manifest.FixtureReplayModel()
    with pytest.raises(ValueError, match="numeric token exceeds"):
        model.apply(malicious, raw, artifact, expected_scope=scope())
    assert not model.snapshot()


def test_builder_fingerprint_is_part_of_verified_binding():
    raw, artifact, _, doc = bundle()
    doc["builder"]["moduleSha256"] = "0" * 64
    with pytest.raises(ValueError, match="manifest"):
        manifest.verify(manifest.canonical(doc), raw, artifact, expected_scope=scope())


def test_capture_id_cannot_rebind_an_existing_generation_page():
    raw, artifact, sealed, _ = bundle()
    model = manifest.FixtureReplayModel()
    assert model.apply(sealed, raw, artifact, expected_scope=scope()) == "inserted"
    original = model.snapshot()
    alternate = scope(captureId="capture-2")
    rebound = manifest.canonical(manifest.seal(raw, artifact, scope=alternate, input_kind="buildout-page"))
    with pytest.raises(ValueError, match="divergent fixture (page binding|replay)"):
        model.apply(rebound, raw, artifact, expected_scope=alternate)
    assert model.snapshot() == original


def test_source_registry_fingerprint_is_part_of_verified_binding():
    raw, artifact, _, doc = bundle()
    doc["builder"]["sourceRegistrySha256"] = "0" * 64
    with pytest.raises(ValueError, match="manifest"):
        manifest.verify(manifest.canonical(doc), raw, artifact, expected_scope=scope())


def test_transaction_switch_cannot_rebind_page_to_new_input_or_capture():
    raw, artifact, sealed, _ = bundle()
    model = manifest.FixtureReplayModel()
    assert model.apply(sealed, raw, artifact, expected_scope=scope()) == "inserted"
    before = model.snapshot()
    changed = page(0)
    changed["inventory"][0]["index_attributes"][0][1]["amount"] = 999
    sale_scope = scope(transaction="sale")
    raw2, artifact2, sealed2, _ = bundle(scope_value=sale_scope, input_page=changed)
    with pytest.raises(ValueError, match="divergent fixture page binding"):
        model.apply(sealed2, raw2, artifact2, expected_scope=sale_scope)
    assert model.snapshot() == before
    changed_capture = scope(transaction="sale", captureId="capture-2")
    rebound = manifest.canonical(manifest.seal(raw, artifact, scope=changed_capture, input_kind="buildout-page"))
    with pytest.raises(ValueError, match="divergent fixture page binding"):
        model.apply(rebound, raw, artifact, expected_scope=changed_capture)
    assert model.snapshot() == before
    same_bytes = manifest.canonical(manifest.seal(raw, artifact, scope=sale_scope, input_kind="buildout-page"))
    assert model.apply(same_bytes, raw, artifact, expected_scope=sale_scope) == "inserted"
    assert len(model.snapshot()) == 2


def test_hashes_independently_match_exact_input_and_artifact_bytes():
    raw, artifact, sealed, doc = bundle()
    assert doc["input"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert doc["artifact"]["sha256"] == hashlib.sha256(artifact).hexdigest()
    assert manifest.verify(sealed, raw, artifact, expected_scope=scope()) == doc


def test_changed_builder_or_source_registry_refuses_verification(tmp_path, monkeypatch):
    raw, artifact, sealed, _ = bundle()
    original_path = Path(builder.__file__)
    replacement = tmp_path / "builder.py"
    replacement.write_bytes(original_path.read_bytes() + b"\n# different builder\n")
    monkeypatch.setattr(builder, "__file__", str(replacement))
    with pytest.raises(ValueError, match="manifest"):
        manifest.verify(sealed, raw, artifact, expected_scope=scope())
    monkeypatch.setattr(builder, "__file__", str(original_path))
    monkeypatch.setattr(manifest, "BUILDOUT_SOURCE_KEYS", manifest.BUILDOUT_SOURCE_KEYS | {"future-source"})
    with pytest.raises(ValueError, match="manifest"):
        manifest.verify(sealed, raw, artifact, expected_scope=scope())


def test_legacy_manifest_is_stale_and_requires_preserving_inputs():
    raw, artifact_bytes, _, doc = bundle()
    legacy = json.loads(json.dumps(doc))
    legacy["builder"].pop("readerModuleSha256")
    legacy.pop("manifestSha256")
    legacy["manifestSha256"] = manifest.digest(manifest.canonical(legacy))
    with pytest.raises(ValueError, match="preserve the original input and artifact, then regenerate"):
        manifest.verify(manifest.canonical(legacy), raw, artifact_bytes, expected_scope=scope())
    fresh = manifest.canonical(manifest.seal(raw, artifact_bytes, scope=scope(), input_kind="buildout-page"))
    assert manifest.verify(fresh, raw, artifact_bytes, expected_scope=scope())["sourceAuthorization"] == "not_evaluated"
