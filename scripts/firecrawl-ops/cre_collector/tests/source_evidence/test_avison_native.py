import copy

import pytest
from source_evidence import acquisition, avison_native


def test_projection_keeps_rows_unknown_values_and_empty_fields():
    original = {"other": {"flag": False}, "items": [
        {"hide_price": 1, "sale_price": 123, "submarket": "", "unknown": {"value": None},
         "description": "Asking price: $123. Rent: $9/SF/year.", "cap_rate": 7},
        {"hide_price": 0, "sale_price": 456, "unknown": [None, False, ""]},
        {"unlabelled": "retained"},
    ]}
    saved = copy.deepcopy(original)
    output, literals, receipt = avison_native.project(original)
    assert original == saved and literals == {}
    assert output["items"][0]["submarket"] == ""
    assert output["items"][0]["unknown"] == {"value": None}
    assert "sale_price" not in output["items"][0]
    assert output["items"][0]["cap_rate"] == 7
    assert output["items"][1:] == original["items"][1:]
    assert {c["pointer"] for c in receipt["changes"]} >= {"/items/0/sale_price", "/items/0/description"}
    assert receipt["unresolvedProse"] == 0
    assert output["items"][0]["description"] == "Asking price: [withheld]. Rent: $9/SF/year."
    assert "123" not in str(receipt)


def test_removed_exceptional_asking_number_does_not_leak_through_literal_map():
    raw = b'{"items":[{"hide_price":1,"sale_price":123456789012345678901,"other":0.10000000000000000001}]}'
    data, literals = acquisition.decode_json(raw)
    result, kept, receipt = avison_native.project(data, literals)
    assert "/items/0/sale_price" not in kept
    assert kept["/items/0/other"] == "0.10000000000000000001"
    assert b"123456789012345678901" not in acquisition.encode_json(result, kept).encode()
    assert receipt["changes"][0]["pointer"] == "/items/0/sale_price"


def test_whitespace_only_source_text_is_retained_exactly():
    data = {"items": [{"hide_price": 1, "name": "Example Building ", "sale_price": 90}]}
    output, _, receipt = avison_native.project(data)
    assert output["items"][0]["name"] == "Example Building "
    assert receipt["unresolvedProse"] == 0


def test_repeated_asking_and_independent_equal_rent_survive_idempotently():
    text = "Asking price: $123. Rent: $123/SF/year. Asking price: $123. Café suite 123."
    data = {"items": [{"hide_price": 1, "sale_price": 123, "description": text}]}
    output, literals, receipt = avison_native.project(data)
    assert output["items"][0]["description"] == (
        "Asking price: [withheld]. Rent: $123/SF/year. Asking price: [withheld]. Café suite 123."
    )
    assert receipt["unresolvedProse"] == 0
    avison_native.validate_projected(output, literals)


def test_ambiguous_money_keeps_other_prose_and_marks_unresolved():
    data = {"items": [{"hide_price": 1, "description": "North\n$55 million near transit."}]}
    output, literals, receipt = avison_native.project(data)
    assert output["items"][0]["description"] == "North\n[unresolved monetary value] near transit."
    assert receipt["unresolvedProse"] == 1
    avison_native.validate_projected(output, literals)


def test_string_asking_field_is_omitted_and_urls_preserved():
    data = {"items": [{"hide_price": 1, "sale_price": "123", "description":
                       "Asking price: $123. See https://example.test/suite/123."}]}
    output, literals, _ = avison_native.project(data)
    assert "sale_price" not in output["items"][0]
    assert output["items"][0]["description"] == "Asking price: [withheld]. See https://example.test/suite/123."
    avison_native.validate_projected(output, literals)


def test_rent_label_does_not_authorize_later_ambiguous_money():
    data = {"items": [{"hide_price": 1, "sale_price": 123,
                       "description": "Rent: $123/SF/year. Market value: $500."}]}
    output, _, receipt = avison_native.project(data)
    assert output["items"][0]["description"] == "Rent: $123/SF/year. Market value: [unresolved monetary value]."
    assert receipt["unresolvedProse"] == 1


@pytest.mark.parametrize("amount", ["123456", "123,456.78", "2.5 million", "35 thousand", "4M"])
def test_unformatted_labeled_asking_masks_only_amount_without_structured_price(amount):
    text = f"Café suite 123. Asking price: {amount}. Rent: $123/SF/year; NOI: $123."
    data = {"items": [{"hide_price": 1, "description": text}]}
    out, literals, receipt = avison_native.project(data)
    assert out["items"][0]["description"] == (
        "Café suite 123. Asking price: [withheld]. Rent: $123/SF/year; NOI: $123."
    )
    assert any(c["reason"] == "withheld_asking_span" for c in receipt["changes"])
    avison_native.validate_projected(out, literals)


def test_labeled_bare_asking_preserves_urls_and_unrelated_numbers():
    text = "https://example.test/asking-price=123; Suite 123\nSale price = 123; Rent: 123/SF/year"
    out, _, _ = avison_native.project({"items": [{"hide_price": 1, "description": text}]})
    assert out["items"][0]["description"] == (
        "https://example.test/asking-price=123; Suite 123\nSale price = [withheld]; Rent: 123/SF/year"
    )


def test_malformed_labeled_bare_asking_remains_refused():
    with pytest.raises(ValueError):
        avison_native.project({"items": [{"hide_price": 1, "description": "Asking price: 123,45"}]})


@pytest.mark.parametrize("value", [True, "1", 2, None])
def test_malformed_visibility_refuses(value):
    with pytest.raises(ValueError):
        avison_native.project({"items": [{"hide_price": value}]})


def test_invalid_shapes_and_literal_map_refuse():
    for value in ({}, {"items": "not an array"}):
        with pytest.raises(ValueError):
            avison_native.project(value)
    with pytest.raises(ValueError):
        avison_native.project({"items": []}, {"/missing": "1.234567890123456789"})


def test_nested_controls_aliases_urls_and_nonobject_rows():
    data = {"context": {"hide_price": 1, "sale_price": 777, "unknown": "777"}, "items": [
        None, 17, {"hide_price": 0, "nested": {"hide_price": 1, "sale_price": 123456,
            "mystery": "123456", "name": "Suite 123456", "rent": 123456,
            "url": "https://example.test/123456?asking_price=123456&id=123456"}},
    ]}
    result, literals, receipt = avison_native.project(data)
    assert result["items"][:2] == [None, 17]
    nested = result["items"][2]["nested"]
    assert "sale_price" not in nested and "mystery" not in nested
    assert nested["rent"] == 123456 and nested["name"] == "Suite 123456"
    assert nested["url"] == "https://example.test/123456?id=123456"
    assert "sale_price" not in result["context"] and "unknown" not in result["context"]
    assert {c["reason"] for c in receipt["changes"]} >= {"withheld_asking_alias", "withheld_url_parameter"}
    avison_native.validate_projected(result, literals)
    with pytest.raises(ValueError):
        avison_native.project({"items": [{"nested": {"HidePrice": 1}}]})


def test_exceptional_equal_alias_removed_but_independent_equal_retained():
    raw = b'{"items":[{"hide_price":1,"sale_price":12345678901234567890.123456789,"unknown":12345678901234567890.123456789,"rent":12345678901234567890.123456789}]}'
    data, literals = acquisition.decode_json(raw)
    out, kept, _ = avison_native.project(data, literals)
    assert "unknown" not in out["items"][0] and "/items/0/unknown" not in kept
    assert kept["/items/0/rent"] == "12345678901234567890.123456789"


def test_list_aliases_are_masked_without_changing_list_cardinality():
    data = {"items": [{"hide_price": 1, "sale_price": 123456,
                       "unknown": [123456, "value 123456", None], "rent": [123456]}]}
    out, literals, _ = avison_native.project(data)
    assert out["items"][0]["unknown"] == ["[withheld]", "value [withheld]", None]
    assert out["items"][0]["rent"] == [123456]
    avison_native.validate_projected(out, literals)


def test_nested_independent_values_preserved_with_nested_withholding():
    data = {"items": [{"hide_price": 1, "sale_price": 123456,
                       "noi": {"value": 123456, "array": [123456, {"value": 123456}],
                               "url": "https://example.test/?asking_price=123456&id=123456"},
                       "rent": {"value": 123456, "detail": {"hide_price": 1,
                           "sale_price": 123456, "unknown": 123456}},
                       "address": {"suite": 123456}}]}
    out, literals, _ = avison_native.project(data)
    row = out["items"][0]
    assert row["noi"]["value"] == 123456 and row["noi"]["array"] == [123456, {"value": 123456}]
    assert row["noi"]["url"] == "https://example.test/?id=123456"
    assert row["rent"]["value"] == 123456 and row["address"] == {"suite": 123456}
    assert row["rent"]["detail"] == {"hide_price": 1}
    avison_native.validate_projected(out, literals)


def test_nested_independent_exceptional_literals_remain_exact():
    raw = (b'{"items":[{"hide_price":1,"sale_price":0.1234567890123456789,'
           b'"noi":{"value":0.1234567890123456789},'
           b'"rent":{"values":[0.1234567890123456789]}}]}')
    data, literals = acquisition.decode_json(raw)
    out, kept, _ = avison_native.project(data, literals)
    assert "/items/0/sale_price" not in kept
    assert kept["/items/0/noi/value"] == "0.1234567890123456789"
    assert kept["/items/0/rent/values/0"] == "0.1234567890123456789"
    assert out["items"][0]["noi"] == {"value": None}
    avison_native.validate_projected(out, kept)


@pytest.mark.parametrize("label", ["suite", "unit", "space", "suite_number", "suiteNumber", "suite_id",
                                   "unit_number", "unitNumber", "unitId", "space_number", "spaceNumber", "spaceId"])
def test_nested_identity_value_survives_but_explicit_asking_does_not(label):
    data = {"items": [{"hide_price": 1, "sale_price": 123456,
                       label: {"value": 123456, "asking_price": 123456},
                       "noi": {"value": 123456, "asking_price": 123456}}]}
    out, literals, _ = avison_native.project(data)
    assert out["items"][0][label] == {"value": 123456}
    assert out["items"][0]["noi"] == {"value": 123456}
    avison_native.validate_projected(out, literals)


@pytest.mark.parametrize("label", ["suite", "unit", "space", "suite_number", "suiteNumber", "suite_id",
                                   "unit_number", "unitNumber", "unitId", "space_number", "spaceNumber", "spaceId"])
def test_nested_identity_exceptional_number_is_retained(label):
    raw = ('{"items":[{"hide_price":1,"sale_price":0.1234567890123456789,'
           f'"{label}":{{"value":0.1234567890123456789,'
           '"asking_price":0.1234567890123456789}}]}').encode()
    data, literals = acquisition.decode_json(raw)
    out, kept, _ = avison_native.project(data, literals)
    assert kept[f"/items/0/{label}/value"] == "0.1234567890123456789"
    assert f"/items/0/{label}/asking_price" not in kept
    assert "asking_price" not in out["items"][0][label]
    avison_native.validate_projected(out, kept)


@pytest.mark.parametrize("label", ["suite_number", "suiteNumber", "suite_id", "unit_number", "unitId", "spaceNumber"])
def test_scalar_identity_alias_does_not_inherit_price_meaning(label):
    data = {"items": [{"hide_price": 1, "sale_price": 123456, label: 123456, "suite_price": 123456}]}
    out, literals, _ = avison_native.project(data)
    assert out["items"][0][label] == 123456
    assert "suite_price" not in out["items"][0]
    avison_native.validate_projected(out, literals)
