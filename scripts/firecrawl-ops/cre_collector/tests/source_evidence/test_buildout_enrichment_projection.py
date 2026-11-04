"""Targeted enrichment projection preserves evidence and existing gates."""

import copy
import json

import pytest
from source_evidence import collector, collector_projection, events
from source_evidence.automatic import _refuse_credential_fields
from source_evidence.projections import buildout_enrichment as projection
from source_evidence.projections import buildout_page
from test_leibsohn_observation_projection import artifact

SOURCES = tuple(projection.VERSIONS)


def sample(source):
    original = artifact()
    original["runMeta"]["mode"] = "enrich"
    original["runMeta"].pop("evidenceOnly")
    original["sources"][0]["sourceKey"] = source
    old = original["sources"][0]["retainedObservations"][0]["evidence"]
    old["claim"] = {"sourceKey": source, "externalId": "identity", "url": "https://example.invalid/p"}
    old["qualification"] = "usable_detail"
    old["sourceObservation"] = {"partial": True, "contacts": [{"ordinal": 0, "name": "Synthetic Agent"}], "sections": [{"label": "Rent", "text": "25 USD/SF"}]}
    old["pageContext"]["metadata"] = [
        {"name": "quokka-content-current-token", "content": "SYNTHETIC"},
        {"property": "QUOKKA-CONTENT-CURRENT-TOKEN", "content": "SYNTHETIC"},
        {"name": "ordinary", "content": "Retained"},
        {"name": "quokka-content-current-token", "content": None},
    ]
    original["sources"][0]["retainedObservations"] = [old]
    original["listings"] = [{"sourceKey": source, "url": "https://example.invalid/p", "id": "identity",
                             "markdown": old["scrapedDoc"]["markdown"],
                             "photos": old["scrapedDoc"]["images"],
                             "documents": [{"url": "https://example.invalid/a.pdf", "title": "Plain"}],
                             "extraFacts": {"detail_tables": {"provenance": {"inputSha256": "b" * 64}},
                                            "detail_tables_attempt": {"provenance": {"inputSha256": "c" * 64}}}}]
    original["totalListings"] = 1
    return original


@pytest.mark.parametrize("source", SOURCES)
def test_complete_normal_artifact_projection_and_sealed_evidence(source):
    original = sample(source)
    before = copy.deepcopy(original)
    out, literals, receipt = collector_projection.prepare(events.encoded(original), source_key=source, version=projection.VERSIONS[source])
    assert original == before and literals == {}
    assert "SYNTHETIC" not in repr((out, receipt))
    assert out["listings"][0]["documents"] == before["listings"][0]["documents"]
    assert out["sources"][0]["retainedObservations"][0]["sourceObservation"] == before["sources"][0]["retainedObservations"][0]["sourceObservation"]
    assert out["sources"][0]["retainedObservations"][0]["pageContext"]["metadata"][2:] == before["sources"][0]["retainedObservations"][0]["pageContext"]["metadata"][2:]
    assert len(receipt["withheld"]) == 6
    collector_projection.validate(out, receipt, source_key=source)
    _refuse_credential_fields(out)
    wrapped = collector.convert(out, source_key=source, projection=receipt)
    assert wrapped["schema"] == collector.SCHEMA_V2
    assert collector.reconstruct(wrapped) == out
    from cre_artifact_scope import require_canonical_artifact
    for evidence in (out, collector.reconstruct(wrapped)):
        with pytest.raises(ValueError, match="private source observations"):
            require_canonical_artifact(evidence)
    assert receipt["scopeGuardAdded"] is True
    events.validate_content_policy(events.plan(wrapped, source_key=source))


@pytest.mark.parametrize("source", SOURCES)
def test_source_mode_and_receipt_are_strictly_bound(source):
    other = next(x for x in SOURCES if x != source)
    doc = sample(source)
    version = projection.VERSIONS[source]
    assert not collector_projection.supported(other, version)
    assert not collector_projection.supported(source, buildout_page.VERSIONS[other])
    for mutate in (lambda x: x["runMeta"].update(mode="observe"),
                   lambda x: x["runMeta"].update(evidenceOnly=True),
                   lambda x: x["sources"][0].update(sourceKey=other),
                   lambda x: x["listings"][0].update(sourceKey=other),
                   lambda x: x["sources"][0]["retainedObservations"][0]["claim"].update(sourceKey=other),
                   lambda x: x.update(totalListings=2)):
        value = copy.deepcopy(doc)
        mutate(value)
        with pytest.raises(ValueError):
            collector_projection.prepare(events.encoded(value), source_key=source, version=version)
    out, literals, receipt = collector_projection.prepare(events.encoded(doc), source_key=source, version=version)
    for mutate in (lambda x: x.update(sourceKey=other), lambda x: x.update(version=projection.VERSIONS[other]),
                   lambda x: x["withheld"].reverse(), lambda x: x["withheld"][0].update(pointer="/wrong"),
                   lambda x: x["map"].update(sourceKey=other)):
        bad = copy.deepcopy(receipt)
        mutate(bad)
        with pytest.raises(ValueError):
            collector_projection.validate(out, bad, source_key=source, numeric_literals=literals)


@pytest.mark.parametrize("source", SOURCES)
def test_missing_partial_and_numeric_witness(source):
    original = sample(source)
    original["sources"][0].pop("retainedObservations")
    out, literals, receipt = collector_projection.prepare(events.encoded(original), source_key=source, version=projection.VERSIONS[source])
    assert "retainedObservations" not in out["sources"][0]
    assert collector.reconstruct(collector.convert(out, source_key=source, projection=receipt)) == out
    value = sample(source)
    # Missing or malformed identity context is retained as unqualified data.
    value["sources"][0]["retainedObservations"].append({"claim": {"externalId": None}, "partial": True})
    value["sources"][0]["retainedObservations"].append({"claim": "unparsed", "partial": True})
    value["listings"][0]["amount"] = 0.0
    value["sources"][0]["retainedObservations"][0]["omissions"] = {"sourceObservation": "source_observation_partial"}
    raw = json.dumps(value).replace('"amount": 0.0', '"amount": 1e999').encode()
    out, literals, receipt = collector_projection.prepare(raw, source_key=source, version=projection.VERSIONS[source])
    assert literals and out["sources"][0]["retainedObservations"][0]["omissions"] == value["sources"][0]["retainedObservations"][0]["omissions"]
    collector_projection.validate(out, receipt, source_key=source, numeric_literals=literals)
    with pytest.raises(ValueError):
        collector_projection.validate(out, receipt, source_key=source, numeric_literals={receipt["withheld"][0]["pointer"]: "1e999"})


@pytest.mark.parametrize("source", SOURCES)
def test_unknown_credentials_and_ambiguous_map_still_refuse(source):
    doc = sample(source)
    doc["listings"][0]["other"] = "https://example.invalid/?access_token=SYNTHETIC"
    out, _, receipt = collector_projection.prepare(events.encoded(doc), source_key=source, version=projection.VERSIONS[source])
    with pytest.raises(ValueError):
        _refuse_credential_fields(out)
    with pytest.raises(ValueError):
        events.validate_content_policy(events.plan(collector.convert(out, source_key=source, projection=receipt), source_key=source))
    doc = sample(source)
    doc["listings"][0]["other"] = "https://maps.googleapis.com/maps/vt?x=1&amp;key=SYNTHETIC"
    with pytest.raises(ValueError):
        collector_projection.prepare(events.encoded(doc), source_key=source, version=projection.VERSIONS[source])


@pytest.mark.parametrize("source", SOURCES)
def test_guarded_worker_custody_seal_and_recovery_binding(source, tmp_path, monkeypatch):
    import cre_checkpoint_refresh as checkpoint
    from source_evidence import (
        automatic,
        enrichment_custody,
        guarded_evidence,
        guarded_fence,
    )
    from test_automatic import SHA, setup_policy
    from test_guarded_evidence import Lock

    monkeypatch.setattr(checkpoint, "git_identity", lambda: (SHA, False))
    config, policy, path = setup_policy(tmp_path, sources=(source,), count=10)
    policy.update(schema_version=automatic.SCHEMA_V2,
                  collector_projections={source: projection.VERSIONS[source]},
                  native_source_formats={}, max_native_pages=128)
    encoded = events.encoded(policy)
    path.write_bytes(encoded)
    config = automatic.configuration(str(path), events.sha256(encoded), config["env_file"],
                                     config["target_sha256"], SHA, [source])
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    custody_marker = enrichment_custody.mark(run, source)
    original = run / "enriched.json"
    original.write_bytes(events.encoded(sample(source)))
    original.chmod(0o600)
    claims = run / "claims.json"
    claims.write_text('{"items": []}')
    claims.chmod(0o600)
    receipt = {"tier": "enrich", "source": source, "apply": True, "expected_sha": SHA,
               "local_settlement": {"state": "confirmed"},
               "enrichment_artifact": enrichment_custody.settle(run, custody_marker),
               "source_evidence": {"config": config, "status": "awaiting_worker"}}
    lock = Lock(tmp_path)
    def save():
        checkpoint.atomic_write_text(run / "receipt.json", json.dumps(receipt) + "\n")
    marker = guarded_evidence.seal_only(run, receipt, lock, save)
    assert marker and guarded_fence.read(lock.path) == marker
    state = receipt["source_evidence"]["job"]
    assert state["state"] == "pending"
    assert automatic.prepare_collector_job(run, config, run.name, source, 1, state)
    sealed = run / state["input_path"]
    assert b"SYNTHETIC" not in sealed.read_bytes()
    altered = json.loads(sealed.read_bytes())
    altered["provenance"]["projection"]["withheld"].pop()
    sealed.write_bytes(events.encoded(altered))
    with pytest.raises(ValueError, match="input changed"):
        automatic.prepare_collector_job(run, config, run.name, source, 1, state)
    assert guarded_fence.read(lock.path) == marker


@pytest.mark.parametrize("source", SOURCES)
def test_evidence_only_failed_enrichment_remains_unqualified(source):
    value = sample(source)
    value["listings"] = []
    value["totalListings"] = 0
    value["runMeta"]["evidenceOnly"] = True
    value["sources"][0]["retainedObservations"][0]["qualification"] = "rejected_missing_detail_shell"
    out, _, receipt = collector_projection.prepare(events.encoded(value), source_key=source, version=projection.VERSIONS[source])
    assert out["runMeta"]["mode"] == "enrich" and out["runMeta"]["evidenceOnly"] is True
    assert out["sources"][0]["retainedObservations"][0]["qualification"] == "rejected_missing_detail_shell"
    wrapped = collector.convert(out, source_key=source, projection=receipt)
    assert collector.reconstruct(wrapped) == out
    events.validate_content_policy(events.plan(wrapped, source_key=source))


@pytest.mark.parametrize("source", SOURCES)
def test_existing_evidence_purpose_is_preserved_and_marker_is_bound(source):
    original = sample(source)
    original["runMeta"]["evidencePurpose"] = "private_unqualified_source_observations"
    out, _, receipt = collector_projection.prepare(events.encoded(original), source_key=source, version=projection.VERSIONS[source])
    assert out["runMeta"]["evidencePurpose"] == "private_unqualified_source_observations"
    assert receipt["scopeGuardAdded"] is False
    collector_projection.validate(out, receipt, source_key=source)
    conflicting = sample(source)
    conflicting["runMeta"]["evidencePurpose"] = "other-purpose"
    with pytest.raises(ValueError):
        collector_projection.prepare(events.encoded(conflicting), source_key=source, version=projection.VERSIONS[source])
    receipt["scopeGuardAdded"] = True
    altered = copy.deepcopy(out)
    altered["runMeta"]["evidencePurpose"] = "other-purpose"
    with pytest.raises(ValueError):
        collector_projection.validate(altered, receipt, source_key=source)


@pytest.mark.parametrize("source", SOURCES)
def test_projected_and_exported_artifacts_refuse_canonical_entry_points(source, tmp_path):
    import subprocess
    import sys

    import cre_enrich
    import cre_gate
    import cre_ingest
    import cre_monitor
    from source_evidence import collector_export

    original = sample(source)
    out, _, receipt = collector_projection.prepare(events.encoded(original), source_key=source, version=projection.VERSIONS[source])
    wrapped = collector.convert(out, source_key=source, projection=receipt)
    reconstructed = collector.reconstruct(wrapped)
    payload, _ = collector_export.reconstruct(events.plan(wrapped, source_key=source)["events"])
    exported = json.loads(payload)
    assert reconstructed == exported == out
    claimed = [{"url": out["listings"][0]["url"], "source_key": source}]
    for index, evidence in enumerate((out, reconstructed, exported)):
        path = tmp_path / f"artifact-{index}.json"
        path.write_bytes(events.encoded(evidence))
        with pytest.raises(ValueError, match="private source observations"):
            cre_ingest.validate_strict_artifact_freshness(evidence)
        with pytest.raises(ValueError, match="private source observations"):
            cre_gate.count_artifacts([str(path)], quiet=True)
        with pytest.raises(ValueError, match="private source observations"):
            cre_monitor.load_artifact_groups([str(path)])
        with pytest.raises(ValueError, match="private source observations"):
            cre_monitor.clean_empty_monitor_source_keys([str(path)])
        with pytest.raises(ValueError, match="private source observations"):
            cre_enrich.validate_enriched_artifact(claimed, evidence)
        result = subprocess.run([sys.executable, "cre_ingest.py", "--in", str(path), "--dry-run"],
                                capture_output=True, text=True, check=False)
        assert result.returncode != 0 and "private source observations" in result.stderr
    # Original canonical-source metadata remains untouched by projection.
    assert "evidencePurpose" not in original["runMeta"]
    assert len(cre_enrich.validate_enriched_artifact(claimed, original)) == 1
