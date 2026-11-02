"""Release scopes bind each actual root independently."""

import copy

import pytest
import test_cre_runtime_maintenance_admission as admission

from cre_code_handoff_evidence import HandoffError

admitted = admission.admitted
complete_case = admission.complete_case
lineage_case = admission.lineage_case
restoration_case = admission.restoration_case
maintenance_case = admission.maintenance_case


@pytest.mark.parametrize("change", [None, "wrong_native", "wrong_nul"])
def test_reviewed_release_binds_distinct_native_and_runtime_scopes(maintenance_case, change, monkeypatch):
    import cre_runtime_maintenance as maintenance
    validate_maintenance_release = maintenance.validate_maintenance_release
    monkeypatch.setattr(maintenance, "validate_owned_qa", lambda *args, **kwargs: None)

    packet, _, inputs, _, _ = maintenance_case
    packet["new"] = {"sha": "e" * 40, "tree": "f" * 40}
    packet["build_source"]["identity"] = packet["new"]
    packet["native_before"] = {"sha": "1" * 40, "tree": "2" * 40}
    path = "scripts/firecrawl-ops/cre_collector/fixture.py"
    objects = {path: {"before": None, "after": {"mode": "100644", "type": "blob", "oid": "e" * 40}}}
    packet.update(install_scope=[], expected_images={},
                  runtime_change={"deployed": packet["old"], "selected": packet["new"], "allowed_paths": [path],
                                  "objects": copy.deepcopy(objects), "changed_dependency_paths": []},
                  native_change={"deployed": packet["native_before"], "selected": packet["new"], "allowed_paths": [path],
                                 "objects": copy.deepcopy(objects), "changed_dependency_paths": []})
    inputs.add("runtime_allowed_paths_nul", path.encode() + b"\0")
    inputs.add("native_allowed_paths_nul", (b"wrong\0" if change == "wrong_nul" else path.encode() + b"\0"))
    if change == "wrong_native":
        packet["native_change"]["deployed"] = packet["old"]
    if change is None:
        validate_maintenance_release(packet, inputs.evidence(packet))
    else:
        with pytest.raises(HandoffError):
            validate_maintenance_release(packet, inputs.evidence(packet))


def test_playwright_source_change_requires_reviewed_playwright_image(maintenance_case, monkeypatch):
    import cre_runtime_maintenance as maintenance
    validate_maintenance_release = maintenance.validate_maintenance_release
    monkeypatch.setattr(maintenance, "validate_owned_qa", lambda *args, **kwargs: None)

    packet, _, inputs, _, _ = maintenance_case
    packet["new"] = {"sha": "e" * 40, "tree": "f" * 40}
    packet["build_source"]["identity"] = packet["new"]
    packet["native_before"] = {"sha": "1" * 40, "tree": "2" * 40}
    path = "apps/playwright-service-ts/scrape_route.ts"
    objects = {path: {"before": None, "after": {"mode": "100644", "type": "blob", "oid": "e" * 40}}}
    packet.update(install_scope=[], expected_images={},
                  runtime_change={"deployed": packet["old"], "selected": packet["new"], "allowed_paths": [path],
                                  "objects": copy.deepcopy(objects), "changed_dependency_paths": []},
                  native_change={"deployed": packet["native_before"], "selected": packet["new"], "allowed_paths": [path],
                                 "objects": copy.deepcopy(objects), "changed_dependency_paths": []})
    inputs.add("runtime_allowed_paths_nul", path.encode() + b"\0")
    inputs.add("native_allowed_paths_nul", path.encode() + b"\0")
    with pytest.raises(HandoffError):
        validate_maintenance_release(packet, inputs.evidence(packet))
    packet["expected_images"] = {"playwright-service": "sha256:" + "a" * 64}
    validate_maintenance_release(packet, inputs.evidence(packet))
    packet["expected_images"] = {"playwright-service": "sha256:" + "z" * 64}
    with pytest.raises(HandoffError):
        validate_maintenance_release(packet, inputs.evidence(packet))


def test_new_commit_with_identical_tree_allows_empty_per_root_diffs(maintenance_case):
    from cre_runtime_maintenance import validate_maintenance_release

    packet, _, inputs, _, _ = maintenance_case
    assert packet["old"]["tree"] == packet["tooling"]["tree"]
    packet["new"] = {"sha": "e" * 40, "tree": packet["old"]["tree"]}
    packet["build_source"]["identity"] = packet["new"]
    packet["native_before"] = packet["old"]
    scope = {"deployed": packet["old"], "selected": packet["new"], "allowed_paths": [],
             "objects": {}, "changed_dependency_paths": []}
    packet.update(install_scope=[], expected_images={}, runtime_change=copy.deepcopy(scope),
                  native_change=copy.deepcopy(scope))
    inputs.add("runtime_allowed_paths_nul", b"")
    inputs.add("native_allowed_paths_nul", b"")
    validate_maintenance_release(packet, inputs.evidence(packet))


@pytest.mark.parametrize("change", [None, "wrong_identity", "runtime_alias", "windows_root", "foreign_failure"])
def test_build_source_is_selected_and_distinct_ext4_role(maintenance_case, change):
    from cre_runtime_maintenance import validate_maintenance_release

    packet, _, inputs, _, _ = maintenance_case
    packet["new"] = {"sha": "e" * 40, "tree": packet["old"]["tree"]}
    packet["native_before"] = packet["old"]
    scope = {"deployed": packet["old"], "selected": packet["new"], "allowed_paths": [],
             "objects": {}, "changed_dependency_paths": []}
    packet.update(install_scope=[], expected_images={}, runtime_change=copy.deepcopy(scope),
                  native_change=copy.deepcopy(scope), build_source={"root": "/home/cayman/build-source",
                                                                    "identity": packet["new"]})
    inputs.add("runtime_allowed_paths_nul", b"")
    inputs.add("native_allowed_paths_nul", b"")
    if change == "wrong_identity":
        packet["build_source"]["identity"] = packet["old"]
    elif change == "runtime_alias":
        packet["build_source"]["root"] = packet["roots"]["runtime"]
    elif change == "windows_root":
        packet["build_source"]["root"] = "C:/build-source"
    elif change == "foreign_failure":
        packet["failed_handoff_release"]["sha256"] = "0" * 64
    if change is None:
        validate_maintenance_release(packet, inputs.evidence(packet))
    else:
        with pytest.raises(HandoffError):
            validate_maintenance_release(packet, inputs.evidence(packet))
