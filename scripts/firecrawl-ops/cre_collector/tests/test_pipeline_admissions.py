"""Exact source map contracts against the real admission validators."""

import copy

import pytest
from test_buildout_current_inventory import CODE, SOURCE, setup

import cre_current_inventory_admission as admission


def pipeline_manifest(manifest, selected):
    manifest["config"].pop("current_inventory_admission", None)
    manifest["config"].update(
        pipeline=True,
        sources=selected,
        current_inventory_admissions=admission.configuration_map([SOURCE], selected),
    )
    return manifest


def test_source_and_aggregate_keep_original_flat_owner(tmp_path):
    _, path, _, manifest, report = setup(tmp_path)
    pipeline_manifest(manifest, [SOURCE, "interra-realty"])
    original = copy.deepcopy(manifest["config"])
    proof = admission.admit(
        path,
        manifest,
        report,
        source=SOURCE,
        current_code_sha=CODE,
        current_git_dirty=False,
        phase="source",
    )
    manifest["sources"][SOURCE]["gate"] = copy.deepcopy(report["per_source"][SOURCE])
    raw = copy.deepcopy(report)
    raw["per_source"][SOURCE].update(
        verdict="first_seen", reason="no baseline row; cannot gate (first sight)"
    )
    assert (
        admission.admit(
            path,
            manifest,
            raw,
            source=SOURCE,
            current_code_sha=CODE,
            current_git_dirty=False,
            phase="aggregate",
        )
        == proof
    )
    assert manifest["config"] == original
    assert proof["database_target_sha256"] == "b" * 64


@pytest.mark.parametrize(
    "keys", [["all"], ["buildout"], [SOURCE, SOURCE], ["interra-realty"]]
)
def test_map_rejects_wildcard_duplicate_or_unselected(keys):
    with pytest.raises(ValueError):
        admission.configuration_map(keys, [SOURCE])


@pytest.mark.parametrize(
    "drift", ["missing", "foreign", "policy", "raw", "path", "target"]
)
def test_map_never_borrows_another_source_proof(tmp_path, drift):
    _, path, _, manifest, report = setup(tmp_path)
    pipeline_manifest(manifest, [SOURCE, "interra-realty"])
    entries = manifest["config"]["current_inventory_admissions"]
    if drift == "missing":
        entries.clear()
    if drift == "foreign":
        entries[SOURCE] = admission.configuration("interra-realty")
    if drift == "policy":
        entries[SOURCE]["source_policy_sha256"] = "e" * 64
    if drift == "raw":
        report["per_source"][SOURCE]["verdict"] = "ok"
    if drift == "path":
        report["generated_for_artifact"] = [str(path.parent / "foreign.json")]
    if drift == "target":
        manifest["preflight"]["database_target"] = {}
    with pytest.raises(ValueError):
        admission.admit(
            path,
            manifest,
            report,
            source=SOURCE,
            current_code_sha=CODE,
            current_git_dirty=False,
            phase="source",
        )


def test_explicit_admission_is_not_required_for_unadmitted_historical_source():
    config = {
        "pipeline": True,
        "sources": [SOURCE, "cbre"],
        "transactions": ["sale", "lease"],
        "current_inventory_admissions": admission.configuration_map(
            [SOURCE], [SOURCE, "cbre"]
        ),
    }
    assert admission.admission_configuration(config, "cbre") is None
    assert admission.admission_configuration(config, SOURCE)["source_key"] == SOURCE


def test_aggregate_admission_keeps_other_raw_verdict_unchanged(tmp_path):
    _, path, _, manifest, report = setup(tmp_path)
    pipeline_manifest(manifest, [SOURCE, "interra-realty"])
    other = path.parent / "interra.json"
    manifest["sources"]["interra-realty"] = {"artifact": {"path": other.name}}
    report["generated_for_artifact"] = [str(path), str(other)]
    raw_other = {"verdict": "first_seen", "reason": "independent source raw reason"}
    report["per_source"]["interra-realty"] = copy.deepcopy(raw_other)
    admission.admit(
        path,
        manifest,
        report,
        source=SOURCE,
        current_code_sha=CODE,
        current_git_dirty=False,
        phase="source",
    )
    assert report["per_source"]["interra-realty"] == raw_other
