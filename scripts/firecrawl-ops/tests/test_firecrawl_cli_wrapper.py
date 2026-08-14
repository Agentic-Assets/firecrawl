"""Unit tests for the local Firecrawl CLI wrapper.

Run from the repo root:

    python3 scripts/firecrawl-ops/tests/test_firecrawl_cli_wrapper.py
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

WRAPPER_PATH = Path(__file__).resolve().parents[1] / "firecrawl_cli.sh"


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class FirecrawlCliWrapperTests(unittest.TestCase):
    def make_env(
        self, tmp: Path, capture_file: Path, extra: dict[str, str] | None = None
    ) -> dict[str, str]:
        bin_dir = tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        write_executable(
            bin_dir / "npx",
            """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

capture = Path(os.environ["CAPTURE_FILE"])
capture.write_text(json.dumps({
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "env": {
        "FIRECRAWL_API_URL": os.environ.get("FIRECRAWL_API_URL"),
        "API_URL": os.environ.get("API_URL"),
        "FIRECRAWL_CLI_PACKAGE": os.environ.get("FIRECRAWL_CLI_PACKAGE"),
        "NPM_CONFIG_LOGLEVEL": os.environ.get("NPM_CONFIG_LOGLEVEL"),
    },
}, indent=2))
""",
        )
        env = os.environ.copy()
        env.pop("NPM_CONFIG_LOGLEVEL", None)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["CAPTURE_FILE"] = str(capture_file)
        if extra:
            env.update(extra)
        return env

    def run_wrapper(
        self,
        args: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(WRAPPER_PATH), *args],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_default_npx_invocation_uses_local_api_and_preserves_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            capture = tmp / "capture.json"
            work = tmp / "caller"
            work.mkdir()
            env = self.make_env(tmp, capture)

            result = self.run_wrapper(
                ["parse", "./fixture.pdf", "--json"],
                cwd=work,
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            data = read_json(capture)
            self.assertEqual(Path(data["cwd"]).resolve(), work.resolve())
            self.assertEqual(
                data["argv"],
                [
                    "-y",
                    "firecrawl-cli@1.20.0",
                    "--api-url",
                    "http://localhost:3002",
                    "parse",
                    "./fixture.pdf",
                    "--json",
                ],
            )
            self.assertEqual(data["env"]["NPM_CONFIG_LOGLEVEL"], "error")
            self.assertNotIn("@latest", data["argv"])

    def test_package_and_api_url_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            capture = tmp / "capture.json"
            env = self.make_env(
                tmp,
                capture,
                {
                    "FIRECRAWL_CLI_PACKAGE": "firecrawl-cli@1.18.0",
                    "API_URL": "http://api-url-env:3002",
                    "FIRECRAWL_API_URL": "http://firecrawl-api-env:3002",
                },
            )

            result = self.run_wrapper(
                ["scrape", "https://example.com"], cwd=tmp, env=env
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                read_json(capture)["argv"],
                [
                    "-y",
                    "firecrawl-cli@1.18.0",
                    "--api-url",
                    "http://firecrawl-api-env:3002",
                    "scrape",
                    "https://example.com",
                ],
            )

    def test_latest_override_is_rejected_even_with_stale_human_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            capture = tmp / "capture.json"
            env = self.make_env(
                tmp,
                capture,
                {
                    "FIRECRAWL_CLI_PACKAGE": "firecrawl-cli@latest",
                    "FIRECRAWL_HUMAN_UPGRADE_PROBE": "1",
                },
            )

            result = self.run_wrapper(["--version"], cwd=tmp, env=env)

            self.assertEqual(result.returncode, 2)
            self.assertIn("package_spec", result.stderr)
            self.assertFalse(capture.exists())

    def test_help_and_removed_mutation_options_do_not_call_npx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            capture = tmp / "capture.json"
            env = self.make_env(tmp, capture)

            help_result = self.run_wrapper(["--firecrawl-help"], cwd=tmp, env=env)
            self.assertEqual(help_result.returncode, 0)
            self.assertIn("Usage: firecrawl_cli.sh", help_result.stdout)
            self.assertFalse(capture.exists())

            for args in (
                [
                    "--firecrawl-model-profile",
                    "budget",
                    "scrape",
                    "https://example.com",
                ],
                ["--firecrawl-model-profile=gateway", "scrape", "https://example.com"],
                ["--firecrawl-no-recreate-api", "scrape", "https://example.com"],
                ["--firecrawl-healthcheck", "scrape", "https://example.com"],
            ):
                with self.subTest(args=args):
                    result = self.run_wrapper(args, cwd=tmp, env=env)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("operator_handoff", result.stderr)
                    self.assertFalse(capture.exists())


if __name__ == "__main__":
    unittest.main()
