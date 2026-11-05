import copy
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cre_colliers_partial import verify_detail_record


def sample():
    raw = json.dumps({"ProjectSummary": {"ProjectId": "42"}})
    source = {"generation": "gen", "attempt": 1, "start": 1, "page": 1, "pageSize": 1, "offset": 0,
              "inventoryObservedAt": "2026-11-04T15:30:00Z", "detailObservedAt": "2026-11-04T16:01:00Z",
              "mapProjectId": "42", "detailTokenSha256": hashlib.sha256(b"fixture").hexdigest(), "listSha256": "a" * 64, "mapSha256": "b" * 64,
              "decodedDetailSha256": hashlib.sha256(raw.encode()).hexdigest(), "nativeSha256": hashlib.sha256(raw.encode()).hexdigest()}
    listing = {"sourceKey": "colliers", "transactionMode": "sale", "transactionType": "Investment Sale", "id": "42", "brokerIds": [],
               "url": "https://my.rcm1.com/handler/landing.aspx?pv=fixture",
               "preserveChildCollections": True, "inventoryObservedAt": source["inventoryObservedAt"],
               "detailObservedAt": source["detailObservedAt"],
               "freshnessProvenance": {"generationId": "gen", "method": "colliers_salestracker_slp_detail"},
               "colliersPartialEvidence": {"schema": "colliers-partial-detail-v1", **source},
               "colliersSalesTrackerDetail": {"projectId": "42"}}
    return {"listing": listing, "source": source, "decodedDetail": raw}


def verify(item):
    return verify_detail_record(item, "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z")


def test_bound_detail_identity_and_clock():
    assert verify(sample())["external_id"] == "42"


@pytest.mark.parametrize("mutate", [
    lambda item: item["source"].update(attempt=True),
    lambda item: item["source"].update(mapProjectId="wrong"),
    lambda item: item["source"].update(decodedDetailSha256="0" * 64),
    lambda item: item["listing"].update(preserveChildCollections=False),
    lambda item: item["listing"].update(brokerIds=[0]),
    lambda item: item["listing"]["freshnessProvenance"].update(generationId="other"),
    lambda item: item["source"].update(detailObservedAt="2026-11-04T15:35:00Z"),
])
def test_refuses_tampered_detail_or_binding(mutate):
    item = copy.deepcopy(sample())
    mutate(item)
    with pytest.raises(ValueError):
        verify(item)


def test_missing_native_project_id_cannot_match_literal_none():
    item = sample()
    item["decodedDetail"] = json.dumps({"ProjectSummary": {}})
    item["source"]["decodedDetailSha256"] = hashlib.sha256(item["decodedDetail"].encode()).hexdigest()
    item["source"]["mapProjectId"] = "None"
    item["listing"]["id"] = "None"
    item["listing"]["colliersSalesTrackerDetail"]["projectId"] = "None"
    item["listing"]["colliersPartialEvidence"] = {"schema": "colliers-partial-detail-v1", **item["source"]}
    with pytest.raises(ValueError):
        verify(item)


def test_canonical_mapper_uses_verified_id_and_registered_source():
    from cre_colliers_partial import stage_detail_record

    row = stage_detail_record(sample(), "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", "2026-11-04T16:02:00Z")
    assert row["slug"] == "colliers" and row["external_id"] == "42" and row["transaction_type"] == "sale"


def test_canonical_mapper_holds_an_untrusted_source_url():
    from cre_colliers_partial import stage_detail_record

    item = sample()
    item["listing"]["url"] = "https://other.example/handler/landing.aspx?pv=fixture"
    with pytest.raises(ValueError, match="source URL"):
        stage_detail_record(item, "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", "2026-11-04T16:02:00Z")


def test_canonical_mapper_rejects_duplicate_source_token():
    from cre_colliers_partial import stage_detail_record

    item = sample()
    item["listing"]["url"] += "&pv=other"
    with pytest.raises(ValueError, match="token"):
        stage_detail_record(item, "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", "2026-11-04T16:02:00Z")


def test_canonical_mapper_rejects_token_from_other_card():
    from cre_colliers_partial import stage_detail_record

    item = sample()
    item["listing"]["url"] = item["listing"]["url"].replace("fixture", "other")
    with pytest.raises(ValueError, match="token"):
        stage_detail_record(item, "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", "2026-11-04T16:02:00Z")


def test_canonical_mapper_rejects_detail_after_artifact_finish():
    from cre_colliers_partial import stage_detail_record

    with pytest.raises(ValueError, match="after artifact finished"):
        stage_detail_record(sample(), "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", "2026-11-04T16:00:30Z")


def test_canonical_mapper_rejects_colliers_main_namespace():
    from cre_colliers_partial import stage_detail_record

    item = sample()
    item["decodedDetail"] = json.dumps({"ProjectSummary": {"ProjectId": "main:42"}})
    item["source"]["decodedDetailSha256"] = hashlib.sha256(item["decodedDetail"].encode()).hexdigest()
    item["source"]["mapProjectId"] = "main:42"
    item["listing"]["id"] = "main:42"
    item["listing"]["colliersSalesTrackerDetail"]["projectId"] = "main:42"
    item["listing"]["colliersPartialEvidence"] = {"schema": "colliers-partial-detail-v1", **item["source"]}
    with pytest.raises(ValueError, match="staged identity"):
        stage_detail_record(item, "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", "2026-11-04T16:02:00Z")


def test_canonical_mapper_rejects_future_artifact_finish():
    from cre_colliers_partial import stage_detail_record

    future = (datetime.now(UTC) + timedelta(minutes=6)).isoformat()
    with pytest.raises(ValueError, match="clock-skew"):
        stage_detail_record(sample(), "gen", 1, "2026-11-04T15:00:00Z", "2026-11-04T16:00:00Z", future)



def test_source_bytes_through_node_replay_into_python_staging(tmp_path):
    from cre_colliers_partial import stage_detail_record

    collector = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        ["node", "--import", "tsx", "tests/ts/fixtures/colliers-window-staging.ts"],
        cwd=collector, env={**os.environ, "COLLIERS_FIXTURE_DIRECTORY": str(tmp_path)},
        capture_output=True, text=True, timeout=60, check=True,
    )
    fixture = json.loads(completed.stdout)
    assert len(fixture["rows"]) == 1
    staged = stage_detail_record(fixture["rows"][0], "gen", 1, "2026-11-04T15:00:00Z",
                                 "2026-11-04T16:00:00Z", fixture["artifactFinishedAt"])
    assert staged["external_id"] == "native-e2e"
    assert fixture["rows"][0]["listing"]["city"] == "Native City"
