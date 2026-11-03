"""Changed paths determine reviewed Windows update scope."""

import sys
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cre_reviewed_update import (
    advance_checkout,
    advance_checkouts,
    assert_live_owner,
    changed_paths_between,
    classify_paths,
    inspect_git_update,
)


class ScopeTests(unittest.TestCase):
    def test_collector_only_change_does_not_rebuild(self):
        self.assertEqual(
            classify_paths(
                ["scripts/firecrawl-ops/cre_collector/sources/essex-realty.ts"]
            ),
            {
                "collector_install": False,
                "image_services": [],
                "compose_changed": False,
                "unsupported": [],
            },
        )

    def test_each_container_context_is_independent(self):
        result = classify_paths(
            [
                "apps/api/src/a.ts",
                "apps/playwright-service-ts/Dockerfile",
                "apps/nuq-postgres/init.sql",
            ]
        )
        self.assertEqual(
            result["image_services"], ["api", "nuq-postgres", "playwright-service"]
        )

    def test_replaced_context_or_ancestor_requires_rebuild(self):
        self.assertEqual(classify_paths(["apps/api"])["image_services"], ["api"])
        self.assertEqual(
            classify_paths(["apps"])["image_services"],
            ["api", "nuq-postgres", "playwright-service"],
        )

    def test_compose_change_is_config_not_automatically_image_build(self):
        result = classify_paths(["docker-compose.yaml"])
        self.assertTrue(result["compose_changed"])
        self.assertEqual(result["image_services"], [])
        for name in (
            "docker-compose.override.yaml",
            "docker-compose.override.yml",
            "compose.yaml",
            "compose.yml",
        ):
            self.assertTrue(classify_paths([name])["compose_changed"])

    def test_collector_lock_and_unsupported_nested_lock(self):
        result = classify_paths(
            [
                "scripts/firecrawl-ops/cre_collector/package-lock.json",
                "scripts/firecrawl-ops/cre_collector/document_text/package-lock.json",
            ]
        )
        self.assertTrue(result["collector_install"])
        self.assertEqual(
            result["unsupported"],
            ["scripts/firecrawl-ops/cre_collector/document_text/package-lock.json"],
        )

    def test_replacing_collector_directory_requires_install(self):
        self.assertTrue(
            classify_paths(["scripts/firecrawl-ops/cre_collector"])["collector_install"]
        )

    def test_invalid_paths_rejected(self):
        with self.assertRaises(TypeError):
            classify_paths("apps/api/file")
        for path in ("", "/apps/api/a", "apps/../x", "apps\\api\\a", "apps/api//x"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                classify_paths([path])


class GitDiffTests(unittest.TestCase):
    def test_selected_requires_forward_ancestry_and_exact_changed_paths(self):
        import subprocess
        import tempfile

        from cre_code_handoff_evidence import HandoffError
        from cre_code_handoff_git import Git

        with tempfile.TemporaryDirectory() as d:

            def g(*a):
                return subprocess.check_output(
                    ["git", "-C", d, *a], stderr=subprocess.DEVNULL, text=True
                ).strip()

            g("init")
            g("config", "user.name", "Synthetic")
            g("config", "user.email", "synthetic@example.invalid")
            Path(d, "file").write_text("a")
            g("add", "file")
            g("commit", "-m", "old")
            old = g("rev-parse", "HEAD")
            Path(d, "file").write_text("b")
            g("commit", "-am", "new")
            new = g("rev-parse", "HEAD")
            git = Git("/usr/bin/git")
            self.assertEqual(changed_paths_between(git, d, old, new), ["file"])
            with self.assertRaises(HandoffError):
                changed_paths_between(git, d, new, old)
            with self.assertRaises(ValueError):
                changed_paths_between(git, d, "HEAD", new)
            g("tag", "-a", "synthetic", "-m", "test")
            tag = g("rev-parse", "synthetic")
            with self.assertRaises(ValueError):
                changed_paths_between(git, d, old, tag)


class GitPlanTests(unittest.TestCase):
    def test_clean_forward_roots_and_dirty_refusal(self):
        import subprocess
        import tempfile

        from cre_code_handoff_evidence import HandoffError
        from cre_code_handoff_git import Git

        with tempfile.TemporaryDirectory() as d:

            def g(repo, *args):
                return subprocess.check_output(
                    ["git", "-C", str(repo), *args],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()

            base = Path(d)
            remote = base / "remote"
            subprocess.run(
                ["git", "init", "--bare", "--initial-branch=main", str(remote)],
                check=True,
                capture_output=True,
            )
            dev = base / "dev"
            subprocess.run(
                ["git", "clone", str(remote), str(dev)], check=True, capture_output=True
            )
            g(dev, "config", "user.name", "Test")
            g(dev, "config", "user.email", "test@example.invalid")
            (dev / "before").write_text("old")
            g(dev, "add", "before")
            g(dev, "commit", "-m", "old")
            g(dev, "push", "-u", "origin", "main")
            native = base / "native"
            runtime = base / "runtime"
            for repo in (native, runtime):
                subprocess.run(
                    ["git", "clone", "--no-hardlinks", str(dev), str(repo)],
                    check=True,
                    capture_output=True,
                )
            (dev / "before").write_text("new")
            g(dev, "commit", "-am", "new")
            g(dev, "push")
            selected = g(dev, "rev-parse", "HEAD")
            result = inspect_git_update(
                Git("/usr/bin/git"), dev, native, runtime, selected
            )
            self.assertEqual(result["native"]["changed_paths"], ["before"])
            self.assertEqual(result["runtime"]["changed_paths"], ["before"])
            # Source work is preserved and does not affect immutable Git objects.
            (dev / "before").write_text("unrelated development work")
            result = inspect_git_update(
                Git("/usr/bin/git"), dev, native, runtime, selected
            )
            self.assertEqual(result["selected"]["sha"], selected)
            (native / "before").write_text("user work")
            with self.assertRaises(HandoffError):
                inspect_git_update(Git("/usr/bin/git"), dev, native, runtime, selected)
            g(native, "checkout", "--", "before")
            alias = base / "parent-alias"
            alias.symlink_to(base, target_is_directory=True)
            with self.assertRaises(ValueError):
                inspect_git_update(
                    Git("/usr/bin/git"), dev, alias / "native", runtime, selected
                )


@pytest.mark.parametrize("loss_at", [0, 4, 6, 8])
def test_real_checkout_advance_under_runtime_lease(tmp_path, loss_at, monkeypatch):
    import subprocess

    import cre_checkpoint_refresh as checkpoint
    from cre_code_handoff_git import Git

    source, runtime = tmp_path / "source", tmp_path / "runtime"
    source.mkdir()

    def run(root, *args):
        return subprocess.check_output(
            ["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL
        ).strip()

    run(source, "init", "--initial-branch=main")
    run(source, "config", "user.name", "Synthetic")
    run(source, "config", "user.email", "fixture@example.invalid")
    (source / ".gitignore").write_text("scripts/firecrawl-ops/cre_collector/out/\n")
    (source / "value").write_text("before")
    run(source, "add", ".")
    run(source, "commit", "-m", "before")
    before = {
        "sha": run(source, "rev-parse", "HEAD"),
        "tree": run(source, "rev-parse", "HEAD^{tree}"),
    }
    subprocess.run(
        ["git", "clone", "--no-hardlinks", str(source), str(runtime)],
        check=True,
        capture_output=True,
    )
    (source / "value").write_text("after")
    run(source, "add", "value")
    run(source, "commit", "-m", "after")
    selected = {
        "sha": run(source, "rev-parse", "HEAD"),
        "tree": run(source, "rev-parse", "HEAD^{tree}"),
    }
    run(source, "update-ref", "refs/remotes/origin/main", selected["sha"])
    bundle = tmp_path / "selected.bundle"
    run(source, "bundle", "create", str(bundle), "refs/remotes/origin/main")
    lock = checkpoint.canonical_shared_lock_dir(runtime)
    lock.parent.mkdir(parents=True)
    checks = []
    with checkpoint.SharedLock(lock, recovery_required=True) as lease:
        import os

        generation = lease.authority_generation
        assert_live_owner(runtime, os.getpid(), generation)
        command = [
            sys.executable,
            "-B",
            str(Path(__file__).resolve().parents[1] / "cre_reviewed_update.py"),
            "--owner-check",
            str(runtime),
            str(os.getpid()),
            generation,
        ]
        subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
        )
        with pytest.raises(ValueError):
            assert_live_owner(runtime, os.getpid(), "wrong-generation")

        def refused():
            raise RuntimeError("fleet not held")

        with pytest.raises(RuntimeError, match="fleet not held"):
            advance_checkout(
                Git("/usr/bin/git"),
                lease,
                runtime,
                runtime,
                before,
                selected,
                source,
                bundle,
                refused,
            )
        assert run(runtime, "rev-parse", "HEAD") == before["sha"]
        if loss_at:
            original = lease._owned_directory_fd
            calls = []

            def lose_after_fetch():
                calls.append(True)
                if len(calls) == loss_at:
                    raise RuntimeError("owner lost")
                return original()

            monkeypatch.setattr(lease, "_owned_directory_fd", lose_after_fetch)
            with pytest.raises(RuntimeError, match="owner lost"):
                advance_checkout(
                    Git("/usr/bin/git"),
                    lease,
                    runtime,
                    runtime,
                    before,
                    selected,
                    source,
                    bundle,
                    lambda: None,
                )
            monkeypatch.setattr(lease, "_owned_directory_fd", original)
        else:
            advance_checkout(
                Git("/usr/bin/git"),
                lease,
                runtime,
                runtime,
                before,
                selected,
                source,
                bundle,
                lambda: checks.append(True),
            )
            lease.clear_recovery_requirement()
    with pytest.raises((ValueError, FileNotFoundError)):
        assert_live_owner(runtime, os.getpid(), generation)
    if loss_at:
        assert lock.exists()
        expected_head = selected["sha"] if loss_at == 8 else before["sha"]
        assert run(runtime, "rev-parse", "HEAD") == expected_head
        expected_remote = selected["sha"] if loss_at in {6, 8} else before["sha"]
        assert run(runtime, "rev-parse", "refs/remotes/origin/main") == expected_remote
    else:
        assert checks == [True, True]
        assert run(runtime, "rev-parse", "HEAD") == selected["sha"]
        assert (runtime / "value").read_text() == "after"

        class ReadOnlyGit(Git):
            def command(self, repo, *args, **kwargs):
                if args and args[0] in {"fetch", "update-ref", "merge"}:
                    raise AssertionError("no-op must not invoke Git writes")
                return super().command(repo, *args, **kwargs)

        with checkpoint.SharedLock(lock, recovery_required=True) as lease:
            advance_checkout(
                ReadOnlyGit("/usr/bin/git"),
                lease,
                runtime,
                runtime,
                selected,
                selected,
                source,
                tmp_path / "missing-bundle",
                lambda: None,
            )
            lease.clear_recovery_requirement()


@pytest.mark.parametrize("fail_after_first", [False, True])
def test_two_real_checkouts_preflight_before_either_is_advanced(
    tmp_path, fail_after_first
):
    import subprocess

    import cre_checkpoint_refresh as checkpoint
    from cre_code_handoff_evidence import HandoffError
    from cre_code_handoff_git import Git

    source, native, runtime = (tmp_path / n for n in ("source", "native", "runtime"))
    source.mkdir()

    def run(root, *args):
        return subprocess.check_output(
            ["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL
        ).strip()

    run(source, "init", "--initial-branch=main")
    run(source, "config", "user.name", "Synthetic")
    run(source, "config", "user.email", "fixture@example.invalid")
    (source / ".gitignore").write_text("scripts/firecrawl-ops/cre_collector/out/\n")
    (source / "value").write_text("before")
    run(source, "add", ".")
    run(source, "commit", "-m", "before")
    before = {
        "sha": run(source, "rev-parse", "HEAD"),
        "tree": run(source, "rev-parse", "HEAD^{tree}"),
    }
    for root in (native, runtime):
        subprocess.run(
            ["git", "clone", "--no-hardlinks", str(source), str(root)],
            check=True,
            capture_output=True,
        )
    (source / "value").write_text("after")
    run(source, "add", "value")
    run(source, "commit", "-m", "after")
    selected = {
        "sha": run(source, "rev-parse", "HEAD"),
        "tree": run(source, "rev-parse", "HEAD^{tree}"),
    }
    run(source, "update-ref", "refs/remotes/origin/main", selected["sha"])
    bundle = tmp_path / "selected.bundle"
    run(source, "bundle", "create", str(bundle), "refs/remotes/origin/main")
    lock = checkpoint.canonical_shared_lock_dir(runtime)
    lock.parent.mkdir(parents=True)
    targets = [
        {"root": root, "before": before, "tracking": before["sha"]}
        for root in (native, runtime)
    ]
    with checkpoint.SharedLock(lock, recovery_required=True) as lease:
        (runtime / "value").write_text("uncommitted work")
        with pytest.raises(HandoffError):
            advance_checkouts(
                Git("/usr/bin/git"),
                lease,
                runtime,
                targets,
                selected,
                source,
                bundle,
                lambda: None,
            )
        assert run(native, "rev-parse", "HEAD") == before["sha"]
        (runtime / "value").write_text("before")
        calls = []

        def check_held():
            calls.append(True)
            if fail_after_first and len(calls) == 4:
                raise RuntimeError("held state uncertain")

        if fail_after_first:
            with pytest.raises(RuntimeError, match="held state uncertain"):
                advance_checkouts(
                    Git("/usr/bin/git"),
                    lease,
                    runtime,
                    targets,
                    selected,
                    source,
                    bundle,
                    check_held,
                )
        else:
            advance_checkouts(
                Git("/usr/bin/git"),
                lease,
                runtime,
                targets,
                selected,
                source,
                bundle,
                check_held,
            )
            lease.clear_recovery_requirement()
    if fail_after_first:
        assert lock.exists()
        assert run(native, "rev-parse", "HEAD") == selected["sha"]
        assert run(runtime, "rev-parse", "HEAD") == before["sha"]
        return
    assert all(
        run(root, "rev-parse", "HEAD") == selected["sha"] for root in (native, runtime)
    )
