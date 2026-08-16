"""Contract tests for the reversible local CRE resource profile.

Run from the repo root:

    python3 scripts/firecrawl-ops/tests/test_cre_resource_profile.py
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "firecrawl-ops" / "set_cre_resource_profile.sh"
COMPOSE = REPO_ROOT / "docker-compose.yaml"


class CreResourceProfileTests(unittest.TestCase):
    def run_script(
        self,
        root: Path,
        env_file: Path,
        state_file: Path,
        *args: str,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "FC_DIR": str(root),
                "ENV_PATH": str(env_file),
                "CRE_RESOURCE_PROFILE_STATE": str(state_file),
            }
        )
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

    def test_compose_has_independent_playwright_cap_and_optional_pid_limit(
        self,
    ) -> None:
        compose = COMPOSE.read_text(encoding="utf-8")
        self.assertIn(
            "MAX_CONCURRENT_PAGES: ${PLAYWRIGHT_MAX_CONCURRENT_PAGES:-${CRAWL_CONCURRENT_REQUESTS:-10}}",
            compose,
        )
        self.assertIn("pids_limit: ${PLAYWRIGHT_PIDS_LIMIT:-0}", compose)

    def test_apply_show_and_restore_preserve_unrelated_env_and_do_not_print_secret(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            (tmp / "apps" / "api").mkdir(parents=True)
            (tmp / "docker-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            env_file = tmp / ".env"
            env_file.write_text(
                "OPENAI_API_KEY=top-secret-value\n"
                "PLAYWRIGHT_MAX_CONCURRENT_PAGES=4\n"
                "PLAYWRIGHT_CPUS=2.5\n"
                "API_CPUS=3.0\n"
                "KEEP_ME=unchanged\n",
                encoding="utf-8",
            )
            state_file = tmp / "state" / "profile.state"

            applied = self.run_script(tmp, env_file, state_file, "apply", "--with-pids")
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertNotIn("top-secret-value", applied.stdout)
            current = env_file.read_text(encoding="utf-8")
            self.assertIn("OPENAI_API_KEY=top-secret-value", current)
            self.assertIn("KEEP_ME=unchanged", current)
            self.assertIn("PLAYWRIGHT_MAX_CONCURRENT_PAGES=1", current)
            self.assertIn("PLAYWRIGHT_CPUS=1.0", current)
            self.assertIn("API_CPUS=1.0", current)
            self.assertIn("PLAYWRIGHT_PIDS_LIMIT=192", current)
            self.assertTrue(state_file.exists())
            self.assertNotIn("top-secret-value", state_file.read_text(encoding="utf-8"))

            shown = self.run_script(tmp, env_file, state_file, "show")
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertIn("PLAYWRIGHT_MAX_CONCURRENT_PAGES=1", shown.stdout)
            self.assertNotIn("top-secret-value", shown.stdout)

            restored = self.run_script(tmp, env_file, state_file, "restore")
            self.assertEqual(restored.returncode, 0, restored.stderr)
            restored_text = env_file.read_text(encoding="utf-8")
            self.assertIn("PLAYWRIGHT_MAX_CONCURRENT_PAGES=4", restored_text)
            self.assertIn("PLAYWRIGHT_CPUS=2.5", restored_text)
            self.assertIn("API_CPUS=3.0", restored_text)
            self.assertNotIn("PLAYWRIGHT_PIDS_LIMIT=", restored_text)
            self.assertIn("OPENAI_API_KEY=top-secret-value", restored_text)
            self.assertIn("KEEP_ME=unchanged", restored_text)
            self.assertFalse(state_file.exists())

    def test_restore_without_state_refuses_to_guess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            (tmp / "apps" / "api").mkdir(parents=True)
            (tmp / "docker-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            env_file = tmp / ".env"
            env_file.write_text("KEEP_ME=unchanged\n", encoding="utf-8")
            state_file = tmp / "missing.state"

            result = self.run_script(tmp, env_file, state_file, "restore")
            self.assertEqual(result.returncode, 2)
            self.assertIn("refusing to guess", result.stderr)
            self.assertEqual(
                env_file.read_text(encoding="utf-8"), "KEEP_ME=unchanged\n"
            )

    def test_missing_env_gives_template_bootstrap_without_model_profile_advice(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            (tmp / "apps" / "api").mkdir(parents=True)
            (tmp / "docker-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            env_file = tmp / ".env"

            result = self.run_script(tmp, env_file, tmp / "state", "apply")

            self.assertEqual(result.returncode, 1)
            self.assertIn("minimal root .env template", result.stderr)
            self.assertIn("LOCAL_DEVELOPMENT_GUIDE.md", result.stderr)
            self.assertNotIn("apps/api/.env.example", result.stderr)
            self.assertNotIn("set_model_profile.sh", result.stderr)


if __name__ == "__main__":
    unittest.main()
