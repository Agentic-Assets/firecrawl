"""Verify skill repair preserves user content and canonical relative targets."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "ensure_skill_links.py"
SPEC = importlib.util.spec_from_file_location("ensure_skill_links", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SkillLinksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for relative, target in MODULE.SKILL_LINKS.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            (path.parent / target).mkdir(parents=True, exist_ok=True)
            path.write_bytes(target.encode())

    def test_doctor_identifies_exact_git_placeholders(self) -> None:
        self.assertEqual(
            set(MODULE.inspect_skill_links(self.root).values()), {"placeholder"}
        )

    def test_unknown_file_or_directory_is_never_replaced(self) -> None:
        path = self.root / ".claude/skills"
        path.write_text("valuable unrelated content", encoding="utf-8")
        with self.assertRaises(ValueError):
            MODULE.ensure_skill_links(self.root)
        self.assertEqual(path.read_text(), "valuable unrelated content")
        path.unlink()
        path.mkdir()
        with self.assertRaises(ValueError):
            MODULE.ensure_skill_links(self.root)
        self.assertTrue(path.is_dir())

    def test_denied_symlink_permission_preserves_all_placeholders(self) -> None:
        before = {key: (self.root / key).read_bytes() for key in MODULE.SKILL_LINKS}
        with (
            patch.object(Path, "symlink_to", side_effect=PermissionError("denied")),
            self.assertRaises(PermissionError),
        ):
            MODULE.ensure_skill_links(self.root)
        self.assertEqual(
            before, {key: (self.root / key).read_bytes() for key in before}
        )

    def test_missing_target_refuses_repair(self) -> None:
        (self.root / "scripts/firecrawl-ops").rmdir()
        with self.assertRaises(ValueError):
            MODULE.ensure_skill_links(self.root)
        self.assertEqual(
            set(MODULE.inspect_skill_links(self.root).values()), {"placeholder"}
        )

    def test_repair_and_second_apply_preserve_relative_targets_and_contents(
        self,
    ) -> None:
        probe = self.root / "probe"
        try:
            probe.symlink_to(
                os.path.normpath(".agents/skills"), target_is_directory=True
            )
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows process lacks symlink privilege; run elevated")
            raise
        probe.unlink()
        source = self.root / ".agents/skills/example/SKILL.md"
        source.parent.mkdir()
        source.write_text("canonical", encoding="utf-8")
        self.assertEqual(set(MODULE.ensure_skill_links(self.root).values()), {"ready"})
        self.assertEqual(
            (self.root / ".claude/skills/example/SKILL.md").read_text(), "canonical"
        )
        for relative, target in MODULE.SKILL_LINKS.items():
            self.assertEqual(
                os.readlink(self.root / relative).replace("\\", "/"), target
            )
        before = {
            key: (self.root / key).lstat().st_mtime_ns for key in MODULE.SKILL_LINKS
        }
        MODULE.ensure_skill_links(self.root)
        self.assertEqual(
            before, {key: (self.root / key).lstat().st_mtime_ns for key in before}
        )


if __name__ == "__main__":
    unittest.main()
