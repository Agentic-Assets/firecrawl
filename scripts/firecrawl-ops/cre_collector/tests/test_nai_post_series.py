import copy
import hashlib

import pytest

from cre_checkpoint_series import _pipeline_member_report_matches, nai_post_partial_child
from cre_checkpoint_refresh import compare_validation_quality


def child_fixture():
    preimage = '[{"external_id":"infabode:1","listing":null,"source_index":null,"children":{}}]'
    digest = hashlib.sha256(preimage.encode()).hexdigest()
    binding = {"artifact_sha256": "a" * 64, "producer_sha": "b" * 40, "generation_id": "gen-1",
               "database_target_sha256": "c" * 64, "partition_sha256": "d" * 64, "preimage_sha256": digest,
               "source_count": 1, "source_held": [], "staged_count": 1, "held": [], "held_count": 0,
               "emitted_count": 1, "schema": "nai-public-post-partial-v1", "scope": "nai_public_post_partial_v1",
               "source_key": "nai-global", "whole_source_coverage": False}
    checkpoint = {"artifact": {"sha256": "a" * 64, "flat_listings": 1, "nai_post_partial": True},
                  "gate": {"verdict": "ok_additive_verified_subset", "mark_missing_safe": False,
                           "admission_scope": "nai_public_post_partial_v1", "current_inventory_proof": binding},
                  "nai_post_identity_probe": {**binding, "preimage": preimage},
                  "subset_ingest_attempt_id": "e" * 32}
    child = {"run_id": "gen-1", "collector_git_sha": "b" * 40,
             "preflight": {"database_target": {"value": "c" * 64}}, "sources": {"nai-global": checkpoint}}
    observed = {"ok": True, "kind": "nai_public_post_partial", "whole_source_coverage": False,
                "generation_id": "gen-1", "artifact_sha256": "a" * 64, "database_target_sha256": "c" * 64,
                "attempt_id": "e" * 32, "partition_sha256": "d" * 64, "preimage_sha256": digest,
                "staged_count": 1, "emitted_count": 1, "held_count": 0, "children_verified": True,
                "new_missing_canonical_count": 1}
    checkpoint["readback"] = {"ok": True, "kind": "nai_public_post_partial", "whole_source_coverage": False,
                              "observation": observed}
    return child, {"nai_post_partial_readback": observed}


def test_exact_source_partial_and_readback():
    child, report = child_fixture()
    assert nai_post_partial_child(child)
    assert _pipeline_member_report_matches(report, child["sources"]["nai-global"], "nai-global", "gen-1")


@pytest.mark.parametrize("field,value", [("source_key", "foreign"), ("whole_source_coverage", True),
                                         ("emitted_count", True), ("preimage_sha256", "0" * 64)])
def test_foreign_or_malformed_partial_markers_are_not_qualified(field, value):
    child, _ = child_fixture()
    child["sources"]["nai-global"]["gate"]["current_inventory_proof"][field] = value
    assert not nai_post_partial_child(child)


def test_foreign_attempt_does_not_match_saved_readback():
    child, report = child_fixture()
    changed = copy.deepcopy(report)
    changed["nai_post_partial_readback"]["attempt_id"] = "f" * 32
    assert not _pipeline_member_report_matches(changed, child["sources"]["nai-global"], "nai-global", "gen-1")


def test_canonical_allowance_is_source_specific_and_bounded():
    before = {"queries": {"quality_by_source": [{"source_key": "nai-global", "missing_canonical_url": "1"},
                                                 {"source_key": "lyon-stahl", "missing_canonical_url": "1"}]}}
    after = {"queries": {"quality_by_source": [{"source_key": "nai-global", "missing_canonical_url": "3"},
                                                {"source_key": "lyon-stahl", "missing_canonical_url": "2"}]}}
    result = compare_validation_quality(before, after, nai_new_missing_canonical=2)
    assert any("lyon-stahl" in failure for failure in result["failures"])
    assert not any("nai-global" in failure for failure in result["failures"])
    with pytest.raises(ValueError):
        compare_validation_quality(before, after, nai_new_missing_canonical=True)
