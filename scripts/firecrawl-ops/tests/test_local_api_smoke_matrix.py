"""Unit tests for the local Firecrawl smoke matrix without live API calls."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "local_api_smoke_matrix.py"
SPEC = importlib.util.spec_from_file_location("local_api_smoke_matrix", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class LocalApiSmokeMatrixTests(unittest.TestCase):
    def make_context(self, tmp: Path, *, optional: bool = False) -> object:
        parse_file = tmp / "fixture.pdf"
        parse_file.write_bytes(b"%PDF-test")
        return MODULE.SmokeContext(
            api_url="http://localhost:3002/",
            api_key="test-key",
            timeout=4,
            poll_timeout=5,
            poll_interval=0,
            parse_file=parse_file,
            crawl_url="https://example.com",
            batch_url="https://batch.example.com",
            search_query="Firecrawl",
            out_dir=tmp / "out",
            include_mutating_optional_probes=optional,
        )

    def test_url_headers_and_payload_helpers_cover_valid_and_invalid_responses(self) -> None:
        self.assertEqual(MODULE.build_url("http://api:3002/", "/v2/scrape"), "http://api:3002/v2/scrape")
        self.assertEqual(MODULE.build_url("http://api:3002", "https://other.example/path"), "https://other.example/path")
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self.make_context(Path(tmp_dir))
            self.assertEqual(MODULE.auth_headers(ctx)["Authorization"], "Bearer test-key")
            ctx.api_key = None
            self.assertEqual(MODULE.auth_headers(ctx), {"Accept": "application/json"})
        self.assertEqual(MODULE.decode_body(b'{"success":true}'), {"success": True})
        self.assertEqual(MODULE.decode_body(b"invalid\xff"), "invalid�")
        self.assertTrue(MODULE.is_success_response({"success": True}))
        self.assertTrue(MODULE.is_success_response({}))
        self.assertFalse(MODULE.is_success_response({"success": False}))
        self.assertFalse(MODULE.is_success_response([]))
        self.assertEqual(MODULE.payload_data({"data": {"id": "one"}}), {"id": "one"})
        self.assertIsNone(MODULE.payload_data("not-json"))
        self.assertEqual(MODULE.extract_job_id({"data": {"jobId": "nested"}}), "nested")
        self.assertEqual(MODULE.extract_job_id({"id": "top"}), "top")
        with self.assertRaisesRegex(AssertionError, "job id"):
            MODULE.extract_job_id({"data": {}})
        self.assertEqual(MODULE.response_status({"data": {"status": "completed"}}), "completed")
        self.assertIsNone(MODULE.response_status("not-json"))
        self.assertEqual(MODULE.require_markdown({"data": {"markdown": " body "}}, "scrape"), " body ")
        with self.assertRaisesRegex(AssertionError, "markdown"):
            MODULE.require_markdown({"data": {}}, "scrape")

    def test_request_builders_use_expected_http_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            ctx = self.make_context(tmp)
            upload = tmp / "upload.txt"
            upload.write_text("payload", encoding="utf-8")
            with patch.object(MODULE, "open_request", return_value=(200, {"success": True})) as open_request:
                self.assertEqual(MODULE.request_json(ctx, "post", "/v2/scrape", {"url": "https://example.com"})[0], 200)
                json_request = open_request.call_args.args[0]
                self.assertEqual(json_request.get_method(), "POST")
                self.assertEqual(json.loads(json_request.data), {"url": "https://example.com"})
                self.assertEqual(json_request.get_header("Authorization"), "Bearer test-key")
                self.assertEqual(json_request.get_header("Content-type"), "application/json")

                MODULE.request_json(ctx, "GET", "/v2/crawl/active")
                get_request = open_request.call_args.args[0]
                self.assertIsNone(get_request.data)

                MODULE.request_multipart(ctx, "/v2/parse", {"options": "{}"}, {"file": upload})
                multipart_request = open_request.call_args.args[0]
                self.assertEqual(multipart_request.get_method(), "POST")
                self.assertIn("multipart/form-data", multipart_request.get_header("Content-type"))
                self.assertIn(b"payload", multipart_request.data)
                self.assertIn(b'name="options"', multipart_request.data)

            with self.assertRaisesRegex(FileNotFoundError, "Missing upload file"):
                MODULE.request_multipart(ctx, "/v2/parse", {}, {"file": tmp / "missing.pdf"})

    def test_open_request_maps_success_http_errors_and_unreachable_api(self) -> None:
        req = Request("http://localhost:3002/v2/scrape")

        class Response:
            status = 201

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"success":true}'

        with patch.object(MODULE, "urlopen", return_value=Response()):
            self.assertEqual(MODULE.open_request(req, 1), (201, {"success": True}))

        http_error = HTTPError(req.full_url, 404, "missing", {}, io.BytesIO(b'{"error":"missing"}'))
        with patch.object(MODULE, "urlopen", side_effect=http_error):
            self.assertEqual(MODULE.open_request(req, 1), (404, {"error": "missing"}))
        http_error.close()

        with patch.object(MODULE, "urlopen", side_effect=MODULE.URLError("offline")):
            with self.assertRaisesRegex(RuntimeError, "Could not reach"):
                MODULE.open_request(req, 1)

    def test_polling_and_probe_recording_cover_terminal_and_failure_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self.make_context(Path(tmp_dir))
            with patch.object(
                MODULE,
                "request_json",
                side_effect=[(200, {"status": "scraping"}), (200, {"data": {"status": "completed"}})],
            ), patch.object(MODULE.time, "time", side_effect=[0, 0, 1]), patch.object(MODULE.time, "sleep") as sleep:
                status, payload = MODULE.poll_job(ctx, "/v2/crawl/job")
            self.assertEqual((status, MODULE.response_status(payload)), (200, "completed"))
            sleep.assert_called_once_with(0)

            with patch.object(MODULE, "request_json", return_value=(503, {"error": "down"})), patch.object(
                MODULE.time, "time", side_effect=[0, 0]
            ):
                self.assertEqual(MODULE.poll_job(ctx, "/v2/crawl/job"), (503, {"error": "down"}))

            MODULE.add_probe(ctx, "ok", lambda: (200, {"success": True}, "done"))
            MODULE.add_probe(ctx, "assertion", lambda: (_ for _ in ()).throw(AssertionError("bad response")))
            MODULE.add_probe(ctx, "error", lambda: (_ for _ in ()).throw(RuntimeError("network")))
            self.assertEqual([item.status for item in ctx.results[-3:]], ["pass", "fail", "fail"])
            self.assertEqual(ctx.results[-2].error, "bad response")
            self.assertIn("RuntimeError: network", ctx.results[-1].error)

    def test_optional_service_gates_require_current_configuration_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self.make_context(Path(tmp_dir))
            with patch.object(
                MODULE,
                "request_json",
                return_value=(
                    503,
                    {"error": "Browser feature is not configured (BROWSER_SERVICE_URL is missing)."},
                ),
            ):
                self.assertEqual(
                    MODULE.check_browser_list(ctx)[2],
                    "browser service not configured as expected",
                )
            with patch.object(MODULE, "request_json", return_value=(503, {"error": "EXTRACT_V3_BETA_URL missing"})):
                self.assertEqual(MODULE.check_optional_agent_create(ctx)[2], "agent service not configured as expected")
            with patch.object(MODULE, "request_json", return_value=(503, {"error": "support_agent_unavailable"})):
                self.assertEqual(MODULE.check_optional_support_proxy(ctx)[2], "support service not configured as expected")
            with patch.object(MODULE, "request_json", return_value=(503, {"error": "BROWSER_SERVICE_URL missing"})):
                self.assertEqual(MODULE.check_optional_browser_create(ctx)[2], "browser service not configured as expected")
            with patch.object(MODULE, "request_json", return_value=(500, {"error": "Agent beta is not enabled"})):
                with self.assertRaisesRegex(AssertionError, "unexpected agent"):
                    MODULE.check_optional_agent_create(ctx)
            with patch.object(MODULE, "request_json", return_value=(500, {"error": "unexpected"})):
                self.assertRaisesRegex(
                    AssertionError,
                    "unexpected browser list",
                    MODULE.check_browser_list,
                    ctx,
                )

    def test_core_probe_contracts_cover_scrape_parse_async_and_queue_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self.make_context(Path(tmp_dir))
            with patch.object(MODULE, "request_json", return_value=(200, {"message": "Firecrawl API"})):
                self.assertEqual(MODULE.check_root(ctx)[2], "API root responded")
            with patch.object(MODULE, "request_json", return_value=(200, {"success": True, "data": {"markdown": "body"}})):
                self.assertEqual(MODULE.check_scrape(ctx)[2], "markdown_len=4")
            with patch.object(MODULE, "request_json", return_value=(200, {"success": True, "data": {"links": ["a", "b"]}})):
                self.assertEqual(MODULE.check_map(ctx)[2], "links=2")
            with patch.object(MODULE, "request_json", return_value=(200, {"success": True, "data": {"web": [1], "news": [2, 3]}})):
                self.assertEqual(MODULE.check_search(ctx)[2], "results=3")
            with patch.object(
                MODULE, "request_multipart", return_value=(200, {"success": True, "data": {"markdown": "pdf"}})
            ):
                self.assertEqual(MODULE.check_parse(ctx)[2], "markdown_len=3")
            with patch.object(MODULE, "request_json", return_value=(202, {"id": "batch-1"})), patch.object(
                MODULE, "poll_job", return_value=(200, {"success": True, "status": "completed"})
            ):
                self.assertEqual(MODULE.check_batch(ctx)[2], "job_id=batch-1")
            with patch.object(MODULE, "request_json", return_value=(202, {"jobId": "crawl-1"})), patch.object(
                MODULE, "poll_job", return_value=(200, {"success": True, "data": {"status": "completed"}})
            ):
                self.assertEqual(MODULE.check_crawl(ctx)[2], "job_id=crawl-1")
            with patch.object(MODULE, "request_json", return_value=(200, {"success": True, "jobsInQueue": 0})):
                self.assertEqual(MODULE.check_queue_status(ctx)[2], "jobsInQueue=0")
            with patch.object(MODULE, "request_json", return_value=(200, {"success": True, "crawls": []})):
                self.assertEqual(MODULE.check_active_crawls(ctx)[2], "active=0")

    def test_optional_browser_list_and_configured_services_report_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self.make_context(Path(tmp_dir))
            with patch.object(MODULE, "request_json", return_value=(200, {"success": True, "sessions": [{"id": "one"}]})):
                self.assertEqual(MODULE.check_browser_list(ctx)[2], "sessions=1")
            for probe in (MODULE.check_optional_browser_create, MODULE.check_optional_agent_create, MODULE.check_optional_support_proxy):
                with self.subTest(probe=probe.__name__), patch.object(
                    MODULE, "request_json", return_value=(201, {"success": True})
                ):
                    self.assertIn("appears configured", probe(ctx)[2])

    def test_probe_validation_errors_are_explicit_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self.make_context(Path(tmp_dir))
            with self.assertRaisesRegex(AssertionError, "HTTP status"):
                MODULE.expect_http_success(500, {"success": True})
            with self.assertRaisesRegex(AssertionError, "success flag"):
                MODULE.expect_http_success(200, {"success": False})
            with patch.object(MODULE, "request_json", return_value=(200, {"success": True, "links": "not-a-list"})):
                with self.assertRaisesRegex(AssertionError, "map links"):
                    MODULE.check_map(ctx)
            with patch.object(MODULE, "request_json", return_value=(200, {"success": True, "data": [1, 2]})):
                self.assertEqual(MODULE.check_search(ctx)[2], "results=2")
            with patch.object(MODULE, "request_json", return_value=(200, {"success": True, "data": "unexpected"})):
                self.assertEqual(MODULE.check_search(ctx)[2], "results=0")
            with patch.object(MODULE, "request_json", return_value=(202, {"id": "batch-1"})), patch.object(
                MODULE, "poll_job", return_value=(200, {"success": True, "status": "failed"})
            ):
                with self.assertRaisesRegex(AssertionError, "batch scrape ended"):
                    MODULE.check_batch(ctx)
            with patch.object(MODULE, "request_json", return_value=(202, {"id": "crawl-1"})), patch.object(
                MODULE, "poll_job", return_value=(200, {"success": True, "status": "cancelled"})
            ):
                with self.assertRaisesRegex(AssertionError, "crawl ended"):
                    MODULE.check_crawl(ctx)
            with patch.object(MODULE, "request_json", return_value=(200, {"success": True})):
                with self.assertRaisesRegex(AssertionError, "queue status"):
                    MODULE.check_queue_status(ctx)
            with patch.object(MODULE, "request_json", return_value=(200, {"success": True, "crawls": "not-a-list"})):
                with self.assertRaisesRegex(AssertionError, "active crawl"):
                    MODULE.check_active_crawls(ctx)
            for probe in (MODULE.check_optional_browser_create, MODULE.check_optional_agent_create, MODULE.check_optional_support_proxy):
                with self.subTest(probe=probe.__name__), patch.object(MODULE, "request_json", return_value=(500, {"error": "unexpected"})):
                    with self.assertRaisesRegex(AssertionError, "unexpected"):
                        probe(ctx)

    def test_matrix_and_artifacts_are_bounded_and_durable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self.make_context(Path(tmp_dir))

            def core_probe(_ctx: object) -> tuple[int, dict[str, bool], str]:
                return 200, {"success": True}, "core"

            def optional_probe(_ctx: object) -> tuple[int, dict[str, str], str]:
                return 503, {"error": "optional"}, "optional"

            with patch.object(MODULE, "CORE_PROBES", [("core", core_probe)]), patch.object(
                MODULE, "OPTIONAL_PROBES", [("optional", optional_probe, "not configured")]
            ):
                MODULE.run_matrix(ctx)
            self.assertEqual([(item.name, item.status) for item in ctx.results], [("core", "pass"), ("optional", "skip")])
            ctx.results.append(MODULE.ProbeResult(name="failed", status="fail", error="line one\nline two"))
            with patch.object(MODULE.time, "strftime", return_value="20260813-120000"):
                json_path, md_path = MODULE.write_artifacts(ctx)
            summary = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["counts"], {"pass": 1, "fail": 1, "skip": 1})
            self.assertIn("line one line two", md_path.read_text(encoding="utf-8"))

            optional_ctx = self.make_context(Path(tmp_dir), optional=True)
            with patch.object(MODULE, "CORE_PROBES", []), patch.object(
                MODULE, "OPTIONAL_PROBES", [("optional", optional_probe, "not configured")]
            ):
                MODULE.run_matrix(optional_ctx)
            self.assertEqual(optional_ctx.results[0].status, "pass")

    def test_main_returns_two_for_missing_fixture_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stderr = io.StringIO()
            with patch.object(sys, "argv", ["smoke", "--parse-file", str(Path(tmp_dir) / "missing.pdf")]), contextlib.redirect_stderr(stderr):
                self.assertEqual(MODULE.main(), 2)
            self.assertIn("Missing parse fixture", stderr.getvalue())

    def test_main_writes_success_artifacts_without_running_live_probes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            fixture = tmp / "fixture.pdf"
            fixture.write_bytes(b"%PDF-test")
            out_dir = tmp / "out"

            def fake_run_matrix(ctx: object) -> None:
                ctx.results.append(MODULE.ProbeResult(name="api_root", status="pass", detail="ok"))

            stdout = io.StringIO()
            argv = ["smoke", "--parse-file", str(fixture), "--out-dir", str(out_dir)]
            with patch.object(sys, "argv", argv), patch.object(MODULE, "run_matrix", fake_run_matrix), contextlib.redirect_stdout(stdout):
                self.assertEqual(MODULE.main(), 0)
            self.assertIn("[ok] api_root: ok", stdout.getvalue())
            self.assertEqual(len(list(out_dir.glob("*-local-api-smoke.json"))), 1)

            def failed_run_matrix(ctx: object) -> None:
                ctx.results.append(MODULE.ProbeResult(name="api_root", status="fail", error="down"))

            with patch.object(sys, "argv", argv), patch.object(MODULE, "run_matrix", failed_run_matrix), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(MODULE.main(), 1)


if __name__ == "__main__":
    unittest.main()
