"""Git admission for dependency-aware maintenance; all repositories are temporary."""

import pytest
import test_cre_code_handoff as handoff_fixtures

from cre_code_handoff_evidence import HandoffError

git_case = handoff_fixtures.git_case
git_command = handoff_fixtures.git_command


def test_maintenance_allows_exact_empty_dependency_change_set(git_case):
    git, target, release, bundle, _ = git_case
    git.maintenance_fast_forward(target, release, bundle, allowed_dependency_paths=[])
    git.clean(target, release["selected"], tracking=release["selected"]["sha"])


@pytest.fixture
def dependency_case(git_case):
    git, target, release, _, _ = git_case
    source = target.parent / "source"
    path = "scripts/firecrawl-ops/cre_collector/package.json"
    leaf = source / path
    leaf.write_text('{"name":"fixture"}\n')
    git_command(source, "add", path)
    git_command(source, "commit", "-m", "add dependency manifest")
    new = {"sha": git.text(source, "rev-parse", "HEAD"),
           "tree": git.text(source, "rev-parse", "HEAD^{tree}")}
    git_command(source, "update-ref", "refs/remotes/origin/main", new["sha"])
    bundle = target.parent / "maintenance.bundle"
    git_command(source, "bundle", "create", str(bundle), "refs/remotes/origin/main")
    before = git.entries(source, release["deployed"]["sha"])
    after = git.entries(source, new["sha"])
    paths = git.command(source, "diff", "--no-renames", "--name-only", "-z",
                        release["deployed"]["sha"], new["sha"]).decode().rstrip("\0").split("\0")
    release.update(selected=new, allowed_paths=paths,
                   objects={p: {"before": before.get(p), "after": after.get(p)} for p in paths})
    return git, target, release, bundle, path


def test_reviewed_manifest_change_can_fast_forward_only_when_explicitly_bound(dependency_case):
    git, target, release, bundle, path = dependency_case
    advance = getattr(git, "maintenance_fast_forward", None)
    assert callable(advance), "dependency-aware maintenance entry point is missing"
    advance(target, release, bundle, allowed_dependency_paths=[path])
    git.clean(target, release["selected"], tracking=release["selected"]["sha"])


@pytest.mark.parametrize("allowed", [[], ["unrelated/package.json"]])
def test_maintenance_refuses_unbound_dependency_change_before_mutation(dependency_case, allowed):
    git, target, release, bundle, _ = dependency_case
    advance = getattr(git, "maintenance_fast_forward", None)
    assert callable(advance), "dependency-aware maintenance entry point is missing"
    with pytest.raises(HandoffError):
        advance(target, release, bundle, allowed_dependency_paths=allowed)
    assert git.text(target, "rev-parse", "HEAD") == release["deployed"]["sha"]
