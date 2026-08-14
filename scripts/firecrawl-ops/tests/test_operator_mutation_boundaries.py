"""Regression tests that legacy agent-facing paths cannot switch local profiles."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

OPS_DIR = Path(__file__).resolve().parents[1]
OCR_SCRIPT = OPS_DIR / "local_firepdf_ocr.sh"
REQUEST_HELPER_PATH = OPS_DIR / "firecrawl_request.py"
SWARM_PATH = OPS_DIR / "firecrawl_swarm_pipeline.py"
REPO_ROOT = OPS_DIR.parents[1]
AGENT_FACING_DOCS = (
    ".agents/skills/firecrawl-ops/SKILL.md",
    ".agents/skills/firecrawl-local-api/SKILL.md",
    "scripts/firecrawl-ops/README.md",
    "scripts/firecrawl-ops/CLAUDE.md",
    "docs/firecrawl-ops/references/local-pdf-ocr-plan.md",
    "docs/firecrawl-ops/references/local-pdf-ocr-research-agent-plan.md",
    "docs/firecrawl-ops/references/tools-capabilities.md",
    "docs/firecrawl-ops/references/ops-playbook.md",
    "docs/firecrawl-ops/references/model-routing.md",
    "docs/firecrawl-ops/references/partner-orbstack-onboarding.md",
    "LOCAL_DEVELOPMENT_GUIDE.md",
    "AGENTS.md",
)
RETIRED_DOC_COMMAND = re.compile(
    r"^\s*scripts/firecrawl-ops/(?:set_model_profile\.sh\b|"
    r"local_firepdf_ocr\.sh "
    r"(?:start-docling|start-adapter|start|restart-adapter|restart|"
    r"stop-adapter|stop-docling|stop|enable-firecrawl)\b|"
    r"firecrawl_cli\.sh --firecrawl-|"
    r"firecrawl_request\.py .*--model-profile)",
    re.MULTILINE,
)


def load_swarm_module():
    spec = importlib.util.spec_from_file_location(
        "firecrawl_swarm_pipeline_boundaries", SWARM_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


swarm = load_swarm_module()


class OperatorMutationBoundaryTests(unittest.TestCase):
    def test_direct_ocr_enable_refuses_and_never_bootstraps_or_overwrites_env(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            env_path = tmp / ".env"
            env = os.environ | {
                "FC_DIR": str(tmp),
                "ENV_PATH": str(env_path),
                "LOCAL_FIREPDF_STATE_DIR": str(tmp / "state"),
            }

            result = subprocess.run(
                ["bash", str(OCR_SCRIPT), "enable-firecrawl"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("Direct OCR routing mutation is disabled", result.stderr)
            self.assertFalse(env_path.exists())
            self.assertNotIn("budget", result.stderr.lower())
            self.assertFalse((tmp / "state").exists())

    def test_legacy_lifecycle_aliases_route_to_handoff_without_docker_or_artifact_writes(
        self,
    ) -> None:
        cases = {
            ("start-docling",): ("ocr-lifecycle", "--action ensure"),
            ("start",): ("ocr-lifecycle", "--action restart"),
            ("restart",): ("ocr-lifecycle", "--action restart"),
            ("stop-adapter",): ("ocr-lifecycle", "--action stop"),
            ("stop-docling",): ("ocr-lifecycle", "--action stop"),
            ("stop",): ("ocr-lifecycle", "--action stop"),
            ("start-adapter",): ("ocr-adapter", "--profile default"),
            (
                "restart-adapter",
                "--profile",
                "qa-debug",
                "--capture-json",
                "--output-dir",
                "raw-output",
            ): ("ocr-adapter", "--profile default"),
        }
        for raw_command, expected in cases.items():
            command = list(raw_command)
            with (
                self.subTest(command=command),
                tempfile.TemporaryDirectory() as tmp_str,
            ):
                tmp = Path(tmp_str)
                bin_dir = tmp / "bin"
                bin_dir.mkdir()
                python_capture = tmp / "python-call.txt"
                docker_capture = tmp / "docker-call.txt"
                (bin_dir / "python3").write_text(
                    '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" > "$PYTHON_CAPTURE"\nexit 2\n',
                    encoding="utf-8",
                )
                (bin_dir / "docker").write_text(
                    '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" > "$DOCKER_CAPTURE"\nexit 99\n',
                    encoding="utf-8",
                )
                for executable in (bin_dir / "python3", bin_dir / "docker"):
                    executable.chmod(0o755)
                env = os.environ | {
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "PYTHON_CAPTURE": str(python_capture),
                    "DOCKER_CAPTURE": str(docker_capture),
                    "LOCAL_FIREPDF_STATE_DIR": str(tmp / "state"),
                }

                result = subprocess.run(
                    ["bash", str(OCR_SCRIPT), *command],
                    env=env,
                    cwd=tmp,
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 2)
                self.assertTrue(python_capture.exists())
                invocation = python_capture.read_text(encoding="utf-8")
                self.assertIn(expected[0], invocation)
                self.assertIn(expected[1], invocation)
                self.assertFalse(docker_capture.exists())
                self.assertFalse((tmp / "state").exists())
                self.assertFalse((tmp / "raw-output").exists())

    def test_legacy_adapter_profile_and_capture_flags_are_parser_rejections(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            docker_capture = tmp / "docker-call.txt"
            (bin_dir / "docker").write_text(
                '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" > "$DOCKER_CAPTURE"\nexit 99\n',
                encoding="utf-8",
            )
            (bin_dir / "docker").chmod(0o755)
            env = os.environ | {
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "DOCKER_CAPTURE": str(docker_capture),
                "LOCAL_FIREPDF_STATE_DIR": str(tmp / "state"),
            }

            result = subprocess.run(
                [
                    "bash",
                    str(OCR_SCRIPT),
                    "restart-adapter",
                    "--profile",
                    "qa-debug",
                    "--capture-json",
                    "--output-dir",
                    "raw-output",
                ],
                env=env,
                cwd=tmp,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("error:", result.stderr)
            self.assertFalse(docker_capture.exists())
            self.assertFalse((tmp / "state").exists())
            self.assertFalse((tmp / "raw-output").exists())

    def test_legacy_swarm_restart_flag_fails_before_input_or_network_work(self) -> None:
        argv = [
            "firecrawl_swarm_pipeline.py",
            "--restart-between-stages",
            "--input",
            "missing-urls.txt",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch.object(swarm, "load_urls") as load_urls,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            swarm.main()

        self.assertEqual(raised.exception.code, 2)
        load_urls.assert_not_called()

    def test_swarm_source_has_no_profile_writer_or_docker_down_path(self) -> None:
        source = SWARM_PATH.read_text(encoding="utf-8")
        self.assertNotIn("run_profile_switch", source)
        self.assertNotIn('"docker", "compose", "down"', source)
        self.assertNotIn("set_model_profile.sh", source)

    def test_request_helper_has_no_model_profile_or_docker_mutation_path(self) -> None:
        source = REQUEST_HELPER_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "apply_model_profile",
            "set_model_profile.sh",
            '"docker", "compose"',
            "--model-profile",
            "--firecrawl-dir",
            "--no-recreate-api",
            "--healthcheck",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_agent_docs_do_not_publish_retired_mutation_commands(self) -> None:
        for relative_path in AGENT_FACING_DOCS:
            with self.subTest(relative_path=relative_path):
                source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertNotRegex(source, RETIRED_DOC_COMMAND)
                self.assertIn("firecrawl_operator_handoff.py", source)

    def test_agent_docs_do_not_recommend_api_env_example_as_compose_contract(
        self,
    ) -> None:
        deprecated_bootstrap = re.compile(
            r"create.{0,120}from\s+`apps/api/\.env\.example`",
            re.DOTALL | re.IGNORECASE,
        )
        compose_contract_warning = re.compile(
            r"(?:not a (?:drop-in )?(?:Docker\s+)?Compose contract|"
            r"do not\s+use\s+`apps/api/\.env\.example`\s+as a "
            r"(?:Docker\s+)?Compose contract)",
            re.IGNORECASE,
        )
        for relative_path in AGENT_FACING_DOCS:
            with self.subTest(relative_path=relative_path):
                source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertNotRegex(source, deprecated_bootstrap)
                if "apps/api/.env.example" in source:
                    self.assertRegex(source, compose_contract_warning)


if __name__ == "__main__":
    unittest.main()
