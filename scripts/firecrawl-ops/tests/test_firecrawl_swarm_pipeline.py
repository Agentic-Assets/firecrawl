#!/usr/bin/env python3
"""Fixture-only tests for firecrawl_swarm_pipeline.py.

Run from the repo root:

    python3 scripts/firecrawl-ops/tests/test_firecrawl_swarm_pipeline.py
"""

from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError


PIPELINE_PATH = Path(__file__).resolve().parents[1] / "firecrawl_swarm_pipeline.py"


def load_pipeline_module():
    spec = importlib.util.spec_from_file_location("firecrawl_swarm_pipeline", PIPELINE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pipeline = load_pipeline_module()


class FirecrawlSwarmPipelineHelperTests(unittest.TestCase):
    def test_load_urls_skips_blank_and_comment_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "urls.txt"
            path.write_text("\n# comment\nhttps://example.com\n https://second.test \n", encoding="utf-8")
            self.assertEqual(pipeline.load_urls(path), ["https://example.com", "https://second.test"])

    def test_access_quality_and_confidence_classification(self) -> None:
        self.assertEqual(pipeline.classify_access("Cloudflare says verify you are human"), "blocked")
        self.assertEqual(pipeline.classify_access("Please sign in to continue"), "login-gated")
        self.assertEqual(pipeline.classify_access("Public content"), "accessible")
        self.assertEqual(pipeline.quality("captcha challenge", min_len=100), "blocked")
        self.assertEqual(pipeline.quality("short", min_len=100), "low_content")
        self.assertEqual(pipeline.quality("x" * 200, min_len=100), "ok")
        self.assertEqual(pipeline.confidence_score("captcha challenge", min_len=100), 0.0)
        self.assertEqual(pipeline.confidence_score("short", min_len=100), 0.35)
        self.assertEqual(pipeline.confidence_score("x" * 9000, min_len=100), 0.95)

    def test_scrape_builds_payload_and_extracts_lengths(self) -> None:
        captured: dict[str, object] = {}

        def fake_post_json(url, payload, timeout=180, headers=None):
            captured.update({"url": url, "payload": payload, "timeout": timeout, "headers": headers})
            return {
                "success": True,
                "data": {
                    "markdown": "markdown",
                    "rawHtml": "<html></html>",
                    "links": ["a", "b"],
                },
            }

        with patch.object(pipeline, "post_json", fake_post_json):
            result = pipeline.scrape(
                "http://local/v2",
                "https://example.com",
                formats=["markdown", "links"],
                only_main_content=False,
                wait_for=100,
            )

        self.assertEqual(captured["url"], "http://local/v2/scrape")
        self.assertEqual(
            captured["payload"],
            {
                "url": "https://example.com",
                "formats": ["markdown", "links"],
                "onlyMainContent": False,
                "waitFor": 100,
            },
        )
        self.assertEqual(result, (True, "markdown", 13, 2, None))

    def test_scrape_converts_http_and_url_errors_to_failed_results(self) -> None:
        http_error = HTTPError("http://local/v2/scrape", 403, "forbidden", {}, None)
        http_error.fp = io.BytesIO(b"nope")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            with patch.object(pipeline, "post_json", side_effect=http_error):
                ok, markdown, raw_html_len, link_count, error = pipeline.scrape(
                    "http://local/v2",
                    "https://example.com",
                )
        self.assertFalse(ok)
        self.assertEqual(markdown, "")
        self.assertEqual(raw_html_len, 0)
        self.assertEqual(link_count, 0)
        self.assertIn("HTTP 403", error or "")
        http_error.close()

        with patch.object(pipeline, "post_json", side_effect=URLError("offline")):
            ok, *_rest, error = pipeline.scrape("http://local/v2", "https://example.com")
        self.assertFalse(ok)
        self.assertIn("offline", error or "")


class FirecrawlSwarmPipelineMainTests(unittest.TestCase):
    def test_main_retries_low_content_without_switching_model_when_restart_is_disabled(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_scrape(api, url, timeout=180, formats=None, only_main_content=None, wait_for=None):
            calls.append(
                {
                    "api": api,
                    "url": url,
                    "formats": formats,
                    "only_main_content": only_main_content,
                    "wait_for": wait_for,
                }
            )
            if url.endswith("/good"):
                return True, "substantial " * 200, 0, 1, None
            if formats and "rawHtml" in formats:
                return True, "recovered " * 200, 50, 4, None
            return True, "thin", 0, 0, None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "urls.txt"
            output_path = root / "report.json"
            input_path.write_text("https://example.com/good\nhttps://example.com/thin\n", encoding="utf-8")
            argv = [
                "firecrawl_swarm_pipeline.py",
                "--input",
                str(input_path),
                "--out",
                str(output_path),
                "--min-len",
                "50",
                "--wide-retry-wait-ms",
                "321",
            ]
            with patch.object(sys, "argv", argv), patch.object(pipeline, "scrape", fake_scrape), patch.object(
                pipeline, "maybe_write_supabase", return_value=(False, "supabase_env_not_set")
            ), patch.object(pipeline.uuid, "uuid4", return_value="run-id"), patch.object(
                pipeline, "now_iso", return_value="2026-06-26T12:00:00+00:00"
            ), contextlib.redirect_stdout(io.StringIO()):
                pipeline.main()

            report = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(report["run_id"], "run-id")
        self.assertEqual(report["summary"]["total_urls"], 2)
        self.assertEqual(report["summary"]["wide_retries"], 1)
        self.assertEqual(report["summary"]["deepseek_pro_escalations"], 0)
        self.assertEqual(report["summary"]["final_ok"], 2)
        self.assertEqual(len(report["items"]), 3)
        retry_item = [item for item in report["items"] if item["stage"] == "retry_wide_content"][0]
        self.assertEqual(retry_item["model_profile"], "budget")
        self.assertEqual(retry_item["provenance"]["requested_model_profile"], "escalated")
        self.assertEqual(retry_item["provenance"]["formats"], ["markdown", "rawHtml", "links"])
        self.assertEqual(retry_item["provenance"]["waitFor"], 321)
        self.assertEqual(calls[-1]["url"], "https://example.com/thin")
        self.assertEqual(calls[-1]["only_main_content"], False)


if __name__ == "__main__":
    unittest.main()
