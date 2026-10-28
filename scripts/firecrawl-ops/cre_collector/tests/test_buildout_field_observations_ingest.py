"""Collector field observations remain evidence, not canonical values."""
import cre_ingest as ingest


def listing(transaction, observation):
    return {
        "sourceKey": "svn",
        "url": f"https://svn.com/x?propertyId=123-{transaction}",
        "transactionMode": transaction,
        "buildoutFieldObservations": observation,
    }


def test_dual_pass_preserves_source_conflict_without_promoting_to_columns():
    sale = {"availableSpaceCount": {"state": "observed", "value": 2},
            "capRate": {"state": "omitted_visibility_unverified", "omittedCount": 1}}
    lease = {"availableSpaceCount": {"state": "observed", "value": 3}}
    a = ingest.to_row(listing("sale", sale), {}, "2026-01-01T00:00:00Z")
    b = ingest.to_row(listing("lease", lease), {}, "2026-01-01T00:00:00Z")
    merged = ingest.merge_rows(a, b)
    assert merged["raw_data"]["primary"]["buildoutFieldObservations"] == sale
    assert merged["raw_data"]["secondary_pass"]["buildoutFieldObservations"] == lease
    assert merged["cap_rate"] is None
    assert "available_space_count" not in merged
