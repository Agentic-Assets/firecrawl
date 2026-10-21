"""Optional held-detail audit stays typed evidence, never listing authority."""

import copy

import cre_current_inventory_admission as admission
import pytest
import test_daum_current_inventory as producer
import test_essex_current_inventory as helpers
from test_daum_empty_title_subset import actual_v2, v2  # noqa: F401
from test_daum_verified_subset import admit, complete, subset  # noqa: F401

SOURCE = "daum-commercial"
EMPTY_TITLE_REASON = "empty_required_property_title"


def detail_observations(proof):
    members = {row[0]: row for row in proof["cohort"]}
    observations = []
    for post, reason in proof["held"]:
        member = members[post]
        if reason == EMPTY_TITLE_REASON:
            detail = {
                "postId": int(post),
                "observedCanonicalUrl": member[1],
                "tenure": member[5],
                "tenureText": member[6],
                "emptyRequiredTitle": True,
            }
        else:
            detail = {
                "postId": int(post),
                "observedCanonicalUrl": member[1],
                "facts": {"Address": "Observed published address"},
                "title": "Held source detail title",
                "tenure": member[5],
                "tenureText": member[6],
                "city": "Observed city",
                "state": "Observed state",
                "postalCode": "12345",
                "salePriceText": "$1,250,000",
                "leaseRateText": None,
                "sizeText": "8,000 SF",
                "description": "Observed published description",
                "highlights": ["Observed highlight"],
                "brochures": [{"name": "Flyer", "url": "https://daumcommercial.com/flyer.pdf"}],
                "photos": ["https://daumcommercial.com/photo.jpg"],
                "contacts": [
                    {
                        "name": "Published broker",
                        "email": "broker@example.test",
                        "phone": None,
                        "office": "Published office",
                        "profileUrl": None,
                        "avatarUrl": None,
                        "license": None,
                    }
                ],
            }
        observations.append({"postId": post, "detail": detail})
    return observations


@pytest.fixture
def audited_subset(subset):  # noqa: F811
    for entry in subset[2]["sources"]:
        proof = entry["currentInventoryProof"]
        proof["heldDetailObservations"] = detail_observations(proof)
    helpers.refresh(subset[1], subset[2], subset[3])
    return subset


@pytest.fixture
def v2_unknown_details(subset):  # noqa: F811
    subset[3]["config"]["current_inventory_admission"] = admission.subset_configuration(
        "tenure-and-empty-title-v2"
    )
    for entry in subset[2]["sources"]:
        proof = entry["currentInventoryProof"]
        proof.update(
            schema="daum-verified-subset-v2",
            subsetPolicy="tenure-and-empty-title-v2",
            titleObservations=[],
            heldDetailObservations=detail_observations(proof),
        )
    helpers.refresh(subset[1], subset[2], subset[3])
    return subset


@pytest.fixture
def no_holds_subset(tmp_path, complete, monkeypatch):  # noqa: F811
    monkeypatch.setattr(helpers, "SOURCE", SOURCE)
    prepared = helpers.setup(tmp_path, complete)
    prepared[3]["config"]["current_inventory_admission"] = admission.subset_configuration()
    for entry in prepared[2]["sources"]:
        proof = entry["currentInventoryProof"]
        proof.update(
            schema="daum-verified-subset-v1",
            kind="verified_subset_with_holds",
            held=[],
            heldInventoryObservations=[],
            heldDetailObservations=[],
        )
        entry["truncated"] = False
    helpers.refresh(prepared[1], prepared[2], prepared[3])
    return prepared


def admit_both_phases(prepared):
    before = copy.deepcopy(prepared[2])
    binding = admit(prepared)
    assert binding["whole_source_coverage"] is False
    prepared[3]["sources"][SOURCE]["gate"] = copy.deepcopy(
        prepared[4]["per_source"][SOURCE]
    )
    assert admit(prepared, "aggregate") == binding
    assert prepared[2] == before
    return binding


def roundtrip_producer_artifact(tmp_path, produced):
    prepared = helpers.setup(tmp_path, produced)
    proof = prepared[2]["sources"][0]["currentInventoryProof"]
    policy = proof.get("subsetPolicy", "tenure-only-v1")
    prepared[3]["config"]["current_inventory_admission"] = admission.subset_configuration(
        policy
    )
    helpers.refresh(prepared[1], prepared[2], prepared[3])
    binding = admit(prepared)
    prepared[3]["sources"][SOURCE]["gate"] = copy.deepcopy(
        prepared[4]["per_source"][SOURCE]
    )
    assert admit(prepared, "aggregate") == binding
    assert binding["whole_source_coverage"] is False
    return prepared[2], binding


def assert_actual_full_detail_audit(artifact, schema):
    held_ids = set()
    for entry in artifact["sources"]:
        proof = entry["currentInventoryProof"]
        assert proof["schema"] == schema
        assert proof["held"]
        held_ids.update(post for post, _reason in proof["held"])
        observations = proof["heldDetailObservations"]
        assert [item["postId"] for item in observations] == [
            post for post, _reason in proof["held"]
        ]
        for observation in observations:
            detail = observation["detail"]
            assert set(detail) == {
                "postId",
                "observedCanonicalUrl",
                "facts",
                "title",
                "tenure",
                "tenureText",
                "city",
                "state",
                "postalCode",
                "salePriceText",
                "leaseRateText",
                "sizeText",
                "description",
                "highlights",
                "brochures",
                "photos",
                "contacts",
            }
            assert detail["title"].strip()
    return held_ids


def test_actual_v1_producer_audit_roundtrips_through_both_gates(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(helpers, "SOURCE", SOURCE)
    produced = producer.produce(
        tmp_path,
        total=102,
        unknown_detail=True,
        unknown_inventory=True,
        subset_policy="tenure-only-v1",
    )
    held_ids = assert_actual_full_detail_audit(produced[0], "daum-verified-subset-v1")
    artifact, binding = roundtrip_producer_artifact(tmp_path, produced)
    assert artifact == produced[0]
    assert held_ids == {"50000", "50090"}
    assert not held_ids.intersection(
        str(row["daumPostId"]) for row in artifact["listings"]
    )
    assert len(held_ids) == binding["held_count"] == 2
    assert binding["inventory_count"] == 102
    assert binding["emitted_count"] == binding["staged_count"] == 100
    detail_by_post = {
        str(item["postId"]): item["detail"]
        for entry in artifact["sources"]
        for item in entry["currentInventoryProof"]["heldDetailObservations"]
    }
    assert {post: detail_by_post[post]["title"] for post in held_ids} == {
        "50000": "Synthetic 0",
        "50090": "Synthetic 90",
    }
    assert all(
        detail_by_post[post]["city"] == "Phoenix"
        and detail_by_post[post]["state"] == "AZ"
        and detail_by_post[post]["sizeText"] == "12,500 SF"
        for post in held_ids
    )


def test_actual_v2_unknown_detail_producer_audit_roundtrips_through_both_gates(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(helpers, "SOURCE", SOURCE)
    produced = producer.produce(
        tmp_path,
        total=102,
        unknown_detail=True,
        unknown_inventory=True,
        subset_policy="tenure-and-empty-title-v2",
    )
    held_ids = assert_actual_full_detail_audit(produced[0], "daum-verified-subset-v2")
    artifact, binding = roundtrip_producer_artifact(tmp_path, produced)
    assert artifact == produced[0]
    assert held_ids == {"50000", "50090"}
    assert not held_ids.intersection(
        str(row["daumPostId"]) for row in artifact["listings"]
    )
    assert len(held_ids) == binding["held_count"] == 2
    assert binding["inventory_count"] == 102
    assert binding["emitted_count"] == binding["staged_count"] == 100
    detail_by_post = {
        str(item["postId"]): item["detail"]
        for entry in artifact["sources"]
        for item in entry["currentInventoryProof"]["heldDetailObservations"]
    }
    assert {post: detail_by_post[post]["title"] for post in held_ids} == {
        "50000": "Synthetic 0",
        "50090": "Synthetic 90",
    }
    assert all(
        detail_by_post[post]["city"] == "Phoenix"
        and detail_by_post[post]["state"] == "AZ"
        and detail_by_post[post]["sizeText"] == "12,500 SF"
        for post in held_ids
    )


def test_actual_v2_empty_title_producer_audit_roundtrips_through_both_gates(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(helpers, "SOURCE", SOURCE)
    produced = producer.produce(
        tmp_path,
        total=101,
        empty_title=True,
        subset_policy="tenure-and-empty-title-v2",
    )
    for entry in produced[0]["sources"]:
        proof = entry["currentInventoryProof"]
        assert proof["schema"] == "daum-verified-subset-v2"
        assert proof["held"]
        assert [item["postId"] for item in proof["heldDetailObservations"]] == [
            post for post, _reason in proof["held"]
        ]
        assert all(
            item["detail"].get("emptyRequiredTitle") is True
            for item in proof["heldDetailObservations"]
        )
    artifact, binding = roundtrip_producer_artifact(tmp_path, produced)
    proofs = [entry["currentInventoryProof"] for entry in artifact["sources"]]
    for proof in proofs:
        assert proof["schema"] == "daum-verified-subset-v2"
        assert len(proof["held"]) == len(proof["heldDetailObservations"]) == 1
        assert proof["held"][0][1] == "empty_required_property_title"
        observation = proof["heldDetailObservations"][0]
        assert observation["postId"] == proof["held"][0][0]
        assert set(observation["detail"]) == {
            "postId",
            "observedCanonicalUrl",
            "tenure",
            "tenureText",
            "emptyRequiredTitle",
        }
        assert observation["detail"]["emptyRequiredTitle"] is True
    held_ids = {proof["held"][0][0] for proof in proofs}
    assert not held_ids.intersection(str(row.get("daumPostId")) for row in artifact["listings"])
    assert binding["held_count"] == 1


def test_full_unknown_tenure_details_pass_both_gates_without_becoming_listings(
    audited_subset,
):
    binding = admit_both_phases(audited_subset)
    assert binding["held_count"] == 2
    held = {
        post
        for post, _reason in audited_subset[2]["sources"][0]["currentInventoryProof"]["held"]
    }
    assert not held.intersection(
        str(row["daumPostId"]) for row in audited_subset[2]["listings"]
    )


def test_v2_unknown_tenure_uses_full_detail_shape(v2_unknown_details):
    binding = admit_both_phases(v2_unknown_details)
    assert binding["scope"] == "daum_verified_subset_v2"
    assert binding["whole_lease_coverage"] == "unknown"


def test_v2_unknown_tenure_cannot_use_empty_title_marker(v2_unknown_details):
    for entry in v2_unknown_details[2]["sources"]:
        proof = entry["currentInventoryProof"]
        observation = proof["heldDetailObservations"][0]
        member = next(row for row in proof["cohort"] if row[0] == observation["postId"])
        observation["detail"] = {
            "postId": int(observation["postId"]),
            "observedCanonicalUrl": member[1],
            "tenure": member[5],
            "tenureText": member[6],
            "emptyRequiredTitle": True,
        }
    helpers.refresh(v2_unknown_details[1], v2_unknown_details[2], v2_unknown_details[3])
    with pytest.raises(ValueError):
        admit(v2_unknown_details)


def test_v2_empty_title_uses_only_the_existing_minimal_marker(v2):  # noqa: F811
    for entry in v2[2]["sources"]:
        proof = entry["currentInventoryProof"]
        proof["heldDetailObservations"] = detail_observations(proof)
    helpers.refresh(v2[1], v2[2], v2[3])
    observations = v2[2]["sources"][0]["currentInventoryProof"][
        "heldDetailObservations"
    ]
    assert set(observations[0]["detail"]) == {
        "postId",
        "observedCanonicalUrl",
        "tenure",
        "tenureText",
        "emptyRequiredTitle",
    }
    assert observations[0]["detail"]["emptyRequiredTitle"] is True
    assert admit_both_phases(v2)["held_count"] == 1


def test_v2_empty_title_cannot_use_full_detail_shape(v2):  # noqa: F811
    for entry in v2[2]["sources"]:
        proof = entry["currentInventoryProof"]
        proof["heldDetailObservations"] = detail_observations(proof)
        observation = proof["heldDetailObservations"][0]
        member = next(row for row in proof["cohort"] if row[0] == observation["postId"])
        variant = copy.deepcopy(proof)
        variant["held"] = [
            (post, "other" if post == observation["postId"] else reason)
            for post, reason in proof["held"]
        ]
        full_observation = next(
            item
            for item in detail_observations(variant)
            if item["postId"] == observation["postId"]
        )
        observation["detail"] = full_observation["detail"]
        observation["detail"]["postId"] = int(observation["postId"])
        observation["detail"]["observedCanonicalUrl"] = member[1]
        observation["detail"]["tenure"] = member[5]
        observation["detail"]["tenureText"] = member[6]
    helpers.refresh(v2[1], v2[2], v2[3])
    with pytest.raises(ValueError):
        admit(v2)


def test_new_no_holds_producer_array_is_accepted(no_holds_subset):
    binding = admit_both_phases(no_holds_subset)
    assert binding["held_count"] == 0
    assert all(
        entry["currentInventoryProof"]["heldDetailObservations"] == []
        for entry in no_holds_subset[2]["sources"]
    )


def test_historical_subset_without_detail_audit_remains_compatible(subset):  # noqa: F811
    assert all(
        "heldDetailObservations" not in entry["currentInventoryProof"]
        for entry in subset[2]["sources"]
    )
    assert admit(subset)["held_count"] == 2


@pytest.mark.parametrize(
    "fault",
    [
        "not-list",
        "missing",
        "duplicate",
        "foreign",
        "extra-observation-key",
        "wrong-inner-post",
        "boolean-inner-post",
        "wrong-url",
        "wrong-tenure",
        "wrong-tenure-text",
        "extra-detail-field",
        "missing-detail-field",
        "empty-title",
        "facts-not-object",
        "facts-value-not-text",
        "highlights-not-text-array",
        "brochure-extra-field",
        "photo-not-text-array",
        "contact-missing-field",
        "contact-value-not-text",
        "minimal-marker-in-v1",
    ],
)
def test_malformed_or_unbound_full_detail_audit_refuses(audited_subset, fault):
    for entry in audited_subset[2]["sources"]:
        proof = entry["currentInventoryProof"]
        observations = proof["heldDetailObservations"]
        observation = observations[0]
        detail = observation["detail"]
        if fault == "not-list":
            proof["heldDetailObservations"] = None
        elif fault == "missing":
            observations.pop()
        elif fault == "duplicate":
            observations.append(copy.deepcopy(observation))
        elif fault == "foreign":
            observation["postId"] = "59999"
        elif fault == "extra-observation-key":
            observation["unexpected"] = True
        elif fault == "wrong-inner-post":
            detail["postId"] += 1
        elif fault == "boolean-inner-post":
            detail["postId"] = True
        elif fault == "wrong-url":
            detail["observedCanonicalUrl"] += "?other=1"
        elif fault == "wrong-tenure":
            detail["tenure"] = "lease"
        elif fault == "wrong-tenure-text":
            detail["tenureText"] = "Sale"
        elif fault == "extra-detail-field":
            detail["invented"] = "not part of DaumDetail"
        elif fault == "missing-detail-field":
            del detail["description"]
        elif fault == "empty-title":
            detail["title"] = " \t\n"
        elif fault == "facts-not-object":
            detail["facts"] = []
        elif fault == "facts-value-not-text":
            detail["facts"]["Address"] = 123
        elif fault == "highlights-not-text-array":
            detail["highlights"] = [None]
        elif fault == "brochure-extra-field":
            detail["brochures"][0]["extra"] = True
        elif fault == "photo-not-text-array":
            detail["photos"] = [1]
        elif fault == "contact-missing-field":
            del detail["contacts"][0]["license"]
        elif fault == "contact-value-not-text":
            detail["contacts"][0]["email"] = 123
        elif fault == "minimal-marker-in-v1":
            row = proof["cohort"]
            post = observation["postId"]
            member = next(item for item in row if item[0] == post)
            observation["detail"] = {
                "postId": int(post),
                "observedCanonicalUrl": member[1],
                "tenure": member[5],
                "tenureText": member[6],
                "emptyRequiredTitle": True,
            }
    helpers.refresh(audited_subset[1], audited_subset[2], audited_subset[3])
    with pytest.raises(ValueError):
        admit(audited_subset)


@pytest.mark.parametrize(
    "fault",
    ["marker-false", "marker-not-boolean", "extra-field", "wrong-url", "wrong-post"],
)
def test_empty_title_marker_is_exact_and_bound_to_the_v2_hold(v2, fault):  # noqa: F811
    for entry in v2[2]["sources"]:
        proof = entry["currentInventoryProof"]
        proof["heldDetailObservations"] = detail_observations(proof)
        observation = proof["heldDetailObservations"][0]
        detail = observation["detail"]
        if fault == "marker-false":
            detail["emptyRequiredTitle"] = False
        elif fault == "marker-not-boolean":
            detail["emptyRequiredTitle"] = 1
        elif fault == "extra-field":
            detail["title"] = "Invented full detail"
        elif fault == "wrong-url":
            detail["observedCanonicalUrl"] += "?other=1"
        elif fault == "wrong-post":
            detail["postId"] += 1
    helpers.refresh(v2[1], v2[2], v2[3])
    with pytest.raises(ValueError):
        admit(v2)


def test_held_details_must_keep_held_url_order(subset):  # noqa: F811
    for entry in subset[2]["sources"]:
        proof = entry["currentInventoryProof"]
        proof["heldDetailObservations"] = detail_observations(proof)[::-1]
    helpers.refresh(subset[1], subset[2], subset[3])
    with pytest.raises(ValueError):
        admit(subset)


def test_detail_audit_must_match_between_sale_and_lease_proofs(audited_subset):
    lease_proof = audited_subset[2]["sources"][1]["currentInventoryProof"]
    lease_proof["heldDetailObservations"][0]["detail"]["description"] = "Drift"
    helpers.refresh(audited_subset[1], audited_subset[2], audited_subset[3])
    with pytest.raises(ValueError):
        admit(audited_subset)
