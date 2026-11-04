"""Synthetic inputs exercise the actual maintenance receipt producer."""

import json

import pytest

from cre_code_handoff_evidence import HandoffError
from cre_code_handoff_git import read_file


def write_json(path, value):
    path.write_text(json.dumps(value))


@pytest.fixture
def transition_inputs(tmp_path):
    operation = tmp_path / "operation"
    independent = tmp_path / "independent"
    operation.mkdir()
    independent.mkdir()
    release_path = tmp_path / "release.json"
    failed = tmp_path / "failed-release.json"
    failed.write_text('{"original":"failed A"}')
    from cre_code_handoff_transition import reference
    old = {"sha": "a" * 40, "tree": "b" * 40}
    new = {"sha": "c" * 40, "tree": "d" * 40}
    tasks = []
    for key in ("interra", "newmark", "qualified"):
        tasks.append({"key": key, "task_path": "\\CRE\\", "task_name": key,
                      "enabled": False, "last_task_result": 1 if key == "qualified" else 0,
                      "last_run_utc": "2026-10-22T02:00:00Z", "next_run_utc": "2026-11-03T02:00:00Z",
                      "actions": [{"execute": "pwsh", "arguments": old["sha"], "working_directory": "C:/runtime"}]})
        for suffix in ("before", "final"):
            (operation / (key + "-" + suffix + ".xml")).write_text(key + "-" + suffix)
        (independent / (key + "-observed.xml")).write_text(key + "-observed")
    after = [{**row, "actions": [{**row["actions"][0], "arguments": new["sha"]}]} for row in tasks]
    write_json(release_path, {"schema": "cre-runtime-maintenance-v1", "old": old, "new": new,
                              "roots": {"native": "C:/runtime", "runtime": "/home/user/runtime"},
                              "failed_handoff_release": reference(failed, read_file), "task_baseline": tasks})
    for name in ("mutation-start.json", "maintenance-ready.json", "release-start.json", "terminal.json"):
        write_json(operation / name, {"synthetic": name})
    write_json(tmp_path / "authorization.json", {"synthetic": "authorization"})
    write_json(tmp_path / "initial-observation.json", {"synthetic": "observation"})
    write_json(operation / "maintenance-ready.json", {
        "authorization": reference(tmp_path / "authorization.json", read_file),
        "initial_observation": reference(tmp_path / "initial-observation.json", read_file),
    })
    for suffix in ("native.json", "stdout.raw", "stderr.raw"):
        (tmp_path / ("operation." + suffix)).write_bytes(b"synthetic " + suffix.encode())
    write_json(independent / "observation.json", {"tasks": after})
    return release_path, operation, independent, old, new


def test_producer_binds_actual_operation_and_independent_task_bytes(transition_inputs):
    try:
        from cre_runtime_maintenance_transition import make_maintenance_transition
    except ImportError:
        pytest.fail("maintenance transition producer is missing")
    release, operation, independent, old, new = transition_inputs
    result = make_maintenance_transition(release, operation, independent, read_file)
    assert result["old"] == old and result["new"] == new
    assert {row["key"] for row in result["tasks"]} == {"interra", "newmark", "qualified"}
    from cre_code_handoff_transition import bound
    qualified = next(row for row in result["tasks"] if row["key"] == "qualified")
    assert bound(qualified["before_xml"], read_file) == b"qualified-before"
    assert bound(qualified["independent_xml"], read_file) == b"qualified-observed"
    assert qualified["before_tuple"]["last_task_result"] == qualified["after_tuple"]["last_task_result"] == 1


def test_producer_rejects_missing_independent_xml(transition_inputs):
    try:
        from cre_runtime_maintenance_transition import make_maintenance_transition
    except ImportError:
        pytest.fail("maintenance transition producer is missing")
    release, operation, independent, _, _ = transition_inputs
    (independent / "qualified-observed.xml").unlink()
    with pytest.raises((HandoffError, FileNotFoundError)):
        make_maintenance_transition(release, operation, independent, read_file)


def test_constructor_retains_original_authorization_and_observation(transition_inputs):
    from cre_runtime_maintenance_transition import make_maintenance_transition

    release, operation, independent, _, _ = transition_inputs
    result = make_maintenance_transition(release, operation, independent, read_file)
    assert read_file(result["authorization"]["path"]) == b'{"synthetic": "authorization"}'
    assert read_file(result["initial_observation"]["path"]) == b'{"synthetic": "observation"}'
