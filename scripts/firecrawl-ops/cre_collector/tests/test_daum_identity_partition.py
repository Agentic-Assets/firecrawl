"""DAUM conflict isolation retains peers and binds its target partition."""
import copy

import pytest

import cre_daum_current_inventory as daum
from cre_checkpoint_refresh import git_identity
from cre_daum_fallback_partition import bound_held_count
from daum.identity_sql import validate_partition
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


def test_only_source_held_identity_conflicts_do_not_reduce_valid_peers(subset):
    original_report = copy.deepcopy(subset[4])
    admit(subset)
    subset[4].clear()
    subset[4].update(original_report)
    probe = subset[3]["sources"][daum.SOURCE]["daum_identity_probe"]
    probe["partition"][-1]["conflicted"] = True
    binding = daum.admit_subset(subset[1], subset[3], subset[4],
                                current_code_sha=git_identity()[0], current_git_dirty=False, phase="source")
    assert len(binding["identity_held"]) == 1
    assert binding["target_held"] == []
    assert (binding["emitted_count"], binding["held_count"]) == (100, 2)
    assert binding["whole_source_coverage"] is False


def test_all_emitted_identities_held_is_refused_until_no_write_path_exists(subset):
    original_report = copy.deepcopy(subset[4])
    admit(subset)
    subset[4].clear()
    subset[4].update(original_report)
    probe = subset[3]["sources"][daum.SOURCE]["daum_identity_probe"]
    for member in probe["partition"]:
        member["conflicted"] = True
    with pytest.raises(daum.DaumAllIdentitiesHeld):
        daum.admit_subset(subset[1], subset[3], subset[4],
                          current_code_sha=git_identity()[0], current_git_dirty=False, phase="source")
    with pytest.raises(ValueError) as aggregate_error:
        daum.admit_subset(subset[1], subset[3], subset[4],
                          current_code_sha=git_identity()[0], current_git_dirty=False, phase="aggregate")
    assert type(aggregate_error.value) is ValueError
    probe["partition"][0]["conflicted"] = "true"
    with pytest.raises(ValueError) as malformed_error:
        daum.admit_subset(subset[1], subset[3], subset[4],
                          current_code_sha=git_identity()[0], current_git_dirty=False, phase="source")
    assert type(malformed_error.value) is ValueError


def test_identity_partition_rejects_omitted_or_changed_members():
    members = [{"native_id": "42734", "external_id": "property/a", "source_url": "https://daumcommercial.com/property/a/"}]
    valid = [{**members[0], "conflicted": True}]
    assert validate_partition(members, valid) == valid
    for invalid in ([], [{**valid[0], "external_id": "property/b"}], [{**valid[0], "conflicted": 1}]):
        with pytest.raises(ValueError):
            validate_partition(members, invalid)


def test_probe_refuses_wrong_initial_target_even_if_later_target_would_match(tmp_path, monkeypatch):
    import hashlib
    import cre_ingest
    import cre_validate
    from daum.identity_sql import run_cohort_probe

    artifact = tmp_path / "artifact.json"
    artifact.write_bytes(b"{}")
    urls = iter([("wrong", None), ("expected", None)])
    monkeypatch.setattr(cre_ingest, "load_db_url", lambda *_args: next(urls))
    monkeypatch.setattr(cre_ingest, "database_target_fingerprint_from_url",
                        lambda url: {"value": "a" * 64 if url == "expected" else "b" * 64})
    monkeypatch.setattr(cre_validate, "_run_psql_script", lambda *_args, **_kwargs: pytest.fail("must not query"))
    with pytest.raises(ValueError, match="initial target differs"):
        run_cohort_probe(artifact, hashlib.sha256(b"{}").hexdigest(), "gen", "c" * 40, "a" * 64, None)


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


@pytest.mark.parametrize("malformed", [False, True])
def test_probe_uses_single_line_partition_and_fails_closed_on_malformed_tsv(tmp_path, monkeypatch, malformed):
    import hashlib
    import json
    from types import SimpleNamespace

    import cre_checkpoint_refresh
    import cre_ingest
    import cre_validate
    from daum import identity_sql

    members = [
        {"native_id": "1", "external_id": "property/a", "source_url": "https://example.test/a"},
        {"native_id": "2", "external_id": "property/b", "source_url": "https://example.test/b"},
    ]
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}")
    target = "a" * 64
    producer = "c" * 40
    partition = [{**item, "conflicted": index == 1} for index, item in enumerate(members)]
    monkeypatch.setattr(identity_sql, "cohort_members", lambda _data: members)
    monkeypatch.setattr(cre_ingest, "load_db_url", lambda _env: ("synthetic", None))
    monkeypatch.setattr(cre_ingest, "database_target_fingerprint_from_url", lambda _url: {"value": target})
    monkeypatch.setattr(cre_ingest, "find_psql", lambda: "synthetic-psql")
    monkeypatch.setattr(cre_checkpoint_refresh, "git_identity", lambda: (producer, False))

    def query(_psql, _url, sql, **_kwargs):
        assert "jsonb_agg(assessed ORDER BY source_url)::text" in sql
        result = json.dumps(partition, separators=(",", ":"))
        if malformed:
            result = result.replace("},{", "}," + chr(10) + "{")
        output = chr(10).join(("joined" + chr(9) + "partition", "2" + chr(9) + result, ""))
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(cre_validate, "_run_psql_script", query)
    args = (artifact, hashlib.sha256(artifact.read_bytes()).hexdigest(), "generation", producer, target, None)
    if malformed:
        with pytest.raises(RuntimeError, match="partition could not be qualified"):
            identity_sql.run_cohort_probe(*args)
    else:
        assert identity_sql.run_cohort_probe(*args)["partition"] == partition
