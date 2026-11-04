"""Finite role proofs use actual canonical staging and duplicate-pass merges."""

import copy

import pytest

import cre_ingest as ingest
from cre_transwestern_image_roles import role_proof

URL = "https://transwestern.com/property/sample-property"
PHOTO = "https://images.transwestern.com/property/primary.jpg"
SECOND = "https://images.transwestern.com/property/second.jpg"
AVATAR = "https://images.transwestern.com/f00000000081388/s3.amazonaws.com/transwestern-userphoto/person.jpg"
MARKER = "https://transwestern.com/assets/images/transwestern-mapmarker.png"
STAMP = "2026-10-19T20:00:00Z"


def listing(*, photos=None, primary=None, exclusions=None, transaction="sale"):
    value = {
        "sourceKey": "transwestern", "id": "sample-property", "url": URL,
        "canonicalUrl": URL, "name": "Sample Property",
        "transactionMode": transaction, "transactionType": transaction.title(),
        "photos": [PHOTO] if photos is None else photos,
        "rawTranswesternFeed": {"PropertyImage": PHOTO},
        "transwesternPhotoRoleVersion": "v1",
        "inventoryObservedAt": STAMP, "detailObservedAt": STAMP,
        "freshnessProvenance": {"method": "transwestern_detail_page", "cacheDisposition": "live"},
    }
    if primary:
        value["transwesternPrimaryPhotoEvidence"] = primary
    if exclusions is not None:
        value["transwesternExcludedPhotoEvidence"] = {
            "schema": "transwestern-photo-role-v1", "entries": exclusions,
        }
    return value


def staged(*passes):
    rows = [ingest.to_row(copy.deepcopy(value), {}, STAMP) for value in passes]
    assert all(row is not None for row in rows)
    result = rows[0]
    for row in rows[1:]:
        ingest.validate_duplicate_identity_before_merge(result, row)
        result = ingest.merge_rows(result, row)
    return result


def portrait():
    return {"url": AVATAR, "role": "broker_portrait", "basis": "parsed_contact_avatar"}


def template(url=MARKER):
    return {"url": url, "role": "template_resource", "basis": "existing_source_template_policy"}


def test_flat_primary_and_exclusions_are_json_compatible_without_mutation():
    value = listing(primary={"url": PHOTO, "basis": "provider_feed"}, exclusions=[portrait(), template()])
    value["contactsDetailed"] = [{"name": "Broker", "avatarUrl": AVATAR}]
    row = staged(value)
    original = copy.deepcopy(row)
    assert role_proof(row) == {
        "excluded": [portrait(), template()], "primary": {"url": PHOTO, "basis": "provider_feed"},
    }
    assert row == original


@pytest.mark.parametrize("photos", [[], [PHOTO]])
def test_absent_primary_witness_is_explicit_uncertainty(photos):
    assert role_proof(staged(listing(photos=photos))) == {"excluded": [], "primary": None}


def test_actual_two_and_three_pass_wrappers_are_fully_read():
    a = listing(exclusions=[template()])
    b = listing(primary={"url": PHOTO, "basis": "provider_feed"}, transaction="lease")
    c = listing(photos=[PHOTO, SECOND], exclusions=[template()])
    for passes in [(a, b), (a, b, c), (a, copy.deepcopy(a))]:
        row = staged(*passes)
        assert set(row["raw_data"]) == {"primary", "secondary_pass"}
        result = role_proof(row)
        assert result["excluded"] == [template()]
        assert result["primary"] == ({"url": PHOTO, "basis": "provider_feed"} if len(passes) == 3 or passes[1] is b else None)


def test_retained_sale_gallery_cannot_be_deleted_using_lease_portrait_role():
    sale = listing(photos=[AVATAR], primary={"url": AVATAR, "basis": "property_gallery"})
    lease = listing(photos=[PHOTO], exclusions=[portrait()], transaction="lease")
    lease["contactsDetailed"] = [{"name": "Broker", "avatarUrl": AVATAR}]
    result = role_proof(staged(sale, lease))
    assert result == {"excluded": [], "primary": {"url": AVATAR, "basis": "property_gallery"}}


def test_final_merged_primary_wins_over_different_secondary_witness():
    sale = listing(primary={"url": PHOTO, "basis": "provider_feed"})
    lease = listing(photos=[SECOND], primary={"url": SECOND, "basis": "property_gallery"}, transaction="lease")
    assert role_proof(staged(sale, lease))["primary"] == {"url": PHOTO, "basis": "provider_feed"}
    assert role_proof(staged(listing(photos=[]), lease))["primary"] == {"url": SECOND, "basis": "property_gallery"}


def test_secondary_witness_does_not_invent_authority_for_unknown_first_primary():
    a = listing()
    b = listing(photos=[SECOND], primary={"url": SECOND, "basis": "property_gallery"}, transaction="lease")
    assert role_proof(staged(a, b))["primary"] is None


@pytest.mark.parametrize("field,value", [
    ("sourceKey", "savills"), ("id", "another-property"),
    ("url", "https://transwestern.com/property/another-property"),
    ("canonicalUrl", "https://transwestern.com/property/another-property"),
    ("transwesternPhotoRoleVersion", "v2"), ("detailError", "failed detail"),
    ("detailObservedAt", None),
])
def test_one_bad_duplicate_leaf_cannot_borrow_good_sibling_proof(field, value):
    row = staged(listing(), listing(transaction="lease"))
    row["raw_data"]["secondary_pass"][field] = value
    with pytest.raises(ValueError):
        role_proof(row)


def test_foreign_direct_leaf_does_not_unwrap_nested_tw_proof():
    row = staged(listing())
    row["raw_data"] = {"sourceKey": "savills", "primary": listing()}
    with pytest.raises(ValueError):
        role_proof(row)


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(slug="savills"),
    lambda r: r.update(raw_data={"primary": r["raw_data"]}),
    lambda r: r["raw_data"].pop("transwesternPhotoRoleVersion"),
    lambda r: r["raw_data"].update(transwesternExcludedPhotoEvidence={"schema": "other", "entries": []}),
    lambda r: r["raw_data"].update(transwesternExcludedPhotoEvidence={"schema": "transwestern-photo-role-v1", "entries": [portrait()]}),
    lambda r: r["raw_data"].update(transwesternPrimaryPhotoEvidence={"url": SECOND, "basis": "provider_feed"}),
    lambda r: r["raw_data"].update(transwesternPrimaryPhotoEvidence={"url": PHOTO, "basis": "filename"}),
    lambda r: r["images"][0].update(isPrimary=1),
    lambda r: r["images"].append({"url": SECOND, "isPrimary": True, "order": 1}),
])
def test_malformed_or_inconsistent_roles_refuse(mutate):
    row = staged(listing())
    mutate(row)
    with pytest.raises(ValueError):
        role_proof(row)


def test_avatar_only_card_cannot_prove_portrait_and_other_leaf_contact_cannot_lend_role():
    bad = listing(exclusions=[portrait()])
    bad["contactsDetailed"] = [{"avatarUrl": AVATAR}]
    good = listing(transaction="lease")
    good["contactsDetailed"] = [{"name": "Broker", "avatarUrl": AVATAR}]
    with pytest.raises(ValueError):
        role_proof(staged(bad, good))


@pytest.mark.parametrize("url", [
    "https://transwestern.com.example.test/assets/images/tw_gl.png",
    "https://transwestern.com/assets/images/mailbox-warehouse.jpg",
    "https://transwestern.com/assets/images/mail-3.png-extra",
    "https://images.transwestern.com/f00000000081388/example.test/assets/images/mail-3.png",
])
def test_template_evidence_does_not_expand_owned_exact_path_policy(url):
    with pytest.raises(ValueError):
        role_proof(staged(listing(exclusions=[template(url)])))


def test_proxy_template_and_duplicate_evidence_are_deduplicated():
    entry = template("https://images.transwestern.com/f00000000081388/transwestern.com/assets/images/mail-3.png")
    assert role_proof(staged(listing(exclusions=[entry, entry]))) == {"excluded": [entry], "primary": None}


def test_exclusion_cannot_contradict_same_leaf_retained_photo():
    with pytest.raises(ValueError):
        role_proof(staged(listing(photos=[MARKER], exclusions=[template()])))


def test_feed_primary_requires_its_own_feed_reference():
    value = listing(primary={"url": PHOTO, "basis": "provider_feed"})
    value["rawTranswesternFeed"]["PropertyImage"] = SECOND
    with pytest.raises(ValueError):
        role_proof(staged(value))


@pytest.mark.parametrize("url", [
    "https://example.test/property/sample-property",
    "https://transwestern.com.example.test/property/sample-property",
    "https://transwestern.com/properties/sample-property",
    "https://transwestern.com/property/",
])
def test_self_consistent_foreign_page_cannot_be_transwestern_role_authority(url):
    value = listing()
    value["url"] = value["canonicalUrl"] = url
    with pytest.raises(ValueError):
        role_proof(staged(value))


@pytest.mark.parametrize("host", ["transwestern.com", "www.transwestern.com"])
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_ordinary_owned_url_forms_preserve_exact_identity(host, scheme):
    value = listing()
    value["url"] = value["canonicalUrl"] = f"{scheme}://{host}/property/sample-property"
    assert role_proof(staged(value)) == {"excluded": [], "primary": None}


def test_feed_reference_uses_source_clean_whitespace_before_url_resolution():
    value = listing(primary={"url": PHOTO, "basis": "provider_feed"})
    value["rawTranswesternFeed"]["PropertyImage"] = f" \n\t{PHOTO}  "
    assert role_proof(staged(value))["primary"] == {"url": PHOTO, "basis": "provider_feed"}
