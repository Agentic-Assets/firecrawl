"""DAUM unknown tenure must not be inferred from the collection pass."""

import pytest

from cre_ingest import transaction_type_of


def test_unknown_tenure_has_no_transaction_type():
    listing = {
        "sourceKey": "daum-commercial",
        "transactionMode": "sale",
        "daumUnknownTenure": True,
        "daumTenureEvidence": {
            "inventory": "unknown",
            "detail": "unknown",
            "resolved": "unknown",
        },
    }
    assert transaction_type_of(listing) is None


def test_actual_producer_admits_unknown_once_without_complete_coverage(
    tmp_path, monkeypatch
):
    import test_essex_current_inventory as helpers
    from test_daum_current_inventory import produce
    from test_daum_verified_subset import admit, context, envelope

    import cre_current_inventory_admission as admission
    import cre_daum_current_inventory as daum
    from cre_ingest import build_sql, to_row

    generated = produce(
        tmp_path,
        total=101,
        all_sale=True,
        unknown_inventory=True,
        unknown_detail=True,
        subset_policy="tenure-and-empty-title-v2",
    )
    monkeypatch.setattr(helpers, "SOURCE", daum.SOURCE)
    prepared = helpers.setup(tmp_path, generated)
    prepared[3]["config"]["current_inventory_admission"] = (
        admission.subset_configuration("tenure-and-empty-title-v2")
    )
    binding = admit(prepared)
    proof = prepared[2]["sources"][0]["currentInventoryProof"]
    unknown_ids = set(proof["retainedUnknownTenure"])
    assert unknown_ids
    unknown_rows = [
        row for row in prepared[2]["listings"] if row["daumPostId"] in unknown_ids
    ]
    assert len(unknown_rows) == len(unknown_ids)
    assert all(row["transactionMode"] == "sale" for row in unknown_rows)
    assert all(
        to_row(row, {}, prepared[2]["runMeta"]["finishedAt"])["transaction_type"]
        is None
        for row in unknown_rows
    )
    assert binding["whole_source_coverage"] is False
    assert binding["whole_lease_coverage"] == "unknown"
    assert binding["retained_unknown_tenure_count"] == len(unknown_ids)
    assert binding["held_count"] == 0
    assert binding["sale_count"] == 101 - len(unknown_ids)
    assert (
        len(proof["held"])
        == len(proof["heldInventoryObservations"])
        == len(proof["heldDetailObservations"])
        == len(unknown_ids)
    )
    assert prepared[4]["per_source"][daum.SOURCE]["raw_verdict"] == "first_seen"
    assert prepared[4]["per_source"][daum.SOURCE]["mark_missing_safe"] is False
    claims = context(prepared, envelope(prepared))
    assert all(
        row["emitted"] for row in claims["members"] if row["native_id"] in unknown_ids
    )
    staged = [
        to_row(row, {}, prepared[2]["runMeta"]["finishedAt"])
        for row in prepared[2]["listings"]
    ]
    sql = build_sql(
        staged, [], prepared[3]["started_at"], set(), daum_verified_subset_claims=claims
    )
    assert "DAUM subset historical identity contradiction" in sql
    assert "latestDaumUnknownTenureObservation" in sql
    assert "WHEN EXCLUDED.transaction_type IS NOT NULL" in sql
    assert (
        "DELETE FROM credeals.cre_listing_documents WHERE listing_id IN (SELECT id FROM _child_refresh)"
        in sql
    )

    import json
    import sys

    import cre_checkpoint_refresh as cp
    import cre_ingest as ingest

    claim_file = tmp_path / "claims.json"
    claim_file.write_text(json.dumps(envelope(prepared)))
    claim_file.chmod(0o600)
    monkeypatch.setattr(
        cp, "git_identity", lambda: (prepared[3]["collector_git_sha"], False)
    )
    monkeypatch.setattr(
        ingest, "load_db_url", lambda *args: pytest.fail("dry run reached database")
    )
    monkeypatch.delenv("CRE_ACTIVATE_STATUS", raising=False)
    out = tmp_path / "sql"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cre_ingest.py",
            "--in",
            str(prepared[1]),
            "--dry-run",
            "--require-strict-freshness",
            "--expected-db-target-sha256",
            "b" * 64,
            "--checkpoint-ingest-attempt",
            "e" * 32,
            "--daum-verified-subset-claims",
            str(claim_file),
            "--keep-artifacts",
            str(out),
        ],
    )
    ingest.main()
    assert "latestDaumUnknownTenureObservation" in (out / "ingest.sql").read_text()


def test_unknown_arrays_do_not_invent_primary_but_known_daum_behavior_is_unchanged():
    from cre_ingest import to_row

    listing = {
        "sourceKey": "daum-commercial",
        "id": "property/example",
        "url": "https://daumcommercial.com/property/example/",
        "transactionMode": "sale",
        "photos": ["https://example.test/first.jpg", "https://example.test/second.jpg"],
        "contactsDetailed": [{"name": "First"}, {"name": "Second"}],
    }
    known = to_row(listing, {}, "2026-11-04T00:00:00Z")
    assert known["images"][0]["isPrimary"] is True
    assert known["contacts"][0]["isPrimary"] is True
    unknown = to_row(
        {
            **listing,
            "daumUnknownTenure": True,
            "daumTenureEvidence": {
                "inventory": "unknown",
                "detail": "unknown",
                "resolved": "unknown",
            },
        },
        {},
        "2026-11-04T00:00:00Z",
    )
    assert all(
        "isPrimary" not in row for row in unknown["images"] + unknown["contacts"]
    )


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate_pass",
        "missing_marker",
        "changed_evidence",
        "lease_carrier",
        "sale_price",
        "sale_price_text",
        "lease_rate",
        "title",
        "photo",
        "contact",
        "broker_ids",
        "unsupported_field",
    ],
)
def test_unknown_admission_rejects_unbound_or_duplicate_rows(
    tmp_path, monkeypatch, fault
):
    import copy

    import test_essex_current_inventory as helpers
    from test_daum_current_inventory import produce
    from test_daum_verified_subset import admit

    import cre_current_inventory_admission as admission
    import cre_daum_current_inventory as daum

    generated = produce(
        tmp_path,
        total=101,
        all_sale=True,
        unknown_inventory=True,
        unknown_detail=True,
        subset_policy="tenure-and-empty-title-v2",
    )
    monkeypatch.setattr(helpers, "SOURCE", daum.SOURCE)
    prepared = helpers.setup(tmp_path, generated)
    artifact = prepared[2]
    prepared[3]["config"]["current_inventory_admission"] = (
        admission.subset_configuration("tenure-and-empty-title-v2")
    )
    unknown = next(row for row in artifact["listings"] if row.get("daumUnknownTenure"))
    if fault == "missing_marker":
        unknown.pop("daumUnknownTenure")
    elif fault == "changed_evidence":
        unknown["daumUnknownEvidence"] = "{}"
    elif fault == "lease_carrier":
        unknown["transactionMode"] = "lease"
    elif fault == "sale_price":
        unknown["salePriceUsd"] = 1234567
    elif fault == "sale_price_text":
        unknown["salePriceText"] = "$1234567"
    elif fault == "lease_rate":
        unknown["leaseRateText"] = "$99/SF"
    elif fault == "unsupported_field":
        unknown["availableSf"] = 99999
    elif fault == "title":
        unknown["name"] = "Unsupported changed title"
    elif fault == "photo":
        unknown["photos"] = ["https://example.test/unobserved.jpg"]
    elif fault == "contact":
        unknown["contactsDetailed"] = [{"name": "Unobserved contact"}]
    elif fault == "broker_ids":
        unknown["brokerIds"] = [0]
    else:
        duplicate = copy.deepcopy(unknown)
        duplicate["transactionMode"] = "lease"
        artifact["listings"].append(duplicate)
    artifact["totalListings"] = len(artifact["listings"])
    for entry in artifact["sources"]:
        entry["listingsCollected"] = sum(
            row["transactionMode"] == entry["transaction"]
            for row in artifact["listings"]
        )
        entry["totalAvailableOnSource"] = entry["listingsCollected"]
    prepared[4]["per_source"][daum.SOURCE]["current_active"] = len(artifact["listings"])
    helpers.refresh(prepared[1], artifact, prepared[3])
    with pytest.raises((ValueError, KeyError, TypeError)):
        admit(prepared)
