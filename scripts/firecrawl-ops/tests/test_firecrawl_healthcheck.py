#!/usr/bin/env python3
"""Unit tests for the bounded local Firecrawl healthcheck."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
HEALTHCHECK = ROOT / "scripts" / "firecrawl-ops" / "firecrawl_healthcheck.sh"


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class FirecrawlHealthcheckTests(unittest.TestCase):
    def test_success_path_completes_and_writes_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            evidence_dir = tmp / "evidence"
            write_executable(
                bin_dir / "docker",
                "#!/usr/bin/env bash\nif [[ \"$*\" == *'image inspect'* ]]; then echo image-id; else echo api-up; fi\n",
            )
            write_executable(
                bin_dir / "curl",
                "#!/usr/bin/env bash\nif [[ \"$*\" == *'/v2/scrape'* ]]; then printf '%s' '{\"success\":true,\"data\":{\"markdown\":\"ok\"}}'; else printf '%s' '{\"message\":\"Firecrawl API\"}'; fi\n",
            )
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            result = subprocess.run(
                ["bash", str(HEALTHCHECK), "--evidence-dir", str(evidence_dir)],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
            evidence = list(evidence_dir.glob("*-firecrawl-healthcheck.json"))
            self.assertEqual(len(evidence), 1)
            payload = json.loads(evidence[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["scrape_response"]["data"]["markdown"], "ok")
            self.assertEqual(payload["scrape_summary"], {"success": True, "markdown_len": 2})

    def test_healthcheck_uses_configured_curl_bounds_and_piped_response_parsing(self) -> None:
        source = HEALTHCHECK.read_text(encoding="utf-8")
        self.assertIn('FIRECRAWL_HEALTHCHECK_MAX_TIME:-90', source)
        self.assertIn('FIRECRAWL_HEALTHCHECK_RETRIES:-12', source)
        self.assertIn('--connect-timeout "$CURL_CONNECT_TIMEOUT" --max-time "$CURL_MAX_TIME"', source)
        self.assertIn("printf '%s' \"$RESP\" | python3 -c", source)
        self.assertIn('python3 "$SCRIPT_DIR/firecrawl_healthcheck_evidence.py"', source)
        self.assertNotIn("python3 - <<", source)
        self.assertIn("ROOT_RESP RESP SCRAPE_SUMMARY", source)


if __name__ == "__main__":
    unittest.main()
