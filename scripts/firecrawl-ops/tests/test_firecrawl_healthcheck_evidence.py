"""Unit tests for local healthcheck evidence serialization."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "firecrawl_healthcheck_evidence.py"
SPEC = importlib.util.spec_from_file_location("firecrawl_healthcheck_evidence", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FirecrawlHealthcheckEvidenceTests(unittest.TestCase):
    def test_parse_json_preserves_plain_text_and_decodes_json(self) -> None:
        self.assertEqual(MODULE.parse_json('{"ok":true}'), {"ok": True})
        self.assertEqual(MODULE.parse_json("not-json"), "not-json")

    def test_main_writes_json_and_markdown_with_error_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            json_path = tmp / "health.json"
            md_path = tmp / "health.md"
            env = {
                "STATUS": "fail",
                "API_URL": "http://localhost:3002",
                "FC_DIR": "/tmp/firecrawl",
                "IMAGE_ID": "sha256:test",
                "ERRORS_JSON": '["api unavailable"]',
                "DOCKER_PS": "api down",
                "ROOT_RESP": '{"message":"Firecrawl API"}',
                "RESP": '{"success":false}',
                "SCRAPE_SUMMARY": "plain failure summary",
                "JSON_PATH": str(json_path),
                "MD_PATH": str(md_path),
            }

            with patch.dict(os.environ, env, clear=True), patch.object(
                MODULE.time, "strftime", return_value="2026-08-13T12:00:00+0000"
            ):
                MODULE.main()

            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["timestamp"], "2026-08-13T12:00:00+0000")
            self.assertEqual(payload["errors"], ["api unavailable"])
            self.assertEqual(payload["scrape_response"], {"success": False})
            self.assertEqual(payload["scrape_summary"], "plain failure summary")
            markdown = md_path.read_text(encoding="utf-8")
            self.assertIn("- Errors: `1`", markdown)
            self.assertIn("- api unavailable", markdown)
            self.assertIn("plain failure summary", markdown)

    def test_main_omits_error_section_when_the_healthcheck_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            env = {
                "STATUS": "pass",
                "API_URL": "http://localhost:3002",
                "FC_DIR": "/tmp/firecrawl",
                "ERRORS_JSON": "[]",
                "JSON_PATH": str(tmp / "health.json"),
                "MD_PATH": str(tmp / "health.md"),
            }
            with patch.dict(os.environ, env, clear=True):
                MODULE.main()
            self.assertNotIn("## Errors", (tmp / "health.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
