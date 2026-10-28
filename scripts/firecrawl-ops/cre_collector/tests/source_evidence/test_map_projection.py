import copy

import pytest
from source_evidence.map_projection import project


def test_projects_only_map_credentials_and_preserves_context():
    url = "https://maps.googleapis.com/maps/vt?pb=coordinates%21x&key=KEY&token=123"
    artifact = {"listings": [{"photos": [url], "markdown": f"![Map]({url}) nearby",
                               "table": [["NOI", "$500"]]}], "errors": [None]}
    before = copy.deepcopy(artifact)
    projected, receipt = project(artifact, source_key="faris-lee")
    safe = "https://maps.googleapis.com/maps/vt?pb=coordinates%21x"
    assert artifact == before
    assert projected["listings"][0]["photos"] == [safe]
    assert projected["listings"][0]["markdown"] == f"![Map]({safe}) nearby"
    assert projected["listings"][0]["table"] == before["listings"][0]["table"]
    assert projected["errors"] == [None]
    assert [r["pointer"] for r in receipt["replacements"]] == ["/listings/0/photos/0", "/listings/0/markdown"]
    assert all(r["removedParameters"] == ["key", "token"] for r in receipt["replacements"])
    assert "KEY" not in repr((projected, receipt))


@pytest.mark.parametrize("url", [
    "https://maps.googleapis.com/maps/vt?pb=x&%6bey=secret",
    "https://maps.googleapis.com/maps/vt?pb=x&KEY=secret",
    "https://maps.googleapis.com/maps/vt?pb=x&token=abc%xx",
    "https://maps.googleapis.com:443/maps/vt?key=secret",
    "https://maps.googleapis.com/maps/vt?token=secret#fragment",
])
def test_ambiguous_maps_are_refused(url):
    with pytest.raises(ValueError):
        project({"value": url}, source_key="faris-lee")


def test_other_sources_and_hosts_get_no_bypass():
    with pytest.raises(ValueError):
        project({}, source_key="transwestern")
    value = "https://other.example/path?token=secret"
    output, receipt = project({"x": value}, source_key="faris-lee")
    assert output == {"x": value}
    assert not receipt["replacements"]


@pytest.mark.parametrize("source", ["cushman-wakefield", "svn"])
def test_observed_shared_map_sources_preserve_facts_and_bind_projection(source):
    from source_evidence.map_projection import prepare, validate
    from source_evidence import events

    original = {"labels": [["Lease rate", "$12 / SF / year"]],
                "links": ["https://maps.googleapis.com/maps/vt?pb=position&key=MAPKEY&token=123"]}
    output, literals, receipt = prepare(events.encoded(original), source_key=source)
    validate(output, receipt, source_key=source, numeric_literals=literals or None)
    assert output["labels"] == original["labels"]
    assert output["links"] == ["https://maps.googleapis.com/maps/vt?pb=position"]
    assert receipt["version"] == "google-map-tile-query/v1"
    assert receipt["sourceKey"] == source
    assert "MAPKEY" not in repr((output, receipt))
    other = "svn" if source == "cushman-wakefield" else "cushman-wakefield"
    with pytest.raises(ValueError):
        validate(output, receipt, source_key=other)


def test_new_map_projection_keeps_non_tile_urls_for_ordinary_policy_checks():
    from source_evidence.map_projection import prepare, validate
    from source_evidence import events

    unchanged = "https://maps.googleapis.com/maps-api-v3/api/js/66/map.js"
    original = {"text": unchanged + " https://maps.googleapis.com/maps/vt?pb=x&key=MAPKEY&token=123"}
    output, literals, receipt = prepare(events.encoded(original), source_key="cushman-wakefield")
    validate(output, receipt, source_key="cushman-wakefield", numeric_literals=literals or None)
    assert unchanged in output["text"]
    bad = "https://maps.googleapis.com/maps/api/js?token=secret"
    output, _ = project({"text": bad}, source_key="cushman-wakefield")
    assert output["text"] == bad
    with pytest.raises(ValueError):
        events.http_url(output["text"])


def test_pointer_escaping_repeated_occurrences_and_idempotence():
    url = "https://maps.googleapis.com/maps/vt?pb=p&token=123&key=abc"
    output, receipt = project({"a/b~c": f"{url} {url}"}, source_key="faris-lee")
    assert [r["pointer"] for r in receipt["replacements"]] == ["/a~1b~0c", "/a~1b~0c"]
    assert [r["occurrence"] for r in receipt["replacements"]] == [0, 1]
    assert all("originalUrlSha256" not in r for r in receipt["replacements"])
    again, second_receipt = project(output, source_key="faris-lee")
    assert again == output
    assert second_receipt["replacements"] == []


def test_other_sensitive_content_still_fails_ordinary_admission():
    from source_evidence.automatic import _refuse_credential_fields

    artifact = {"map": "https://maps.googleapis.com/maps/vt?pb=p&key=abc&token=123",
                "other": "https://elsewhere.example/?token=secret"}
    output, _ = project(artifact, source_key="faris-lee")
    with pytest.raises(ValueError):
        _refuse_credential_fields(output)


def test_prepared_receipt_revalidates_and_tampering_refuses():
    import json

    from source_evidence import collector, events, map_projection

    raw = json.dumps({"sources": [{"sourceKey": "faris-lee"}],
                      "runMeta": {"finishedAt": "2026-10-28T12:00:00Z"},
                      "listings": [{"sourceKey": "faris-lee", "photos": [
                          "https://maps.googleapis.com/maps/vt?pb=x&key=abc&token=123"]}]}).encode()
    doc, literals, receipt = map_projection.prepare(raw, source_key="faris-lee")
    wrapped = collector.convert(doc, source_key="faris-lee", numeric_literals=literals or None, projection=receipt)
    assert wrapped["provenance"]["originalArtifactHashKind"] == "projected_canonical_json_utf8"
    assert wrapped["provenance"]["originalSourceHashesVerification"] == "omitted_due_to_projection"
    assert "originalRawSha256" not in repr(receipt)
    assert "originalCanonicalSha256" not in repr(receipt)
    plan = events.plan(wrapped, source_key="faris-lee")
    assert plan["events"][0]["facts"][0]["value"]["provenance"]["projection"] == receipt
    for field, value in (("projectedUrlSha256", "0" * 64), ("pointer", "/listings/9/photos/0"),
                         ("occurrence", 1), ("removedParameters", ["other"])):
        changed = copy.deepcopy(receipt)
        changed["replacements"][0][field] = value
        with pytest.raises(ValueError):
            collector.convert(doc, source_key="faris-lee", projection=changed)
    changed = copy.deepcopy(doc)
    changed["listings"][0]["other"] = "tampered"
    with pytest.raises(ValueError):
        collector.convert(changed, source_key="faris-lee", projection=receipt)


def test_projected_cli_roundtrip_and_opt_in(tmp_path):
    import json

    from source_evidence import cli, events

    artifact = {"sources": [{"sourceKey": "faris-lee"}],
                "runMeta": {"finishedAt": "2026-10-28T12:00:00Z"},
                "listings": [{"sourceKey": "faris-lee", "photos": [
                    "https://maps.googleapis.com/maps/vt?pb=x&key=abc&token=123"]}]}
    source, output = tmp_path / "input.json", tmp_path / "out.json"
    source.write_text(json.dumps(artifact))
    cli.main(["convert-collector", "--artifact", str(source), "--source-key", "faris-lee",
              "--out", str(output), "--project-map-credentials"])
    value = json.loads(output.read_text())
    assert "token=123" not in output.read_text()
    events.validate_content_policy(events.plan(value, source_key="faris-lee"))
    with pytest.raises(ValueError):
        cli.main(["convert-collector", "--artifact", str(source), "--source-key", "svn",
                  "--out", str(tmp_path / "other.json"), "--project-map-credentials"])


def test_projection_retains_exceptional_numeric_literals():
    from source_evidence import collector, events, map_projection

    raw = (b'{"sources":[{"sourceKey":"faris-lee"}],'
           b'"runMeta":{"finishedAt":"2026-10-28T12:00:00Z"},'
           b'"listings":[{"sourceKey":"faris-lee","fact":0.10000000000000000001,'
           b'"photo":"https://maps.googleapis.com/maps/vt?pb=x&key=abc&token=123"}]}')
    document, literals, receipt = map_projection.prepare(raw, source_key="faris-lee")
    assert literals["/listings/0/fact"] == "0.10000000000000000001"
    wrapped = collector.convert(document, source_key="faris-lee", numeric_literals=literals, projection=receipt)
    assert wrapped["provenance"]["originalArtifactHashKind"] == "projected_canonical_json_exact_numeric_literals_utf8"
    events.validate_content_policy(events.plan(wrapped, source_key="faris-lee"))
    with pytest.raises(ValueError):
        collector.convert(document, source_key="faris-lee", numeric_literals={}, projection=receipt)


@pytest.mark.parametrize("suffix", [")", "]", "}", ".", ",", ")],"])
def test_url_punctuation_boundaries_validate(suffix):
    import json

    from source_evidence import map_projection

    url = "https://maps.googleapis.com/maps/vt?pb=x&key=abc&token=123"
    doc, literals, receipt = map_projection.prepare(json.dumps({"text": url + suffix}).encode(), source_key="faris-lee")
    assert doc["text"].endswith(suffix)
    map_projection.validate(doc, receipt, source_key="faris-lee", numeric_literals=literals or None)


def test_projection_pointer_and_aggregate_receipt_bounds(monkeypatch):
    from source_evidence import map_projection

    url = "https://maps.googleapis.com/maps/vt?pb=x&key=abc&token=123"
    with pytest.raises(ValueError, match="pointer"):
        project({"é" * 3000: url}, source_key="faris-lee")
    with pytest.raises(ValueError, match="pointer"):
        project({"x" * 50000: " ".join([url] * 500)}, source_key="faris-lee")
    monkeypatch.setattr(map_projection, "MAX_RECEIPT_BYTES", 1000)
    with pytest.raises(ValueError, match="receipt"):
        project({"x": " ".join([url] * 10)}, source_key="faris-lee")


def test_untrusted_receipt_byte_bound(monkeypatch):
    import json

    from source_evidence import map_projection

    url = "https://maps.googleapis.com/maps/vt?pb=x&key=abc&token=123"
    doc, literals, receipt = map_projection.prepare(json.dumps({"x": " ".join([url] * 10)}).encode(), source_key="faris-lee")
    monkeypatch.setattr(map_projection, "MAX_RECEIPT_BYTES", 1000)
    with pytest.raises(ValueError, match="receipt"):
        map_projection.validate(doc, receipt, source_key="faris-lee", numeric_literals=literals or None)


def test_projection_receipt_does_not_commit_to_removed_values():
    from source_evidence import map_projection

    first = b'{"map":"https://maps.googleapis.com/maps/vt?pb=x&key=abc&token=12"}'
    second = b'{"map":"https://maps.googleapis.com/maps/vt?pb=x&key=abc&token=34"}'
    a, _, a_receipt = map_projection.prepare(first, source_key="faris-lee")
    b, _, b_receipt = map_projection.prepare(second, source_key="faris-lee")
    assert a == b
    assert a_receipt == b_receipt


def test_projected_policy_binding_requires_projected_digest():
    import json

    from source_evidence import collector, map_projection

    original = {"sources": [{"sourceKey": "faris-lee"}],
                "runMeta": {"finishedAt": "2026-10-28T12:00:00Z"},
                "listings": [{"sourceKey": "faris-lee", "photo":
                    "https://maps.googleapis.com/maps/vt?pb=x&key=abc&token=123"}]}
    doc, literals, receipt = map_projection.prepare(json.dumps(original).encode(), source_key="faris-lee")
    binding = {"policySha256": "1" * 64, "collectorGitSha": "2" * 40, "targetSha256": "3" * 64,
               "transportSha256": "4" * 64, "artifactFileSha256": receipt["projectedCanonicalSha256"],
               "artifactFileHashKind": "projected_canonical_json_utf8", "generation": "run", "attempt": 1}
    collector.convert(doc, source_key="faris-lee", numeric_literals=literals or None,
                      projection=receipt, policy_binding=binding)
    for field in ("artifactFileSha256", "artifactFileHashKind"):
        changed = {**binding, field: "0" * 64}
        with pytest.raises(ValueError):
            collector.convert(doc, source_key="faris-lee", projection=receipt, policy_binding=changed)
