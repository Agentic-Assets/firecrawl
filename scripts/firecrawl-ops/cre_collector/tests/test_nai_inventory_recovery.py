"""Pure source recovery partition; no process, provider or database IO."""
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from cre_nai_inventory_recovery import SCHEMA, derive
from tests.test_cre_checkpoint_refresh import strict_artifact
from tests.test_nai_post_partial import post


def recovery_source():
    data = strict_artifact("nai-global", detail_scope="source_native_public_record",
        generation="gen-1", generation_started_at="2026-11-05T09:58:00Z")
    data["runMeta"].update(startedAt="2026-11-05T09:59:00Z", finishedAt="2026-11-05T10:01:00Z")
    one, two = post(1), post(2, tx="lease")
    one.pop("publicPost")
    one.pop("detailObservedAt")
    one["freshnessProvenance"]["detailScope"] = "inventory_only"
    data["listings"] = [one, two]
    data["sources"][0]["freshness"]["detailErrors"] = 1
    return data


def partition(data):
    raw = json.dumps(data, separators=(",", ":")).encode()
    return derive(raw, "gen-1", "2026-11-05T09:58:00Z", "2026-11-05T09:58:30Z",
                  "2026-11-05T10:02:00Z", now=datetime(2026, 11, 5, 10, 2, tzinfo=timezone.utc))


def test_partial_preserves_identified_inventory_and_holds_collision():
    data = recovery_source()
    data["listings"][1]["canonicalUrl"] = data["listings"][0]["canonicalUrl"]
    data["listings"][1]["publicPost"]["urlOriginal"] = data["listings"][0]["canonicalUrl"]
    data["listings"][1]["feedRow"]["urlOriginal"] = data["listings"][0]["canonicalUrl"]
    data["listings"].append(post(3))
    data["totalListings"] = 3
    data["sources"][0]["listingsCollected"] = 2
    candidate, result = partition(data)
    assert json.loads(candidate)["schema"] == SCHEMA
    assert [row["external_id"] for row in result["staged"]] == ["infabode:3"]
    assert [row["reason"] for row in result["held"]] == ["conflicting_source_alias", "conflicting_source_alias"]


def test_source_denial_and_truncation_are_not_recovery_authority():
    data = recovery_source()
    data["listings"][0]["detailError"] = "HTTP 403"
    with pytest.raises(ValueError, match="access outcome"):
        partition(data)
    data["listings"][0].pop("detailError")
    data["sources"][0]["truncated"] = True
    with pytest.raises(ValueError, match="source pass"):
        partition(data)


def test_retained_source_numeric_precision_is_not_binary_rounded():
    data = recovery_source()
    data["listings"][0]["feedRow"]["decimalWitness"] = "REPLACE_WITH_JSON_NUMBER"
    raw = json.dumps(data).replace('"REPLACE_WITH_JSON_NUMBER"', '0.12345678901234567890123456789').encode()
    candidate, _ = derive(raw, "gen-1", "2026-11-05T09:58:00Z", "2026-11-05T09:58:30Z",
                          "2026-11-05T10:02:00Z", now=datetime(2026, 11, 5, 10, 2, tzinfo=timezone.utc))
    assert json.loads(candidate, parse_float=Decimal)["listings"][0]["feedRow"]["decimalWitness"] == Decimal("0.12345678901234567890123456789")
