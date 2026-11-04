"""Three actual temporary Git commits and a divergent native checkout."""

import pytest
import test_cre_code_handoff as fixtures

from cre_code_handoff_evidence import HandoffError

git_case = fixtures.git_case
git_command = fixtures.git_command


def test_native_intermediate_commit_must_be_exact_ancestor_on_both_sides(git_case):
    git, target, release, _, path = git_case
    source = target.parent / "source"
    old, native = release["deployed"], release["selected"]
    (source / path).write_text("third reviewed version\n")
    git_command(source, "commit", "-am", "third")
    selected = {"sha": git.text(source, "rev-parse", "HEAD"),
                "tree": git.text(source, "rev-parse", "HEAD^{tree}")}
    git.maintenance_ancestry(source, old, native, selected)
    with pytest.raises(HandoffError):
        git.maintenance_ancestry(source, old, {**native, "tree": old["tree"]}, selected)
    git_command(source, "checkout", "-b", "foreign", old["sha"])
    (source / path).write_text("divergent version\n")
    git_command(source, "commit", "-am", "foreign")
    foreign = {"sha": git.text(source, "rev-parse", "HEAD"),
               "tree": git.text(source, "rev-parse", "HEAD^{tree}")}
    with pytest.raises(HandoffError):
        git.maintenance_ancestry(source, old, foreign, selected)


def test_maintenance_advances_when_exact_dependency_diff_is_empty(git_case):
    git, target, release, bundle, _ = git_case
    git.maintenance_fast_forward(target, release, bundle, allowed_dependency_paths=[])
    git.clean(target, release["selected"], tracking=release["selected"]["sha"])


def test_history_reader_checks_exact_objects_without_requiring_old_checkout_head(git_case):
    from cre_runtime_maintenance_transition import validate_maintenance_git_history

    git, target, scope, _, path = git_case
    source = target.parent / "source"
    (source / path).write_text("later unrelated commit\n")
    git_command(source, "commit", "-am", "later")
    scope["changed_dependency_paths"] = []
    validate_maintenance_git_history(git, source, scope)
    scope["objects"][path]["after"]["oid"] = "0" * 40
    with pytest.raises(HandoffError):
        validate_maintenance_git_history(git, source, scope)


def test_installed_dependency_hashes_bind_selected_git_manifest_bytes(git_case):
    from cre_code_handoff_evidence import sha
    from cre_runtime_maintenance_transition import (
        validate_maintenance_dependency_history,
    )

    git, target, _, _, _ = git_case
    source = target.parent / "source"
    collector = source / "scripts/firecrawl-ops/cre_collector"
    (collector / "package.json").write_bytes(b'{"name":"test"}\n')
    (collector / "package-lock.json").write_bytes(b'{"lockfileVersion":3}\n')
    git_command(source, "add", "scripts/firecrawl-ops/cre_collector/package.json",
                "scripts/firecrawl-ops/cre_collector/package-lock.json")
    git_command(source, "commit", "-m", "reviewed manifests")
    selected = {"sha": git.text(source, "rev-parse", "HEAD"), "tree": git.text(source, "rev-parse", "HEAD^{tree}")}
    deps = {"package.json": sha((collector / "package.json").read_bytes()),
            "package-lock.json": sha((collector / "package-lock.json").read_bytes()),
            "node_modules/.package-lock.json": "a" * 64}
    validate_maintenance_dependency_history(git, source, selected, deps)
    deps["package-lock.json"] = "b" * 64
    with pytest.raises(HandoffError):
        validate_maintenance_dependency_history(git, source, selected, deps)


def test_historical_tracking_ref_must_lie_between_deployed_and_selected(git_case):
    from cre_runtime_maintenance_transition import validate_maintenance_tracking_history

    git, target, release, _, path = git_case
    source = target.parent / "source"
    old, tracking = release["deployed"], release["selected"]
    (source / path).write_text("third version\n")
    git_command(source, "commit", "-am", "third")
    selected = {"sha": git.text(source, "rev-parse", "HEAD"), "tree": git.text(source, "rev-parse", "HEAD^{tree}")}
    validate_maintenance_tracking_history(git, source, old, tracking["sha"], selected)
    git_command(source, "checkout", "-b", "foreign-tracking", old["sha"])
    (source / path).write_text("foreign version\n")
    git_command(source, "commit", "-am", "foreign")
    with pytest.raises(HandoffError):
        validate_maintenance_tracking_history(git, source, old, git.text(source, "rev-parse", "HEAD"), selected)
