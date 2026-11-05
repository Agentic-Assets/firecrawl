import base64
import json

import pytest

from cre_lyon_partial import identity_probe_sql, stage_record


def record(url, kind, value):
    return {"url": url, "sourceIdentity": {"kind": kind, "value": value}, "occurrences": [{
        "filter": "all", "index": 0, "observedAt": "2026-11-05T06:00:00Z",
        "responseTextSha256": "a" * 64, "row": {"permalink": url, "title": "Example"},
    }]}


def test_query_identity_retains_source_and_does_not_merge_to_path():
    item = record("https://lyonstahl.com/?post_type=properties&p=40201", "wordpress_post", "40201")
    staged = stage_record(item)
    assert staged["external_id"] == "wordpress:40201"
    assert json.loads(base64.b64decode(staged["observation_base64"])) == item
    path = stage_record(record("https://lyonstahl.com/properties/example/", "property_path", "example"))
    assert path["external_id"] == "example"


@pytest.mark.parametrize("url", [
    "https://lyonstahl.com/?post_type=properties&p=40201&p=7",
    "https://evil.example/?post_type=properties&p=40201",
    "https://lyonstahl.com/?post_type=properties&p=40202",
])
def test_query_identity_rejects_other_or_ambiguous_url(url):
    with pytest.raises(ValueError):
        stage_record(record(url, "wordpress_post", "40201"))


def test_duplicate_occurrence_index_or_mismatched_source_row_is_rejected():
    item = record("https://lyonstahl.com/properties/example/", "property_path", "example")
    item["occurrences"].append(dict(item["occurrences"][0]))
    with pytest.raises(ValueError):
        stage_record(item)
    item["occurrences"].pop()
    item["occurrences"][0]["row"]["permalink"] = "https://lyonstahl.com/properties/other/"
    with pytest.raises(ValueError):
        stage_record(item)


def test_ambiguous_literal_urls_cannot_choose_a_winner():
    first = stage_record(record("https://lyonstahl.com/properties/example/", "property_path", "example"))
    second = stage_record(record("https://lyonstahl.com/properties/example", "property_path", "example"))
    sibling = stage_record(record("https://lyonstahl.com/properties/sibling/", "property_path", "sibling"))
    with pytest.raises(ValueError):
        identity_probe_sql([first, second, sibling])
    assert "sibling" in identity_probe_sql([sibling])


def test_conflicting_optional_values_stay_in_evidence_and_do_not_override_columns():
    item = record("https://lyonstahl.com/properties/example/", "property_path", "example")
    item["occurrences"][0]["row"].update(title="First", latitude="34.1", longitude="-118.2")
    item["occurrences"].append({**item["occurrences"][0], "filter": "sale", "row": {
        "permalink": item["url"], "title": "Second", "latitude": "34.2", "longitude": "-118.2"}})
    staged = stage_record(item)
    assert staged["title"] is None
    assert staged["lat"] is None and staged["lng"] is None
    assert json.loads(base64.b64decode(staged["observation_base64"])) == item


def test_unrepresentable_optional_text_is_preserved_but_not_projected():
    item = record("https://lyonstahl.com/properties/example/", "property_path", "example")
    item["occurrences"][0]["row"]["title"] = "unsafe\x00value"
    staged = stage_record(item)
    assert staged["title"] is None
    assert json.loads(base64.b64decode(staged["observation_base64"])) == item


def test_split_coordinate_values_are_retained_without_inventing_a_pair():
    item = record("https://lyonstahl.com/properties/example/", "property_path", "example")
    item["occurrences"][0]["row"].update(latitude="34.1")
    item["occurrences"].append({**item["occurrences"][0], "index": 1, "row": {
        "permalink": item["url"], "longitude": "-118.2"}})
    staged = stage_record(item)
    assert staged["lat"] is None and staged["lng"] is None
    assert json.loads(base64.b64decode(staged["observation_base64"])) == item
    item["occurrences"].append({**item["occurrences"][0], "index": 2, "row": {
        "permalink": item["url"], "latitude": "34.1", "longitude": "-118.2"}})
    staged = stage_record(item)
    assert (staged["lat"], staged["lng"]) == (34.1, -118.2)
    item["occurrences"][1]["row"]["longitude"] = "-118.3"
    staged = stage_record(item)
    assert staged["lat"] is None and staged["lng"] is None
