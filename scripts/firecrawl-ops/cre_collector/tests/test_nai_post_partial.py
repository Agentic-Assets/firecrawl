import base64
import json

import pytest

from cre_nai_post_partial import partition_posts, stage_post


def post(number=1, alias=None, tx="sale"):
    post_id = str(number)
    alias = alias or f"https://www.naiglobal.com/listings/?propertyId=post-{post_id}"
    raw = {"id": number, "url": f"https://infabode.com/post/{post_id}",
           "urlOriginal": alias, "contentType": {"id": 4 if tx == "sale" else 10},
           "listingStatus": ["FOR_SALE_ON_MARKET"], "unusedSourceField": {"x": [1, None]}}
    return {"sourceKey": "nai-global", "id": f"infabode:{post_id}",
            "url": f"https://infabode.com/services/listings/{post_id}",
            "canonicalUrl": alias, "transactionMode": tx, "transactionType": tx.title(),
            "name": "Fixture post", "feedRow": raw, "publicPost": dict(raw),
            "inventoryObservedAt": "2026-11-05T10:00:00Z",
            "detailObservedAt": "2026-11-05T10:00:01Z",
            "freshnessProvenance": {"generationId": "gen-1", "detailScope": "source_native_public_record",
                                    "method": "infabode_public_posts_graphql", "cacheDisposition": "live"}}


def test_source_post_retains_full_input_without_fabricating_status_or_location():
    source = post()
    row = stage_post(source, "gen-1")
    assert row["external_id"] == "infabode:1"
    assert row["source_url"] == "https://infabode.com/services/listings/1"
    assert row["lat"] is None and row["lng"] is None
    assert "status" not in row
    assert json.loads(base64.b64decode(row["observation_base64"])) == source


def test_identified_inventory_post_survives_missing_detail_and_category():
    source = post()
    source.pop("publicPost")
    source.pop("detailObservedAt")
    source.pop("canonicalUrl")
    source.pop("transactionMode")
    source["feedRow"].pop("contentType")
    row = stage_post(source, "gen-1")
    assert row["external_id"] == "infabode:1"
    assert row["transaction_type"] is None and row["alias_key"] == "https://www.naiglobal.com/listings?propertyId=post-1"
    assert json.loads(base64.b64decode(row["observation_base64"])) == source


def test_prices_require_us_currency_and_lease_amount_has_no_invented_rate_basis():
    sale = post()
    sale["salePriceUsd"] = 2500000
    sale["publicPost"].update(currency="DOLLAR", locations=[{"path": "Seattle, WA, United States"}])
    assert stage_post(sale, "gen-1")["sale_price_usd"] == 2500000
    assert stage_post(sale, "gen-1")["country"] == "US"
    sale["publicPost"]["locations"] = [{"path": "Toronto, ON, Canada"}]
    assert stage_post(sale, "gen-1")["sale_price_usd"] is None
    assert stage_post(sale, "gen-1")["country"] is None
    sale["publicPost"]["locations"] = [{"path": "Seattle, WA, United States"}]
    sale["feedRow"]["contentType"]["id"] = 999
    assert stage_post(sale, "gen-1")["sale_price_usd"] is None
    lease = post(2, tx="lease")
    lease["leaseRateMin"] = 12
    lease["publicPost"].update(currency="DOLLAR", locations=[{"path": "Seattle, WA, United States"}])
    assert stage_post(lease, "gen-1")["lease_rate_min"] is None


def test_conflicting_building_class_tags_remain_raw_without_projection():
    source = post()
    source["buildingClass"] = "A"
    source["publicPost"]["tags"] = ["BuildingClassA", "BuildingClassB"]
    staged = stage_post(source, "gen-1")
    assert staged["building_class"] is None
    assert json.loads(base64.b64decode(staged["observation_base64"])) == source


def test_alias_and_native_id_collisions_hold_only_affected_peers():
    alias = "https://www.naiglobal.com/listings/?propertyId=collision"
    records = [post(1, alias), post(2, alias), post(3), post(3), post(4)]
    part = partition_posts(records, "gen-1")
    assert [x["external_id"] for x in part["staged"]] == ["infabode:4"]
    assert [x["reason"] for x in part["held"]] == [
        "conflicting_source_alias", "conflicting_source_alias", "duplicate_post_id", "duplicate_post_id"]


@pytest.mark.parametrize("change", [
    lambda x: x["publicPost"].update(id=123),
    lambda x: x["feedRow"].update(url="https://infabode.com/post/other"),
    lambda x: x["publicPost"]["contentType"].update(id=10),
    lambda x: x.update(url="https://example.test/1"),
    lambda x: x["freshnessProvenance"].update(generationId="other"),
    lambda x: x.update(id="infabode:9007199254740992"),
])
def test_unqualified_source_evidence_holds_without_discarding_valid_peer(change):
    bad = post(1)
    change(bad)
    result = partition_posts([bad, post(2)], "gen-1")
    assert [x["external_id"] for x in result["staged"]] == ["infabode:2"]
    assert result["held"] == [{"index": 0, "reason": "unqualified_source_post"}]

def test_invalid_peer_still_conflicts_with_valid_source_identity():
    bad = post(1)
    bad["publicPost"]["id"] = 99
    result = partition_posts([bad, post(1), post(2)], "gen-1")
    assert [row["external_id"] for row in result["staged"]] == ["infabode:2"]
    assert [row["reason"] for row in result["held"]] == [
        "unqualified_source_post", "duplicate_post_id"]


def test_incomplete_peer_still_witnesses_its_raw_source_alias():
    alias = "https://example.test/shared"
    incomplete = post(1, alias)
    incomplete.pop("canonicalUrl")
    result = partition_posts([incomplete, post(2, alias), post(3)], "gen-1")
    assert [row["external_id"] for row in result["staged"]] == ["infabode:3"]
    assert [row["reason"] for row in result["held"]] == [
        "conflicting_source_alias", "conflicting_source_alias"]


def test_unqualified_post_source_url_still_witnesses_an_alias_conflict():
    bad = post(1)
    bad["publicPost"]["id"] = 99
    candidate = post(2, bad["url"])
    result = partition_posts([bad, candidate, post(3)], "gen-1")
    assert [row["external_id"] for row in result["staged"]] == ["infabode:3"]
    assert [row["reason"] for row in result["held"]] == [
        "unqualified_source_post", "conflicting_source_alias"]
