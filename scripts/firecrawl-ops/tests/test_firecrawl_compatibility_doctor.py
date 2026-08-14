"""Fixture-only tests for the local Firecrawl tooling compatibility doctor."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

OPS_DIR = Path(__file__).resolve().parents[1]
DOCTOR_PATH = OPS_DIR / "firecrawl_compatibility_doctor.py"
SPEC = importlib.util.spec_from_file_location(
    "firecrawl_compatibility_doctor", DOCTOR_PATH
)
assert SPEC is not None and SPEC.loader is not None
doctor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = doctor
SPEC.loader.exec_module(doctor)


class FirecrawlCompatibilityDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = doctor.load_manifest()

    def test_static_manifest_has_exact_candidate_pins_and_human_only_probe(
        self,
    ) -> None:
        self.assertEqual(
            doctor.package_spec(self.manifest, "cli"), "firecrawl-cli@1.20.0"
        )
        self.assertEqual(
            doctor.package_spec(self.manifest, "mcp"), "firecrawl-mcp@3.24.0"
        )
        self.assertEqual(
            doctor.validate_package_override(
                self.manifest, "cli", "firecrawl-cli@1.18.0"
            ),
            "firecrawl-cli@1.18.0",
        )
        with self.assertRaisesRegex(doctor.CompatibilityError, "package_spec"):
            doctor.validate_package_override(
                self.manifest, "cli", "firecrawl-cli@latest"
            )

    def test_static_invocation_emits_only_body_free_json(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = doctor.main([])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "pass")
        self.assertEqual(payload["checks"]["cli"]["status"], "not_run")
        self.assertEqual(payload["body_bytes_persisted"], 0)
        self.assertRegex(
            payload["observed_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
        )
        self.assertNotIn("markdown", stdout.getvalue())

    def test_loopback_validation_rejects_remote_and_credentials(self) -> None:
        self.assertEqual(
            doctor.validate_loopback_api_url("http://localhost:3002"),
            "http://localhost:3002",
        )
        for value in (
            "https://localhost:3002",
            "http://example.com:3002",
            "http://token@localhost:3002",
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(doctor.CompatibilityError, "api"),
            ):
                doctor.validate_loopback_api_url(value)

    def test_preflight_rejects_redirect_without_following_it(self) -> None:
        class RedirectingOpener:
            def open(self, *_: object, **__: object) -> None:
                raise doctor.CompatibilityError("api")

        handlers: list[object] = []
        with (
            mock.patch.object(
                doctor,
                "build_opener",
                side_effect=lambda *provided_handlers: (
                    handlers.extend(provided_handlers) or RedirectingOpener()
                ),
            ),
            self.assertRaisesRegex(doctor.CompatibilityError, "api"),
        ):
            doctor.preflight_api("http://localhost:3002", 1)

        self.assertEqual(len(handlers), 2)
        proxy_handlers = [
            handler for handler in handlers if isinstance(handler, doctor.ProxyHandler)
        ]
        self.assertEqual(len(proxy_handlers), 1)
        self.assertEqual(proxy_handlers[0].proxies, {})
        redirect_handler = next(
            handler
            for handler in handlers
            if isinstance(handler, doctor._RejectRedirects)
        )
        with self.assertRaisesRegex(doctor.CompatibilityError, "api"):
            redirect_handler.redirect_request(None, None, None, None, None, None)

    def test_preflight_bypasses_ambient_proxy_and_connects_directly(self) -> None:
        class TargetHandler(BaseHTTPRequestHandler):
            requests = 0

            def do_GET(self) -> None:
                type(self).requests += 1
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_: object) -> None:
                return

        class ProxyHandler(BaseHTTPRequestHandler):
            requests = 0

            def do_GET(self) -> None:
                type(self).requests += 1
                self.send_response(502)
                self.end_headers()

            def log_message(self, *_: object) -> None:
                return

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        target_thread.start()
        proxy_thread.start()
        try:
            proxy_url = f"http://127.0.0.1:{proxy.server_port}"
            with mock.patch.dict(
                os.environ,
                {"http_proxy": proxy_url, "HTTP_PROXY": proxy_url},
                clear=True,
            ):
                doctor.preflight_api(f"http://127.0.0.1:{target.server_port}", 2)
        finally:
            target.shutdown()
            proxy.shutdown()
            target.server_close()
            proxy.server_close()
            target_thread.join(timeout=2)
            proxy_thread.join(timeout=2)

        self.assertEqual(TargetHandler.requests, 1)
        self.assertEqual(ProxyHandler.requests, 0)

    def test_command_environment_bypasses_proxy_for_loopback_children(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://proxy.invalid:8080",
                "https_proxy": "http://proxy.invalid:8443",
                "NO_PROXY": "internal.example",
                "no_proxy": "lower.example",
            },
            clear=True,
        ):
            environment = doctor._command_environment("http://localhost:3002")

        self.assertEqual(
            environment["NO_PROXY"],
            "internal.example,lower.example,localhost,127.0.0.1,::1",
        )
        self.assertEqual(environment["no_proxy"], environment["NO_PROXY"])
        self.assertEqual(environment["HTTP_PROXY"], "http://proxy.invalid:8080")
        self.assertEqual(environment["https_proxy"], "http://proxy.invalid:8443")

    def test_doctor_reports_api_preflight_failure_without_running_tools(self) -> None:
        with (
            mock.patch.object(
                doctor, "preflight_api", side_effect=doctor.CompatibilityError("api")
            ),
            mock.patch.object(
                doctor,
                "run_cli_probe",
            ) as cli_probe,
            mock.patch.object(doctor, "run_mcp_probe") as mcp_probe,
        ):
            result = doctor.doctor_result(self.manifest, mode="normal", run=True)

        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["failure_code"], "api")
        self.assertEqual(result["body_bytes_persisted"], 0)
        cli_probe.assert_not_called()
        mcp_probe.assert_not_called()

    def test_run_uses_an_explicit_checked_loopback_origin_when_provided(self) -> None:
        with (
            mock.patch.object(doctor, "preflight_api") as preflight,
            mock.patch.object(
                doctor, "run_cli_probe", return_value={"status": "pass"}
            ) as cli_probe,
            mock.patch.object(
                doctor, "run_mcp_probe", return_value={"status": "pass"}
            ) as mcp_probe,
        ):
            result = doctor.doctor_result(
                self.manifest,
                mode="normal",
                run=True,
                api_url="http://127.0.0.1:3002",
            )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(
            result["checks"]["api"], {"status": "pass", "url": "http://127.0.0.1:3002"}
        )
        preflight.assert_called_once()
        self.assertEqual(preflight.call_args.args[0], "http://127.0.0.1:3002")
        self.assertEqual(cli_probe.call_args.args[2], "http://127.0.0.1:3002")
        self.assertEqual(mcp_probe.call_args.args[2], "http://127.0.0.1:3002")

    def test_agent_safe_compatibility_checks_version_and_mcp_without_map_probe(
        self,
    ) -> None:
        with (
            mock.patch.object(doctor, "preflight_api") as preflight,
            mock.patch.object(
                doctor,
                "run_cli_version_probe",
                return_value={"status": "pass", "body_bytes_persisted": 0},
            ) as version_probe,
            mock.patch.object(
                doctor,
                "run_mcp_probe",
                return_value={"status": "pass", "body_bytes_persisted": 0},
            ) as mcp_probe,
            mock.patch.object(
                doctor,
                "run_cli_probe",
                side_effect=AssertionError("agent-safe path must not run map"),
            ) as map_probe,
        ):
            result = doctor.agent_safe_result(
                self.manifest, api_url="http://127.0.0.1:3002"
            )
        self.assertEqual(result["kind"], "firecrawl-agent-safe-compatibility")
        self.assertEqual(result["status"], "pass")
        preflight.assert_called_once()
        version_probe.assert_called_once()
        mcp_probe.assert_called_once()
        map_probe.assert_not_called()

    def test_run_failure_returns_nonzero_after_body_free_result(self) -> None:
        stdout = io.StringIO()
        with (
            mock.patch.object(
                doctor, "preflight_api", side_effect=doctor.CompatibilityError("api")
            ),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = doctor.main(["--run"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["failure_code"], "api")
        self.assertEqual(payload["body_bytes_persisted"], 0)

    def test_cli_probe_accepts_body_free_map_payload_with_fixture_runner(self) -> None:
        calls: list[list[str]] = []

        def runner(
            command: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:
            calls.append(command)
            if command[-1] == "--version":
                return subprocess.CompletedProcess(
                    command, 0, b"firecrawl-cli 1.20.0\n", b""
                )
            return subprocess.CompletedProcess(
                command, 0, b'{"success":true,"data":{"links":[]}}', b""
            )

        result = doctor.run_cli_probe(
            self.manifest,
            "normal",
            "http://localhost:3002",
            time.monotonic() + 5,
            runner=runner,
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["resolved_version"], "1.20.0")
        self.assertEqual(result["body_bytes_persisted"], 0)

    def test_cli_probe_rejects_invalid_contract_without_echoing_fixture_body(
        self,
    ) -> None:
        def runner(
            command: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:
            if command[-1] == "--version":
                return subprocess.CompletedProcess(
                    command, 0, b"firecrawl-cli 1.20.0\n", b""
                )
            return subprocess.CompletedProcess(
                command,
                0,
                b'{"success":true,"data":{"links":"invalid"},"markdown":"secret"}',
                b"",
            )

        with self.assertRaisesRegex(doctor.CompatibilityError, "cli_contract"):
            doctor.run_cli_probe(
                self.manifest,
                "normal",
                "http://localhost:3002",
                time.monotonic() + 5,
                runner=runner,
            )

    def test_cli_probe_reports_package_resolution_failure(self) -> None:
        def runner(
            command: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(command, 1, b"", b"unavailable")

        with self.assertRaisesRegex(doctor.CompatibilityError, "package_resolution"):
            doctor.run_cli_probe(
                self.manifest,
                "normal",
                "http://localhost:3002",
                time.monotonic() + 5,
                runner=runner,
            )

    def test_cli_probe_fails_when_normal_version_differs_from_pin(self) -> None:
        def runner(
            command: list[str], **_: object
        ) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(
                command, 0, b"firecrawl-cli 1.20.1\n", b""
            )

        with self.assertRaisesRegex(doctor.CompatibilityError, "package_resolution"):
            doctor.run_cli_probe(
                self.manifest,
                "normal",
                "http://localhost:3002",
                time.monotonic() + 5,
                runner=runner,
            )

    def test_upgrade_probe_uses_direct_latest_commands_only_inside_doctor(self) -> None:
        cli_command = doctor._cli_command(
            self.manifest,
            "upgrade_probe",
            "http://localhost:3002",
            ["--version"],
        )
        self.assertEqual(cli_command[:3], ["npx", "-y", "firecrawl-cli@latest"])
        self.assertEqual(cli_command[3:5], ["--api-url", "http://localhost:3002"])
        self.assertEqual(
            doctor._mcp_command(self.manifest, "upgrade_probe"),
            ["npx", "-y", "firecrawl-mcp@latest"],
        )

    def test_upgrade_probe_requires_run_and_explicit_acknowledgement(self) -> None:
        for arguments, failure_code in (
            (["--upgrade-probe"], "upgrade_probe_requires_run"),
            (["--run", "--upgrade-probe"], "upgrade_probe_acknowledgement"),
        ):
            with self.subTest(arguments=arguments):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = doctor.main(arguments)

                self.assertEqual(exit_code, 2)
                self.assertEqual(
                    json.loads(stdout.getvalue())["failure_code"], failure_code
                )

    def test_jsonl_transcript_requires_matching_responses_and_inventory(self) -> None:
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "capabilities": {},
                    "serverInfo": {"name": "fixture", "version": "3.24.0"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "tools": [
                        {"name": "firecrawl_scrape"},
                        {"name": "firecrawl_map"},
                    ]
                },
            },
        ]

        result = doctor.validate_mcp_transcript(messages, self.manifest, "normal")

        self.assertEqual(result, {"resolved_version": "3.24.0", "tool_count": 2})
        with self.assertRaisesRegex(doctor.CompatibilityError, "protocol"):
            doctor.parse_jsonl_message(b"not json\n")
        with self.assertRaisesRegex(doctor.CompatibilityError, "inventory"):
            doctor.validate_mcp_transcript(
                messages[:1] + [{"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}],
                self.manifest,
                "normal",
            )
        with self.assertRaisesRegex(doctor.CompatibilityError, "protocol"):
            doctor.validate_mcp_transcript(
                messages[:1] + [{"jsonrpc": "2.0", "id": 3, "result": {"tools": []}}],
                self.manifest,
                "normal",
            )

    def test_mcp_transcript_fails_when_normal_version_differs_from_pin(self) -> None:
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "capabilities": {},
                    "serverInfo": {"name": "fixture", "version": "3.24.1"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "tools": [
                        {"name": "firecrawl_scrape"},
                        {"name": "firecrawl_map"},
                    ]
                },
            },
        ]
        with self.assertRaisesRegex(doctor.CompatibilityError, "package_resolution"):
            doctor.validate_mcp_transcript(messages, self.manifest, "normal")
        self.assertEqual(
            doctor.validate_mcp_transcript(messages, self.manifest, "upgrade_probe"),
            {"resolved_version": "3.24.1", "tool_count": 2},
        )

    def test_cleanup_does_not_wait_past_absolute_deadline(self) -> None:
        class Clock:
            now = 0.0

            def monotonic(self) -> float:
                return self.now

        class Process:
            stdin = None
            stdout = None
            stderr = None

            def __init__(self, clock: Clock) -> None:
                self.clock = clock
                self.terminated = False
                self.killed = False
                self.wait_timeouts: list[float] = []

            def poll(self) -> None:
                return None

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.killed = True

            def wait(self, timeout: float) -> None:
                self.wait_timeouts.append(timeout)
                self.clock.now += timeout
                raise subprocess.TimeoutExpired("fixture", timeout)

        clock = Clock()
        process = Process(clock)
        with mock.patch.object(doctor.time, "monotonic", clock.monotonic):
            doctor._terminate_process(process, deadline=45)

        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(process.wait_timeouts, [45])
        self.assertEqual(clock.now, 45)


if __name__ == "__main__":
    unittest.main()
