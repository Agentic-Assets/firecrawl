import copy
import json

import pytest

from source_evidence import collector, collector_projection, events, map_projection
from source_evidence.automatic import _refuse_credential_fields
from source_evidence.projections import leibsohn as projection

PDF = "https://buildout.com/docs/example/42?token=SYNTHETIC"
MAP = "https://maps.googleapis.com/maps/vt?x=1&key=SYNTHETIC&token=123"


def artifact():
    observation = {"input": {"context": {"originalContext": {"nativeContext": {"pdf_url": PDF, "amount": 0.0}},
                                          "replay": {"sdkCaptureSha256": "a" * 64, "sourceCollectorSha": "c" * 40}}},
                   "evidence": {"pageContext": {"images": [{"src": MAP, "alt": "Street view"}],
                                                 "tables": {"provenance": {"inputSha256": "b" * 64}}},
                                "scrapedDoc": {"markdown": f"Rent 20 USD. ![Street view]({MAP})",
                                               "images": [MAP], "metadata": {"quokka-content-current-token": "SYNTHETIC"}}}}
    return {"runMeta": {"mode": "observe", "evidenceOnly": True, "finishedAt": "2026-10-29T01:00:00Z"},
            "sources": [{"sourceKey": "leibsohn", "retainedObservations": [observation]}],
            "listings": [], "brokers": [], "totalListings": 0}


def test_projection_preserves_text_positions_and_explicit_withholding():
    original = artifact()
    before = copy.deepcopy(original)
    result, receipt = projection.project(original, source_key="leibsohn")
    assert original == before
    obs = result["sources"][0]["retainedObservations"][0]
    assert obs["input"]["context"]["originalContext"]["nativeContext"] == {"pdf_url": None, "amount": 0.0}
    assert obs["input"]["context"]["replay"]["sourceCollectorSha"] == "c" * 40
    assert "Rent 20 USD. ![Street view]" in obs["evidence"]["scrapedDoc"]["markdown"]
    assert len(receipt["withheld"]) == 4 and len(receipt["map"]["replacements"]) == 3
    assert "SYNTHETIC" not in repr((result, receipt))
    projection.validate(result, receipt, source_key="leibsohn")
    again, repeat = projection.project(result, source_key="leibsohn")
    assert again == result and not repeat["withheld"] and not repeat["map"]["replacements"]
    _refuse_credential_fields(result)
    wrapped = collector.convert(result, source_key="leibsohn", projection=receipt)
    assert collector.reconstruct(wrapped) == result
    events.validate_content_policy(events.plan(wrapped, source_key="leibsohn"))


def test_missing_null_and_token_only_mutations():
    original = artifact()
    replacement = json.loads(json.dumps(original).replace(MAP, MAP.replace("SYNTHETIC", "OTHER").replace("token=123", "token=456")))
    obs = replacement["sources"][0]["retainedObservations"][0]
    ctx = obs["input"]["context"]
    ctx["originalContext"]["nativeContext"]["pdf_url"] = PDF.replace("SYNTHETIC", "OTHER")
    ctx["replay"]["sdkCaptureSha256"] = "d" * 64
    obs["evidence"]["pageContext"]["tables"]["provenance"]["inputSha256"] = "e" * 64
    obs["evidence"]["scrapedDoc"]["metadata"]["quokka-content-current-token"] = "OTHER"
    assert projection.project(original, source_key="leibsohn") == projection.project(replacement, source_key="leibsohn")
    rows = original["sources"][0]["retainedObservations"]
    rows.extend([{"input": {"context": {"originalContext": {"nativeContext": {"pdf_url": None}}}}},
                 {"input": {"context": {"originalContext": {"nativeContext": {}}}}}])
    result, receipt = projection.project(original, source_key="leibsohn")
    assert result["sources"][0]["retainedObservations"][1:] == rows[1:]
    assert len(receipt["withheld"]) == 4


def test_exceptional_numbers_and_forged_receipts_refuse():
    raw = json.dumps(artifact()).replace('"amount": 0.0', '"amount": 1e999').encode()
    result, literals, receipt = projection.prepare(raw, source_key="leibsohn")
    assert len(literals) == 1
    projection.validate(result, receipt, source_key="leibsohn", numeric_literals=literals)
    for mutate in (lambda r: r["withheld"].reverse(), lambda r: r["withheld"][0].update(pointer="/forged"),
                   lambda r: r.update(projectedCanonicalSha256="0" * 64)):
        forged = copy.deepcopy(receipt); mutate(forged)
        with pytest.raises(ValueError):
            projection.validate(result, forged, source_key="leibsohn", numeric_literals=literals)
    pointer = receipt["withheld"][0]["pointer"]
    with pytest.raises(ValueError):
        projection.validate(result, receipt, source_key="leibsohn", numeric_literals={pointer: "1e999"})


def test_unknown_credential_url_and_source_scope_refuse():
    original = artifact()
    original["sources"][0]["retainedObservations"][0]["evidence"]["other"] = "https://elsewhere.test/file?token=SYNTHETIC"
    result, _ = projection.project(original, source_key="leibsohn")
    with pytest.raises(ValueError):
        _refuse_credential_fields(result)
    with pytest.raises(ValueError):
        projection.project(original, source_key="svn")
    assert not collector_projection.supported("leibsohn", map_projection.VERSION)




def test_unknown_map_variant_and_standalone_map_opt_in_refuse():
    original = artifact()
    row = original["sources"][0]["retainedObservations"][0]
    row["evidence"]["other"] = "https://maps.googleapis.com/other?token=SYNTHETIC"
    with pytest.raises(ValueError):
        projection.project(original, source_key="leibsohn")
    with pytest.raises(ValueError):
        map_projection.project(original, source_key="leibsohn")
    with pytest.raises(ValueError):
        collector_projection.prepare(events.encoded(original), source_key="leibsohn", version="google-map-tile-query/v1")
    del row["evidence"]["other"]
    native = original["sources"][0]["retainedObservations"][0]["input"]["context"]["originalContext"]["nativeContext"]
    native["pdf_url"] = "https://other.example.test/docs/42?token=SYNTHETIC"
    result, _ = projection.project(original, source_key="leibsohn")
    with pytest.raises(ValueError):
        _refuse_credential_fields(result)


def test_normal_context_and_unknown_values_preserved():
    original = artifact()
    row = original["sources"][0]["retainedObservations"][0]
    context = row["input"]["context"]
    context["nativeContext"] = {"pdf_url": PDF, "amount": 0}
    for value in (0, False, [], "clean document label"):
        other = copy.deepcopy(row)
        other["input"]["context"]["nativeContext"]["pdf_url"] = value
        original["sources"][0]["retainedObservations"].append(other)
    original["sources"][0]["retainedObservations"].append({"input": {"context": "unstructured context"}})
    output, receipt = projection.project(original, source_key="leibsohn")
    rows = output["sources"][0]["retainedObservations"]
    assert rows[0]["input"]["context"]["nativeContext"]["pdf_url"] is None
    assert [r["input"]["context"]["nativeContext"]["pdf_url"] for r in rows[1:5]] == [0, False, [], "clean document label"]
    assert rows[5]["input"]["context"] == "unstructured context"
    projection.validate(output, receipt, source_key="leibsohn")


@pytest.mark.parametrize("query", ["x=1&amp;key=SYNTHETIC", "x=1&#38;key=SYNTHETIC",
    "x=1&#x26;key=SYNTHETIC", "x=1&%6bey=SYNTHETIC", "x=1&amp;amp;key=SYNTHETIC",
    "x=1%2526key%253DSYNTHETIC", "x=1&KEY=SYNTHETIC", "x=1&ke%y=SYNTHETIC"])
def test_ambiguous_google_map_credentials_fail_closed(query):
    original = artifact()
    original["sources"][0]["retainedObservations"][0]["evidence"]["scrapedDoc"]["rawHtml"] = (
        '<img src="https://maps.googleapis.com/maps/vt?' + query + '">'
    )
    with pytest.raises(ValueError):
        projection.project(original, source_key="leibsohn")


@pytest.mark.parametrize("url", [
    "http://maps.googleapis.com/maps/vt?key=SYNTHETIC",
    "https://MAPS.GOOGLEAPIS.COM/maps/vt?key=SYNTHETIC",
    "https://maps.googleapis.com./maps/vt?key=SYNTHETIC",
    "https://maps.googleapis.com:443/maps/vt?key=SYNTHETIC",
    "https://maps.googleapis.com/maps/vt?key=SYNTHETIC#fragment",
    "https://maps.googleapis.com/maps/vt#key=SYNTHETIC",
    "https://maps.googleapis.com/maps/vt#token=SYNTHETIC",
    "https://maps.googleapis.com/maps/vt#route?key=SYNTHETIC",
    "https://maps.googleapis.com/maps/vt#route%3Fkey%3DSYNTHETIC",
    "https://maps.googleapis.com/other?key=SYNTHETIC",
    "https%3A%2F%2Fmaps.googleapis.com%2Fmaps%2Fvt%3Fkey%3DSYNTHETIC",
])
def test_unsupported_credential_map_url_fails_at_composite_admission(url):
    original = artifact()
    original["sources"][0]["retainedObservations"][0]["evidence"]["scrapedDoc"]["rawHtml"] = url
    with pytest.raises(ValueError):
        projection.project(original, source_key="leibsohn")


def test_clean_encoded_map_references_remain_unchanged():
    original = artifact()
    row = original["sources"][0]["retainedObservations"][0]
    value = '<a href="https://maps.googleapis.com/maps/vt?q=Main%20Street&amp;zoom=12">Map</a>'
    row["evidence"]["scrapedDoc"]["rawHtml"] = value
    output, receipt = projection.project(original, source_key="leibsohn")
    assert output["sources"][0]["retainedObservations"][0]["evidence"]["scrapedDoc"]["rawHtml"] == value
    projection.validate(output, receipt, source_key="leibsohn")
    _refuse_credential_fields(output)
    wrapped = collector.convert(output, source_key="leibsohn", projection=receipt)
    events.validate_content_policy(events.plan(wrapped, source_key="leibsohn"))

def test_automatic_seal_replay_and_tampered_receipt(tmp_path):
    from test_automatic import SHA, setup_policy

    from source_evidence import automatic

    config, policy, policy_path = setup_policy(tmp_path, sources=("leibsohn",), count=10)
    policy.update(schema_version=automatic.SCHEMA_V2,
                  collector_projections={"leibsohn": projection.VERSION},
                  native_source_formats={}, max_native_pages=128)
    raw = events.encoded(policy)
    policy_path.write_bytes(raw)
    config = automatic.configuration(str(policy_path), events.sha256(raw), config["env_file"],
                                     config["target_sha256"], SHA, ["leibsohn"])
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    source = run / "original.json"
    source.write_bytes(events.encoded(artifact()))
    source.chmod(0o600)
    state = automatic.seal_collector(run, config, ["leibsohn"], SHA, False, "generation-1", "leibsohn", 1, source)
    blocked = artifact()
    blocked["sources"][0]["retainedObservations"][0]["evidence"]["scrapedDoc"]["rawHtml"] = (
        '<img src="https://maps.googleapis.com/maps/vt?x=1&amp;key=SYNTHETIC">'
    )
    blocked_run = tmp_path / "blocked"
    blocked_run.mkdir(mode=0o700)
    blocked_source = blocked_run / "original.json"
    blocked_source.write_bytes(events.encoded(blocked))
    blocked_source.chmod(0o600)
    refused = automatic.seal_collector(blocked_run, config, ["leibsohn"], SHA, False, "generation-1", "leibsohn", 1, blocked_source)
    assert refused == {"state": "refused", "reason": "ValueError"}
    assert state["state"] == "pending"
    assert automatic.prepare_collector_job(run, config, "generation-1", "leibsohn", 1, state)
    stored = run / state["input_path"]
    assert b"SYNTHETIC" not in stored.read_bytes()
    wrapped = json.loads(stored.read_bytes())
    wrapped["provenance"]["projection"]["withheld"].pop()
    stored.write_bytes(events.encoded(wrapped))
    with pytest.raises(ValueError, match="input changed"):
        automatic.prepare_collector_job(run, config, "generation-1", "leibsohn", 1, state)


def test_credential_map_preserves_encoded_noncredential_query():
    original = artifact()
    row = original["sources"][0]["retainedObservations"][0]
    row["evidence"]["scrapedDoc"]["rawHtml"] = "https://maps.googleapis.com/maps/vt?q=Main%20Street&key=SYNTHETIC"
    changed, _ = projection.project(original, source_key="leibsohn")
    assert changed["sources"][0]["retainedObservations"][0]["evidence"]["scrapedDoc"]["rawHtml"] == "https://maps.googleapis.com/maps/vt?q=Main%20Street"
