"""Mocked boundary tests for the deliberately tiny agent-safe Firecrawl pilot."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

HELPER_PATH = Path(__file__).resolve().parents[1] / "firecrawl_request.py"
SPEC = importlib.util.spec_from_file_location(
    "firecrawl_request_agent_safe", HELPER_PATH
)
assert SPEC and SPEC.loader
HELPER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HELPER
SPEC.loader.exec_module(HELPER)

DOCTOR_PATH = Path(__file__).resolve().parents[1] / "firecrawl_compatibility_doctor.py"
DOCTOR_SPEC = importlib.util.spec_from_file_location(
    "firecrawl_compatibility_doctor_agent_safe", DOCTOR_PATH
)
assert DOCTOR_SPEC and DOCTOR_SPEC.loader
DOCTOR = importlib.util.module_from_spec(DOCTOR_SPEC)
sys.modules[DOCTOR_SPEC.name] = DOCTOR
DOCTOR_SPEC.loader.exec_module(DOCTOR)


class CapturedStdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()


def utc_timestamp(age_seconds: int = 0) -> str:
    value = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def prerequisite_record(*, age_seconds: int = 0) -> dict[str, dict[str, str]]:
    return {
        "preflight": {
            "evidence_digest": "sha256:" + "a" * 64,
            "observed_at": utc_timestamp(age_seconds),
        },
        "compatibility_doctor": {
            "evidence_digest": "sha256:" + "b" * 64,
            "observed_at": utc_timestamp(age_seconds),
        },
    }


def ready_preflight_document(observed_at: str | None = None) -> dict[str, object]:
    return {
        "observed_at": observed_at or utc_timestamp(),
        "evidence_digest": "sha256:" + "c" * 64,
        "capabilities": {"base_http": {"state": "ready"}},
        "host_observations": {
            "queue_status": {"result": "success", "safe_fields": {"jobs_in_queue": 0}},
            "crawl_active": {
                "result": "success",
                "safe_fields": {"active_crawl_count": 0},
            },
        },
    }


def idle_gate() -> list[tuple[int, bytes]]:
    return [
        (200, b'{"success":true,"jobsInQueue":0}'),
        (200, b'{"success":true,"crawls":[]}'),
    ]


class AgentSafeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        task_root = HELPER.REPO_ROOT / "tasks"
        task_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(
            dir=task_root, prefix="agent-safe-test-"
        )
        self.evidence_dir = Path(self.temporary.name)
        self.evidence_patch = patch.object(
            HELPER, "AGENT_SAFE_EVIDENCE_DIR", self.evidence_dir
        )
        self.evidence_patch.start()
        self.addCleanup(self.evidence_patch.stop)
        self.addCleanup(self.temporary.cleanup)
        self.prerequisite_patch = patch.object(
            HELPER, "run_agent_safe_prerequisites", return_value=prerequisite_record()
        )
        self.prerequisite_patch.start()
        self.addCleanup(self.prerequisite_patch.stop)

    @property
    def receipt_dir(self) -> str:
        return HELPER.AGENT_SAFE_EVIDENCE_RELATIVE.as_posix()

    def common(self, command: str, *, prerequisites: bool = False) -> list[str]:
        del prerequisites
        values = [
            command,
            "--agent-safe",
            "--metrics-only",
            "--timeout",
            "5",
            "--receipt-dir",
            self.receipt_dir,
        ]
        return values

    def parse(self, argv: list[str]):
        return HELPER.build_parser().parse_args(argv)

    def invoke(self, argv: list[str]) -> int:
        with patch.object(sys, "argv", ["firecrawl_request.py", *argv]):
            return HELPER.main()

    def receipts(self) -> tuple[dict[str, object], dict[str, object], str]:
        metrics_files = list(self.evidence_dir.glob("*-metrics.json"))
        manifest_files = list(self.evidence_dir.glob("*-receipt.json"))
        self.assertEqual(len(metrics_files), 1)
        self.assertEqual(len(manifest_files), 1)
        metrics_text = metrics_files[0].read_text(encoding="utf-8")
        manifest_text = manifest_files[0].read_text(encoding="utf-8")
        return (
            json.loads(metrics_text),
            json.loads(manifest_text),
            metrics_text + manifest_text,
        )

    def clear_receipts(self) -> None:
        for pattern in ("*-metrics.json", "*-receipt.json"):
            for path in self.evidence_dir.glob(pattern):
                path.unlink()


class AgentSafeValidationTests(AgentSafeTestCase):
    def test_accepts_only_canonical_loopback_origin_for_health(self) -> None:
        for supplied, expected in (
            ("http://localhost:3002", "http://localhost:3002"),
            ("http://127.0.0.1:3002/", "http://127.0.0.1:3002"),
            ("http://[::1]:3002", "http://[::1]:3002"),
        ):
            with self.subTest(supplied=supplied):
                args = self.parse([*self.common("health"), "--api-url", supplied])
                HELPER.validate_agent_safe_args(args)
                self.assertEqual(args.api_url, expected)

        for supplied in (
            "https://localhost:3002",
            "http://localhost:3003",
            "http://api.example.test:3002",
            "http://user@localhost:3002",
            "http://localhost:3002/v2",
            "http://localhost:3002?via=proxy",
        ):
            with (
                self.subTest(supplied=supplied),
                self.assertRaises(HELPER.AgentSafeViolation),
            ):
                HELPER.validate_agent_safe_args(
                    self.parse([*self.common("health"), "--api-url", supplied])
                )

    def test_rejects_target_receipt_and_raw_or_ai_options_before_evidence_read(
        self,
    ) -> None:
        invalid_targets = (
            "https://example.com",
            "https://example.com/?token=secret-marker",
            "https://example.com/#fragment",
            "https://127.0.0.1:3002/",
            "https://10.0.0.1/",
            "https://host.local/",
            "https://example.com:444/",
            "https://other.example/",
        )
        for target in invalid_targets:
            argv = [*self.common("scrape", prerequisites=True), target]
            with (
                self.subTest(target=target),
                self.assertRaises(HELPER.AgentSafeViolation),
            ):
                HELPER.validate_agent_safe_args(self.parse(argv))

        for extra in (
            ["--out", "tasks/raw.json"],
            ["--out-dir", "tasks/raw"],
            ["--save-fields", "tasks/fields"],
            ["--unwrap"],
            ["--schema", "{}"],
            ["--query", "marker"],
            ["--prompt", "marker"],
            ["--summary"],
            ["--headers-file", "headers.json"],
            ["--formats", "markdown,markdown"],
            ["--receipt-dir", "tasks/raw-secret/evidence"],
            ["--receipt-dir", "/tmp/escape"],
        ):
            argv = [
                *self.common("scrape", prerequisites=True),
                "https://example.com/",
                *extra,
            ]
            with (
                self.subTest(extra=extra),
                self.assertRaises(HELPER.AgentSafeViolation),
            ):
                HELPER.validate_agent_safe_args(self.parse(argv))

    def test_rejects_mutable_aliases_and_all_nonexact_crawl_bounds(self) -> None:
        for argv in (
            [*self.common("post"), "/v2/team/queue-status"],
            [*self.common("crawl-status"), "crawl_123"],
        ):
            with self.subTest(argv=argv), self.assertRaises(HELPER.AgentSafeViolation):
                HELPER.validate_agent_safe_args(self.parse(argv))

    def test_rejects_false_zero_and_empty_scrape_controls_by_presence(self) -> None:
        base = [*self.common("scrape"), "https://example.com/"]
        for extra in (
            ["--only-main-content", "false"],
            ["--wait-for", "0"],
            ["--max-age", "0"],
            ["--country", ""],
            ["--languages", ""],
            ["--proxy", ""],
            ["--headers-file", ""],
            ["--user-agent", ""],
        ):
            with (
                self.subTest(extra=extra),
                self.assertRaises(HELPER.AgentSafeViolation),
            ):
                HELPER.validate_agent_safe_args(self.parse([*base, *extra]))

        base = [
            *self.common("crawl", prerequisites=True),
            "https://example.com/",
            "--limit",
            "1",
            "--max-concurrency",
            "1",
            "--include-paths",
            "/",
            "--scrape-formats",
            "markdown",
            "--wait",
            "--poll-timeout",
            "30",
            "--poll-interval",
            "1",
        ]
        variants = (
            [item for item in base if item != "--wait"],
            [*base, "--timeout", "6"],
            [*base, "--include-paths", "/*"],
            [*base, "--limit", "2"],
            [*base, "--max-concurrency", "2"],
            [*base, "--scrape-formats", "markdown,links"],
            [*base, "--poll-timeout", "31"],
            [*base, "--poll-interval", "2"],
        )
        for argv in variants:
            with (
                self.subTest(argv=argv[-4:]),
                self.assertRaises(HELPER.AgentSafeViolation),
            ):
                HELPER.validate_agent_safe_args(self.parse(argv))

    def test_parse_rejects_nonfixture_without_reading_it_and_enforces_one_fast_page(
        self,
    ) -> None:
        argv = [
            *self.common("parse", prerequisites=True),
            ".env",
            "--pdf-mode",
            "fast",
            "--max-pages",
            "1",
        ]
        with (
            patch.object(Path, "read_bytes", autospec=True) as read_bytes,
            self.assertRaises(HELPER.AgentSafeViolation),
        ):
            HELPER.validate_agent_safe_args(self.parse(argv))
        read_bytes.assert_not_called()

        for extra in (
            ["--pdf-mode", "ocr", "--max-pages", "1"],
            ["--pdf-mode", "fast", "--max-pages", "2"],
            ["--pdf-mode", "fast", "--max-pages", "1", "--query", "x"],
        ):
            argv = [
                *self.common("parse", prerequisites=True),
                HELPER.AGENT_SAFE_PARSE_FILE.as_posix(),
                *extra,
            ]
            with (
                self.subTest(extra=extra),
                self.assertRaises(HELPER.AgentSafeViolation),
            ):
                HELPER.validate_agent_safe_args(self.parse(argv))

    def test_rejected_input_never_networks_or_writes(self) -> None:
        argv = [
            *self.common("scrape", prerequisites=True),
            "https://example.com/?secret=marker",
        ]
        with (
            patch.object(sys, "argv", ["firecrawl_request.py", *argv]),
            patch.object(HELPER, "request_json") as request_json,
            patch.object(HELPER, "request_multipart") as request_multipart,
            patch.object(HELPER, "write_outputs") as write_outputs,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            HELPER.main()
        self.assertEqual(error.exception.code, 2)
        request_json.assert_not_called()
        request_multipart.assert_not_called()
        write_outputs.assert_not_called()
        self.assertEqual(list(self.evidence_dir.glob("*-receipt.json")), [])

    def test_symlinked_receipt_component_is_rejected_before_network_or_output(
        self,
    ) -> None:
        task_root = HELPER.REPO_ROOT / "tasks"
        symlink = task_root / "agent-safe-symlink-test"
        with tempfile.TemporaryDirectory() as outside:
            symlink.symlink_to(outside, target_is_directory=True)
            self.addCleanup(symlink.unlink)
            argv = [*self.common("health"), "--quiet"]
            with (
                patch.object(HELPER, "AGENT_SAFE_EVIDENCE_DIR", symlink / "evidence"),
                patch.object(sys, "argv", ["firecrawl_request.py", *argv]),
                patch.object(HELPER, "request_json") as request_json,
                patch.object(HELPER, "write_outputs") as write_outputs,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                HELPER.main()
        self.assertEqual(error.exception.code, 2)
        request_json.assert_not_called()
        write_outputs.assert_not_called()

    def test_fake_prerequisite_artifacts_cannot_enable_post(self) -> None:
        argv = [
            *self.common("map", prerequisites=True),
            "https://example.com/",
            "--limit",
            "1",
        ]
        (self.evidence_dir / "preflight.json").write_text(
            '{"authorized":true}', encoding="utf-8"
        )
        (self.evidence_dir / "compatibility-doctor.json").write_text(
            '{"authorized":true}', encoding="utf-8"
        )
        with (
            patch.object(sys, "argv", ["firecrawl_request.py", *argv]),
            patch.object(
                HELPER,
                "run_agent_safe_prerequisites",
                side_effect=HELPER.AgentSafeViolation("untrusted"),
            ) as prerequisite_run,
            patch.object(HELPER, "request_json") as request_json,
            patch.object(HELPER, "write_outputs") as write_outputs,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaisesRegex(SystemExit, "agent_safe_prerequisite_failed"),
        ):
            HELPER.main()
        prerequisite_run.assert_called_once()
        request_json.assert_not_called()
        write_outputs.assert_not_called()
        self.assertEqual(list(self.evidence_dir.glob("*-receipt.json")), [])

    def test_same_process_prerequisites_require_normal_pinned_doctor(self) -> None:
        self.prerequisite_patch.stop()
        observed_at = utc_timestamp()
        document = {
            "observed_at": observed_at,
            "evidence_digest": "sha256:" + "c" * 64,
            "capabilities": {"base_http": {"state": "ready"}},
            "host_observations": {
                "queue_status": {
                    "result": "success",
                    "safe_fields": {"jobs_in_queue": 0},
                },
                "crawl_active": {
                    "result": "success",
                    "safe_fields": {"active_crawl_count": 0},
                },
            },
        }
        manifest = SimpleNamespace(sha256="d" * 64)
        preflight = SimpleNamespace(
            build_document=Mock(return_value=document), validate_document=Mock()
        )
        doctor = SimpleNamespace(
            load_manifest=Mock(return_value=manifest),
            agent_safe_result=Mock(
                return_value={
                    "schema_version": 1,
                    "kind": "firecrawl-agent-safe-compatibility",
                    "mode": "normal",
                    "status": "pass",
                    "body_bytes_persisted": 0,
                    "manifest_sha256": manifest.sha256,
                    "observed_at": observed_at,
                    "checks": {
                        "api": {"status": "pass"},
                        "cli": {"status": "pass"},
                        "mcp": {"status": "pass"},
                    },
                }
            ),
        )
        with patch.object(HELPER, "local_ops_module", side_effect=[preflight, doctor]):
            result = HELPER.run_agent_safe_prerequisites(
                SimpleNamespace(api_url="http://localhost:3002")
            )
        preflight.build_document.assert_called_once_with(
            api_url="http://localhost:3002", maximum_age_seconds=45, timeout_seconds=5.0
        )
        doctor.agent_safe_result.assert_called_once_with(
            manifest, api_url="http://localhost:3002"
        )
        self.assertEqual(set(result), {"preflight", "compatibility_doctor"})

    def test_stale_or_unready_same_process_preflight_fails_closed(self) -> None:
        self.prerequisite_patch.stop()
        for observed_at, queue_jobs in (
            (utc_timestamp(age_seconds=46), 0),
            (utc_timestamp(), 1),
        ):
            with self.subTest(observed_at=observed_at, queue_jobs=queue_jobs):
                document = {
                    "observed_at": observed_at,
                    "evidence_digest": "sha256:" + "c" * 64,
                    "capabilities": {"base_http": {"state": "ready"}},
                    "host_observations": {
                        "queue_status": {
                            "result": "success",
                            "safe_fields": {"jobs_in_queue": queue_jobs},
                        },
                        "crawl_active": {
                            "result": "success",
                            "safe_fields": {"active_crawl_count": 0},
                        },
                    },
                }
                preflight = SimpleNamespace(
                    build_document=Mock(return_value=document), validate_document=Mock()
                )
                with (
                    patch.object(HELPER, "local_ops_module", return_value=preflight),
                    self.assertRaises(HELPER.AgentSafeViolation),
                ):
                    HELPER.run_agent_safe_prerequisites(
                        SimpleNamespace(api_url="http://localhost:3002")
                    )


class AgentSafeTransportTests(AgentSafeTestCase):
    def test_safe_json_and_multipart_use_proxy_free_redirect_rejecting_transport(
        self,
    ) -> None:
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self) -> bytes:
                return b"{}"

        opener = Mock()
        opener.open.return_value = Response()
        handlers: list[object] = []
        with (
            patch.dict(
                os.environ, {"HTTP_PROXY": "http://proxy.invalid:1"}, clear=True
            ),
            patch.object(
                HELPER,
                "build_opener",
                side_effect=lambda *items: handlers.extend(items) or opener,
            ),
            patch.object(HELPER, "urlopen") as urlopen,
        ):
            self.assertEqual(
                HELPER.request_json(
                    "http://localhost:3002", "GET", "/", None, None, 5, agent_safe=True
                ),
                (200, b"{}"),
            )
        urlopen.assert_not_called()
        proxy = next(item for item in handlers if isinstance(item, HELPER.ProxyHandler))
        self.assertEqual(proxy.proxies, {})
        redirect = next(
            item
            for item in handlers
            if isinstance(item, HELPER.AgentSafeNoRedirectHandler)
        )
        self.assertIsNone(redirect.redirect_request(None, None, None, None, None, None))

        source = self.evidence_dir / "fixture.pdf"
        source.write_bytes(b"fixture")
        with patch.object(
            HELPER, "open_request", return_value=(200, b"{}")
        ) as open_request:
            HELPER.request_multipart(
                "http://localhost:3002",
                "/v2/parse",
                {"options": "{}"},
                {"file": source},
                None,
                5,
                agent_safe=True,
            )
        self.assertTrue(open_request.call_args.kwargs["agent_safe"])


class AgentSafeDispatchAndReceiptTests(AgentSafeTestCase):
    def test_real_prerequisite_wiring_never_runs_a_map_before_the_recipe_post(
        self,
    ) -> None:
        self.prerequisite_patch.stop()
        preflight = SimpleNamespace(
            build_document=Mock(return_value=ready_preflight_document()),
            validate_document=Mock(),
        )
        manifest = DOCTOR.load_manifest()
        argv = [
            *self.common("map"),
            "https://example.com/",
            "--limit",
            "1",
            "--quiet",
        ]
        with (
            patch.object(HELPER, "local_ops_module", side_effect=[preflight, DOCTOR]),
            patch.object(DOCTOR, "load_manifest", return_value=manifest),
            patch.object(DOCTOR, "preflight_api"),
            patch.object(
                DOCTOR,
                "run_cli_version_probe",
                return_value={"status": "pass", "body_bytes_persisted": 0},
            ) as cli_version,
            patch.object(
                DOCTOR,
                "run_mcp_probe",
                return_value={"status": "pass", "body_bytes_persisted": 0},
            ) as mcp_probe,
            patch.object(
                DOCTOR,
                "run_cli_probe",
                side_effect=AssertionError("agent-safe prerequisite must not map"),
            ) as cli_map,
            patch.object(sys, "argv", ["firecrawl_request.py", *argv]),
            patch.object(
                HELPER,
                "request_json",
                side_effect=[*idle_gate(), (200, b'{"links":[]}')],
            ) as request_json,
        ):
            self.assertEqual(HELPER.main(), 0)
        cli_version.assert_called_once()
        mcp_probe.assert_called_once()
        cli_map.assert_not_called()
        self.assertEqual(
            [call.args[1:3] for call in request_json.call_args_list],
            [
                ("GET", "/v2/team/queue-status"),
                ("GET", "/v2/crawl/active"),
                ("POST", "/v2/map"),
            ],
        )

    def test_read_only_compatibility_failure_cannot_follow_an_unreceipted_map(
        self,
    ) -> None:
        self.prerequisite_patch.stop()
        preflight = SimpleNamespace(
            build_document=Mock(return_value=ready_preflight_document()),
            validate_document=Mock(),
        )
        manifest = DOCTOR.load_manifest()
        argv = [
            *self.common("map"),
            "https://example.com/",
            "--limit",
            "1",
            "--quiet",
        ]
        with (
            patch.object(HELPER, "local_ops_module", side_effect=[preflight, DOCTOR]),
            patch.object(DOCTOR, "load_manifest", return_value=manifest),
            patch.object(DOCTOR, "preflight_api"),
            patch.object(
                DOCTOR,
                "run_cli_version_probe",
                return_value={"status": "pass", "body_bytes_persisted": 0},
            ),
            patch.object(
                DOCTOR,
                "run_mcp_probe",
                side_effect=DOCTOR.CompatibilityError("mcp"),
            ),
            patch.object(
                DOCTOR,
                "run_cli_probe",
                side_effect=AssertionError("agent-safe prerequisite must not map"),
            ) as cli_map,
            patch.object(sys, "argv", ["firecrawl_request.py", *argv]),
            patch.object(HELPER, "request_json") as request_json,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaisesRegex(SystemExit, "agent_safe_prerequisite_failed"),
        ):
            HELPER.main()
        cli_map.assert_not_called()
        request_json.assert_not_called()
        self.assertEqual(list(self.evidence_dir.glob("*-receipt.json")), [])

    def test_idle_recheck_then_scrape_projects_only_closed_metrics(self) -> None:
        secret = "source-secret-marker"
        injected_status = "server-status-secret-marker"
        response = json.dumps(
            {
                "id": secret,
                "status": injected_status,
                "data": {"markdown": secret, "links": [secret]},
            }
        ).encode()
        argv = [*self.common("scrape", prerequisites=True), "https://example.com/"]
        stdout = CapturedStdout()
        with (
            patch.object(sys, "argv", ["firecrawl_request.py", *argv]),
            patch.object(
                HELPER,
                "request_json",
                side_effect=[
                    *idle_gate(),
                    (200, response),
                ],
            ) as request_json,
            patch.object(HELPER.sys, "stdout", stdout),
        ):
            self.assertEqual(HELPER.main(), 0)

        self.assertEqual(request_json.call_count, 3)
        self.assertEqual(
            request_json.call_args_list[2].args[1:3], ("POST", "/v2/scrape")
        )
        self.assertTrue(
            all(call.kwargs["agent_safe"] for call in request_json.call_args_list)
        )
        metrics, manifest, rendered = self.receipts()
        self.assertEqual(metrics["outcome"], "success")
        self.assertEqual(metrics["markdown_chars"], len(secret))
        self.assertNotIn("id", metrics)
        self.assertNotIn("status", metrics)
        self.assertEqual(
            manifest["input"],
            {
                "kind": "public_example_fixture",
                "retained": False,
                "content_sha256": None,
            },
        )
        self.assertEqual(manifest["preflight"]["status"], "passed")
        self.assertEqual(manifest["compatibility_doctor"]["status"], "passed")
        HELPER.validate_agent_safe_receipt(manifest, metrics)
        self.assertNotIn(self.receipt_dir, rendered)
        self.assertNotIn("example.com", rendered)
        self.assertNotIn(secret, rendered + stdout.buffer.getvalue().decode())
        self.assertNotIn(injected_status, rendered + stdout.buffer.getvalue().decode())

    def test_nonzero_or_malformed_direct_idle_check_blocks_post_with_redacted_receipt(
        self,
    ) -> None:
        argv = [
            *self.common("map", prerequisites=True),
            "https://example.com/",
            "--limit",
            "1",
            "--quiet",
        ]
        for queue_response, active_response in (
            (b'{"success":true,"jobsInQueue":1}', b'{"success":true,"crawls":[]}'),
            (b"not-json", b'{"success":true,"crawls":[]}'),
            (b'{"success":true,"jobsInQueue":0}', b'{"success":true,"crawls":[{}]}'),
            (b'{"success":true,"jobsInQueue":0}', b"not-json"),
            (b'{"success":false,"jobsInQueue":0}', b'{"success":true,"crawls":[]}'),
            (b'{"success":true,"jobsInQueue":0}', b'{"success":false,"crawls":[]}'),
        ):
            with (
                self.subTest(
                    queue_response=queue_response, active_response=active_response
                ),
                patch.object(sys, "argv", ["firecrawl_request.py", *argv]),
                patch.object(
                    HELPER,
                    "request_json",
                    side_effect=[(200, queue_response), (200, active_response)],
                ) as request_json,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaisesRegex(SystemExit, "agent_safe_queue_not_idle"),
            ):
                HELPER.main()
            self.assertEqual(request_json.call_count, 2)
            metrics, _manifest, rendered = self.receipts()
            self.assertEqual(metrics["outcome"], "queue_not_idle")
            self.assertNotIn("jobsInQueue", rendered)
            self.clear_receipts()

    def test_safe_map_and_parse_use_the_same_preflight_and_transport_contract(
        self,
    ) -> None:
        map_argv = [
            *self.common("map", prerequisites=True),
            "https://example.com/",
            "--limit",
            "1",
            "--quiet",
        ]
        with (
            patch.object(sys, "argv", ["firecrawl_request.py", *map_argv]),
            patch.object(
                HELPER,
                "request_json",
                side_effect=[
                    *idle_gate(),
                    (200, b'{"links":["server-secret"]}'),
                ],
            ) as request_json,
        ):
            self.assertEqual(HELPER.main(), 0)
        self.assertEqual(request_json.call_args_list[2].args[1:3], ("POST", "/v2/map"))
        self.assertTrue(request_json.call_args_list[2].kwargs["agent_safe"])
        metrics, _manifest, rendered = self.receipts()
        self.assertEqual(metrics["links_count"], 1)
        self.assertNotIn("server-secret", rendered)
        self.clear_receipts()

        parse_argv = [
            *self.common("parse", prerequisites=True),
            HELPER.AGENT_SAFE_PARSE_FILE.as_posix(),
            "--pdf-mode",
            "fast",
            "--max-pages",
            "1",
            "--quiet",
        ]
        with (
            patch.object(sys, "argv", ["firecrawl_request.py", *parse_argv]),
            patch.object(
                HELPER,
                "request_json",
                side_effect=idle_gate(),
            ),
            patch.object(
                HELPER,
                "request_multipart",
                return_value=(200, b'{"data":{"markdown":"server-secret"}}'),
            ) as request_multipart,
        ):
            self.assertEqual(HELPER.main(), 0)
        self.assertTrue(request_multipart.call_args.kwargs["agent_safe"])
        options = json.loads(request_multipart.call_args.args[2]["options"])
        self.assertEqual(
            options,
            {
                "formats": ["markdown"],
                "parsers": [{"type": "pdf", "mode": "fast", "maxPages": 1}],
            },
        )
        metrics, _manifest, rendered = self.receipts()
        self.assertEqual(metrics["markdown_chars"], len("server-secret"))
        self.assertNotIn("server-secret", rendered)

    def test_invalid_crawl_id_emits_one_unknown_receipt_without_poll_or_id_retention(
        self,
    ) -> None:
        unsafe_id = "crawl/secret-id-marker"
        argv = self.crawl_argv()
        with (
            patch.object(sys, "argv", ["firecrawl_request.py", *argv]),
            patch.object(
                HELPER,
                "request_json",
                side_effect=[
                    *idle_gate(),
                    (202, json.dumps({"id": unsafe_id}).encode()),
                ],
            ) as request_json,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaisesRegex(SystemExit, "agent_safe_unknown_submit"),
        ):
            HELPER.main()
        self.assertEqual(request_json.call_count, 3)
        metrics, manifest, rendered = self.receipts()
        self.assertEqual(metrics["outcome"], "unknown_submit")
        self.assertEqual(manifest["outcome"]["terminal_disposition"], "unknown")
        self.assertNotIn(unsafe_id, rendered)

    def crawl_argv(self) -> list[str]:
        return [
            *self.common("crawl", prerequisites=True),
            "https://example.com/",
            "--limit",
            "1",
            "--max-concurrency",
            "1",
            "--include-paths",
            "/",
            "--scrape-formats",
            "markdown",
            "--wait",
            "--poll-timeout",
            "30",
            "--poll-interval",
            "1",
            "--quiet",
        ]

    def test_crawl_cancel_malformed_and_transport_diagnostics_are_finite_and_redacted(
        self,
    ) -> None:
        job_id = "crawl_safe_123"
        cases: dict[str, list[object]] = {
            "crawl_cancelled": [
                *idle_gate(),
                (202, json.dumps({"id": job_id}).encode()),
                (200, b'{"status":"cancelled","message":"server-secret"}'),
            ],
            "invalid_response": [
                *idle_gate(),
                (202, json.dumps({"id": job_id}).encode()),
                (200, b'{"status":"server-secret"}'),
            ],
            "transport_unreachable": [SystemExit("agent_safe_transport_error")],
        }
        for expected, side_effect in cases.items():
            stderr = io.StringIO()
            with (
                self.subTest(expected=expected),
                patch.object(sys, "argv", ["firecrawl_request.py", *self.crawl_argv()]),
                patch.object(HELPER, "request_json", side_effect=side_effect),
                contextlib.redirect_stderr(stderr),
                self.assertRaises(SystemExit),
            ):
                HELPER.main()
            metrics, _manifest, rendered = self.receipts()
            self.assertEqual(metrics["outcome"], expected)
            self.assertNotIn(job_id, rendered + stderr.getvalue())
            self.assertNotIn("server-secret", rendered + stderr.getvalue())
            self.clear_receipts()

    def test_crawl_timeout_is_redacted_and_receipted_once(self) -> None:
        with (
            patch.object(sys, "argv", ["firecrawl_request.py", *self.crawl_argv()]),
            patch.object(
                HELPER,
                "request_json",
                side_effect=[
                    *idle_gate(),
                    (202, b'{"id":"crawl_safe_123"}'),
                    (200, b'{"status":"queued","message":"server-secret"}'),
                ],
            ),
            patch.object(HELPER.time, "monotonic", side_effect=[0, 0, 0, 31, 31]),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaisesRegex(SystemExit, "agent_safe_poll_timeout"),
        ):
            HELPER.main()
        metrics, _manifest, rendered = self.receipts()
        self.assertEqual(metrics["outcome"], "poll_timeout")
        self.assertNotIn("crawl_safe_123", rendered)
        self.assertNotIn("server-secret", rendered)

    def test_every_safe_3xx_is_rejected_without_a_followup_request(self) -> None:
        scenarios = (
            (
                "scrape",
                [*self.common("scrape"), "https://example.com/", "--quiet"],
                [*idle_gate(), (302, b'{"id":"crawl_safe_123"}')],
                None,
                3,
            ),
            (
                "map",
                [
                    *self.common("map"),
                    "https://example.com/",
                    "--limit",
                    "1",
                    "--quiet",
                ],
                [*idle_gate(), (302, b'{"id":"crawl_safe_123"}')],
                None,
                3,
            ),
            (
                "parse",
                [
                    *self.common("parse"),
                    HELPER.AGENT_SAFE_PARSE_FILE.as_posix(),
                    "--pdf-mode",
                    "fast",
                    "--max-pages",
                    "1",
                    "--quiet",
                ],
                idle_gate(),
                (302, b'{"id":"crawl_safe_123"}'),
                2,
            ),
            (
                "crawl_submit",
                self.crawl_argv(),
                [*idle_gate(), (302, b'{"id":"crawl_safe_123"}')],
                None,
                3,
            ),
            (
                "crawl_poll",
                self.crawl_argv(),
                [
                    *idle_gate(),
                    (202, b'{"id":"crawl_safe_123"}'),
                    (302, b'{"status":"queued"}'),
                ],
                None,
                4,
            ),
            (
                "health",
                [*self.common("health"), "--quiet"],
                [(302, b'{"message":"redirect"}')],
                None,
                1,
            ),
        )
        for (
            name,
            argv,
            json_responses,
            multipart_response,
            expected_json_calls,
        ) in scenarios:
            with self.subTest(name=name):
                kwargs: dict[str, object] = {
                    "side_effect": json_responses,
                }
                with (
                    patch.object(sys, "argv", ["firecrawl_request.py", *argv]),
                    patch.object(HELPER, "request_json", **kwargs) as request_json,
                    patch.object(
                        HELPER, "request_multipart", return_value=multipart_response
                    ) as request_multipart,
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    HELPER.main()
                self.assertEqual(request_json.call_count, expected_json_calls)
                if multipart_response is None:
                    request_multipart.assert_not_called()
                else:
                    request_multipart.assert_called_once()
                metrics, manifest, _rendered = self.receipts()
                self.assertEqual(metrics["outcome"], "http_rejected")
                self.assertEqual(manifest["outcome"]["terminal_disposition"], "reject")
                self.assertEqual(manifest["outcome"]["reason_code"], "http_rejected")
                self.clear_receipts()

    def test_partial_manifest_write_leaves_no_terminal_receipt(self) -> None:
        argv = [
            *self.common("map"),
            "https://example.com/",
            "--limit",
            "1",
            "--quiet",
        ]
        real_atomic_write = HELPER.atomic_write

        def fail_manifest(path: Path, content: bytes) -> None:
            if path.name.endswith("-receipt.json"):
                raise OSError("interrupted")
            real_atomic_write(path, content)

        with (
            patch.object(sys, "argv", ["firecrawl_request.py", *argv]),
            patch.object(
                HELPER,
                "request_json",
                side_effect=[*idle_gate(), (200, b'{"links":[]}')],
            ),
            patch.object(HELPER, "atomic_write", side_effect=fail_manifest) as atomic,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaisesRegex(SystemExit, "agent_safe_receipt_write_failed"),
        ):
            HELPER.main()
        self.assertEqual(atomic.call_count, 2)
        self.assertEqual(len(list(self.evidence_dir.glob("*-metrics.json"))), 1)
        self.assertEqual(list(self.evidence_dir.glob("*-receipt.json")), [])
        self.assertEqual(list(self.evidence_dir.glob(".*.tmp")), [])

    def test_receipt_schema_rejects_unknown_and_nonpilot_bound_values(self) -> None:
        argv = [
            *self.common("map"),
            "https://example.com/",
            "--limit",
            "1",
            "--quiet",
        ]
        with (
            patch.object(sys, "argv", ["firecrawl_request.py", *argv]),
            patch.object(
                HELPER,
                "request_json",
                side_effect=[*idle_gate(), (200, b'{"links":[]}')],
            ),
        ):
            self.assertEqual(HELPER.main(), 0)
        metrics, manifest, _rendered = self.receipts()
        for mutation in (
            lambda value: value["outcome"].update({"server_message": "not-retained"}),
            lambda value: value["bounds"].update({"limit": 2}),
            lambda value: value.update({"unexpected": True}),
        ):
            invalid = json.loads(json.dumps(manifest))
            mutation(invalid)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                HELPER.validate_agent_safe_receipt(invalid, metrics)

    def test_receipt_validation_binds_metrics_and_keeps_historical_receipts_valid(
        self,
    ) -> None:
        argv = [
            *self.common("map"),
            "https://example.com/",
            "--limit",
            "1",
            "--quiet",
        ]
        with (
            patch.object(sys, "argv", ["firecrawl_request.py", *argv]),
            patch.object(
                HELPER,
                "request_json",
                side_effect=[*idle_gate(), (200, b'{"links":[]}')],
            ),
        ):
            self.assertEqual(HELPER.main(), 0)
        metrics, manifest, _rendered = self.receipts()
        historical = json.loads(json.dumps(manifest))
        old_timestamp = "2020-01-01T00:00:00Z"
        historical["observed_at"] = old_timestamp
        historical["preflight"]["observed_at"] = old_timestamp
        historical["compatibility_doctor"]["observed_at"] = old_timestamp
        HELPER.validate_agent_safe_receipt(historical, metrics)

        tampered_metrics = json.loads(json.dumps(metrics))
        tampered_metrics["http_status"] = 201
        with self.assertRaisesRegex(ValueError, "metrics provenance"):
            HELPER.validate_agent_safe_receipt(manifest, tampered_metrics)

        bad_digest = json.loads(json.dumps(manifest))
        bad_digest["outcome"]["metrics_artifact"]["sha256"] = "sha256:" + "d" * 64
        with self.assertRaisesRegex(ValueError, "metrics provenance"):
            HELPER.validate_agent_safe_receipt(bad_digest, metrics)

        future = json.loads(json.dumps(manifest))
        future["observed_at"] = "2099-01-01T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "nonfuture"):
            HELPER.validate_agent_safe_receipt(future, metrics)

    def test_receipt_remains_terminal_when_prerequisites_age_after_execution_gate(
        self,
    ) -> None:
        base = datetime.now(UTC).replace(microsecond=0)
        observed_at = base.isoformat().replace("+00:00", "Z")

        class Clock:
            calls = 0

            @classmethod
            def now(cls, _timezone: object) -> datetime:
                cls.calls += 1
                return base if cls.calls <= 2 else base + timedelta(seconds=46)

            @staticmethod
            def fromisoformat(value: str) -> datetime:
                return datetime.fromisoformat(value)

        prerequisites = {
            "preflight": {
                "evidence_digest": "sha256:" + "a" * 64,
                "observed_at": observed_at,
            },
            "compatibility_doctor": {
                "evidence_digest": "sha256:" + "b" * 64,
                "observed_at": observed_at,
            },
        }
        argv = [
            *self.common("map"),
            "https://example.com/",
            "--limit",
            "1",
            "--quiet",
        ]
        with (
            patch.object(sys, "argv", ["firecrawl_request.py", *argv]),
            patch.object(HELPER, "datetime", Clock),
            patch.object(
                HELPER, "run_agent_safe_prerequisites", return_value=prerequisites
            ),
            patch.object(
                HELPER,
                "request_json",
                side_effect=[*idle_gate(), (200, b'{"links":[]}')],
            ),
        ):
            self.assertEqual(HELPER.main(), 0)
            metrics, manifest, _rendered = self.receipts()
            self.assertEqual(manifest["outcome"]["terminal_disposition"], "accept")
            HELPER.validate_agent_safe_receipt(manifest, metrics)


if __name__ == "__main__":
    unittest.main()
