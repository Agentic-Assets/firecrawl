"""Exact partition checks refuse same-count identity swaps and binding drift."""

import copy
from uuid import uuid5

import pytest

from cre_ingest import (
    _RUN_UUID_NAMESPACE,
    artifact_run_identity_from_digests,
    build_sql,
    to_row,
)
from source_fields.buildout_partial_receipt import validate_partition_consistency


@pytest.fixture
def evidence():
    scope = {"source_key": "greysteel", "generation_id": "run-1", "artifact_sha256": "a" * 64,
             "database_target_sha256": "b" * 64, "collector_git_sha": "c" * 40,
             "staged_external_ids": ["Alpha", "beta", "Cobblestone-Village"]}
    emitted = [{"external_id": "Alpha", "listing_id": "11111111-1111-4111-8111-111111111111"},
               {"external_id": "beta", "listing_id": "22222222-2222-4222-8222-222222222222"}]
    receipt = {"schema": "buildout-partition-v1", "source_key": "greysteel", "generation_id": "run-1",
               "artifact_sha256": "a" * 64, "database_target_sha256": "b" * 64,
               "collector_git_sha": "c" * 40, "attempt_id": "d" * 32,
               "job_id": str(uuid5(_RUN_UUID_NAMESPACE, f"{artifact_run_identity_from_digests(['a' * 64])[0]}:greysteel")),
               "held": ["Cobblestone-Village"],
               "emitted": emitted}
    return scope, "d" * 32, receipt, copy.deepcopy(emitted)


def test_exact_subset_and_all_held_are_honest(evidence):
    scope, attempt, receipt, rows = evidence
    result = validate_partition_consistency(scope, attempt, receipt, list(reversed(rows)))
    assert result == {"outcome": "partition_consistent", "source_key": "greysteel", "generation_id": "run-1",
                      "attempt_id": attempt, "staged_count": 3, "emitted_count": 2,
                      "held": ["Cobblestone-Village"], "whole_source_coverage": False}
    receipt["held"] = list(scope["staged_external_ids"])
    receipt["emitted"] = []
    result = validate_partition_consistency(scope, attempt, receipt, [])
    assert result["outcome"] == "all_held" and result["emitted_count"] == 0


@pytest.mark.parametrize("mutation", ["swap", "missing", "extra", "parent", "duplicate", "held_overlap"])
def test_same_count_or_other_partition_mismatch_is_refused(evidence, mutation):
    scope, attempt, receipt, rows = evidence
    if mutation == "swap":
        rows[0]["external_id"] = "foreign"
    elif mutation == "missing":
        rows.pop()
    elif mutation == "extra":
        rows.append({"external_id": "foreign", "listing_id": "44444444-4444-4444-8444-444444444444"})
    elif mutation == "parent":
        rows[0]["listing_id"] = "44444444-4444-4444-8444-444444444444"
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    else:
        receipt["held"].append("Alpha")
    with pytest.raises(ValueError):
        validate_partition_consistency(scope, attempt, receipt, rows)


@pytest.mark.parametrize("field", ["source_key", "generation_id", "artifact_sha256", "database_target_sha256",
                                   "collector_git_sha", "attempt_id", "job_id"])
def test_wrong_receipt_binding_is_refused(evidence, field):
    scope, attempt, receipt, rows = evidence
    receipt[field] = "wrong"
    with pytest.raises(ValueError):
        validate_partition_consistency(scope, attempt, receipt, rows)


def test_other_well_formed_job_is_refused(evidence):
    scope, attempt, receipt, rows = evidence
    receipt["job_id"] = "33333333-3333-4333-8333-333333333333"
    with pytest.raises(ValueError, match="receipt binding differs"):
        validate_partition_consistency(scope, attempt, receipt, rows)


def test_no_hold_cannot_be_reported_as_partial(evidence):
    scope, attempt, receipt, rows = evidence
    receipt["held"] = []
    with pytest.raises(ValueError, match="not a held partial"):
        validate_partition_consistency(scope, attempt, receipt, rows)


def test_staged_buildout_requires_exactly_one_job():
    row = to_row({"sourceKey": "greysteel", "id": "123", "url": "https://example.test/123"}, {},
                 "2026-11-11T00:00:00Z")
    job = {"slug": "greysteel", "discovered": 1, "saved": 1, "errors": 0, "notes": "fixture"}
    for jobs in ([], [job, job]):
        with pytest.raises(ValueError, match="exactly one job"):
            build_sql([row], jobs, "2026-11-11T00:00:00Z", set())
