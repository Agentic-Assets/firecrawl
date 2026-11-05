import hashlib
import json
import os

import pytest

from cre_franklin_site_projection import project


def artifact():
    html = '<article data-listing-card="true" data-listing-id="12345678-1234-5123-8123-123456789abc" data-offer-type="FOR_SALE"><a href="/properties/for-sale/a">A</a></article>'
    attrs = {"data-listing-card": "true", "data-listing-id": "12345678-1234-5123-8123-123456789abc", "data-offer-type": "FOR_SALE"}
    page = {"schema": "franklin-street-page-observation-v1", "coverage": "partial_unknown", "transaction": "sale",
            "sourceUrl": "https://franklinst.com/properties/for-sale/", "observedAt": "2026-11-04T12:00:01Z",
            "decodedHtmlSha256": hashlib.sha256(html.encode()).hexdigest(), "cards": [{"attributes": attrs, "html": html}],
            "decodedHtml": html, "httpStatus": 200, "contentType": "text/html"}
    return {"runMeta": {"mode": "full", "startedAt": "2026-11-04T12:00:00Z", "finishedAt": "2026-11-04T12:00:02Z",
                        "freshness": {"generationId": "run-1"}},
            "sources": [{"sourceKey": "franklin-street", "transaction": "sale", "error": "failed",
                         "retainedObservations": [page]}],
            "listings": [], "brokers": [], "totalListings": 0}


def test_projection_bridges_exact_private_bytes():
    raw = json.dumps(artifact()).encode()
    result = project(raw, "run-1", {"PATH": os.environ["PATH"]})
    assert result["artifact_sha256"] == hashlib.sha256(raw).hexdigest()
    assert len(result["projection"]["listings"]) == 1
    assert result["projection"]["listings"][0]["franklinSiteGenerationId"] == "run-1"


def test_projection_rejects_changed_hash_and_invalid_generation():
    value = artifact()
    value["sources"][0]["retainedObservations"][0]["decodedHtmlSha256"] = "0" * 64
    with pytest.raises(RuntimeError):
        project(json.dumps(value).encode(), "run-1", {"PATH": os.environ["PATH"]})
    with pytest.raises(ValueError):
        project(b"{}", "../bad", {"PATH": os.environ["PATH"]})

def test_two_maximum_pages_survive_aggregate_artifact_limit():
    value = artifact()
    first = value["sources"][0]["retainedObservations"][0]
    suffix = "x" * (8 * 1024 * 1024 - len(first["decodedHtml"].encode()))
    first["decodedHtml"] += suffix
    first["decodedHtmlSha256"] = hashlib.sha256(first["decodedHtml"].encode()).hexdigest()
    second = json.loads(json.dumps(first))
    old_html = second["cards"][0]["html"]
    new_html = old_html.replace('data-offer-type="FOR_SALE"', 'data-offer-type="FOR_LEASE"').replace(
        "/properties/for-sale/a", "/properties/for-lease/a")
    second["decodedHtml"] = new_html + "x" * (8 * 1024 * 1024 - len(new_html.encode()))
    second["decodedHtmlSha256"] = hashlib.sha256(second["decodedHtml"].encode()).hexdigest()
    second["transaction"] = "lease"
    second["sourceUrl"] = "https://franklinst.com/properties/for-lease/"
    second["cards"][0]["html"] = new_html
    second["cards"][0]["attributes"]["data-offer-type"] = "FOR_LEASE"
    value["sources"].append({"sourceKey": "franklin-street", "transaction": "lease", "error": "failed",
                             "retainedObservations": [second]})
    raw = json.dumps(value).encode()
    assert len(raw) > 16 * 1024 * 1024
    output = project(raw, "run-1", {"PATH": os.environ["PATH"]})["projection"]
    assert len(output["listings"]) == 2
    assert len(output["identityUncertainty"]["reusedSiteIds"]) == 1
    assert output["listings"][0]["franklinSiteObservation"]["card"] == first["cards"][0]


def test_maximum_page_and_large_buildout_peer_remain_in_mixed_projection():
    value = artifact()
    page = value["sources"][0]["retainedObservations"][0]
    page["decodedHtml"] += "x" * (8 * 1024 * 1024 - len(page["decodedHtml"].encode()))
    page["decodedHtmlSha256"] = hashlib.sha256(page["decodedHtml"].encode()).hexdigest()
    peer = {"sourceKey": "franklin-street", "transactionMode": "lease", "id": "123", "rawBuildout": "y" * (9 * 1024 * 1024)}
    value["sources"].append({"sourceKey": "franklin-street", "transaction": "lease", "supported": True,
                             "truncated": False, "listingsCollected": 1})
    value["listings"] = [peer]
    value["totalListings"] = 1
    raw = json.dumps(value).encode()
    assert len(raw) > 16 * 1024 * 1024
    output = project(raw, "run-1", {"PATH": os.environ["PATH"]})["projection"]
    assert output["buildout"]["listings"] == [peer]
    assert output["buildout"]["pendingListings"] == 1
    assert output["buildout"]["disposition"] == "private_pending_admission"
    assert len(output["listings"]) == 1
