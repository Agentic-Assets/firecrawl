"""Ingress diagnostics against private Docker/curl stubs, never a live runtime."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OPS = ROOT / "scripts/firecrawl-ops"
API_ID = "a" * 64
SIDECAR_ID = "b" * 64


def publication(host: str, port: str = "3002") -> dict:
    return {
        "network_mode": "firecrawl_backend",
        "ports": {"3002/tcp": [{"HostIp": host, "HostPort": port}]},
    }


class FirecrawlIngressTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.log = self.tmp / "docker.jsonl"
        docker = bin_dir / "docker"
        docker.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "with open(os.environ['DOCKER_LOG'], 'a') as log:\n"
            "    log.write(json.dumps(args) + '\\n')\n"
            "if args == ['compose', 'ps', '--quiet', 'api']:\n"
            "    print(os.environ.get('API_IDS', 'a' * 64))\n"
            "elif args == ['compose', 'ps', '--quiet']:\n"
            "    print('a' * 64 + '\\n' + 'b' * 64)\n"
            "elif args[:2] == ['inspect', '--format']:\n"
            "    if os.environ.get('INSPECT_FAIL'):\n"
            "        print('private daemon error must not be logged', file=sys.stderr)\n"
            "        sys.exit(1)\n"
            "    if 'b' * 64 in args:\n"
            "        print('unrelated sidecar publication must not be inspected')\n"
            "    else:\n"
            "        print(os.environ['INSPECTION'])\n"
            "elif args[:2] == ['image', 'inspect']:\n"
            "    print('synthetic-image')\n"
            "else:\n"
            "    print('synthetic-api-up')\n",
            encoding="utf-8",
        )
        docker.chmod(0o755)
        curl = bin_dir / "curl"
        curl.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "if any('/v2/scrape' in arg for arg in sys.argv):\n"
            "    print(json.dumps({'success': True, 'data': {'markdown': 'ok'}}))\n"
            "else:\n"
            "    print(json.dumps({'message': 'Firecrawl API'}))\n",
            encoding="utf-8",
        )
        curl.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "FC_DIR": str(ROOT),
            "DOCKER_LOG": str(self.log),
            "INSPECTION": json.dumps(publication("127.0.0.1")),
            "FIRECRAWL_HEALTHCHECK_RETRIES": "1",
        }
        for name in (
            "API_URL",
            "FIRECRAWL_API_URL",
            "FIRECRAWL_HEALTHCHECK_EVIDENCE_DIR",
        ):
            self.env.pop(name, None)

    def run_diagnostic(self, api_url: str = "http://localhost:3002", **overrides: str):
        return subprocess.run(
            [
                sys.executable,
                str(OPS / "firecrawl_ingress_check.py"),
                "--api-url",
                api_url,
            ],
            env={**self.env, **overrides},
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

    def test_loopback_ipv4_ipv6_and_mapped_addresses_have_no_exposure_warning(self):
        for host in ("127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1"):
            with self.subTest(host=host):
                result = self.run_diagnostic(INSPECTION=json.dumps(publication(host)))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("loopback-only", result.stdout)
                self.assertNotIn("WARNING", result.stdout)

    def test_wildcard_lan_ipv4_ipv6_and_unspecified_binding_warn_even_for_localhost_url(
        self,
    ):
        for host in ("0.0.0.0", "192.0.2.20", "::", "2001:db8::20", ""):
            with self.subTest(host=host):
                result = self.run_diagnostic(
                    INSPECTION=json.dumps(publication(host, "43002"))
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(
                    "WARNING: Docker API service publishes non-loopback", result.stdout
                )
                self.assertIn(":43002 -> 3002/tcp", result.stdout)
                self.assertNotIn("loopback-only", result.stdout)

    def test_only_api_service_is_inspected_and_unrelated_sidecar_does_not_warn(self):
        result = self.run_diagnostic()
        self.assertNotIn("WARNING", result.stdout)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(calls[0], ["compose", "ps", "--quiet", "api"])
        self.assertEqual(calls[1][-1], API_ID)
        self.assertNotIn(SIDECAR_ID, calls[1])
        self.assertIn(".NetworkSettings.Ports", calls[1][2])
        self.assertNotIn(".Config.Env", calls[1][2])

    def test_dual_stack_and_partial_inspection_do_not_hide_known_exposure(self):
        record = publication("0.0.0.0")
        record["ports"]["3002/tcp"].extend(
            [
                {"HostIp": "::", "HostPort": "3002"},
                {"HostIp": "malformed", "HostPort": "3002"},
            ]
        )
        result = self.run_diagnostic(INSPECTION=json.dumps(record))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("non-loopback 0.0.0.0:3002", result.stdout)
        self.assertIn("non-loopback [::]:3002", result.stdout)
        self.assertIn("ingress exposure is unknown", result.stdout)

    def test_remote_endpoint_warning_redacts_credentials_path_and_query(self):
        result = self.run_diagnostic(
            "https://private-user:private-password@remote.example/token?key=private-query"
        )
        self.assertIn("non-loopback host 'remote.example'", result.stdout)
        for private in ("private-user", "private-password", "/token", "private-query"):
            self.assertNotIn(private, result.stdout + result.stderr)

    def test_ipv6_loopback_endpoint_is_local(self):
        result = self.run_diagnostic("http://[::1]:3002")
        self.assertNotIn("WARNING", result.stdout)

    def test_invalid_endpoint_is_unknown(self):
        for endpoint in ("not a URL", "http://[invalid", "http://localhost:bad"):
            with self.subTest(endpoint=endpoint):
                result = self.run_diagnostic(endpoint)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("endpoint address is invalid", result.stdout)

    def test_missing_failed_or_malformed_docker_inspection_is_unknown(self):
        cases = (
            {"API_IDS": ""},
            {"API_IDS": "unexpected-not-a-container-id"},
            {"INSPECT_FAIL": "1"},
            {"INSPECTION": "malformed daemon response"},
            {"INSPECTION": "[]"},
            {"INSPECTION": json.dumps({"network_mode": "bridge"})},
            {"INSPECTION": json.dumps(publication("not-an-address"))},
            {"INSPECTION": json.dumps(publication("127.0.0.1", ""))},
            {"INSPECTION": json.dumps(publication("127.0.0.1", "1" * 5000))},
            {"INSPECTION": json.dumps({"network_mode": "host", "ports": None})},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                result = self.run_diagnostic(**overrides)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("unknown", result.stdout)
                self.assertNotIn(
                    "Ingress: inspected API publications are loopback-only",
                    result.stdout,
                )
                self.assertNotIn("private daemon error", result.stdout + result.stderr)

    def test_unpublished_container_port_is_not_reported_as_lan_exposure(self):
        result = self.run_diagnostic(
            INSPECTION=json.dumps(
                {"network_mode": "bridge", "ports": {"3002/tcp": None}}
            )
        )
        self.assertIn("no API container ports are published", result.stdout)
        self.assertNotIn("WARNING", result.stdout)

    def test_healthcheck_still_passes_four_stages_and_records_exposure_warning(self):
        for host in ("127.0.0.1", "0.0.0.0", "unknown"):
            with self.subTest(host=host):
                evidence = self.tmp / host
                result = subprocess.run(
                    [
                        "bash",
                        str(OPS / "firecrawl_healthcheck.sh"),
                        "--evidence-dir",
                        str(evidence),
                    ],
                    env={
                        **self.env,
                        "INSPECTION": "malformed"
                        if host == "unknown"
                        else json.dumps(publication(host)),
                    },
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("[4/4] done", result.stdout)
                payload = json.loads(next(evidence.glob("*.json")).read_text())
                self.assertEqual(payload["status"], "pass")
                self.assertEqual(payload["errors"], [])
                self.assertEqual(
                    payload["scrape_summary"], {"success": True, "markdown_len": 2}
                )
                if host == "0.0.0.0":
                    self.assertIn(
                        "non-loopback 0.0.0.0:3002", payload["ingress_diagnostic"]
                    )
                elif host == "unknown":
                    self.assertIn(
                        "ingress exposure is unknown", payload["ingress_diagnostic"]
                    )
                else:
                    self.assertNotIn("WARNING", payload["ingress_diagnostic"])


if __name__ == "__main__":
    unittest.main()
