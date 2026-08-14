"""Tests for local Firecrawl model-profile routing."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILE_SCRIPT = REPO_ROOT / "scripts" / "firecrawl-ops" / "set_model_profile.sh"
VERCEL_GATEWAY_URL = "https://ai-gateway.vercel.sh/v1"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


class SetModelProfileTests(unittest.TestCase):
    def run_profile(self, env_path: Path, *args: str) -> None:
        env = os.environ | {"ENV_PATH": str(env_path), "FC_DIR": str(REPO_ROOT)}
        result = subprocess.run(
            ["bash", str(PROFILE_SCRIPT), *args],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def apply_profile(self, *args: str) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text("OPENAI_API_KEY=placeholder\n", encoding="utf-8")
            self.run_profile(env_path, *args)
            return read_env(env_path)

    def test_default_gateway_profile_uses_requested_flash_snapshot(self) -> None:
        values = self.apply_profile()

        self.assertEqual(values["OPENAI_BASE_URL"], VERCEL_GATEWAY_URL)
        self.assertEqual(values["MODEL_NAME"], "deepseek/deepseek-v4-flash-0731")
        self.assertEqual(
            values["MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK"],
            "deepseek/deepseek-v4-pro-0813",
        )

    def test_gateway_pro_profile_uses_requested_pro_snapshot(self) -> None:
        values = self.apply_profile("gateway-pro")

        self.assertEqual(values["OPENAI_BASE_URL"], VERCEL_GATEWAY_URL)
        self.assertEqual(values["MODEL_NAME"], "deepseek/deepseek-v4-pro-0813")
        self.assertEqual(values["MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK"], "")

    def test_non_gateway_profile_clears_gateway_structured_output_fallback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text("OPENAI_API_KEY=placeholder\n", encoding="utf-8")
            self.run_profile(env_path)

            for profile in (
                "gateway-pro",
                "gateway-codex",
                "openai-direct",
                "budget",
                "escalated",
            ):
                with self.subTest(profile=profile):
                    self.run_profile(env_path, profile)
                    self.assertEqual(
                        read_env(env_path)["MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK"],
                        "",
                    )

    def test_invalid_profile_leaves_existing_or_missing_env_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env = os.environ | {"ENV_PATH": str(env_path), "FC_DIR": str(REPO_ROOT)}

            missing_result = subprocess.run(
                ["bash", str(PROFILE_SCRIPT), "not-a-profile"],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(missing_result.returncode, 2)
            self.assertIn("Unknown profile", missing_result.stderr)
            self.assertFalse(env_path.exists())

            original = b"UNRELATED_SETTING=preserve\n"
            env_path.write_bytes(original)
            existing_result = subprocess.run(
                ["bash", str(PROFILE_SCRIPT), "not-a-profile"],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(existing_result.returncode, 2)
            self.assertEqual(env_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
