"""Offline collector admission for explicitly withheld Buildout references."""

import copy
import json

import pytest
from source_evidence import (
    automatic,
    buildout_pages,
    buildout_projection,
    cli,
    collector,
    collector_projection,
    document_candidates,
    events,
)
from test_automatic import SHA, setup_policy

SOURCE = "cawley-chicago"


def source_artifact():
    return {"sources": [{"sourceKey": SOURCE, "truncated": True, "error": "partial fixture"}],
            "runMeta": {"finishedAt": "2026-10-29T00:00:00Z", "mode": "full"},
            "listings": [{"sourceKey": SOURCE, "id": None if i == 0 else "duplicate-fixture-id",
                          "unknown": [None, False, {"label": "Unmapped fixture", "value": i}],
                          "brochures": [{"url": f"https://buildout.com/docs/fixture/{i}?token=opaque-fixture"}]}
                         for i in range(80)]}


def configured(tmp_path):
    config, policy, path = setup_policy(tmp_path, sources=(SOURCE,), count=80)
    policy.update(schema_version=automatic.SCHEMA_V2,
                  collector_projections={SOURCE: buildout_projection.VERSION},
                  native_source_formats={SOURCE: buildout_pages.PROJECTED_FORMAT}, max_native_pages=128)
    raw = events.encoded(policy)
    path.write_bytes(raw)
    return automatic.configuration(str(path), events.sha256(raw), config["env_file"],
                                   config["target_sha256"], SHA, [SOURCE])


def sealed(tmp_path, config, artifact):
    tmp_path.mkdir(mode=0o700)
    source = tmp_path / "collector.json"
    source.write_bytes(events.encoded(artifact))
    source.chmod(0o600)
    return automatic.seal_collector(tmp_path, config, [SOURCE], SHA, False, "generation-1", SOURCE, 1, source)


def test_cli_projection_preserves_partial_addressless_duplicate_records(tmp_path):
    artifact = source_artifact()
    source = tmp_path / "original.json"
    source.write_bytes(events.encoded(artifact))
    output = tmp_path / "projected.json"
    cli.main(["convert-collector", "--artifact", str(source), "--source-key", SOURCE, "--out", str(output),
              "--collector-projection-version", buildout_projection.VERSION])
    wrapped = json.loads(output.read_bytes())
    plan = events.plan(wrapped, source_key=SOURCE)
    events.validate_content_policy(plan)
    assert len(plan["events"]) == 81
    reconstructed = collector.reconstruct(wrapped)
    restored = copy.deepcopy(reconstructed)
    for i in range(80):
        assert restored["listings"][i]["brochures"][0]["url"] is None
        restored["listings"][i]["brochures"][0]["url"] = artifact["listings"][i]["brochures"][0]["url"]
    assert restored == artifact
    assert len(wrapped["provenance"]["projection"]["withheld"]) == 80
    assert not document_candidates.discover(plan)["candidates"]
    assert b"opaque-fixture" not in events.encoded(wrapped)
    assert source.read_bytes() == events.encoded(artifact)
    # Omitting the opt-in preserves the original private conversion, and its
    # credential-bearing payload still cannot enter the evidence store.
    unprojected = collector.convert(artifact, source_key=SOURCE)
    with pytest.raises(ValueError):
        events.validate_content_policy(events.plan(unprojected, source_key=SOURCE))


def test_automatic_projection_replay_uses_only_projected_identity_and_trusted_receipt(tmp_path):
    config = configured(tmp_path)
    artifact = source_artifact()
    run = tmp_path / "first"
    state = sealed(run, config, artifact)
    assert state["state"] == "pending"
    job = automatic.prepare_collector_job(run, config, "generation-1", SOURCE, 1, state)
    payload = (run / state["path"]).read_bytes()
    assert b"opaque-fixture" not in payload
    assert state["artifact_sha256"] != events.sha256(events.encoded(artifact))
    changed = copy.deepcopy(artifact)
    for row in changed["listings"]:
        row["brochures"][0]["url"] = row["brochures"][0]["url"].replace("opaque-fixture", "different-fixture")
    other = sealed(tmp_path / "second", config, changed)
    assert other["capture_id"] == state["capture_id"]
    assert other["artifact_sha256"] == state["artifact_sha256"]
    assert job
    # A self-consistent lossy receipt is not independent authority: changing
    # even only its asserted withholding list invalidates the trusted seal.
    path = run / state["input_path"]
    wrapped = json.loads(path.read_bytes())
    wrapped["provenance"]["projection"]["withheld"].pop()
    path.write_bytes(events.encoded(wrapped))
    with pytest.raises(ValueError, match="input changed"):
        automatic.prepare_collector_job(run, config, "generation-1", SOURCE, 1, state)


@pytest.mark.parametrize("unknown", ["extra_field", "unknown_url", "unknown_path"])
def test_unreviewed_sensitive_content_still_refuses_automatic_admission(tmp_path, unknown):
    config = configured(tmp_path)
    artifact = source_artifact()
    row = artifact["listings"][0]
    if unknown == "extra_field":
        row["private_api_key"] = "synthetic-only"
    elif unknown == "unknown_url":
        row["brochures"][0]["url"] = "https://other.example.test/docs/fixture/1?token=synthetic-only"
    else:
        row["other_reference"] = row["brochures"][0]["url"]
    state = sealed(tmp_path / "refused", config, artifact)
    assert state == {"state": "refused", "reason": "ValueError"}


def test_projection_version_cannot_cross_source_or_representation_scope():
    assert collector_projection.supported(SOURCE, buildout_projection.VERSION)
    assert not collector_projection.supported("transwestern", buildout_projection.VERSION)
    artifact = source_artifact()
    _, _, receipt = buildout_projection.prepare(events.encoded(artifact), source_key=SOURCE, representation="native")
    with pytest.raises(ValueError):
        collector.convert(artifact, source_key=SOURCE, projection=receipt)
