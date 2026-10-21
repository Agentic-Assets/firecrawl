"""Optional held-card audit is finite evidence, never emitted listing authority."""

import copy

import pytest
import test_essex_current_inventory as helpers
from test_daum_current_inventory import produce
from test_daum_empty_title_subset import actual_v2, v2  # noqa: F401
from test_daum_verified_subset import admit, complete, subset  # noqa: F401


def observations(proof):
    members = {row[0]: row for row in proof["cohort"]}
    return [
        {
            "postId": post,
            "inventory": {
                "url": members[post][1],
                "title": "Published inventory card title",
                "tenure": members[post][3],
                "tenureText": members[post][4],
                "city": "Published city",
                "state": "CA",
                "postalCode": "90001",
                "sizeText": "12,500 SF",
                "spaceCount": 2,
                "assetType": "Industrial",
                "salePriceText": "$3,250,000",
                "leaseRateText": None,
                "brokerNames": ["Published broker"],
                "imageUrl": "https://daumcommercial.com/card.jpg",
                "latitude": 33.0,
                "longitude": -112.0,
            },
        }
        for post, _reason in proof["held"]
    ]


@pytest.fixture(params=["subset", "v2"])
def audited(request):
    prepared = request.getfixturevalue(request.param)
    for entry in prepared[2]["sources"]:
        proof = entry["currentInventoryProof"]
        proof["heldInventoryObservations"] = observations(proof)
    helpers.refresh(prepared[1], prepared[2], prepared[3])
    return prepared


def test_rich_held_cards_preserve_partition_through_both_gates(audited):
    before = copy.deepcopy(audited[2])
    binding = admit(audited)
    assert binding["held_count"] > 0
    assert binding["whole_source_coverage"] is False
    held = {post for post, _ in before["sources"][0]["currentInventoryProof"]["held"]}
    assert not held.intersection(str(row["daumPostId"]) for row in before["listings"])
    audited[3]["sources"]["daum-commercial"]["gate"] = copy.deepcopy(
        audited[4]["per_source"]["daum-commercial"]
    )
    assert admit(audited, "aggregate") == binding
    assert audited[2] == before


def test_absent_optional_audit_remains_compatible(audited):
    for entry in audited[2]["sources"]:
        del entry["currentInventoryProof"]["heldInventoryObservations"]
    helpers.refresh(audited[1], audited[2], audited[3])
    assert admit(audited)["held_count"] > 0


@pytest.mark.parametrize(
    "fault",
    ["not-list", "missing", "duplicate", "foreign", "extra-key", "missing-field",
     "extra-field", "wrong-url", "wrong-tenure", "wrong-raw-tenure", "bool-number",
     "nonfinite", "numeric-overflow", "bad-string", "bad-brokers", "one-proof-drift"],
)
def test_malformed_or_unbound_card_refuses(audited, fault):
    before = copy.deepcopy(audited[2])
    for index, entry in enumerate(audited[2]["sources"]):
        audit = entry["currentInventoryProof"]["heldInventoryObservations"]
        row = audit[0]
        card = row["inventory"]
        if fault == "not-list":
            entry["currentInventoryProof"]["heldInventoryObservations"] = None
        elif fault == "missing":
            audit.pop()
        elif fault == "duplicate":
            audit.append(copy.deepcopy(row))
        elif fault == "foreign":
            row["postId"] = "59999"
        elif fault == "extra-key":
            row["invented"] = True
        elif fault == "missing-field":
            del card["city"]
        elif fault == "extra-field":
            card["invented"] = None
        elif fault == "wrong-url":
            card["url"] = "https://daumcommercial.com/property/Foreign/"
        elif fault == "wrong-tenure":
            card["tenure"] = "sale" if card["tenure"] != "sale" else "lease"
        elif fault == "wrong-raw-tenure":
            card["tenureText"] = "Sale"
        elif fault == "bool-number":
            card["spaceCount"] = True
        elif fault == "nonfinite":
            card["latitude"] = float("inf")
        elif fault == "numeric-overflow":
            card["longitude"] = 10**400
        elif fault == "bad-string":
            card["city"] = 3
        elif fault == "bad-brokers":
            card["brokerNames"] = [None]
        elif fault == "one-proof-drift" and index == 0:
            card["title"] += " changed"
    assert audited[2] != before
    helpers.refresh(audited[1], audited[2], audited[3])
    with pytest.raises(ValueError):
        admit(audited)


def test_held_cards_must_keep_cohort_order(request):
    prepared = request.getfixturevalue("subset")
    for entry in prepared[2]["sources"]:
        proof = entry["currentInventoryProof"]
        proof["heldInventoryObservations"] = observations(proof)[::-1]
    helpers.refresh(prepared[1], prepared[2], prepared[3])
    with pytest.raises(ValueError):
        admit(prepared)


def test_nullable_fields_and_empty_arrays_are_preserved(audited):
    for entry in audited[2]["sources"]:
        for observation in entry["currentInventoryProof"]["heldInventoryObservations"]:
            card = observation["inventory"]
            for field in set(card) - {"url", "title", "tenure", "tenureText", "brokerNames"}:
                card[field] = None
            card["brokerNames"] = []
    helpers.refresh(audited[1], audited[2], audited[3])
    assert admit(audited)["held_count"] > 0


@pytest.mark.parametrize("title", ["", " \t\n\u00a0"])
def test_empty_inventory_card_title_refuses_even_for_empty_detail_hold(audited, title):
    for entry in audited[2]["sources"]:
        entry["currentInventoryProof"]["heldInventoryObservations"][0]["inventory"]["title"] = title
    helpers.refresh(audited[1], audited[2], audited[3])
    with pytest.raises(ValueError):
        admit(audited)


@pytest.mark.parametrize("policy", ["tenure-only-v1", "tenure-and-empty-title-v2"])
def test_actual_subset_producer_audit_through_source_and_aggregate(
    tmp_path, monkeypatch, policy
):
    folder = tmp_path / "producer"
    folder.mkdir()
    produced = produce(folder, total=101, subset_policy=policy,
                       empty_title=policy == "tenure-and-empty-title-v2")
    monkeypatch.setattr(helpers, "SOURCE", "daum-commercial")
    prepared = helpers.setup(tmp_path, produced)
    import cre_current_inventory_admission as admission

    prepared[3]["config"]["current_inventory_admission"] = admission.subset_configuration(policy)
    proof = prepared[2]["sources"][0]["currentInventoryProof"]
    cards = proof["heldInventoryObservations"]
    assert [row["postId"] for row in cards] == [row[0] for row in proof["held"]]
    if policy == "tenure-only-v1":
        assert cards == []
    else:
        assert len(cards) == 1
        assert cards[0]["inventory"]["title"] == "Synthetic 100"
        assert cards[0]["inventory"]["tenure"] == "lease"
    before = copy.deepcopy(prepared[2])
    binding = admit(prepared)
    assert binding["held_count"] == len(cards)
    prepared[3]["sources"]["daum-commercial"]["gate"] = copy.deepcopy(
        prepared[4]["per_source"]["daum-commercial"]
    )
    assert admit(prepared, "aggregate") == binding
    assert prepared[2] == before
