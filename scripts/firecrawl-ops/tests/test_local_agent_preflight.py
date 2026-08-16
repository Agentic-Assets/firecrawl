"""Deterministic contract tests for the read-only local agent preflight."""

from __future__ import annotations

import argparse
import ast
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Self
from unittest.mock import patch
from urllib.error import HTTPError

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "local_agent_preflight.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
SPEC = importlib.util.spec_from_file_location("local_agent_preflight", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


class Response:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        return None

    def read(self, _limit: int | None = None) -> bytes:
        return self._body if _limit is None else self._body[:_limit]


class LocalAgentPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.v2_routes = self.root / "routes.ts"
        self.root_route = self.root / "index.ts"
        self.cli_wrapper = self.root / "firecrawl_cli.sh"
        self.mcp_wrapper = self.root / "firecrawl_mcp.sh"
        self.env_file = self.root / ".env"
        self.smoke_dir = self.root / "smoke"
        self.smoke_dir.mkdir()
        self.v2_routes.write_text(
            """v2Router.get(\"/team/queue-status\");
v2Router.get(\"/crawl/active\");
v2Router.get(\"/crawl/:jobId\");
v2Router.get(\"/batch/scrape/:jobId\");
v2Router.post(\"/scrape\");
v2Router.post(\"/extract\");
v2Router.post(\"/parse\");
""",
            encoding="utf-8",
        )
        self.root_route.write_text('app.get("/", () => {});', encoding="utf-8")
        self.cli_wrapper.write_text(
            'CLI_PACKAGE="${FIRECRAWL_CLI_PACKAGE:-firecrawl-cli@latest}"',
            encoding="utf-8",
        )
        self.mcp_wrapper.write_text(
            'PACKAGE="${FIRECRAWL_MCP_PACKAGE:-firecrawl-mcp@latest}"', encoding="utf-8"
        )
        self.env_file.write_text(
            "OPENAI_API_KEY=super-secret-key\n"
            "OPENAI_BASE_URL=https://provider.example\n"
            "MODEL_NAME=private-model\n"
            "FIRE_PDF_ENABLE=true\n"
            "FIRE_PDF_BASE_URL=http://host.docker.internal:31337\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @contextlib.contextmanager
    def patched_paths(self) -> object:
        with (
            patch.object(MODULE, "V2_ROUTE_FILE", self.v2_routes),
            patch.object(MODULE, "ROOT_ROUTE_FILE", self.root_route),
            patch.object(MODULE, "CLI_WRAPPER", self.cli_wrapper),
            patch.object(MODULE, "MCP_WRAPPER", self.mcp_wrapper),
            patch.object(MODULE, "ENV_FILE", self.env_file),
        ):
            yield

    def write_smoke(self, name: str, observed_at: object, passed: list[str]) -> Path:
        path = self.smoke_dir / name
        path.write_text(
            json.dumps(
                {
                    "observed_at": observed_at,
                    "results": [{"name": item, "status": "pass"} for item in passed],
                }
            ),
            encoding="utf-8",
        )
        return path

    def build_with_responses(
        self,
        *,
        smoke_file: Path | None = None,
        offline: bool = False,
        active_crawls: list[object] | None = None,
    ) -> tuple[dict[str, object], object]:
        if active_crawls is None:
            active_crawls = [
                {"url": "https://hidden.example", "token": "response-token"}
            ]
        responses = [
            Response(
                200, {"message": "Firecrawl API", "markdown": "secret source body"}
            ),
            Response(
                200,
                {
                    "success": True,
                    "jobsInQueue": 0,
                    "activeJobsInQueue": 0,
                    "url": "https://hidden.example",
                },
            ),
            Response(200, {"success": True, "crawls": active_crawls}),
        ]
        with (
            self.patched_paths(),
            patch.object(MODULE, "open_get", side_effect=responses) as urlopen_mock,
        ):
            document = MODULE.build_document(
                smoke_file=smoke_file,
                smoke_dir=self.smoke_dir,
                offline=offline,
                now=NOW,
            )
        return document, urlopen_mock

    def test_document_validates_all_named_capabilities_and_uses_only_get(self) -> None:
        smoke = self.write_smoke(
            "fresh-local-api-smoke.json",
            "2026-08-14T11:59:30Z",
            ["v2_crawl", "v2_batch_scrape"],
        )
        document, urlopen_mock = self.build_with_responses(smoke_file=smoke)

        self.assertEqual(set(document["capabilities"]), set(MODULE.CAPABILITIES))
        self.assertEqual(document["capabilities"]["base_http"]["state"], "ready")
        self.assertEqual(document["capabilities"]["async_jobs"]["state"], "degraded")
        self.assertEqual(
            document["capabilities"]["async_jobs"]["reason_code"],
            "active_crawls_present",
        )
        self.assertEqual(document["capabilities"]["ai_formats"]["state"], "degraded")
        self.assertEqual(document["capabilities"]["pdf_ocr"]["state"], "degraded")
        MODULE.validate_document(document)
        self.assertEqual(urlopen_mock.call_count, 3)
        for call in urlopen_mock.call_args_list:
            request = call.args[0]
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertIn(
                request.full_url,
                {
                    "http://localhost:3002/",
                    "http://localhost:3002/v2/team/queue-status",
                    "http://localhost:3002/v2/crawl/active",
                },
            )
            self.assertLessEqual(call.args[1], 10)

        rendered = json.dumps(document, sort_keys=True)
        for forbidden in (
            "super-secret-key",
            "secret source body",
            "hidden.example",
            "response-token",
            "private-model",
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertNotIn(
            "url", document["host_observations"]["queue_status"]["safe_fields"]
        )
        self.assertEqual(
            document["host_observations"]["crawl_active"]["safe_fields"],
            {"active_crawl_count": 1},
        )

    def test_smoke_freshness_rejects_missing_invalid_future_and_expired_timestamps(
        self,
    ) -> None:
        fixtures = {
            "fresh": ("2026-08-14T11:59:59Z", "fresh"),
            "missing": (None, "unknown"),
            "invalid": ("20260814-115959", "unknown"),
            "space_separated": ("2026-08-14 11:59:59Z", "unknown"),
            "future": ("2026-08-14T12:06:00Z", "unknown"),
            "expired": ("2026-08-13T11:59:59Z", "stale"),
        }
        for label, (observed_at, expected) in fixtures.items():
            with self.subTest(label=label):
                smoke = self.write_smoke(
                    f"{label}-local-api-smoke.json", observed_at, []
                )
                evidence = MODULE.select_smoke_evidence(smoke, self.smoke_dir, 60, NOW)
                self.assertEqual(evidence.state, expected)

    def test_require_fails_closed_for_all_nonready_states_and_unknown_names(
        self,
    ) -> None:
        smoke = self.write_smoke(
            "fresh-local-api-smoke.json",
            "2026-08-14T11:59:30Z",
            ["v2_crawl", "v2_batch_scrape"],
        )
        with (
            self.patched_paths(),
            patch.object(
                MODULE,
                "open_get",
                side_effect=[
                    Response(200, {"message": "Firecrawl API"}),
                    Response(200, {"jobsInQueue": 0}),
                    Response(200, {"crawls": []}),
                ],
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                MODULE.main(["--smoke-file", str(smoke), "--require", "base_http"]), 0
            )
        for name in ("async_jobs", "cli", "mcp", "ai_formats", "pdf_ocr"):
            with (
                self.subTest(name=name),
                self.patched_paths(),
                patch.object(
                    MODULE,
                    "open_get",
                    side_effect=[
                        Response(200, {"message": "Firecrawl"}),
                        Response(200, {"jobsInQueue": 0}),
                        Response(200, {"crawls": []}),
                    ],
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    MODULE.main(["--smoke-file", str(smoke), "--require", name]), 1
                )
        with (
            self.assertRaises(SystemExit) as error,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            MODULE.main(["--require", "not_real"])
        self.assertEqual(error.exception.code, 2)

    def test_require_fails_closed_for_stale_and_unavailable_capabilities(self) -> None:
        stale_smoke = self.write_smoke(
            "stale-local-api-smoke.json",
            "2026-08-13T12:00:00Z",
            ["v2_crawl", "v2_batch_scrape"],
        )
        healthy_responses = [
            Response(200, {"message": "Firecrawl API"}),
            Response(200, {"jobsInQueue": 0}),
            Response(200, {"crawls": []}),
        ]
        with (
            self.patched_paths(),
            patch.object(MODULE, "open_get", side_effect=healthy_responses),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                MODULE.main(
                    [
                        "--smoke-file",
                        str(stale_smoke),
                        "--max-evidence-age-seconds",
                        "1",
                        "--require",
                        "async_jobs",
                    ]
                ),
                1,
            )
        self.assertEqual(
            json.loads(stdout.getvalue())["capabilities"]["async_jobs"]["state"],
            "stale",
        )

        with (
            self.patched_paths(),
            patch.object(
                MODULE,
                "open_get",
                side_effect=[
                    Response(503, {"token": "do-not-print"}),
                    Response(200, {"jobsInQueue": 0}),
                    Response(200, {"crawls": []}),
                ],
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(MODULE.main(["--require", "base_http"]), 1)
        rendered = stdout.getvalue()
        self.assertEqual(
            json.loads(rendered)["capabilities"]["base_http"]["state"], "unavailable"
        )
        self.assertNotIn("do-not-print", rendered)

    def test_offline_package_specs_are_not_resolved_or_leaked_when_invalid(
        self,
    ) -> None:
        with (
            self.patched_paths(),
            patch.object(
                MODULE, "open_get", side_effect=AssertionError("HTTP must not run")
            ),
        ):
            valid = MODULE.build_document(
                smoke_dir=self.smoke_dir,
                offline=True,
                cli_package_spec="firecrawl-cli@1.18.0",
                mcp_package_spec="firecrawl-mcp@3.17.0",
                now=NOW,
            )
            invalid = MODULE.build_document(
                smoke_dir=self.smoke_dir,
                offline=True,
                cli_package_spec="firecrawl-cli@latest-secret",
                now=NOW,
            )
        self.assertEqual(valid["capabilities"]["cli"]["state"], "degraded")
        self.assertEqual(valid["declared_package_specs"]["cli"], "firecrawl-cli@1.18.0")
        self.assertEqual(invalid["capabilities"]["cli"]["state"], "unavailable")
        self.assertNotIn("latest-secret", json.dumps(invalid))
        self.assertFalse(hasattr(MODULE, "subprocess"))

    def test_matching_static_pin_is_not_ready_without_a_doctor_receipt(self) -> None:
        self.cli_wrapper.write_text(
            'CLI_PACKAGE="${FIRECRAWL_CLI_PACKAGE:-firecrawl-cli@1.18.0}"',
            encoding="utf-8",
        )
        self.mcp_wrapper.write_text(
            'PACKAGE="${FIRECRAWL_MCP_PACKAGE:-firecrawl-mcp@3.17.0}"',
            encoding="utf-8",
        )
        with (
            self.patched_paths(),
            patch.object(
                MODULE, "open_get", side_effect=AssertionError("HTTP must not run")
            ),
        ):
            document = MODULE.build_document(
                smoke_dir=self.smoke_dir,
                offline=True,
                cli_package_spec="firecrawl-cli@1.18.0",
                mcp_package_spec="firecrawl-mcp@3.17.0",
                now=NOW,
            )
        for name in ("cli", "mcp"):
            with self.subTest(name=name):
                self.assertEqual(document["capabilities"][name]["state"], "degraded")
                self.assertEqual(
                    document["capabilities"][name]["reason_code"],
                    "immutable_package_spec_declared_not_doctor_verified",
                )

        with (
            self.patched_paths(),
            patch.object(
                MODULE, "open_get", side_effect=AssertionError("HTTP must not run")
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                MODULE.main(
                    [
                        "--offline",
                        "--cli-package-spec",
                        "firecrawl-cli@1.18.0",
                        "--mcp-package-spec",
                        "firecrawl-mcp@3.17.0",
                        "--require",
                        "cli",
                        "--require",
                        "mcp",
                    ]
                ),
                1,
            )

    def test_fresh_smoke_cannot_authorize_async_jobs_and_active_crawls_fail_closed(
        self,
    ) -> None:
        smoke = self.write_smoke(
            "fresh-local-api-smoke.json",
            datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            ["v2_crawl", "v2_batch_scrape"],
        )
        for crawls, reason in (
            ([], "smoke_producer_contract_untrusted"),
            ([{}], "active_crawls_present"),
        ):
            with (
                self.subTest(crawls=crawls),
                self.patched_paths(),
                patch.object(
                    MODULE,
                    "open_get",
                    side_effect=[
                        Response(200, {"message": "Firecrawl API"}),
                        Response(200, {"success": True, "jobsInQueue": 0}),
                        Response(200, {"success": True, "crawls": crawls}),
                    ],
                ),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    MODULE.main(
                        ["--smoke-file", str(smoke), "--require", "async_jobs"]
                    ),
                    1,
                )
            capability = json.loads(stdout.getvalue())["capabilities"]["async_jobs"]
            self.assertEqual(capability["state"], "degraded")
            self.assertEqual(capability["reason_code"], reason)

    def test_queue_and_active_observations_require_explicit_success_true(self) -> None:
        for queue, active in (
            ({"success": False, "jobsInQueue": 0}, {"success": True, "crawls": []}),
            ({"success": True, "jobsInQueue": 0}, {"success": False, "crawls": []}),
            ({"jobsInQueue": 0}, {"success": True, "crawls": []}),
        ):
            with (
                self.subTest(queue=queue, active=active),
                self.patched_paths(),
                patch.object(
                    MODULE,
                    "open_get",
                    side_effect=[
                        Response(200, {"message": "Firecrawl API"}),
                        Response(200, queue),
                        Response(200, active),
                    ],
                ),
            ):
                document = MODULE.build_document(smoke_dir=self.smoke_dir, now=NOW)
            observations = document["host_observations"]
            self.assertIn(
                "invalid_response",
                {
                    observations["queue_status"]["result"],
                    observations["crawl_active"]["result"],
                },
            )
            self.assertEqual(document["capabilities"]["async_jobs"]["state"], "unknown")

    def test_exact_root_identity_rejects_misleading_message(self) -> None:
        with (
            self.patched_paths(),
            patch.object(
                MODULE,
                "open_get",
                side_effect=[
                    Response(
                        200, {"message": "Another service mentioning Firecrawl API"}
                    ),
                    Response(200, {"jobsInQueue": 0}),
                    Response(200, {"crawls": []}),
                ],
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(MODULE.main(["--require", "base_http"]), 1)
        document = json.loads(stdout.getvalue())
        self.assertEqual(document["capabilities"]["base_http"]["state"], "unavailable")
        self.assertFalse(
            document["host_observations"]["api_root"]["safe_fields"][
                "firecrawl_identity"
            ]
        )

    def test_proxy_environment_cannot_configure_the_read_only_opener(self) -> None:
        proxy_environment = {
            "http_proxy": "http://127.0.0.1:9",
            "HTTP_PROXY": "http://127.0.0.1:9",
        }
        with (
            patch.dict(os.environ, proxy_environment, clear=False),
            patch.object(
                MODULE, "ProxyHandler", wraps=MODULE.ProxyHandler
            ) as proxy_handler,
        ):
            opener = MODULE.build_read_only_opener()
        proxy_handler.assert_called_once_with({})
        proxy_handlers = [
            handler
            for handler in opener.handlers
            if isinstance(handler, MODULE.ProxyHandler)
        ]
        self.assertEqual(proxy_handlers, [])
        self.assertTrue(
            any(
                isinstance(handler, MODULE.NoRedirectHandler)
                for handler in opener.handlers
            )
        )

    def test_evidence_digest_binds_only_redacted_environment_evidence(self) -> None:
        def build_offline() -> dict[str, object]:
            with (
                self.patched_paths(),
                patch.object(
                    MODULE, "open_get", side_effect=AssertionError("HTTP must not run")
                ),
            ):
                return MODULE.build_document(
                    smoke_dir=self.smoke_dir, offline=True, now=NOW
                )

        configured = build_offline()
        self.env_file.write_text(
            "OPENAI_API_KEY=super-secret-key\n"
            "OPENAI_BASE_URL=https://provider.example\n"
            "MODEL_NAME=\n"
            "FIRE_PDF_ENABLE=true\n"
            "FIRE_PDF_BASE_URL=http://host.docker.internal:31337\n",
            encoding="utf-8",
        )
        model_missing = build_offline()
        self.env_file.write_text(
            "OPENAI_API_KEY=super-secret-key\n"
            "OPENAI_BASE_URL=https://provider.example\n"
            "MODEL_NAME=private-model\n"
            "FIRE_PDF_ENABLE=false\n"
            "FIRE_PDF_BASE_URL=http://host.docker.internal:31337\n",
            encoding="utf-8",
        )
        ocr_missing = build_offline()

        digests = {
            configured["evidence_digest"],
            model_missing["evidence_digest"],
            ocr_missing["evidence_digest"],
        }
        self.assertEqual(len(digests), 3)
        self.assertEqual(
            model_missing["capabilities"]["ai_formats"]["state"], "unavailable"
        )
        self.assertEqual(ocr_missing["capabilities"]["pdf_ocr"]["state"], "unavailable")
        self.assertNotEqual(
            MODULE.canonical_env_evidence(MODULE.EnvEvidence("configured", True, True)),
            MODULE.canonical_env_evidence(MODULE.EnvEvidence("unknown", True, True)),
        )
        for document in (configured, model_missing, ocr_missing):
            self.assertNotIn("super-secret-key", json.dumps(document, sort_keys=True))
            self.assertNotIn("private-model", json.dumps(document, sort_keys=True))

    def test_preflight_has_no_mutation_or_resolution_path(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        syntax_tree = ast.parse(source)
        imported_roots: set[str] = set()
        call_names: set[str] = set()
        for node in ast.walk(syntax_tree):
            if isinstance(node, ast.Import):
                imported_roots.update(
                    alias.name.split(".", 1)[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    call_names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute) and isinstance(
                    node.func.value, ast.Name
                ):
                    call_names.add(f"{node.func.value.id}.{node.func.attr}")
        self.assertFalse(
            {"subprocess", "shutil", "socket", "requests"} & imported_roots
        )
        self.assertFalse(
            {"os.system", "subprocess.run", "subprocess.Popen"} & call_names
        )

        initial_env = self.env_file.read_bytes()

        def refuse_mutation(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("preflight must not mutate files")

        guarded_path_methods = (
            patch.object(Path, "write_text", side_effect=refuse_mutation),
            patch.object(Path, "write_bytes", side_effect=refuse_mutation),
            patch.object(Path, "touch", side_effect=refuse_mutation),
            patch.object(Path, "unlink", side_effect=refuse_mutation),
            patch.object(Path, "rename", side_effect=refuse_mutation),
            patch.object(Path, "replace", side_effect=refuse_mutation),
            patch.object(Path, "mkdir", side_effect=refuse_mutation),
        )
        with self.patched_paths(), contextlib.ExitStack() as guards:
            for guarded_method in guarded_path_methods:
                guards.enter_context(guarded_method)
            with patch.object(
                MODULE,
                "open_get",
                side_effect=[
                    Response(200, {"message": "Firecrawl API"}),
                    Response(200, {"jobsInQueue": 0}),
                    Response(200, {"crawls": []}),
                ],
            ) as normal_get:
                MODULE.build_document(smoke_dir=self.smoke_dir, now=NOW)
            with patch.object(
                MODULE,
                "open_get",
                side_effect=AssertionError("offline mode must not use HTTP"),
            ):
                MODULE.build_document(smoke_dir=self.smoke_dir, offline=True, now=NOW)
        self.assertEqual(normal_get.call_count, 3)
        self.assertEqual(self.env_file.read_bytes(), initial_env)

    def test_static_only_mode_keeps_host_readiness_unknown(self) -> None:
        with (
            self.patched_paths(),
            patch.object(
                MODULE, "open_get", side_effect=AssertionError("HTTP must not run")
            ),
        ):
            document = MODULE.build_document(
                smoke_dir=self.smoke_dir, offline=True, now=NOW
            )
        self.assertEqual(document["capabilities"]["base_http"]["state"], "unknown")
        self.assertEqual(
            document["host_observations"]["api_root"]["result"], "not_checked"
        )

    def test_schema_validator_rejects_unknown_states_and_raw_body_fields(self) -> None:
        document, _mock = self.build_with_responses(offline=True)
        document["capabilities"]["base_http"]["state"] = "works locally"
        with self.assertRaisesRegex(ValueError, "enum"):
            MODULE.validate_document(document)
        document["capabilities"]["base_http"]["state"] = "unknown"
        document["host_observations"]["api_root"]["raw_body"] = "forbidden"
        with self.assertRaisesRegex(ValueError, "unexpected key"):
            MODULE.validate_document(document)

    def test_loopback_origin_and_http_error_projection_are_safe(self) -> None:
        for invalid in (
            "https://localhost:3002",
            "http://example.com",
            "http://localhost:3002/path",
            "http://user@localhost:3002",
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(argparse.ArgumentTypeError),
            ):
                MODULE.validate_local_api_url(invalid)
        error = HTTPError(
            "http://localhost:3002/",
            503,
            "error",
            {},
            io.BytesIO(b'{"token":"do-not-print"}'),
        )
        try:
            with patch.object(MODULE, "open_get", side_effect=error):
                observation = MODULE.request_get(
                    "http://localhost:3002", "/", 1, "2026-08-14T12:00:00Z"
                )
        finally:
            error.close()
        self.assertEqual(
            observation,
            {
                "checked_at": "2026-08-14T12:00:00Z",
                "result": "http_error",
                "http_status": 503,
                "safe_fields": {},
            },
        )


if __name__ == "__main__":
    unittest.main()
