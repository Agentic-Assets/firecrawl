import copy

import pytest
from source_evidence import acquisition, events, map_projection
from source_evidence import buildout_projection as projection
from source_evidence.automatic import _refuse_credential_fields

URL = "https://buildout.com/docs/example/42?token=SYNTHETIC"
OTHER = "https://elsewhere.example/file?token=SYNTHETIC"


def test_native_withholds_only_reviewed_path_preserving_rows_and_missing_id():
    source = {"inventory": [{"id": None, "pdf_url": URL, "other": 0},
                            {"id": "duplicate", "pdf_url": None},
                            {"id": "duplicate"}, None, {"pdf_url": 17}], "meta": {"count": 5}}
    before = copy.deepcopy(source)
    output, receipt = projection.project(source, source_key="svn", representation="native")
    assert source == before
    assert output["inventory"] == [{"id": None, "pdf_url": None, "other": 0},
                                   {"id": "duplicate", "pdf_url": None},
                                   {"id": "duplicate"}, None, {"pdf_url": 17}]
    assert receipt["withheld"] == [{"pointer": "/inventory/0/pdf_url", "recordPointer": "/inventory/0",
                                    "reason": projection.REASON}]
    assert URL not in repr((output, receipt))
    assert "SYNTHETIC" not in repr(receipt)
    projection.validate(output, receipt, source_key="svn", representation="native")
    _refuse_credential_fields(output)


def test_collector_preserves_brochure_metadata_and_ambiguous_identifiers():
    source = {"listings": [{"id": "x", "brochures": [{"name": "Brochure", "url": URL}, None]},
                           {"id": "x", "brochures": [{"url": None}]},
                           {"brochures": [{"name": "missing URL"}]}, {"brochures": None}]}
    output, receipt = projection.project(source, source_key="cawley-chicago")
    assert output["listings"][0]["brochures"][0] == {"name": "Brochure", "url": None}
    assert output["listings"][0]["brochures"][1] is None
    assert output["listings"][1:] == source["listings"][1:]
    assert receipt["withheld"][0]["recordPointer"] == "/listings/0"
    assert receipt["withheld"][0]["pointer"] == "/listings/0/brochures/0/url"
    projection.validate(output, receipt, source_key="cawley-chicago")


@pytest.mark.parametrize("url", [OTHER, "https://buildout.com/docs/example/42?%74oken=SYNTHETIC",
    "https://buildout.com/docs/example/42?token=SYNTHETIC&other=1",
    "https://buildout.com:443/docs/example/42?token=SYNTHETIC",
    "https://buildout.com/sharing/example?token=SYNTHETIC"])
def test_unreviewed_credential_url_is_never_generalized(url):
    source = {"inventory": [{"pdf_url": url}]}
    output, receipt = projection.project(source, source_key="svn", representation="native")
    assert output == source and receipt["withheld"] == []
    with pytest.raises(ValueError):
        _refuse_credential_fields(output)


def test_other_credential_locations_remain_for_existing_scanner():
    source = {"inventory": [{"pdf_url": URL, "other": OTHER}]}
    output, receipt = projection.project(source, source_key="svn", representation="native")
    assert len(receipt["withheld"]) == 1
    with pytest.raises(ValueError):
        _refuse_credential_fields(output)


def test_exceptional_numeric_literals_round_trip_and_bind_digest():
    raw = ('{"inventory":[{"pdf_url":"' + URL + '","amount":0.10000000000000000001},'
           '{"pdf_url":0.10000000000000000001}]}').encode()
    output, literals, receipt = projection.prepare(raw, source_key="svn", representation="native")
    assert literals["/inventory/0/amount"] == "0.10000000000000000001"
    assert literals["/inventory/1/pdf_url"] == "0.10000000000000000001"
    projection.validate(output, receipt, source_key="svn", representation="native", numeric_literals=literals)
    assert acquisition.decode_json(acquisition.encode_json(output, literals).encode()) == (output, literals)
    with pytest.raises(ValueError):
        projection.validate(output, receipt, source_key="svn", representation="native")


def test_receipt_mutation_and_schema_bounds_fail_without_body_in_errors(monkeypatch):
    output, receipt = projection.project({"inventory": [{"pdf_url": URL}]}, source_key="svn", representation="native")
    for field, value in [("pointer", "/inventory/1/pdf_url"), ("recordPointer", "/inventory/1"),
                         ("pointer", "/inventory/00/pdf_url"), ("reason", "other")]:
        changed = copy.deepcopy(receipt)
        changed["withheld"][0][field] = value
        with pytest.raises(ValueError) as exc:
            projection.validate(output, changed, source_key="svn", representation="native")
        assert "SYNTHETIC" not in str(exc.value)
    changed = copy.deepcopy(receipt)
    changed["withheld"].append(copy.deepcopy(changed["withheld"][0]))
    with pytest.raises(ValueError):
        projection.validate(output, changed, source_key="svn", representation="native")
    for malformed in ([None], [{"pointer": {}, "recordPointer": "/inventory/0", "reason": projection.REASON}],
                      [{"pointer": "/inventory/" + "9" * 100 + "/pdf_url",
                        "recordPointer": "/inventory/0", "reason": projection.REASON}]):
        changed = copy.deepcopy(receipt)
        changed["withheld"] = malformed
        with pytest.raises(ValueError):
            projection.validate(output, changed, source_key="svn", representation="native")
    changed = copy.deepcopy(output)
    changed["other"] = "changed"
    with pytest.raises(ValueError):
        projection.validate(changed, receipt, source_key="svn", representation="native")
    monkeypatch.setattr(projection, "MAX_URL_BYTES", 20)
    with pytest.raises(ValueError):
        projection.project({"inventory": [{"pdf_url": URL}]}, source_key="svn", representation="native")


def test_other_rows_and_brochure_shapes_are_preserved_without_interpretation():
    source = {"listings": [None, 12, {"brochures": "not-a-list"},
                           {"brochures": [42, None, {"url": URL}]}]}
    output, receipt = projection.project(source, source_key="svn")
    assert output["listings"][:3] == source["listings"][:3]
    assert output["listings"][3]["brochures"][:2] == [42, None]
    assert receipt["withheld"][0]["pointer"] == "/listings/3/brochures/2/url"
    projection.validate(output, receipt, source_key="svn")


def test_scope_and_duplicate_json_keys_refused():
    with pytest.raises(ValueError):
        projection.project({"inventory": []}, source_key="transwestern", representation="native")
    with pytest.raises(ValueError):
        projection.project({"inventory": []}, source_key="svn", representation="other")
    with pytest.raises(ValueError):
        projection.prepare(b'{"inventory":[],"inventory":[]}', source_key="svn", representation="native")


@pytest.mark.parametrize("source", [{"unexpected": [1, None, "retained"]},
                                    {"inventory": {"unexpected": "retained"}},
                                    [1, None, {"arbitrary": "retained"}], "retained"])
def test_unknown_native_shapes_are_preserved_for_existing_admission(source):
    output, receipt = projection.project(source, source_key="svn", representation="native")
    assert output == source and receipt["withheld"] == []
    projection.validate(output, receipt, source_key="svn", representation="native")


def test_unknown_shape_with_sensitive_content_is_not_projected_and_scanner_refuses():
    source = {"inventory": {"unknown_document_reference": URL}}
    output, receipt = projection.project(source, source_key="svn", representation="native")
    assert output == source and receipt["withheld"] == []
    with pytest.raises(ValueError):
        _refuse_credential_fields(output)


def test_receipt_cannot_authenticate_preexisting_null_without_trusted_seal():
    source = {"inventory": [{"pdf_url": URL}, {"pdf_url": None}]}
    output, receipt = projection.project(source, source_key="svn", representation="native")
    forged = copy.deepcopy(receipt)
    forged["withheld"][0].update(pointer="/inventory/1/pdf_url", recordPointer="/inventory/1")
    projection.validate(output, forged, source_key="svn", representation="native")
    assert events.sha256(events.encoded(receipt)) != events.sha256(events.encoded(forged))


def test_withholding_pointer_cannot_claim_exceptional_numeric_literal():
    output, receipt = projection.project({"inventory": [{"pdf_url": URL}]}, source_key="svn", representation="native")
    literals = {"/inventory/0/pdf_url": "1e999"}
    forged = copy.deepcopy(receipt)
    forged["projectedCanonicalSha256"] = map_projection.canonical_digest(output, literals)
    with pytest.raises(ValueError, match="conflicts with numeric literal"):
        projection.validate(output, forged, source_key="svn", representation="native", numeric_literals=literals)
    genuine, genuine_literals, genuine_receipt = projection.prepare(
        b'{"inventory":[{"pdf_url":1e999}]}', source_key="svn", representation="native")
    assert genuine_receipt["withheld"] == []
    projection.validate(genuine, genuine_receipt, source_key="svn", representation="native", numeric_literals=genuine_literals)
