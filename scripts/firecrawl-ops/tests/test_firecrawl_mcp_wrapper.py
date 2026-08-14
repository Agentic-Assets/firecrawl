"""Unit and opt-in smoke tests for the local Firecrawl MCP wrapper.

Run wrapper unit tests from the repo root:

    python3 scripts/firecrawl-ops/tests/test_firecrawl_mcp_wrapper.py

Run the real MCP stdio smoke only when npm package resolution is acceptable:

    FIRECRAWL_RUN_MCP_SMOKE=1 python3 scripts/firecrawl-ops/tests/test_firecrawl_mcp_wrapper.py
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

WRAPPER_PATH = Path(__file__).resolve().parents[1] / "firecrawl_mcp.sh"


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class FirecrawlMcpWrapperTests(unittest.TestCase):
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
        "FIRECRAWL_API_KEY": os.environ.get("FIRECRAWL_API_KEY"),
        "TEST_API_KEY": os.environ.get("TEST_API_KEY"),
        "API_URL": os.environ.get("API_URL"),
        "FIRECRAWL_MCP_PACKAGE": os.environ.get("FIRECRAWL_MCP_PACKAGE"),
        "NPM_CONFIG_LOGLEVEL": os.environ.get("NPM_CONFIG_LOGLEVEL"),
    },
}, indent=2))
""",
        )
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["CAPTURE_FILE"] = str(capture_file)
        if extra:
            env.update(extra)
        return env

    def run_wrapper(
        self, *, cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(WRAPPER_PATH)],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_default_mcp_wrapper_invokes_local_package_with_local_dev_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            capture = tmp / "capture.json"
            env = self.make_env(tmp, capture)

            result = self.run_wrapper(cwd=tmp, env=env)

            self.assertEqual(result.returncode, 0, result.stderr)
            data = read_json(capture)
            self.assertEqual(data["argv"], ["-y", "firecrawl-mcp@3.24.0"])
            self.assertEqual(data["env"]["FIRECRAWL_API_URL"], "http://localhost:3002")
            self.assertEqual(data["env"]["FIRECRAWL_API_KEY"], "local-dev")
            self.assertEqual(data["env"]["NPM_CONFIG_LOGLEVEL"], "error")
            self.assertNotIn("@latest", data["argv"])

    def test_mcp_wrapper_honors_api_url_package_and_key_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            capture = tmp / "capture.json"
            env = self.make_env(
                tmp,
                capture,
                {
                    "API_URL": "http://api-url-env:3002",
                    "FIRECRAWL_API_URL": "http://firecrawl-api-env:3002",
                    "TEST_API_KEY": "test-key",
                    "FIRECRAWL_API_KEY": "firecrawl-key",
                    "FIRECRAWL_MCP_PACKAGE": "firecrawl-mcp@1.0.0",
                },
            )

            result = self.run_wrapper(cwd=tmp, env=env)

            self.assertEqual(result.returncode, 0, result.stderr)
            data = read_json(capture)
            self.assertEqual(data["argv"], ["-y", "firecrawl-mcp@1.0.0"])
            self.assertEqual(
                data["env"]["FIRECRAWL_API_URL"], "http://firecrawl-api-env:3002"
            )
            self.assertEqual(data["env"]["FIRECRAWL_API_KEY"], "firecrawl-key")

    def test_mcp_wrapper_uses_test_api_key_when_firecrawl_key_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            capture = tmp / "capture.json"
            env = self.make_env(tmp, capture, {"TEST_API_KEY": "test-key"})
            env.pop("FIRECRAWL_API_KEY", None)

            result = self.run_wrapper(cwd=tmp, env=env)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(read_json(capture)["env"]["FIRECRAWL_API_KEY"], "test-key")

    def test_latest_override_is_rejected_even_with_stale_human_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            capture = tmp / "capture.json"
            env = self.make_env(
                tmp,
                capture,
                {
                    "FIRECRAWL_MCP_PACKAGE": "firecrawl-mcp@latest",
                    "FIRECRAWL_HUMAN_UPGRADE_PROBE": "1",
                },
            )

            result = self.run_wrapper(cwd=tmp, env=env)

            self.assertEqual(result.returncode, 2)
            self.assertIn("package_spec", result.stderr)
            self.assertFalse(capture.exists())

    def test_mcp_wrapper_has_no_model_or_docker_mutation_surface(self) -> None:
        source = WRAPPER_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "set_model_profile.sh",
            "docker compose",
            "--model-profile",
            "--firecrawl-model-profile",
            "--healthcheck",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


def mcp_frame(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def read_mcp_message(
    proc: subprocess.Popen[bytes], timeout: float = 15.0
) -> dict[str, Any]:
    import select
    import time

    deadline = time.time() + timeout
    assert proc.stdout is not None
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for MCP JSONL response")
        ready, _, _ = select.select(
            [proc.stdout.fileno()], [], [], min(remaining, 0.25)
        )
        if not ready:
            continue
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP process closed before response")
        if line.strip():
            break
    return json.loads(line.decode("utf-8"))


@unittest.skipUnless(
    os.getenv("FIRECRAWL_RUN_MCP_SMOKE") == "1", "set FIRECRAWL_RUN_MCP_SMOKE=1 to run"
)
class FirecrawlMcpOptInSmokeTests(unittest.TestCase):
    def test_mcp_initialize_and_tools_list(self) -> None:
        if shutil.which("npx") is None:
            self.skipTest("npx is not available")

        proc = subprocess.Popen(
            ["bash", str(WRAPPER_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        assert proc.stdin is not None
        try:
            proc.stdin.write(
                mcp_frame(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {
                                "name": "firecrawl-local-smoke",
                                "version": "0.1.0",
                            },
                        },
                    }
                )
            )
            proc.stdin.flush()
            initialize = read_mcp_message(proc)
            self.assertEqual(initialize.get("id"), 1)
            self.assertIn("capabilities", initialize.get("result", {}))

            proc.stdin.write(
                mcp_frame(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    }
                )
            )
            proc.stdin.write(
                mcp_frame(
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
                )
            )
            proc.stdin.flush()
            tools = read_mcp_message(proc)
            self.assertEqual(tools.get("id"), 2)
            tool_names = {
                tool.get("name") for tool in tools.get("result", {}).get("tools", [])
            }
            self.assertIn("firecrawl_scrape", tool_names)
            self.assertTrue(
                tool_names.intersection(
                    {"firecrawl_map", "firecrawl_search", "firecrawl_crawl"}
                )
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            if proc.stdin:
                proc.stdin.close()
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
