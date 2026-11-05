import copy
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import quote_plus

import pytest
from test_franklin_site_projection import artifact as page_artifact

import cre_franklin_site_mixed as mixed
from cre_buildout_current_inventory import SCHEMA, SORT
from source_fields.buildout import _core_definitions


def fixture():
    data = page_artifact()
    start = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    generation_start = (datetime.now(UTC) - timedelta(minutes=3)).isoformat()
    observed = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    finished = datetime.now(UTC).isoformat()
    generation = "mixed-run"
    data["runMeta"].update(transactions=["sale", "lease"], maxItemsPerSource=None,
                           startedAt=start, finishedAt=finished,
                           freshness={"generationId": generation, "generationStartedAt": generation_start,
                                      "requireFreshDetails": True})
    page = data["sources"][0]["retainedObservations"][0]
    page["observedAt"] = observed
    definition = _core_definitions()["franklin-street"]["lease"]
    url = f"https://{definition['host']}/listings/?propertyId=20-lease"
    native = {"id": "20", "showLink": url, "sale": False, "closed": False,
              "underContract": False, "alsoForSaleOrLease": False}
    row = {"sourceKey": "franklin-street", "sourceCompany": definition["company"], "id": "20",
           "name": "Buildout peer", "transactionMode": "lease", "transactionType": "Lease",
           "url": url, "canonicalUrl": url, "preserveChildCollections": True,
           "inventoryObservedAt": observed,
           "freshnessProvenance": {"generationId": generation, "cacheDisposition": "live",
                                   "method": "buildout_inventory_feed", "detailScope": "authoritative_inventory_feed"}}
    feed_page = {"index": 0, "endpoint": f"https://buildout.com/plugins/{definition['plugin']}/inventory.json?page=0&q%5Bs%5D%5B%5D={quote_plus(SORT)}",
                 "observedAt": observed, "generationId": generation, "cacheDisposition": "live",
                 "transport": "direct_json", "parsedJsonSha256": "c" * 64,
                 "total": 1, "limit": 10, "rows": [native]}
    proof = {"schema": SCHEMA, "kind": "complete_paginated_feed", "sourceKey": "franklin-street-lease",
             "transaction": "lease", "generationId": generation, "pluginKey": definition["plugin"],
             "listingsPage": definition["page"], "sort": SORT, "total": 1, "limit": 10, "pages": [feed_page]}
    data["sources"].append({"sourceKey": "franklin-street", "transaction": "lease", "supported": True,
                            "truncated": False, "listingsCollected": 1, "totalAvailableOnSource": 1,
                            "currentInventoryProof": proof})
    data["listings"] = [row]
    data["brokers"] = []
    data["totalListings"] = 1
    return data


def make_binding(data):
    raw = json.dumps(data).encode()
    return raw, {"artifact_sha256": hashlib.sha256(raw).hexdigest(), "attempt_id": "a" * 32,
                 "generation_id": "mixed-run", "producer_sha": "b" * 40, "database_target_sha256": "c" * 64,
                 "generation_started_at": data["runMeta"]["freshness"]["generationStartedAt"],
                 "started_at": data["runMeta"]["startedAt"], "finished_at": data["runMeta"]["finishedAt"]}


def test_mixed_plan_retains_both_lanes_and_rejects_mutations():
    data = fixture()
    raw, binding = make_binding(data)
    result = mixed.plan(raw, binding, [], {"PATH": os.environ["PATH"]})
    assert result["buildout_count"] == 1 and result["site_count"] == 1
    assert result["buildout_sql"] and result["site_sql"]
    assert result["coverage"] == "partial_unknown"
    for mutation in ("proof", "row", "count"):
        changed = copy.deepcopy(data)
        if mutation == "proof":
            changed["sources"][1]["currentInventoryProof"]["pages"][0]["rows"][0]["id"] = "99"
        elif mutation == "row":
            changed["listings"][0]["url"] = "https://evil.example/a"
        else:
            changed["sources"][1]["listingsCollected"] = 2
        bad, bound = make_binding(changed)
        with pytest.raises((ValueError, RuntimeError)):
            mixed.plan(bad, bound, [], {"PATH": os.environ["PATH"]})