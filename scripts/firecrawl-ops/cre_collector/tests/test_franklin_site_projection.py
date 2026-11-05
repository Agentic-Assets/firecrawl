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