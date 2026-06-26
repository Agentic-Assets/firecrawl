#!/usr/bin/env python3
"""Unit tests for the local Firecrawl direct HTTP helper.

Run from the repo root:

    python3 scripts/firecrawl-ops/tests/test_firecrawl_request.py
"""

from __future__ import annotations

import argparse
import contextlib
import io
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request


HELPER_PATH = Path(__file__).resolve().parents[1] / "firecrawl_request.py"


def load_helper_module():
    spec = importlib.util.spec_from_file_location("firecrawl_request", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = load_helper_module()


def header_value(req: Request, name: str) -> str | None:
    lowered = name.lower()
    for key, value in req.header_items():
        if key.lower() == lowered:
            return value
    return None


class HelperParsingTests(unittest.TestCase):
    def test_parse_csv_trims_and_drops_empty_values(self) -> None:
        self.assertEqual(helper.parse_csv(" markdown, links, ,html "), ["markdown", "links", "html"])
        self.assertEqual(helper.parse_csv(None), [])

    def test_parse_bool_accepts_common_values_and_rejects_invalid(self) -> None:
        self.assertTrue(helper.parse_bool("YES"))
        self.assertFalse(helper.parse_bool("off"))
        with self.assertRaises(argparse.ArgumentTypeError):
            helper.parse_bool("sometimes")

    def test_load_json_arg_and_file_validate_json(self) -> None:
        self.assertEqual(helper.load_json_arg('{"ok": true}', label="inline"), {"ok": True})
        with self.assertRaises(SystemExit):
            helper.load_json_arg("{bad", label="inline")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payload.json"
            path.write_text('{"value": 3}', encoding="utf-8")
            self.assertEqual(helper.load_json_file(str(path), label="payload"), {"value": 3})
            with self.assertRaises(SystemExit):
                helper.load_json_file(str(path.with_name("missing.json")), label="payload")

    def test_slugify_and_build_url(self) -> None:
        self.assertEqual(helper.slugify("https://example.com/A Path/?x=1"), "example.com-A-Path-x-1")
        self.assertEqual(helper.slugify("://"), "firecrawl")
        self.assertEqual(helper.build_url("http://localhost:3002", "/v2/scrape"), "http://localhost:3002/v2/scrape")
        self.assertEqual(helper.build_url("http://localhost:3002/", "v2/map"), "http://localhost:3002/v2/map")
        self.assertEqual(helper.build_url("https://api.example/v1", "https://other.test/x"), "https://other.test/x")


class RequestCompositionTests(unittest.TestCase):
    def test_request_json_builds_authenticated_json_request(self) -> None:
        captured: dict[str, object] = {}

        def fake_open(req: Request, timeout: float):
            captured["req"] = req
            captured["timeout"] = timeout
            return 200, b'{"success":true}'

        with patch.object(helper, "open_request", fake_open):
            status, body = helper.request_json(
                "http://localhost:3002",
                "POST",
                "/v2/scrape",
                {"url": "https://example.com"},
                "secret",
                7.5,
            )

        req = captured["req"]
        self.assertIsInstance(req, Request)
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"success":true}')
        self.assertEqual(req.full_url, "http://localhost:3002/v2/scrape")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data.decode("utf-8")), {"url": "https://example.com"})
        self.assertEqual(header_value(req, "Authorization"), "Bearer secret")
        self.assertEqual(header_value(req, "Content-type"), "application/json")
        self.assertEqual(captured["timeout"], 7.5)

    def test_request_json_omits_body_headers_for_get(self) -> None:
        captured: dict[str, Request] = {}

        def fake_open(req: Request, _timeout: float):
            captured["req"] = req
            return 200, b"{}"

        with patch.object(helper, "open_request", fake_open):
            helper.request_json("http://localhost:3002", "GET", "/v2/crawl/active", None, None, 1)

        req = captured["req"]
        self.assertIsNone(req.data)
        self.assertIsNone(header_value(req, "Content-type"))
        self.assertEqual(req.get_method(), "GET")

    def test_request_multipart_builds_options_and_file_part(self) -> None:
        captured: dict[str, Request] = {}

        def fake_open(req: Request, _timeout: float):
            captured["req"] = req
            return 200, b'{"success":true}'

        class FakeUUID:
            hex = "abc123"

        with tempfile.TemporaryDirectory() as tmp:
            upload = Path(tmp) / "sample.pdf"
            upload.write_bytes(b"%PDF-1.4\n")
            with patch.object(helper, "open_request", fake_open), patch.object(helper.uuid, "uuid4", lambda: FakeUUID()):
                helper.request_multipart(
                    "http://localhost:3002",
                    "/v2/parse",
                    {"options": '{"formats":["markdown"]}'},
                    {"file": upload},
                    "secret",
                    3,
                )

        req = captured["req"]
        body = req.data
        self.assertIn(b'name="options"', body)
        self.assertIn(b'{"formats":["markdown"]}', body)
        self.assertIn(b'filename="sample.pdf"', body)
        self.assertIn(b"%PDF-1.4", body)
        self.assertEqual(header_value(req, "Authorization"), "Bearer secret")
        self.assertIn("boundary=----firecrawl-local-abc123", header_value(req, "Content-type") or "")

    def test_request_multipart_missing_file_exits(self) -> None:
        with self.assertRaises(SystemExit):
            helper.request_multipart(
                "http://localhost:3002",
                "/v2/parse",
                {"options": "{}"},
                {"file": Path("/missing/nope.pdf")},
                None,
                1,
            )


class ResponseAndOutputTests(unittest.TestCase):
    def test_decode_format_and_payload_helpers(self) -> None:
        self.assertEqual(helper.decode_json_or_bytes(b'{"ok": true}'), {"ok": True})
        self.assertEqual(helper.decode_json_or_bytes(b"not-json"), b"not-json")
        self.assertEqual(helper.response_payload({"data": {"markdown": "x"}}), {"markdown": "x"})
        self.assertEqual(helper.response_payload({"data": ["x"]}), {"data": ["x"]})
        self.assertEqual(helper.format_result({"b": 1}, b"", pretty=False), b'{"b":1}\n')
        self.assertIn(b'\n  "b": 1\n', helper.format_result({"b": 1}, b"", pretty=True))
        self.assertEqual(helper.format_result("raw", b"raw-bytes", pretty=False), b"raw-bytes")

    def test_write_outputs_writes_json_and_split_fields(self) -> None:
        result = {
            "success": True,
            "data": {
                "markdown": "# Hello",
                "html": "<h1>Hello</h1>",
                "rawHtml": "<html></html>",
                "links": ["https://example.com"],
                "images": [],
                "metadata": {"title": "Example"},
                "json": {"domain": "example.com"},
                "summary": "Example summary",
                "query": {"answer": "yes"},
            },
        }
        with tempfile.TemporaryDirectory() as tmp, patch.object(helper, "timestamp", lambda: "20260626-120000"):
            out_dir = Path(tmp) / "responses"
            fields_dir = Path(tmp) / "fields"
            written = helper.write_outputs(
                result,
                b"raw",
                out=None,
                out_dir=str(out_dir),
                basename="https://example.com",
                pretty=True,
                save_fields=str(fields_dir),
                quiet=True,
            )

            self.assertTrue((out_dir / "20260626-120000-example.com.json").is_file())
            self.assertTrue((fields_dir / "markdown.md").is_file())
            self.assertEqual(json.loads((fields_dir / "links.json").read_text()), ["https://example.com"])
            self.assertEqual((fields_dir / "summary.txt").read_text(), "Example summary\n")
            self.assertGreaterEqual(len(written), 10)

    def test_write_outputs_saves_raw_response_for_non_dict_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            field_dir = Path(tmp) / "fields"
            written = helper.write_outputs(
                ["not", "a", "dict"],
                b"raw-bytes",
                out=None,
                out_dir=None,
                basename="x",
                pretty=False,
                save_fields=str(field_dir),
                quiet=True,
            )
            self.assertEqual(written, [field_dir / "response.bin"])
            self.assertEqual((field_dir / "response.bin").read_bytes(), b"raw-bytes")


class BodyBuilderTests(unittest.TestCase):
    def test_scrape_body_includes_optional_fields_and_schema_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            schema_file = Path(tmp) / "schema.json"
            headers_file = Path(tmp) / "headers.json"
            schema_file.write_text('{"type":"object"}', encoding="utf-8")
            headers_file.write_text('{"X-Test":"yes"}', encoding="utf-8")
            args = SimpleNamespace(
                url="https://example.com",
                formats="markdown,links",
                schema='{"ignored":true}',
                schema_file=str(schema_file),
                prompt="Extract fields",
                query=None,
                summary=False,
                only_main_content=True,
                wait_for=100,
                country="US",
                languages="en,es",
                proxy="basic",
                max_age=60,
                headers_file=str(headers_file),
            )

            body = helper.scrape_body(args)

        self.assertEqual(body["formats"][0:2], ["markdown", "links"])
        self.assertEqual(body["formats"][2], {"type": "json", "prompt": "Extract fields", "schema": {"type": "object"}})
        self.assertEqual(body["location"], {"country": "US", "languages": ["en", "es"]})
        self.assertEqual(body["headers"], {"X-Test": "yes"})
        self.assertTrue(body["onlyMainContent"])

    def test_parse_options_covers_pdf_and_tag_controls(self) -> None:
        args = SimpleNamespace(
            formats="markdown,html",
            no_pdf_parse=False,
            pdf_mode="fast",
            max_pages=2,
            fire_pdf_async=True,
            only_main_content=False,
            include_tags="main,article",
            exclude_tags="nav,footer",
            query="What is this?",
        )

        options = helper.parse_options(args)

        self.assertEqual(options["formats"][0:2], ["markdown", "html"])
        self.assertEqual(options["formats"][2], {"type": "query", "prompt": "What is this?"})
        self.assertEqual(options["parsers"], [{"type": "pdf", "mode": "fast", "maxPages": 2, "__firePdfAsync": True}])
        self.assertEqual(options["includeTags"], ["main", "article"])
        self.assertEqual(options["excludeTags"], ["nav", "footer"])
        self.assertFalse(options["onlyMainContent"])

    def test_parse_options_can_disable_pdf_parsers(self) -> None:
        args = SimpleNamespace(
            formats="markdown",
            no_pdf_parse=True,
            pdf_mode=None,
            max_pages=None,
            fire_pdf_async=False,
            only_main_content=None,
            include_tags=None,
            exclude_tags=None,
            query=None,
        )
        self.assertEqual(helper.parse_options(args)["parsers"], [])


class CommandBoundaryTests(unittest.TestCase):
    def test_cmd_post_inline_body_overrides_body_file(self) -> None:
        calls: list[tuple[str, str, str, object, str]] = []

        def fake_run_and_write(args, method, path, body, basename):
            calls.append((args.path, method, path, body, basename))

        with tempfile.TemporaryDirectory() as tmp:
            body_file = Path(tmp) / "body.json"
            body_file.write_text('{"from":"file"}', encoding="utf-8")
            args = SimpleNamespace(
                path="/v2/scrape",
                method="POST",
                body_file=str(body_file),
                body_json='{"from":"inline"}',
                basename=None,
            )
            with patch.object(helper, "run_and_write", fake_run_and_write):
                helper.cmd_post(args)

        self.assertEqual(calls[0], ("/v2/scrape", "POST", "/v2/scrape", {"from": "inline"}, "/v2/scrape"))

    def test_cmd_parse_sends_compact_options_and_file_stem_basename(self) -> None:
        captured: dict[str, object] = {}

        def fake_request_multipart(api_url, path, fields, files, api_key, timeout):
            captured.update(
                {
                    "api_url": api_url,
                    "path": path,
                    "fields": fields,
                    "files": files,
                    "api_key": api_key,
                    "timeout": timeout,
                }
            )
            return 200, b'{"success":true,"data":{"markdown":"ok"}}'

        with tempfile.TemporaryDirectory() as tmp, patch.object(helper, "request_multipart", fake_request_multipart):
            upload = Path(tmp) / "sample.pdf"
            upload.write_bytes(b"pdf")
            out = Path(tmp) / "out.json"
            args = SimpleNamespace(
                api_url="http://local",
                api_key="key",
                timeout=8,
                file=str(upload),
                formats="markdown",
                no_pdf_parse=False,
                pdf_mode="fast",
                max_pages=1,
                fire_pdf_async=False,
                only_main_content=None,
                include_tags=None,
                exclude_tags=None,
                query=None,
                out=str(out),
                out_dir=None,
                basename=None,
                pretty=False,
                save_fields=None,
                quiet=True,
                print_paths=False,
            )
            helper.cmd_parse(args)

        self.assertEqual(captured["path"], "/v2/parse")
        self.assertEqual(captured["files"], {"file": upload})
        self.assertEqual(json.loads(captured["fields"]["options"]), {"formats": ["markdown"], "parsers": [{"type": "pdf", "mode": "fast", "maxPages": 1}]})

    def test_run_and_write_raises_on_http_error_status_after_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "response.json"
            args = SimpleNamespace(
                api_url="http://local",
                api_key=None,
                timeout=1,
                out=str(out),
                out_dir=None,
                basename=None,
                pretty=False,
                save_fields=None,
                quiet=True,
                print_paths=False,
            )
            with patch.object(helper, "request_json", lambda *_args: (500, b'{"success":false}')):
                with self.assertRaises(SystemExit):
                    helper.run_and_write(args, "GET", "/broken", None, "broken")
            self.assertTrue(out.is_file())

    def test_apply_model_profile_is_mocked_at_subprocess_boundary(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, check):
            calls.append([str(part) for part in cmd])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yaml").write_text("services: {}\n", encoding="utf-8")
            scripts = root / "scripts" / "firecrawl-ops"
            scripts.mkdir(parents=True)
            (scripts / "set_model_profile.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            (scripts / "firecrawl_healthcheck.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            args = SimpleNamespace(
                model_profile="budget",
                firecrawl_dir=str(root),
                no_recreate_api=False,
                healthcheck=True,
            )
            with patch.object(helper.subprocess, "run", fake_run):
                helper.apply_model_profile(args)

        self.assertEqual(calls[0][-1], "budget")
        self.assertEqual(calls[1][0:2], ["docker", "compose"])
        self.assertTrue(calls[2][0].endswith("firecrawl_healthcheck.sh"))


class NetworkBoundaryTests(unittest.TestCase):
    def test_open_request_maps_http_and_url_errors_to_system_exit(self) -> None:
        req = Request("http://localhost:3002/v2/scrape")
        http_error = HTTPError(req.full_url, 500, "boom", {}, None)
        http_error.fp = io.BytesIO(b'{"error":"boom"}')
        with patch.object(helper, "urlopen", side_effect=http_error):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    helper.open_request(req, 1)
        http_error.close()

        with patch.object(helper, "urlopen", side_effect=URLError("no route")):
            with self.assertRaises(SystemExit):
                helper.open_request(req, 1)


if __name__ == "__main__":
    unittest.main()
