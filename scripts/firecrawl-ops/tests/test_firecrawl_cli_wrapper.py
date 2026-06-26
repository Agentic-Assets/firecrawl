#!/usr/bin/env python3
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
    def make_env(self, tmp: Path, capture_file: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
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
                ["-y", "firecrawl-cli@latest", "--api-url", "http://localhost:3002", "parse", "./fixture.pdf", "--json"],
            )
            self.assertEqual(data["env"]["NPM_CONFIG_LOGLEVEL"], "error")

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

            result = self.run_wrapper(["scrape", "https://example.com"], cwd=tmp, env=env)

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

    def test_help_and_missing_profile_value_do_not_call_npx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            capture = tmp / "capture.json"
            env = self.make_env(tmp, capture)

            help_result = self.run_wrapper(["--firecrawl-help"], cwd=tmp, env=env)
            self.assertEqual(help_result.returncode, 0)
            self.assertIn("Usage: firecrawl_cli.sh", help_result.stdout)
            self.assertFalse(capture.exists())

            missing_profile = self.run_wrapper(["--firecrawl-model-profile"], cwd=tmp, env=env)
            self.assertEqual(missing_profile.returncode, 2)
            self.assertIn("requires a value", missing_profile.stderr)
            self.assertFalse(capture.exists())

    def test_model_profile_no_recreate_runs_profile_script_without_docker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            capture = tmp / "capture.json"
            profile_capture = tmp / "profile.txt"
            docker_capture = tmp / "docker.txt"
            fc_root = tmp / "fc"
            scripts = fc_root / "scripts" / "firecrawl-ops"
            scripts.mkdir(parents=True)
            (fc_root / "docker-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            write_executable(
                scripts / "set_model_profile.sh",
                f"#!/usr/bin/env bash\nprintf '%s\\n' \"$1\" > {profile_capture}\n",
            )
            write_executable(
                scripts / "firecrawl_healthcheck.sh",
                "#!/usr/bin/env bash\nexit 0\n",
            )
            env = self.make_env(
                tmp,
                capture,
                {"FC_DIR": str(fc_root), "DOCKER_CAPTURE": str(docker_capture)},
            )

            result = self.run_wrapper(
                ["--firecrawl-model-profile", "budget", "--firecrawl-no-recreate-api", "scrape", "https://example.com"],
                cwd=tmp,
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(profile_capture.read_text(encoding="utf-8").strip(), "budget")
            self.assertFalse(docker_capture.exists())
            self.assertIn("running api was not recreated", result.stderr)
            self.assertTrue(capture.exists())

    def test_model_profile_recreate_runs_docker_and_healthcheck_before_npx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            capture = tmp / "capture.json"
            call_log = tmp / "calls.log"
            fc_root = tmp / "fc"
            scripts = fc_root / "scripts" / "firecrawl-ops"
            scripts.mkdir(parents=True)
            (fc_root / "docker-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            write_executable(
                scripts / "set_model_profile.sh",
                f"#!/usr/bin/env bash\necho profile:$1 >> {call_log}\n",
            )
            write_executable(
                scripts / "firecrawl_healthcheck.sh",
                f"#!/usr/bin/env bash\necho healthcheck >> {call_log}\n",
            )
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            write_executable(
                bin_dir / "docker",
                f"#!/usr/bin/env bash\necho docker:$* >> {call_log}\n",
            )
            env = self.make_env(tmp, capture, {"FC_DIR": str(fc_root)})
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"

            result = self.run_wrapper(
                [
                    "--firecrawl-model-profile",
                    "escalated",
                    "--firecrawl-healthcheck",
                    "search",
                    "Firecrawl docs",
                ],
                cwd=tmp,
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = call_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(calls[0], "profile:escalated")
            self.assertEqual(
                calls[1],
                f"docker:compose --project-directory {fc_root} up -d --force-recreate api",
            )
            self.assertEqual(calls[2], "healthcheck")
            self.assertEqual(read_json(capture)["argv"][-2:], ["search", "Firecrawl docs"])


if __name__ == "__main__":
    unittest.main()
