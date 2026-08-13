"""Regression tests for the local Firecrawl skill installer."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SYNC_SCRIPT = REPO_ROOT / "scripts" / "firecrawl-ops" / "sync_agent_skills.sh"


class SyncAgentSkillsTests(unittest.TestCase):
    def make_source(self, root: Path, skill: str = "firecrawl-ops") -> Path:
        source = root / "source"
        skill_dir = source / skill
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"---\nname: {skill}\n---\n", encoding="utf-8")
        (skill_dir / "nested.txt").write_text("canonical\n", encoding="utf-8")
        return source

    def run_sync(self, root: Path, source: Path, target: Path, links: Path) -> subprocess.CompletedProcess[str]:
        env = os.environ | {
            "FC_DIR": str(REPO_ROOT),
            "SOURCE_ROOT": str(source),
            "FIRECRAWL_USER_SKILLS_ROOT": str(target),
            "FIRECRAWL_SKILL_LINK_ROOTS": str(links),
        }
        return subprocess.run(
            ["bash", str(SYNC_SCRIPT), "firecrawl-ops"],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_replaces_canonical_destination_symlink_without_touching_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = self.make_source(root)
            target = root / "installed"
            external = root / "external"
            links = root / "links"
            external.mkdir()
            (external / "keep.txt").write_text("do not delete\n", encoding="utf-8")
            target.mkdir()
            destination = target / "firecrawl-ops"
            destination.symlink_to(external, target_is_directory=True)

            result = self.run_sync(root, source, target, links)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(destination.is_dir())
            self.assertFalse(destination.is_symlink())
            self.assertEqual((destination / "nested.txt").read_text(encoding="utf-8"), "canonical\n")
            self.assertEqual((external / "keep.txt").read_text(encoding="utf-8"), "do not delete\n")
            self.assertEqual((links / "firecrawl-ops").resolve(), destination.resolve())

    def test_replaces_stale_files_in_the_managed_canonical_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = self.make_source(root)
            target = root / "installed"
            destination = target / "firecrawl-ops"
            destination.mkdir(parents=True)
            (destination / "stale.txt").write_text("remove me\n", encoding="utf-8")

            result = self.run_sync(root, source, target, root / "links")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((destination / "stale.txt").exists())
            self.assertEqual((destination / "nested.txt").read_text(encoding="utf-8"), "canonical\n")


if __name__ == "__main__":
    unittest.main()
