"""Inventory-index readback is separate from verified canonical parents."""

import copy

import pytest

import test_notification_summary as support


@pytest.fixture
def inventory_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(support, "SOURCE", "cbre-dealflow")
    evidence = support.evidence.__wrapped__(tmp_path, monkeypatch)
    child = evidence["values"]["child"]
    checkpoint = child["sources"][support.SOURCE]
    checkpoint["artifact"]["inventory_only"] = 2
    checkpoint["readback"].update(
        evidence_class="authoritative_inventory_feed",
        inventory_only={
            "expected_active": 2,
            "active": 2,
            "latest_batch_active": 2,
            "latest_enumerated_at": child["finished_at"],
            "scope_watermark_at": child["finished_at"],
            "ok": True,
            "reason": None,
        },
    )
    evidence["save"]()
    return evidence


def read(evidence, pipeline):
    if not pipeline:
        return support.read(evidence)
    setup = support.pipeline_evidence(evidence, primary_source=support.SOURCE)
    support.qualify_pipeline_second_source(evidence, setup)
    other = setup["child"]["sources"]["cbre"]
    other["artifact"]["inventory_only"] = 0
    other["readback"]["inventory_only"] = {"expected_active": 0, "ok": True}
    evidence["save"]()
    return support.read_pipeline(evidence, [support.SOURCE, "cbre"])


@pytest.mark.parametrize("pipeline", [False, True])
def test_real_private_receipts_keep_parent_and_index_counts_separate(
    inventory_evidence, pipeline
):
    result = read(inventory_evidence, pipeline)
    if pipeline:
        assert result["verified"] and result["complete"]
        result = result["per_source_summaries"][0]
    assert result["verified_listings"] == 3
    assert result["verified_inventory_only"] == 2
    assert result["transaction_breakdown"]["sale"] == 2
    assert result["transaction_breakdown"]["lease"] == 3
    assert result["transaction_breakdown"]["both"] == 2


@pytest.mark.parametrize("pipeline", [False, True])
@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "failed",
        "expected",
        "active",
        "latest",
        "stale",
        "watermark",
        "future",
        "generation",
        "hash",
        "canonical",
        "rejected",
        "boolean",
    ],
)
def test_inventory_scope_cannot_borrow_missing_or_failed_readback(
    inventory_evidence, pipeline, fault
):
    evidence = inventory_evidence
    child = evidence["values"]["child"]
    checkpoint = child["sources"][support.SOURCE]
    index = checkpoint["readback"]["inventory_only"]
    if fault == "missing":
        checkpoint["readback"].pop("inventory_only")
    elif fault == "failed":
        index["ok"] = False
    elif fault in {"expected", "active", "latest"}:
        index[
            {
                "expected": "expected_active",
                "active": "active",
                "latest": "latest_batch_active",
            }[fault]
        ] = 1
    elif fault == "stale":
        index["latest_enumerated_at"] = "2000-01-01T00:00:00Z"
    elif fault == "watermark":
        index.pop("scope_watermark_at")
    elif fault == "future":
        index["scope_watermark_at"] = "2999-01-01T00:00:00Z"
    elif fault == "generation":
        checkpoint["readback"]["generation_id"] = "foreign"
    elif fault == "hash":
        checkpoint["artifact"]["sha256"] = "bad"
    elif fault == "canonical":
        checkpoint["readback"]["latest_inventory_batch_active"] = 5
    elif fault == "rejected":
        checkpoint["artifact"]["rejected_by_ingest"] = 1
    elif fault == "boolean":
        checkpoint["artifact"]["inventory_only"] = True
    evidence["save"]()
    with pytest.raises(support.REFUSED):
        read(evidence, pipeline)


def test_unsupported_source_cannot_acquire_index_scope(inventory_evidence, monkeypatch):
    from cre_source_policy import load_source_policy

    policy = copy.deepcopy(load_source_policy())
    policy[support.SOURCE]["inventory_only_namespace"] = None
    monkeypatch.setattr("cre_source_policy.load_source_policy", lambda: policy)
    with pytest.raises(support.REFUSED):
        read(inventory_evidence, False)
