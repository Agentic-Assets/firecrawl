"""Bound a held-prefix failure with distinct native and runtime ancestors."""
import pytest
import test_cre_code_handoff as fixtures

from cre_code_handoff_evidence import HandoffError
from cre_runtime_maintenance_continuation import validate_continuation_code

git_case = fixtures.git_case
git_command = fixtures.git_command


@pytest.mark.parametrize("fault", [None, "untyped", "image-input"])
def test_held_prefix_allows_exact_two_ancestor_roots(git_case, fault):
    git, target, release, _, path = git_case
    source = target.parent / "source"
    runtime = release["selected"]
    (source / path).write_text("native moved after Hold\n")
    git_command(source, "add", ".")
    git_command(source, "commit", "-m", "native advanced")
    native = {"sha": git.text(source, "rev-parse", "HEAD"), "tree": git.text(source, "rev-parse", "HEAD^{tree}")}
    (source / path).write_text("reviewed successor\n")
    if fault == "image-input":
        file = source / "apps/api/Dockerfile"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("unreviewed image change\n")
    git_command(source, "add", ".")
    git_command(source, "commit", "-m", "successor")
    new = {"sha": git.text(source, "rev-parse", "HEAD"), "tree": git.text(source, "rev-parse", "HEAD^{tree}")}
    packet = {"old": release["deployed"], "runtime_before": runtime, "native_before": native, "new": new,
              "expected_images": {}, "image_tags": {}, "install_scope": [],
              "continuation": {"failure_kind": "native-advanced-hold-only-v1"}}
    if fault == "untyped": packet.pop("continuation")
    if fault:
        with pytest.raises(HandoffError): validate_continuation_code(packet, git, source)
    else:
        validate_continuation_code(packet, git, source)
