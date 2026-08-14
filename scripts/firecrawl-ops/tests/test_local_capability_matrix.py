#!/usr/bin/env python3
"""Tests for local capability matrix evidence selection."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "local_capability_matrix.py"
SPEC = importlib.util.spec_from_file_location("local_capability_matrix", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class LocalCapabilityMatrixTests(unittest.TestCase):
    def test_latest_smoke_file_uses_modification_time_not_filename_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            older = tmp / "legacy" / "20260813-235959-local-api-smoke.json"
            newer = tmp / "current" / "20260812-000000-local-api-smoke.json"
            older.parent.mkdir()
            newer.parent.mkdir()
            older.write_text("{}", encoding="utf-8")
            newer.write_text("{}", encoding="utf-8")
            now = time.time()
            os.utime(older, (now - 60, now - 60))
            os.utime(newer, (now, now))

            self.assertEqual(MODULE.latest_smoke_file(tmp), newer)

    def test_route_extraction_status_classification_and_documentation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            route_file = tmp / "routes.ts"
            route_file.write_text(
                'v2Router.get("/scrape");\n'
                'v2Router.post(["/browser", "/crawl/:id"]);\n'
                'v2Router.post("/browser");\n',
                encoding="utf-8",
            )
            routes = MODULE.extract_routes(route_file)
            self.assertEqual(routes, [MODULE.Route("POST", "/browser"), MODULE.Route("POST", "/crawl/:id"), MODULE.Route("GET", "/scrape")])
            smoke = {
                ("GET", "/scrape"): {"status": "pass", "detail": "ok"},
                ("POST", "/browser"): {"status": "skip", "detail": "disabled"},
            }
            self.assertEqual(MODULE.classify(MODULE.Route("GET", "/scrape"), smoke), ("works locally", "ok"))
            self.assertEqual(MODULE.classify(MODULE.Route("POST", "/browser"), smoke), ("needs optional service", "disabled"))
            self.assertEqual(MODULE.classify(MODULE.Route("POST", "/crawl/:id"), smoke)[0], "partly covered")
            self.assertEqual(MODULE.classify(MODULE.Route("POST", "/agent"), smoke)[0], "needs optional service")
            self.assertEqual(MODULE.classify(MODULE.Route("POST", "/x402/pay"), smoke)[0], "hosted or configured only")
            self.assertEqual(MODULE.classify(MODULE.Route("GET", "/team/activity"), smoke)[0], "not tested")
            self.assertTrue(MODULE.doc_mentions("POST /v2/scrape", "/scrape"))
            self.assertTrue(MODULE.doc_mentions("batch scrape", "/batch/scrape"))
            self.assertTrue(MODULE.doc_mentions("queue-status", "/team/queue-status"))

    def test_route_classification_covers_configuration_and_unknown_categories(self) -> None:
        smoke = {("POST", "/scrape"): {"status": "fail", "error": "timeout"}}
        self.assertEqual(MODULE.load_smoke_status(None), {})
        self.assertEqual(MODULE.classify(MODULE.Route("POST", "/scrape"), smoke), ("not working in latest smoke", "timeout"))
        expected = {
            "/interact/session": "needs optional service",
            "/scrape/interact": "needs optional service",
            "/agent": "needs optional service",
            "/support/ask": "needs optional service",
            "/research/query": "needs optional service",
            "/x402/pay": "hosted or configured only",
            "/monitor": "hosted or configured only",
            "/extract": "needs model env",
            "/crawl/params-preview": "needs model env",
            "/team/credit-usage": "not tested",
            "/feedback": "not tested",
            "/keyless/eligibility": "not tested",
            "/crawl/example": "partly covered",
            "/unmapped": "not tested",
        }
        for path, status in expected.items():
            with self.subTest(path=path):
                self.assertEqual(MODULE.classify(MODULE.Route("GET", path), {})[0], status)
        self.assertTrue(MODULE.doc_mentions("scrape", "/scrape"))
        self.assertFalse(MODULE.doc_mentions("unrelated", "/scrape"))
        rows = MODULE.matrix_rows([MODULE.Route("GET", "/scrape")], "POST /v2/scrape", {})
        self.assertEqual(rows[0]["documented"], "yes")

    def test_load_smoke_write_markdown_and_main_error_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            smoke = tmp / "smoke-local-api-smoke.json"
            smoke.write_text('{"results":[{"name":"v2_scrape","status":"pass","detail":"ok"}]}', encoding="utf-8")
            statuses = MODULE.load_smoke_status(smoke)
            self.assertEqual(statuses[("POST", "/scrape")]["detail"], "ok")
            out = tmp / "matrix.md"
            MODULE.write_markdown(
                [{"method": "POST", "path": "/scrape", "status": "works locally", "documented": "yes", "note": "line one\nline two|pipe"}],
                out,
                tmp / "routes.ts",
                tmp / "docs.md",
                smoke,
            )
            self.assertIn("line one line two\\|pipe", out.read_text(encoding="utf-8"))
            no_smoke_out = tmp / "no-smoke.md"
            MODULE.write_markdown([], no_smoke_out, tmp / "routes.ts", tmp / "docs.md", None)
            self.assertIn("Smoke source: `none found`", no_smoke_out.read_text(encoding="utf-8"))

            stderr = StringIO()
            with patch.object(sys, "argv", ["matrix", "--smoke-dir", str(tmp / "none")]), redirect_stderr(stderr):
                self.assertEqual(MODULE.main(), 2)
            self.assertIn("No local API smoke artifact", stderr.getvalue())

    def test_main_generates_matrix_from_explicit_safe_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            route_file = tmp / "routes.ts"
            route_file.write_text('v2Router.post("/scrape");\n', encoding="utf-8")
            doc_file = tmp / "docs.md"
            doc_file.write_text("POST /v2/scrape", encoding="utf-8")
            smoke_file = tmp / "smoke-local-api-smoke.json"
            smoke_file.write_text('{"results":[{"name":"v2_scrape","status":"pass","detail":"ok"}]}', encoding="utf-8")
            out = tmp / "matrix.md"
            stdout = StringIO()
            argv = [
                "matrix",
                "--route-file",
                str(route_file),
                "--doc-file",
                str(doc_file),
                "--smoke-file",
                str(smoke_file),
                "--out",
                str(out),
            ]
            with patch.object(sys, "argv", argv), redirect_stdout(stdout):
                self.assertEqual(MODULE.main(), 0)
            self.assertIn("wrote", stdout.getvalue())
            self.assertIn("works locally", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
