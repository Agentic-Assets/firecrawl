"""DAUM conflict isolation retains peers and binds its target partition."""
import copy

import pytest

import cre_daum_current_inventory as daum
from cre_checkpoint_refresh import git_identity
from cre_daum_fallback_partition import bound_held_count
from cre_daum_identity_sql import validate_partition
from cre_ingest import build_sql, merge_rows, to_row
from test_daum_verified_subset import admit, context, subset, complete  # noqa: F401


def qualified_conflict(prepared):
    original_report = copy.deepcopy(prepared[4])
    admit(prepared)  # Install a valid synthetic no-conflict read-only probe.
    prepared[4].clear()
    prepared[4].update(original_report)
    probe = prepared[3]["sources"][daum.SOURCE]["daum_identity_probe"]
    probe["partition"][0]["conflicted"] = True
    # A separately source-held member also conflicts; it must not count twice.
    probe["partition"][-1]["conflicted"] = True
    result = daum.admit_subset(prepared[1], prepared[3], prepared[4],
                               current_code_sha=git_identity()[0], current_git_dirty=False, phase="source")
    return result, probe


def test_conflicted_emitted_and_already_held_members_are_distinct(subset):
    binding, probe = qualified_conflict(subset)
    assert len(binding["identity_held"]) == 2
    assert len(binding["target_held"]) == 1
    assert (binding["emitted_count"], binding["held_count"]) == (99, 3)
    assert binding["whole_source_coverage"] is False
    assert bound_held_count(binding, subset[3]["sources"][daum.SOURCE]) == 1
    probe["partition"][0]["conflicted"] = False
    with pytest.raises(ValueError, match="partition differs"):
        bound_held_count(binding, subset[3]["sources"][daum.SOURCE])


def test_identity_partition_rejects_omitted_or_changed_members():
    members = [{"native_id": "42734", "external_id": "property/a", "source_url": "https://daumcommercial.com/property/a/"}]
    valid = [{**members[0], "conflicted": True}]
    assert validate_partition(members, valid) == valid
    for invalid in ([], [{**valid[0], "external_id": "property/b"}], [{**valid[0], "conflicted": 1}]):
        with pytest.raises(ValueError):
            validate_partition(members, invalid)


def test_generated_ingest_rechecks_before_filter_and_never_activates(subset):
    binding, _ = qualified_conflict(subset)
    manifest = subset[3]
    source_report = copy.deepcopy(subset[4])
    manifest["sources"][daum.SOURCE]["gate"] = copy.deepcopy(source_report["per_source"][daum.SOURCE])
    assert daum.admit_subset(subset[1], manifest, subset[4], current_code_sha=git_identity()[0],
                             current_git_dirty=False, phase="aggregate") == binding
    envelope = {"manifest": manifest, "source_report": source_report,
                "aggregate_report": copy.deepcopy(subset[4]), "binding": binding, "attempt_id": "e" * 32}
    claims = context(subset, envelope)
    staged = {}
    for listing in subset[2]["listings"]:
        row = to_row(listing, {}, subset[2]["runMeta"]["finishedAt"])
        key = row["external_id"]
        staged[key] = merge_rows(staged[key], row) if key in staged else row
    sql = build_sql(list(staged.values()), [], manifest["started_at"], set(), daum_verified_subset_claims=claims)
    assert "{daum_historical_conflict}" not in sql
    assert sql.index("DAUM identity partition changed") < sql.index("DELETE FROM _src s USING _daum_identity_held")
    assert sql.index("DELETE FROM _src s USING _daum_identity_held") < sql.index("DAUM identity contradiction in historical rows")
    assert "UPDATE credeals.cre_source_baseline" not in sql
