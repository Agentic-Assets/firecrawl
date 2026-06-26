#!/usr/bin/env python3
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
        "FIRECRAWL_API_KEY": os.environ.get("FIRECRAWL_API_KEY"),
        "TEST_API_KEY": os.environ.get("TEST_API_KEY"),
        "API_URL": os.environ.get("API_URL"),
        "FIRECRAWL_MCP_PACKAGE": os.environ.get("FIRECRAWL_MCP_PACKAGE"),
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

    def run_wrapper(self, *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
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
            self.assertEqual(data["argv"], ["-y", "firecrawl-mcp@latest"])
            self.assertEqual(data["env"]["FIRECRAWL_API_URL"], "http://localhost:3002")
            self.assertEqual(data["env"]["FIRECRAWL_API_KEY"], "local-dev")

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
            self.assertEqual(data["env"]["FIRECRAWL_API_URL"], "http://firecrawl-api-env:3002")
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


def mcp_frame(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def read_mcp_message(proc: subprocess.Popen[bytes], timeout: float = 15.0) -> dict[str, Any]:
    import select
    import time

    deadline = time.time() + timeout
    header = b""
    while b"\r\n\r\n" not in header:
        if time.time() > deadline:
            raise TimeoutError("Timed out waiting for MCP response headers")
        ready, _, _ = select.select([proc.stdout], [], [], 0.25)
        if not ready:
            continue
        chunk = proc.stdout.read(1)
        if not chunk:
            raise RuntimeError("MCP process closed before response")
        header += chunk
    header_text, body_prefix = header.split(b"\r\n\r\n", 1)
    content_length = None
    for line in header_text.decode("ascii", errors="replace").split("\r\n"):
        name, _, value = line.partition(":")
        if name.lower() == "content-length":
            content_length = int(value.strip())
    if content_length is None:
        raise RuntimeError("MCP response missing Content-Length")
    body = body_prefix
    while len(body) < content_length:
        if time.time() > deadline:
            raise TimeoutError("Timed out waiting for MCP response body")
        body += proc.stdout.read(content_length - len(body))
    return json.loads(body[:content_length].decode("utf-8"))


@unittest.skipUnless(os.getenv("FIRECRAWL_RUN_MCP_SMOKE") == "1", "set FIRECRAWL_RUN_MCP_SMOKE=1 to run")
class FirecrawlMcpOptInSmokeTests(unittest.TestCase):
    def test_mcp_initialize_and_tools_list(self) -> None:
        if shutil.which("npx") is None:
            self.skipTest("npx is not available")

        proc = subprocess.Popen(
            ["bash", str(WRAPPER_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
                            "clientInfo": {"name": "firecrawl-local-smoke", "version": "0.1.0"},
                        },
                    }
                )
            )
            proc.stdin.flush()
            initialize = read_mcp_message(proc)
            self.assertEqual(initialize.get("id"), 1)
            self.assertIn("capabilities", initialize.get("result", {}))

            proc.stdin.write(mcp_frame({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}))
            proc.stdin.write(mcp_frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}))
            proc.stdin.flush()
            tools = read_mcp_message(proc)
            self.assertEqual(tools.get("id"), 2)
            tool_names = {tool.get("name") for tool in tools.get("result", {}).get("tools", [])}
            self.assertIn("firecrawl_scrape", tool_names)
            self.assertTrue(tool_names.intersection({"firecrawl_map", "firecrawl_search", "firecrawl_crawl"}))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    unittest.main()
