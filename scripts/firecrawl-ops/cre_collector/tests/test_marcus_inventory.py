import base64
import copy
import json

import pytest

from cre_marcus_inventory import partition_inventory, stage_inventory

START = "2026-11-05T18:49:40Z"
FINISH = "2026-11-05T19:08:55Z"
GENERATION = "generation"


def listing(native="187396", activity="activity-1"):
    path = f"/properties/{native}/source-name"
    url = f"https://www.marcusmillichap.com{path}"
    tile = (f'<div class="mm-tile" data-dealid="{native}" data-activityid="{activity}">'
            f'<a href="{path}"><h2>Source title</h2><h2>Second title</h2></a></div>')
    return {"id": native, "activityId": activity, "sourceKey": "marcus-millichap", "url": url,
            "inventoryObservedAt": "2026-11-05T18:50:00Z",
            "freshnessProvenance": {"generationId": GENERATION},
            "rawMarcusSearchRow": {"ActivityId": activity, "PropertyUrl": path,
                "Latitude": "34.1", "Longitude": "-118.2", "rawMarcusMapDetail": {
                    "ActivityId": activity, "PropertyUrl": path, "PropertyDetail": tile}},
            "detailError": "detail HTML did not contain a verified property page"}


def stage(row):
    return stage_inventory(row, GENERATION, START, FINISH)


def test_inventory_preserves_unverified_observation_and_first_source_title():
    source = listing()
    staged = stage(source)
    assert staged["title"] == "Source title"
    assert staged["detail_qualified"] is False
    assert staged["detail_error"] is True
    assert (staged["lat"], staged["lng"]) == (34.1, -118.2)
    assert json.loads(base64.b64decode(staged["observation_base64"])) == source


@pytest.mark.parametrize("mutation", [
    lambda row: row.update(id="999"),
    lambda row: row.update(url="https://example.com/properties/187396/source-name"),
    lambda row: row["rawMarcusSearchRow"].update(ActivityId="another"),
    lambda row: row["rawMarcusSearchRow"]["rawMarcusMapDetail"].update(PropertyUrl="/properties/999/other"),
    lambda row: row["freshnessProvenance"].update(generationId="different"),
    lambda row: row.update(inventoryObservedAt="2026-11-05T19:09:00Z"),
])
def test_mismatched_identity_or_custody_is_held(mutation):
    row = listing()
    mutation(row)
    with pytest.raises(ValueError):
        stage(row)


def test_invalid_witness_cannot_hide_a_conflict_with_valid_peer():
    valid = listing()
    invalid = copy.deepcopy(valid)
    invalid["activityId"] = "different"
    result = partition_inventory([valid, invalid, listing("123", "activity-2")], GENERATION, START, FINISH)
    assert [item["external_id"] for item in result["staged"]] == ["123"]
    assert {item["index"] for item in result["held"]} == {0, 1}


def test_missing_title_and_invalid_coordinates_do_not_discard_identity():
    row = listing()
    raw = row["rawMarcusSearchRow"]
    raw["Latitude"] = "NaN"
    raw["rawMarcusMapDetail"]["PropertyDetail"] = raw["rawMarcusMapDetail"]["PropertyDetail"].replace(
        "<h2>Source title</h2><h2>Second title</h2>", "")
    staged = stage(row)
    assert staged["title"] is None
    assert staged["lat"] is None and staged["lng"] is None
