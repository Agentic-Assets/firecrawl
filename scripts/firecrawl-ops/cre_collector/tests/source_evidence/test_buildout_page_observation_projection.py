"""Explicit source profiles retain useful data while withholding observed credentials."""

import copy
import json

import pytest
from source_evidence import collector, collector_projection, events, map_projection
from source_evidence.automatic import _refuse_credential_fields
from source_evidence.projections import buildout_page, leibsohn
from test_leibsohn_observation_projection import artifact

SOURCES = ("cawley-chicago", "bradford-allen")


def source_artifact(source):
    result = artifact()
    result["sources"][0]["sourceKey"] = source
    observation = result["sources"][0]["retainedObservations"][0]
    observation["evidence"]["sourceObservation"] = {
        "sections": [{"label": "Terms", "text": "Rent 25 USD per SF", "links": ["/plain"]}],
        "contacts": [{"ordinal": 0, "name": "Synthetic"}, {"ordinal": 1, "name": None}],
        "partial": True,
    }
    observation["evidence"]["pageContext"]["metadata"] = [
        {"name": "quokka-content-current-token", "content": "SYNTHETIC"},
        {"property": "QUOKKA-CONTENT-CURRENT-TOKEN", "content": "SYNTHETIC"},
        {"name": "ordinary", "content": "Retained"},
        {"name": "quokka-content-current-token", "content": None},
    ]
    return result


@pytest.mark.parametrize("source", SOURCES)
def test_explicit_profile_round_trip_retains_fields_and_scanners(source):
    original = source_artifact(source)
    before = copy.deepcopy(original)
    projected, literals, receipt = collector_projection.prepare(
        events.encoded(original), source_key=source, version=buildout_page.VERSIONS[source]
    )
    assert original == before and literals == {}
    assert receipt["version"] == buildout_page.VERSIONS[source]
    assert receipt["map"]["version"] == buildout_page.MAP_VERSIONS[source]
    assert "SYNTHETIC" not in repr((projected, receipt))
    row = projected["sources"][0]["retainedObservations"][0]
    assert row["evidence"]["sourceObservation"] == before["sources"][0]["retainedObservations"][0]["evidence"]["sourceObservation"]
    assert row["evidence"]["pageContext"]["metadata"][2]["content"] == "Retained"
    assert len([entry for entry in receipt["withheld"] if entry["reason"] == "token_named_page_metadata"]) == 2
    collector_projection.validate(projected, receipt, source_key=source)
    _refuse_credential_fields(projected)
    wrapped = collector.convert(projected, source_key=source, projection=receipt)
    assert collector.reconstruct(wrapped) == projected
    events.validate_content_policy(events.plan(wrapped, source_key=source))


@pytest.mark.parametrize("source", SOURCES)
def test_source_version_scope_and_receipt_cross_binding(source):
    other = SOURCES[1] if source == SOURCES[0] else SOURCES[0]
    original = source_artifact(source)
    version = buildout_page.VERSIONS[source]
    assert collector_projection.supported(source, version)
    assert not collector_projection.supported(other, version)
    assert not collector_projection.supported(source, leibsohn.VERSION)
    assert not collector_projection.supported(source, map_projection.VERSION)
    with pytest.raises(ValueError):
        collector_projection.prepare(events.encoded(original), source_key=source, version=buildout_page.VERSIONS[other])
    with pytest.raises(ValueError):
        leibsohn.project(original, source_key=source)
    for mutate in (lambda x: x["runMeta"].update(mode="enrich"),
                   lambda x: x.update(listings=[{"id": "synthetic"}]),
                   lambda x: x["sources"][0].update(sourceKey=other)):
        bad = copy.deepcopy(original)
        mutate(bad)
        with pytest.raises(ValueError):
            collector_projection.prepare(events.encoded(bad), source_key=source, version=version)
    projected, literals, receipt = collector_projection.prepare(events.encoded(original), source_key=source, version=version)
    for mutate in (lambda r: r.update(sourceKey=other), lambda r: r.update(version=buildout_page.VERSIONS[other]),
                   lambda r: r["map"].update(version=buildout_page.MAP_VERSIONS[other]),
                   lambda r: r["withheld"][0].update(pointer="/wrong")):
        bad = copy.deepcopy(receipt)
        mutate(bad)
        with pytest.raises(ValueError):
            collector_projection.validate(projected, bad, source_key=source, numeric_literals=literals)


@pytest.mark.parametrize("source", SOURCES)
def test_numeric_witness_and_unknown_credentials_remain_guarded(source):
    raw = json.dumps(source_artifact(source)).replace('"amount": 0.0', '"amount": 1e999').encode()
    projected, literals, receipt = collector_projection.prepare(raw, source_key=source, version=buildout_page.VERSIONS[source])
    assert literals
    collector_projection.validate(projected, receipt, source_key=source, numeric_literals=literals)
    collision = {receipt["withheld"][0]["pointer"]: "1e999"}
    with pytest.raises(ValueError):
        collector_projection.validate(projected, receipt, source_key=source, numeric_literals=collision)
    original = source_artifact(source)
    original["sources"][0]["retainedObservations"][0]["evidence"]["extra"] = "https://example.invalid/?access_token=SYNTHETIC"
    projected, _, receipt = collector_projection.prepare(events.encoded(original), source_key=source, version=buildout_page.VERSIONS[source])
    with pytest.raises(ValueError):
        _refuse_credential_fields(projected)
    with pytest.raises(ValueError):
        events.validate_content_policy(events.plan(collector.convert(projected, source_key=source, projection=receipt), source_key=source))


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("value", [
    'https://maps.googleapis.com/maps/vt?x=1&amp;key=SYNTHETIC',
    'https://MAPS.GOOGLEAPIS.COM/maps/vt?key=SYNTHETIC',
    'https://maps.googleapis.com/maps/vt#route?key=SYNTHETIC',
])
def test_ambiguous_known_map_credentials_refuse(source, value):
    original = source_artifact(source)
    original["sources"][0]["retainedObservations"][0]["evidence"]["extra"] = value
    with pytest.raises(ValueError):
        collector_projection.prepare(events.encoded(original), source_key=source, version=buildout_page.VERSIONS[source])


@pytest.mark.parametrize("source", SOURCES)
def test_missing_null_metadata_and_literal_clean_references_are_conserved(source):
    original = source_artifact(source)
    row = original["sources"][0]["retainedObservations"][0]
    metadata = row["evidence"]["pageContext"]["metadata"]
    metadata.append({"name": "quokka-content-current-token"})
    row["evidence"]["extra"] = 'https://maps.googleapis.com/maps/vt?q=Main%20Street&amp;zoom=3'
    projected, _, receipt = collector_projection.prepare(
        events.encoded(original), source_key=source, version=buildout_page.VERSIONS[source]
    )
    selected = projected["sources"][0]["retainedObservations"][0]
    assert selected["evidence"]["extra"] == row["evidence"]["extra"]
    assert selected["evidence"]["pageContext"]["metadata"][-2:] == metadata[-2:]
    assert not any(entry["pointer"].endswith("/metadata/3/content") or entry["pointer"].endswith("/metadata/4/content")
                   for entry in receipt["withheld"])
    with pytest.raises(ValueError):
        map_projection.project(original, source_key=source)
    with pytest.raises(ValueError):
        map_projection.project(original, source_key=source, _observation_version=buildout_page.MAP_VERSIONS[SOURCES[1] if source == SOURCES[0] else SOURCES[0]])


@pytest.mark.parametrize("source", SOURCES)
def test_automatic_seal_and_receipt_tamper_fail_closed(source, tmp_path):
    from source_evidence import automatic
    from test_automatic import SHA, setup_policy

    config, policy, path = setup_policy(tmp_path, sources=(source,), count=10)
    policy.update(schema_version=automatic.SCHEMA_V2,
                  collector_projections={source: buildout_page.VERSIONS[source]},
                  native_source_formats={}, max_native_pages=128)
    raw = events.encoded(policy)
    path.write_bytes(raw)
    config = automatic.configuration(str(path), events.sha256(raw), config["env_file"],
                                     config["target_sha256"], SHA, [source])
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    original = run / "original.json"
    original.write_bytes(events.encoded(source_artifact(source)))
    original.chmod(0o600)
    state = automatic.seal_collector(run, config, [source], SHA, False, "generation-1", source, 1, original)
    assert state["state"] == "pending"
    assert automatic.prepare_collector_job(run, config, "generation-1", source, 1, state)
    stored = run / state["input_path"]
    assert b"SYNTHETIC" not in stored.read_bytes()
    wrapped = json.loads(stored.read_bytes())
    wrapped["provenance"]["projection"]["withheld"].pop()
    stored.write_bytes(events.encoded(wrapped))
    with pytest.raises(ValueError, match="input changed"):
        automatic.prepare_collector_job(run, config, "generation-1", source, 1, state)
