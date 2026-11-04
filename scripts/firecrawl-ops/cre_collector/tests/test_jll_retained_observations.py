"""Offline JLL observation preservation and fail-closed visibility tests."""
import json
import sys

import jll_retained_observations as adapter
import pytest


def row(visibility="visible", **values):
    control = "visible" if visibility == "visible" else visibility
    price = {"sourceShape": "structured", "normalization": "unavailable"}
    return {"sourceKey": "jll", "id": "fixture-id", "street": "fixture street",
            "salePriceUsd": 1234, "buildingSizeSqft": 5000,
            "jllSearchResult": {"priceWithholdingControl": control},
            "jllDetail": {"pricing": {"visibility": "visible", "searchWithholdingControl": "visible",
                                      "detailWithholdingControl": "absent", "sale": price, "lease": price}},
            **values}


def capture(rows):
    raw = json.dumps({"listings": rows, "brokers": [{"name": "private"}]}).encode()
    return json.loads(adapter.project(raw))


def test_visible_preserves_listing_fields_and_marks_unknown_identity():
    source = row(extraFacts={"example": "data"}, street=None, id=None)
    result = capture([source]); observation = result["observations"][0]
    assert observation["facts"] == source
    assert observation["identity"] == "unresolved"
    assert result["sourceCompleteness"] == "unverified"
    assert "parent_brokers_and_source_metadata_omitted" in observation["omissions"]


@pytest.mark.parametrize("state", ["withheld", "unknown", "malformed"])
def test_uncertain_control_guards_entire_page_and_hides_price(state):
    result = capture([row(transactionMode="sale"), row(state, transactionMode="lease")])
    assert result["pageWideVisibilityGuard"]
    assert all("salePriceUsd" not in x["facts"] for x in result["observations"])
    assert all(x["facts"]["buildingSizeSqft"] == 5000 for x in result["observations"])
    assert all(x["omissions"] for x in result["observations"])
    assert result["observations"][1]["visibility"] == "withheld_or_unknown"


def test_missing_pricing_is_unknown_and_cannot_expose_values():
    source = row(); source.pop("jllDetail")
    result = capture([source])
    assert result["observations"][0]["visibility"] == "unknown"
    assert "salePriceUsd" not in result["observations"][0]["facts"]


def test_invalid_shapes_duplicate_keys_and_input_limit_rejected():
    with pytest.raises(ValueError): adapter.project(b'{"listings":[],"listings":[]}')
    with pytest.raises(ValueError): capture([{"sourceKey": "other"}])
    with pytest.raises(ValueError): adapter.project(b" " * (adapter.MAX_INPUT_BYTES + 1))


def test_cli_writes_private_file_and_never_overwrites_input(tmp_path, monkeypatch):
    source = tmp_path / "input.json"; out = tmp_path / "output.json"
    original = json.dumps({"listings": [row()]}).encode(); source.write_bytes(original)
    monkeypatch.setattr(sys, "argv", ["adapter", "--input", str(source), "--out", str(out)])
    adapter.main()
    assert source.read_bytes() == original
    assert out.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError): adapter.main()
    assert source.read_bytes() == original


def test_malformed_pricing_fails_closed():
    source = row(jllDetail={"pricing": {"visibility": []}})
    result = capture([source])
    assert result["pageWideVisibilityGuard"]
    assert result["observations"][0]["visibility"] == "withheld_or_unknown"
    assert "salePriceUsd" not in result["observations"][0]["facts"]


def test_fractional_price_does_not_hide_valid_visible_row():
    source = row()
    source["jllDetail"]["pricing"]["sale"] = {
        "sourceShape": "structured", "normalization": "available",
        "normalizedAmount": 12.75, "normalizedText": "12.75",
        "currency": "USD", "unit": "SF"}
    result = capture([source])
    assert not result["pageWideVisibilityGuard"]
    assert result["observations"][0]["facts"] == source


def test_nested_hidden_secondary_pass_guards_every_row():
    source = row(secondary_pass={"sourceKey": "jll", "hidePrice": True,
                                 "salePriceUsd": 123456789})
    result = capture([source, row(id="another")])
    assert result["pageWideVisibilityGuard"]
    assert result["observations"][0]["visibility"] == "withheld_or_unknown"
    assert all("salePriceUsd" not in x["facts"] for x in result["observations"])
    assert "123456789" not in json.dumps(result)


def test_nested_unknown_control_and_hidden_marker_guard():
    for extra in ({"nested": {"hidePrice": "maybe"}},
                  {"nested": {"jllPriceWithheld": True}},
                  {"nested": {"secondaryPass": {"salePriceUsd": 999}}}):
        assert capture([row(**extra)])["pageWideVisibilityGuard"]


def test_nested_pricing_guard_and_unvalidated_coordinate_retention():
    source = row(nested={"pricing": {"visibility": "withheld"}},
                 latitude=999.123, longitude=-999.456)
    result = capture([source]); observation = result["observations"][0]
    assert result["pageWideVisibilityGuard"]
    assert observation["factsValidation"] == "unvalidated_source_values"
    assert observation["facts"]["latitude"] == 999.123
    assert observation["facts"]["longitude"] == -999.456
    assert observation["omissions"]


@pytest.mark.parametrize("key", ["hide_price", "hide-price", "price_withholding_control",
                                "price-withholding-control", "jll_price_withheld"])
def test_control_name_variants_fail_closed(key):
    result = capture([row(nested={key: False, "salePriceUsd": 123456789})])
    assert result["pageWideVisibilityGuard"]
    assert "123456789" not in json.dumps(result)
    assert result["observations"][0]["omissions"]


@pytest.mark.parametrize(("control", "value"), [
    ("searchWithholdingControl", "withheld"),
    ("detailWithholdingControl", "unknown"),
    ("visibility", "withheld"),
    ("visibility", "unknown"),
    ("visibility", []),
    ("search_withholding_control", "visible"),
    ("detail-withholding-control", "visible"),
])
def test_nested_normalized_controls_guard_arbitrary_objects(control, value):
    source = row(nested={control: value, "salePriceUsd": 123456789})
    result = capture([source])
    assert result["pageWideVisibilityGuard"]
    assert "123456789" not in json.dumps(result)
    assert result["observations"][0]["omissions"]
