"""Source ordering is advisory; no fixture opens a provider or database."""

import json
from datetime import UTC, datetime, timedelta

import cre_checkpoint_series as series
import pytest


def _manifest(sources, target="target-a"):
    return {"config": {"sources": sources, "pipeline": True}, "database_target": target}


def _directory(root, minute):
    stamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minute)
    path = root / stamp.strftime("%Y-%m-%dT%H%M%SZ")
    path.mkdir()
    return path


@pytest.mark.parametrize("status", ["complete", "failed", "interrupted", "resource_stopped", "running"])
def test_rotation_advances_regardless_of_prior_outcome(tmp_path, status):
    sources = [f"source-{number:02d}" for number in range(51)]
    first = _directory(tmp_path, 1)
    initial = series.scheduled_source_rotation(tmp_path, first, _manifest(sources), None)
    (first / "manifest.json").write_text(json.dumps({
        **_manifest(sources), "series_id": first.name, "status": status,
        "pipeline_dispatch": initial,
    }))
    following = _directory(tmp_path, 2)
    result = series.scheduled_source_rotation(tmp_path, following, _manifest(sources), None)
    assert result["order"] == initial["order"][1:] + initial["order"][:1]
    assert set(result["order"]) == set(sources)
    assert result["previous_series"] == first.name


def test_each_registered_source_reaches_first_position(tmp_path):
    sources = [f"source-{number:02d}" for number in range(51)]
    firsts = []
    for minute in range(1, 52):
        current = _directory(tmp_path, minute)
        dispatch = series.scheduled_source_rotation(tmp_path, current, _manifest(sources), None)
        firsts.append(dispatch["order"][0])
        assert len(dispatch["order"]) == len(set(dispatch["order"])) == 51
        assert set(dispatch["order"]) == set(sources)
        (current / "manifest.json").write_text(json.dumps({
            **_manifest(sources), "series_id": current.name,
            "status": "failed", "pipeline_dispatch": dispatch,
        }))
    assert set(firsts) == set(sources)


def test_qualified_deadline_is_initial_seed(tmp_path):
    sources = ["a", "b", "c"]
    current = _directory(tmp_path, 1)
    result = series.scheduled_source_rotation(tmp_path, current, _manifest(sources), {"order": ["c", "b", "a"]})
    assert result["order"] in (["c", "b", "a"], ["b", "a", "c"], ["a", "c", "b"])


def test_corrupt_foreign_and_different_scope_are_not_holds(tmp_path):
    sources = ["a", "b", "c"]
    one = _directory(tmp_path, 1)
    (one / "manifest.json").write_text("{bad")
    two = _directory(tmp_path, 2)
    (two / "manifest.json").write_text(json.dumps({
        **_manifest(sources, "foreign"), "series_id": two.name,
        "pipeline_dispatch": {"rotation": "source_rotation_v1", "order": sources},
    }))
    three = _directory(tmp_path, 3)
    (three / "manifest.json").write_text(json.dumps({
        **_manifest(["a"]), "series_id": three.name,
        "pipeline_dispatch": {"rotation": "source_rotation_v1", "order": ["a"]},
    }))
    fourth = _directory(tmp_path, 4)
    fifth = _directory(tmp_path, 5)
    first = series.scheduled_source_rotation(tmp_path, fourth, _manifest(sources), None)
    second = series.scheduled_source_rotation(tmp_path, fifth, _manifest(sources), None)
    assert set(first["order"]) == set(second["order"]) == set(sources)
    assert first["order"] != second["order"]

def test_real_registry_scope_is_never_truncated(tmp_path):
    sources = list(series.parse_sources("all"))
    current = _directory(tmp_path, 1)
    dispatch = series.scheduled_source_rotation(tmp_path, current, _manifest(sources), None)
    assert len(sources) >= 51
    assert len(dispatch["order"]) == len(sources)
    assert set(dispatch["order"]) == set(sources)

@pytest.mark.parametrize("deadline_seed", [False, True])
def test_missing_rotation_history_covers_fixed_daily_starts(tmp_path, deadline_seed):
    sources = [f"source-{number:02d}" for number in range(51)]
    priority = {"order": list(reversed(sources))} if deadline_seed else None
    firsts = []
    for day in range(51):
        current = _directory(tmp_path, day * 24 * 60)
        dispatch = series.scheduled_source_rotation(tmp_path, current, _manifest(sources), priority)
        assert set(dispatch["order"]) == set(sources)
        firsts.append(dispatch["order"][0])
    assert set(firsts) == set(sources)


def test_missing_rotation_history_changes_during_daily_multiple_generations(tmp_path):
    sources = [f"source-{number:02d}" for number in range(51)]
    first = _directory(tmp_path, 60)
    second = _directory(tmp_path, 61)
    assert series.scheduled_source_rotation(tmp_path, first, _manifest(sources), None)["order"] != (
        series.scheduled_source_rotation(tmp_path, second, _manifest(sources), None)["order"]
    )
