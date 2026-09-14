"""Pure contracts for the no-write CRE capacity experiment planner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import cre_capacity_experiment as experiment


def test_bold_profile_resolves_global_budget_and_unimplemented_adapter() -> None:
    profile, digest = experiment.load_profile(experiment.DEFAULT_CONFIG, "bold-jll-128")
    plan = experiment.resolve(profile, "bold-jll-128", digest, None)

    assert plan["requested"]["global_pages"] == 10
    assert plan["requested"]["jll_detail_concurrency"] == 10
    assert plan["workload"]["source"] == "jll"
    assert plan["provider_budget"]["later_two_provider_split"] == [6, 4]
    assert plan["execution"]["startable"] is False
    assert plan["execution"]["blockers"] == ["runtime_evidence_unverified"]


def test_effective_runtime_match_is_distinguished_from_proposed_cpu_settings() -> None:
    profile, digest = experiment.load_profile(experiment.DEFAULT_CONFIG, "bold-jll-128")
    plan = experiment.resolve(
        profile, "bold-jll-128", digest, profile["runtime_baseline"]
    )

    assert plan["runtime_baseline_check"]["state"] == "match"
    assert plan["requested"]["browser_cpus"] == 6
    assert plan["requested"]["api_cpus"] == 2


def test_effective_runtime_drift_is_reported_without_mutating_anything() -> None:
    profile, digest = experiment.load_profile(experiment.DEFAULT_CONFIG, "bold-jll-128")
    effective = dict(profile["runtime_baseline"])
    effective["api_memory_bytes"] = 1

    plan = experiment.resolve(profile, "bold-jll-128", digest, effective)

    assert plan["runtime_baseline_check"]["state"] == "drift"
    assert plan["runtime_baseline_check"]["drift"] == [
        {"field": "api_memory_bytes", "expected": 8589934592, "actual": 1}
    ]
    assert plan["execution"]["blockers"] == ["effective_runtime_drift"]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda profile: profile["requested"].__setitem__("global_pages", True),
            "global_pages",
        ),
        (
            lambda profile: profile["requested"].__setitem__(
                "jll_detail_concurrency", 11
            ),
            "JLL detail",
        ),
        (
            lambda profile: profile["provider_budget"].__setitem__(
                "later_two_provider_split", [6, 5]
            ),
            "provider split",
        ),
    ],
)
def test_profile_validation_rejects_unsafe_or_inconsistent_settings(
    tmp_path: Path, mutate: object, message: str
) -> None:
    document = json.loads(experiment.DEFAULT_CONFIG.read_text(encoding="utf-8"))
    profile = document["profiles"]["bold-jll-128"]
    mutate(profile)  # type: ignore[operator]
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(experiment.ProfileError, match=message):
        experiment.load_profile(path, "bold-jll-128")


def test_write_plan_is_canonical_and_private_mode(tmp_path: Path) -> None:
    profile, digest = experiment.load_profile(
        experiment.DEFAULT_CONFIG, "production-current"
    )
    target = tmp_path / "restricted" / "resolved.json"

    experiment._write_json(
        target, experiment.resolve(profile, "production-current", digest, None)
    )

    assert target.stat().st_mode & 0o777 == 0o600
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["profile"] == "production-current"
