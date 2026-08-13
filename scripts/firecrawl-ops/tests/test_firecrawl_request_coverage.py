"""Additional unit coverage for agent-helper CLI and failure boundaries."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HELPER_PATH = Path(__file__).resolve().parents[1] / "firecrawl_request.py"
SPEC = importlib.util.spec_from_file_location("firecrawl_request_coverage", HELPER_PATH)
assert SPEC and SPEC.loader
HELPER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HELPER
SPEC.loader.exec_module(HELPER)


def output_args(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "out": None,
        "out_dir": None,
        "basename": None,
        "pretty": False,
        "save_fields": None,
        "quiet": True,
        "print_paths": False,
        "unwrap": False,
        "metrics_only": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FirecrawlRequestParserTests(unittest.TestCase):
    def test_parser_builds_every_command_and_rejects_invalid_page_caps(self) -> None:
        parser = HELPER.build_parser()
        cases = [
            (["health", "--metrics-only"], "health", HELPER.cmd_health),
            (["scrape", "https://example.com", "--query", "What changed?"], "scrape", HELPER.cmd_scrape),
            (["search", "Firecrawl", "--safe", "false"], "search", HELPER.cmd_search),
            (["map", "https://example.com", "--sitemap", "include", "--include-subdomains"], "map", HELPER.cmd_map),
            (["crawl", "https://example.com", "--wait", "--poll-timeout", "3"], "crawl", HELPER.cmd_crawl),
            (["crawl-status", "crawl-123", "--wait"], "crawl-status", HELPER.cmd_crawl_status),
            (["parse", "fixture.pdf", "--max-pages", "2"], "parse", HELPER.cmd_parse),
            (["post", "/v2/team/queue-status", "--body-json", "{}"], "post", HELPER.cmd_post),
        ]
        for argv, command, func in cases:
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertEqual(args.command, command)
                self.assertIs(args.func, func)

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["parse", "fixture.pdf", "--max-pages", "0"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["post", "/v2/scrape", "--body-json", "{}", "--body-file", "payload.json"])

    def test_main_dispatches_after_profile_application(self) -> None:
        calls: list[str] = []

        def command(args: SimpleNamespace) -> None:
            calls.append(args.command)

        args = SimpleNamespace(command="health", func=command)

        class Parser:
            def parse_args(self) -> SimpleNamespace:
                return args

        with patch.object(HELPER, "build_parser", return_value=Parser()), patch.object(HELPER, "apply_model_profile") as apply:
            self.assertEqual(HELPER.main(), 0)
        apply.assert_called_once_with(args)
        self.assertEqual(calls, ["health"])


class FirecrawlRequestOutputTests(unittest.TestCase):
    def test_write_response_handles_unwrap_metrics_crawl_ids_and_path_reporting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            unwrap_out = tmp / "unwrap.json"
            args = output_args(out=str(unwrap_out), unwrap=True, print_paths=True)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                HELPER.write_response(args, {"success": True, "data": {"value": 3}}, b"{}", 200, "scrape")
            self.assertEqual(json.loads(unwrap_out.read_text(encoding="utf-8")), {"value": 3})
            self.assertIn(str(unwrap_out), stderr.getvalue())

            metrics_out = tmp / "metrics.json"
            HELPER.write_response(
                output_args(out=str(metrics_out), metrics_only=True),
                {"success": True, "id": "submit-id", "data": {"markdown": "body"}},
                b"{}",
                202,
                "crawl",
                "crawl-id",
            )
            self.assertEqual(json.loads(metrics_out.read_text(encoding="utf-8"))["id"], "crawl-id")

    def test_write_outputs_prints_newline_and_skips_null_saved_fields(self) -> None:
        class CapturedStdout:
            def __init__(self) -> None:
                self.buffer = io.BytesIO()

        captured = CapturedStdout()
        with patch.object(HELPER.sys, "stdout", captured):
            written = HELPER.write_outputs(
                {"data": {"markdown": None}},
                b'{"data":{"markdown":null}}',
                out=None,
                out_dir=None,
                basename="response",
                pretty=False,
                save_fields=None,
                quiet=False,
            )
        self.assertEqual(written, [])
        self.assertTrue(captured.buffer.getvalue().endswith(b"\n"))

        with tempfile.TemporaryDirectory() as tmp_dir:
            fields = Path(tmp_dir) / "fields"
            written = HELPER.write_outputs(
                {"data": {"markdown": None}},
                b"raw",
                out=None,
                out_dir=None,
                basename="response",
                pretty=False,
                save_fields=str(fields),
                quiet=True,
            )
        self.assertEqual(written, [])


class FirecrawlRequestCommandTests(unittest.TestCase):
    def crawl_args(self, **overrides: object) -> SimpleNamespace:
        values: dict[str, object] = {
            "api_url": "http://local",
            "api_key": None,
            "timeout": 1,
            "url": "https://example.com",
            "limit": None,
            "max_concurrency": None,
            "include_paths": None,
            "exclude_paths": None,
            "scrape_formats": None,
            "headers_file": None,
            "user_agent": None,
            "wait": False,
            "poll_timeout": 1,
            "poll_interval": 0,
        }
        values.update(output_args().__dict__)
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_apply_model_profile_supports_noop_and_no_recreate(self) -> None:
        HELPER.apply_model_profile(SimpleNamespace(model_profile=None))
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "docker-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            scripts = root / "scripts" / "firecrawl-ops"
            scripts.mkdir(parents=True)
            (scripts / "set_model_profile.sh").write_text("", encoding="utf-8")
            args = SimpleNamespace(model_profile="gateway", firecrawl_dir=str(root), no_recreate_api=True, healthcheck=False)
            stderr = io.StringIO()
            with patch.object(HELPER.subprocess, "run") as run, contextlib.redirect_stderr(stderr):
                HELPER.apply_model_profile(args)
        run.assert_called_once()
        self.assertIn("not recreated", stderr.getvalue())

    def test_map_parse_and_health_error_paths_write_durable_output(self) -> None:
        calls: list[tuple[str, object]] = []

        def record(_args: object, _method: str, path: str, body: object, _basename: str) -> None:
            calls.append((path, body))

        with patch.object(HELPER, "run_and_write", record):
            HELPER.cmd_map(
                SimpleNamespace(url="https://example.com", limit=2, search="listing", sitemap="include", include_subdomains=True)
            )
        self.assertEqual(
            calls,
            [
                (
                    "/v2/map",
                    {"url": "https://example.com", "limit": 2, "search": "listing", "sitemap": "include", "includeSubdomains": True},
                )
            ],
        )

        parse_args = output_args(api_url="http://local", api_key=None, timeout=1, file="document.pdf")
        parse_args.formats = "markdown"
        parse_args.no_pdf_parse = False
        parse_args.pdf_mode = None
        parse_args.max_pages = None
        parse_args.fire_pdf_async = False
        parse_args.only_main_content = None
        parse_args.include_tags = None
        parse_args.exclude_tags = None
        parse_args.query = None
        with patch.object(HELPER, "request_multipart", return_value=(500, b'{"success":false}')), patch.object(
            HELPER, "write_response"
        ) as write:
            with self.assertRaises(SystemExit):
                HELPER.cmd_parse(parse_args)
        self.assertEqual(write.call_args.args[3], 500)

        health_args = output_args(api_url="http://local", api_key=None, timeout=1)
        with patch.object(HELPER, "request_json", return_value=(503, b'{"error":"down"}')), patch.object(
            HELPER, "write_response"
        ) as write:
            with self.assertRaises(SystemExit):
                HELPER.cmd_health(health_args)
        self.assertEqual(write.call_args.args[3], 503)

    def test_poll_and_crawl_commands_handle_failed_and_waited_workflows(self) -> None:
        args = self.crawl_args()
        writes: list[object] = []

        def record(*record_args: object) -> None:
            writes.append(record_args[1])

        with patch.object(HELPER, "request_json", return_value=(202, b'{"id":"crawl-1"}')), patch.object(
            HELPER, "write_response", record
        ):
            HELPER.cmd_crawl(args)
        self.assertEqual(writes, [{"id": "crawl-1"}])

        waited_args = self.crawl_args(wait=True)
        with patch.object(HELPER, "request_json", return_value=(202, b'{"id":"crawl-2"}')), patch.object(
            HELPER, "poll_crawl", return_value=(200, {"status": "completed"}, b"{}")
        ), patch.object(HELPER, "write_response", record):
            HELPER.cmd_crawl(waited_args)
        self.assertEqual(writes[-1], {"status": "completed"})

        with patch.object(HELPER, "request_json", return_value=(500, b'{"error":"down"}')), patch.object(
            HELPER, "write_response", record
        ):
            with self.assertRaises(SystemExit):
                HELPER.cmd_crawl(args)

        status_args = self.crawl_args(id="crawl-3", wait=True)
        with patch.object(HELPER, "poll_crawl", return_value=(200, {"status": "completed"}, b"{}")), patch.object(
            HELPER, "write_response", record
        ):
            HELPER.cmd_crawl_status(status_args)
        self.assertEqual(writes[-1], {"status": "completed"})

        for payload, expected in ((b"not-json", "non-JSON"), (b'{"status":"failed"}', "status=failed")):
            with self.subTest(payload=payload), patch.object(HELPER, "request_json", return_value=(200, payload)), patch.object(
                HELPER, "write_response", record
            ):
                with self.assertRaises(SystemExit) as exc:
                    HELPER.poll_crawl(args, "crawl-4")
            self.assertIn(expected, str(exc.exception))

        with patch.object(HELPER, "request_json", return_value=(503, b'{"error":"down"}')), patch.object(
            HELPER, "write_response", record
        ):
            with self.assertRaises(SystemExit):
                HELPER.poll_crawl(args, "crawl-5")


if __name__ == "__main__":
    unittest.main()
