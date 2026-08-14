"""Regression tests for the retired direct model-profile writer."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILE_SCRIPT = REPO_ROOT / "scripts" / "firecrawl-ops" / "set_model_profile.sh"


class SetModelProfileTests(unittest.TestCase):
    def fixture_env(self, path: Path) -> bytes:
        original = (
            b"OPENAI_API_KEY=top-secret-value\n"
            b"OPENAI_BASE_URL=before\n"
            b"MODEL_NAME=before\n"
            b"MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK=before\n"
            b"FIRE_PDF_API_KEY=other-secret\n"
            b"KEEP_ME=unchanged\n"
        )
        path.write_bytes(original)
        return original

    def test_direct_invocation_cannot_mutate_under_any_handoff_marker(self) -> None:
        for profile in (
            "gateway",
            "gateway-pro",
            "gateway-codex",
            "openai-direct",
            "budget",
            "escalated",
        ):
            with (
                self.subTest(profile=profile),
                tempfile.TemporaryDirectory() as tmp_str,
            ):
                env_path = Path(tmp_str) / ".env"
                original = self.fixture_env(env_path)
                env = os.environ | {
                    "FC_DIR": str(REPO_ROOT),
                    "ENV_PATH": str(env_path),
                    "FIRECRAWL_OPERATOR_HANDOFF": "1",
                }

                result = subprocess.run(
                    ["bash", str(PROFILE_SCRIPT), profile],
                    cwd=REPO_ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn(
                    "Direct model-profile mutation is disabled", result.stderr
                )
                self.assertEqual(env_path.read_bytes(), original)
                self.assertNotIn("top-secret-value", result.stderr)

    def test_retired_writer_contains_no_env_or_stream_editor_mutation_path(
        self,
    ) -> None:
        source = PROFILE_SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("ENV_PATH", "FIRECRAWL_OPERATOR_HANDOFF", "sed -i", "docker"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
