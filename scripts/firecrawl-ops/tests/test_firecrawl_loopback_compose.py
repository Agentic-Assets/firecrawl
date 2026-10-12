"""Validate the opt-in overlay with Compose's real merger; never start services."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class FirecrawlLoopbackComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        docker = shutil.which("docker")
        if not docker:
            raise unittest.SkipTest(
                "Docker Compose CLI unavailable; no daemon is needed"
            )
        result = subprocess.run(
            [docker, "compose", "version", "--short"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        version = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout)
        if (
            result.returncode
            or not version
            or tuple(map(int, version.groups())) < (2, 24, 4)
        ):
            raise unittest.SkipTest("Docker Compose 2.24.4+ required for !override")
        cls.docker = docker

    def compose_config(self, env_file: Path, env: dict, *, overlay: bool):
        args = [
            self.docker,
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            "docker-compose.yaml",
        ]
        if overlay:
            args.extend(["-f", "docker-compose.loopback.yaml"])
        result = subprocess.run(
            [*args, "config", "--format", "json"],
            env=env,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        # Resolved config can contain environment secrets: never attach it to
        # assertion failures, stdout or stderr.
        self.assertEqual(
            result.returncode, 0, "Compose config failed; inspect diagnostics privately"
        )
        return json.loads(result.stdout)

    def test_overlay_replaces_wildcard_without_changing_other_settings_or_ports(self):
        with tempfile.TemporaryDirectory() as directory:
            empty_env = Path(directory) / "empty.env"
            empty_env.touch()
            for host_port, internal_port in ((None, None), ("43002", "43003")):
                with self.subTest(host_port=host_port, internal_port=internal_port):
                    env = os.environ.copy()
                    env.pop("PORT", None)
                    env.pop("INTERNAL_PORT", None)
                    if host_port:
                        env.update(PORT=host_port, INTERNAL_PORT=internal_port)
                    baseline = self.compose_config(empty_env, env, overlay=False)
                    merged = self.compose_config(empty_env, env, overlay=True)
                    ports = merged["services"]["api"]["ports"]
                    self.assertEqual(len(ports), 1)
                    self.assertEqual(ports[0]["host_ip"], "127.0.0.1")
                    self.assertEqual(str(ports[0]["published"]), host_port or "3002")
                    self.assertEqual(ports[0]["target"], int(internal_port or "3002"))
                    self.assertEqual(ports[0]["protocol"], "tcp")
                    baseline["services"]["api"]["ports"] = ports
                    self.assertTrue(
                        merged == baseline,
                        "Overlay changed settings beyond API publication (config withheld)",
                    )


if __name__ == "__main__":
    unittest.main()
