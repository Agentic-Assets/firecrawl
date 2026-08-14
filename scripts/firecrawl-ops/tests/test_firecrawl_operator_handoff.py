"""Unit contracts for the dry-run-first local operator handoff."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "firecrawl_operator_handoff.py"


def load_handoff_module():
    spec = importlib.util.spec_from_file_location(
        "firecrawl_operator_handoff", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


handoff = load_handoff_module()


def idle_queue() -> dict[str, object]:
    return {
        "success": True,
        "jobsInQueue": 0,
        "activeJobsInQueue": 0,
        "waitingJobsInQueue": 0,
    }


class FakeClient:
    def __init__(
        self,
        queues: list[dict[str, object]],
        crawls: list[dict[str, object]] | None = None,
    ) -> None:
        self.queues = list(queues)
        self.crawls = list(crawls or [{"success": True, "crawls": []} for _ in queues])
        self.paths: list[str] = []

    def get_json(self, path: str) -> dict[str, object]:
        self.paths.append(path)
        if path == "/":
            return {"message": "Firecrawl API"}
        if path == "/v2/team/queue-status":
            return self.queues.pop(0)
        if path == "/v2/crawl/active":
            return self.crawls.pop(0)
        raise AssertionError(path)


class FakeAdapterClient:
    def __init__(self, settings: dict[str, object]) -> None:
        self.settings = settings

    def get_json(self, path: str) -> dict[str, object]:
        if path != "/settings":
            raise AssertionError(path)
        return self.settings


def safe_adapter_settings(
    *, active_ocr: int = 0, capture: bool = False
) -> dict[str, object]:
    return {
        "ok": True,
        "settings_fingerprint": "a" * 64,
        "adapter": {"active_ocr": active_ocr, "max_concurrent_ocr": 2},
        "profile": {"capture_docling_json": capture},
    }


class FakeDoclingClient:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def require_success_status(self, path: str) -> None:
        self.paths.append(path)


class FirecrawlOperatorHandoffTests(unittest.TestCase):
    def make_env(self, directory: Path) -> Path:
        (directory / "scripts" / "firecrawl-ops").mkdir(parents=True, exist_ok=True)
        env_path = directory / ".env"
        env_path.write_text(
            "OPENAI_API_KEY=top-secret-value\n"
            "OPENAI_BASE_URL=before\n"
            "MODEL_NAME=before\n"
            "MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK=before\n"
            "FIRE_PDF_ENABLE=false\n"
            "FIRE_PDF_PERCENT=10\n"
            "FIRE_PDF_BASE_URL=\n"
            "FIRE_PDF_API_KEY=\n"
            "PDF_RUST_EXTRACT_ENABLE=true\n"
            "MINERU_PERCENT=0\n"
            "KEEP_ME=unchanged\n",
            encoding="utf-8",
        )
        return env_path

    def parse(self, root: Path, env_path: Path, receipt_dir: Path, *args: str):
        parsed = handoff.build_parser().parse_args(["--snapshot-delay", "0.1", *args])
        parsed.test_paths = handoff.TransitionPaths(root, env_path, receipt_dir)
        return parsed

    def run_transition(self, args, **kwargs):
        return handoff.run_transition(args, paths=args.test_paths, **kwargs)

    def test_model_dry_run_is_body_free_and_leaves_env_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            env_path = self.make_env(tmp)
            original = env_path.read_bytes()
            receipt_dir = tmp / "receipts"
            args = self.parse(
                tmp, env_path, receipt_dir, "model", "--profile", "gateway"
            )
            client = FakeClient([idle_queue(), idle_queue()])

            receipt, receipt_path = self.run_transition(
                args, client=client, sleeper=lambda _: None
            )

            self.assertEqual(receipt["mode"], "dry_run")
            self.assertEqual(receipt["final_state"], "planned")
            self.assertEqual(receipt["new_values"], handoff.MODEL_PROFILES["gateway"])
            self.assertEqual(receipt["body_retained_bytes"], 0)
            self.assertEqual(env_path.read_bytes(), original)
            self.assertEqual(client.paths.count("/v2/team/queue-status"), 2)
            self.assertEqual(client.paths.count("/v2/crawl/active"), 2)
            persisted = receipt_path.read_text(encoding="utf-8")
            self.assertNotIn("top-secret-value", persisted)
            self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)

    def test_busy_malformed_or_active_state_fails_before_writing(self) -> None:
        cases = {
            "busy": (
                [{**idle_queue(), "jobsInQueue": 1, "activeJobsInQueue": 1}],
                None,
            ),
            "mismatched": ([{**idle_queue(), "jobsInQueue": 1}], None),
            "active-crawl": (
                [idle_queue()],
                [{"success": True, "crawls": [{"url": "https://private.test"}]}],
            ),
        }
        for name, (queues, crawls) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp_str:
                tmp = Path(tmp_str)
                env_path = self.make_env(tmp)
                original = env_path.read_bytes()
                receipt_dir = tmp / "receipts"
                args = self.parse(
                    tmp, env_path, receipt_dir, "model", "--profile", "gateway"
                )

                with self.assertRaises(handoff.HandoffError):
                    self.run_transition(
                        args, client=FakeClient(queues, crawls), sleeper=lambda _: None
                    )

                self.assertEqual(env_path.read_bytes(), original)
                self.assertFalse(receipt_dir.exists())

    def test_apply_requires_complete_attestation_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            env_path = self.make_env(tmp)
            original = env_path.read_bytes()
            args = self.parse(
                handoff.REPO_ROOT,
                env_path,
                tmp / "receipts",
                "--apply",
                "model",
                "--profile",
                "gateway",
            )
            calls: list[list[str]] = []

            def runner(command, **_kwargs):
                calls.append(command)

            with self.assertRaises(handoff.HandoffError):
                self.run_transition(
                    args,
                    client=FakeClient([idle_queue(), idle_queue()]),
                    sleeper=lambda _: None,
                    runner=runner,
                )

            self.assertEqual(calls, [])
            self.assertEqual(env_path.read_bytes(), original)

    def test_retained_ocr_apply_has_ordered_nonsecret_calls_and_preserves_keys(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            env_path = self.make_env(tmp)
            receipt_dir = tmp / "receipts"
            args = self.parse(
                handoff.REPO_ROOT,
                env_path,
                receipt_dir,
                "--apply",
                "--operator",
                "cayman",
                "--approval-ref",
                "AGENTIC-2280",
                "--approve-provider-cost",
                "--confirm",
                "APPLY ocr-routing local",
                "--retain",
                "--handoff-ref",
                "AGENTIC-2280",
                "--retain-confirm",
                "RETAIN ocr-routing local",
                "ocr-routing",
                "--mode",
                "local",
            )
            calls: list[list[str]] = []

            def runner(command, **_kwargs):
                calls.append(command)

            receipt, receipt_path = self.run_transition(
                args,
                client=FakeClient(
                    [idle_queue(), idle_queue(), idle_queue(), idle_queue()]
                ),
                sleeper=lambda _: None,
                runner=runner,
            )

            values = dict(
                line.split("=", 1)
                for line in env_path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(values["FIRE_PDF_ENABLE"], "true")
            self.assertEqual(values["FIRE_PDF_PERCENT"], "100")
            self.assertEqual(
                values["FIRE_PDF_BASE_URL"], "http://host.docker.internal:31337"
            )
            self.assertEqual(values["FIRE_PDF_API_KEY"], "")
            self.assertEqual(values["OPENAI_API_KEY"], "top-secret-value")
            self.assertEqual(values["KEEP_ME"], "unchanged")
            self.assertEqual(receipt["final_state"], "retained")
            self.assertEqual(receipt["canary_status"], "not_run_no_automatic_canary")
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][0:2], ["docker", "compose"])
            self.assertIn("firecrawl_healthcheck.sh", calls[1][0])
            self.assertNotIn(
                "FIRE_PDF_API_KEY", receipt_path.read_text(encoding="utf-8")
            )

    def test_model_apply_writes_only_allowlisted_values_then_recreates_and_checks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            env_path = self.make_env(tmp)
            args = self.parse(
                tmp,
                env_path,
                tmp / "receipts",
                "--apply",
                "--operator",
                "cayman",
                "--approval-ref",
                "AGENTIC-2280",
                "--approve-provider-cost",
                "--confirm",
                "APPLY model gateway",
                "--retain",
                "--handoff-ref",
                "AGENTIC-2280",
                "--retain-confirm",
                "RETAIN model gateway",
                "model",
                "--profile",
                "gateway",
            )
            calls: list[list[str]] = []

            def runner(command, **_kwargs):
                calls.append(command)

            receipt, _ = self.run_transition(
                args,
                client=FakeClient(
                    [idle_queue(), idle_queue(), idle_queue(), idle_queue()]
                ),
                sleeper=lambda _: None,
                runner=runner,
            )

            self.assertEqual(receipt["final_state"], "retained")
            self.assertEqual(calls[0][0:2], ["docker", "compose"])
            self.assertIn("firecrawl_healthcheck.sh", calls[1][0])
            self.assertEqual(
                handoff.read_env(env_path)[1]["MODEL_NAME"],
                "deepseek/deepseek-v4-flash-0731",
            )

    def test_ocr_routing_refuses_to_touch_an_external_firepdf_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            env_path = self.make_env(tmp)
            env_path.write_text(
                env_path.read_text(encoding="utf-8").replace(
                    "FIRE_PDF_API_KEY=\n", "FIRE_PDF_API_KEY=secret\n"
                ),
                encoding="utf-8",
            )
            original = env_path.read_bytes()
            args = self.parse(
                tmp, env_path, tmp / "receipts", "ocr-routing", "--mode", "local"
            )

            with self.assertRaises(handoff.HandoffError) as raised:
                self.run_transition(args, client=FakeClient([]), sleeper=lambda _: None)

            self.assertIn("external FirePDF key", str(raised.exception))
            self.assertEqual(env_path.read_bytes(), original)

    def test_restore_rejects_secret_or_cre_receipt_keys_before_any_write(self) -> None:
        for malicious_key in (
            "OPENAI_API_KEY",
            "FIRE_PDF_API_KEY",
            "PLAYWRIGHT_MAX_CONCURRENT_PAGES",
        ):
            with (
                self.subTest(malicious_key=malicious_key),
                tempfile.TemporaryDirectory() as tmp_str,
            ):
                tmp = Path(tmp_str)
                env_path = self.make_env(tmp)
                original = env_path.read_bytes()
                receipt_dir = tmp / "receipts"
                receipt_dir.mkdir()
                source = handoff.make_receipt(
                    operation="model",
                    target="gateway",
                    mode="apply",
                    operator="cayman",
                    approval_ref="AGENTIC-2280",
                    handoff_ref="AGENTIC-2280",
                    snapshots=[],
                    adapter=None,
                    old_values=handoff.MODEL_PROFILES["gateway"],
                    new_values=handoff.MODEL_PROFILES["gateway"],
                    env_before=original,
                    env_transition=original,
                    env_after=original,
                    final_state="retained",
                )
                source["receipt_id"] = "malicious"
                source["changed_keys"] = [malicious_key]
                source["old_values"] = {malicious_key: "secret-sentinel"}
                source["new_values"] = {malicious_key: "secret-sentinel"}
                source["config_fingerprint"] = handoff.non_secret_digest(
                    source["new_values"]
                )
                (receipt_dir / "malicious.json").write_text(
                    json.dumps(source), encoding="utf-8"
                )
                args = self.parse(
                    handoff.REPO_ROOT,
                    env_path,
                    receipt_dir,
                    "restore",
                    "--receipt",
                    "malicious",
                )

                with self.assertRaises(handoff.HandoffError):
                    self.run_transition(
                        args, client=FakeClient([]), sleeper=lambda _: None
                    )

                self.assertEqual(env_path.read_bytes(), original)
                self.assertEqual(
                    list(receipt_dir.glob("*.json")), [receipt_dir / "malicious.json"]
                )

    def test_parser_does_not_expose_alternate_env_or_receipt_paths(self) -> None:
        parser = handoff.build_parser()
        for option in ("--repo-root", "--env-path", "--receipt-dir"):
            with self.subTest(option=option), self.assertRaises(SystemExit):
                parser.parse_args(
                    [option, "/tmp/not-the-root-env", "model", "--profile", "gateway"]
                )

    def test_env_symlink_is_refused_before_any_handoff_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            env_path = self.make_env(tmp)
            external_env = tmp / "external.env"
            external_env.write_bytes(env_path.read_bytes())
            original = external_env.read_bytes()
            env_path.unlink()
            env_path.symlink_to(external_env)
            receipt_dir = tmp / "receipts"
            args = self.parse(
                tmp, env_path, receipt_dir, "model", "--profile", "gateway"
            )

            with self.assertRaises(handoff.HandoffError) as raised:
                self.run_transition(args, client=FakeClient([]), sleeper=lambda _: None)

            self.assertIn(
                "local env file contains a symbolic link", str(raised.exception)
            )
            self.assertEqual(external_env.read_bytes(), original)
            self.assertFalse(receipt_dir.exists())

    def test_receipt_symlink_component_is_refused_before_any_handoff_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            env_path = self.make_env(tmp)
            external_dir = tmp / "external-receipts"
            external_dir.mkdir()
            linked_parent = tmp / "linked-receipts"
            linked_parent.symlink_to(external_dir, target_is_directory=True)
            receipt_dir = linked_parent / "nested"
            args = self.parse(
                tmp, env_path, receipt_dir, "model", "--profile", "gateway"
            )
            original = env_path.read_bytes()

            with self.assertRaises(handoff.HandoffError) as raised:
                self.run_transition(args, client=FakeClient([]), sleeper=lambda _: None)

            self.assertIn(
                "receipt directory contains a symbolic link", str(raised.exception)
            )
            self.assertEqual(env_path.read_bytes(), original)
            self.assertEqual(list(external_dir.iterdir()), [])

    def test_receipt_symlink_leaf_is_refused_without_an_outside_write(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp_str,
            tempfile.TemporaryDirectory() as outside_str,
        ):
            tmp = Path(tmp_str)
            env_path = self.make_env(tmp)
            receipt_dir = tmp / "receipts"
            receipt_dir.mkdir()
            receipt_id = "1" * 32
            external_receipt = Path(outside_str) / "outside-receipt.json"
            external_receipt.write_bytes(b"outside content remains unchanged\n")
            original = external_receipt.read_bytes()
            leaf = receipt_dir / f"{receipt_id}.json"
            leaf.symlink_to(external_receipt)
            args = self.parse(
                tmp, env_path, receipt_dir, "model", "--profile", "gateway"
            )

            with (
                patch.object(
                    handoff.uuid,
                    "uuid4",
                    return_value=uuid.UUID(hex=receipt_id),
                ),
                self.assertRaises(handoff.HandoffError) as raised,
            ):
                self.run_transition(
                    args,
                    client=FakeClient([idle_queue(), idle_queue()]),
                    sleeper=lambda _: None,
                )

            self.assertIn("receipt destination already exists", str(raised.exception))
            self.assertTrue(leaf.is_symlink())
            self.assertEqual(external_receipt.read_bytes(), original)

    def test_post_mutation_runner_and_idle_failures_restore_and_record_actual_final_digest(
        self,
    ) -> None:
        for failure_point in ("compose", "healthcheck", "final-idle"):
            with (
                self.subTest(failure_point=failure_point),
                tempfile.TemporaryDirectory() as tmp_str,
            ):
                tmp = Path(tmp_str)
                env_path = self.make_env(tmp)
                original = env_path.read_bytes()
                receipt_dir = tmp / "receipts"
                args = self.parse(
                    handoff.REPO_ROOT,
                    env_path,
                    receipt_dir,
                    "--apply",
                    "--operator",
                    "cayman",
                    "--approval-ref",
                    "AGENTIC-2280",
                    "--approve-provider-cost",
                    "--confirm",
                    "APPLY model gateway",
                    "--retain",
                    "--handoff-ref",
                    "AGENTIC-2280",
                    "--retain-confirm",
                    "RETAIN model gateway",
                    "model",
                    "--profile",
                    "gateway",
                )
                calls: list[list[str]] = []

                def runner(
                    command,
                    *,
                    calls=calls,
                    failure_point=failure_point,
                    **_kwargs,
                ):
                    calls.append(command)
                    if failure_point == "compose" and command[:2] == [
                        "docker",
                        "compose",
                    ]:
                        raise RuntimeError("compose failed")
                    if (
                        failure_point == "healthcheck"
                        and "firecrawl_healthcheck.sh" in command[0]
                    ):
                        raise RuntimeError("health failed")

                queues = [idle_queue(), idle_queue(), idle_queue()]
                if failure_point == "final-idle":
                    queues.append(
                        {**idle_queue(), "jobsInQueue": 1, "activeJobsInQueue": 1}
                    )
                with self.assertRaises(handoff.HandoffError) as raised:
                    self.run_transition(
                        args,
                        client=FakeClient(queues),
                        sleeper=lambda _: None,
                        runner=runner,
                    )

                self.assertIn("redacted receipt", str(raised.exception))
                self.assertEqual(env_path.read_bytes(), original)
                receipt_paths = list(receipt_dir.glob("*.json"))
                self.assertEqual(len(receipt_paths), 1)
                receipt = json.loads(receipt_paths[0].read_text(encoding="utf-8"))
                self.assertEqual(receipt["final_state"], "restored-after-failure")
                self.assertEqual(receipt["compose_or_adapter_status"], "failed")
                self.assertEqual(
                    receipt["env_sha256_after"], handoff.sha256_bytes(original)
                )
                self.assertNotIn("top-secret-value", json.dumps(receipt))

    def test_nonretained_apply_records_transition_and_final_restore_digests(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            env_path = self.make_env(tmp)
            original = env_path.read_bytes()
            args = self.parse(
                handoff.REPO_ROOT,
                env_path,
                tmp / "receipts",
                "--apply",
                "--operator",
                "cayman",
                "--approval-ref",
                "AGENTIC-2280",
                "--approve-provider-cost",
                "--confirm",
                "APPLY model gateway",
                "model",
                "--profile",
                "gateway",
            )
            calls: list[list[str]] = []

            receipt, _ = self.run_transition(
                args,
                client=FakeClient([idle_queue() for _ in range(5)]),
                sleeper=lambda _: None,
                runner=lambda command, **_kwargs: calls.append(command),
            )

            self.assertEqual(env_path.read_bytes(), original)
            self.assertEqual(receipt["final_state"], "restored")
            self.assertEqual(
                receipt["env_sha256_after"], handoff.sha256_bytes(original)
            )
            self.assertNotEqual(
                receipt["env_sha256_transition"], receipt["env_sha256_after"]
            )
            self.assertEqual(len(calls), 4)

    def test_restore_refuses_when_the_post_transition_digest_does_not_match(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            env_path = self.make_env(tmp)
            receipt_dir = tmp / "receipts"
            receipt_dir.mkdir()
            source = handoff.make_receipt(
                operation="model",
                target="gateway",
                mode="apply",
                operator="cayman",
                approval_ref="AGENTIC-2280",
                handoff_ref="AGENTIC-2280",
                snapshots=[],
                adapter=None,
                old_values={key: "old" for key in handoff.MODEL_KEYS},
                new_values=handoff.MODEL_PROFILES["gateway"],
                env_before=b"before",
                env_transition=b"transition",
                env_after=b"transition",
                final_state="retained",
            )
            source["receipt_id"] = "prior"
            source["env_sha256_after"] = "sha256:" + "0" * 64
            (receipt_dir / "prior.json").write_text(
                json.dumps(source), encoding="utf-8"
            )
            args = self.parse(
                tmp, env_path, receipt_dir, "restore", "--receipt", "prior"
            )

            with self.assertRaises(handoff.HandoffError) as raised:
                self.run_transition(args, client=FakeClient([]), sleeper=lambda _: None)

            self.assertIn("digest", str(raised.exception))

    def test_loopback_validation_rejects_proxyable_or_remote_origins(self) -> None:
        for value in (
            "https://localhost:3002",
            "http://localhost:3003",
            "http://example.com:3002",
            "http://localhost:3002/path",
            "http://user@localhost:3002",
        ):
            with self.subTest(value=value), self.assertRaises(handoff.HandoffError):
                handoff.canonical_loopback_url(value, expected_port=3002)

        client = handoff.ReadOnlyLoopbackClient(
            "http://localhost:3002", timeout_seconds=1
        )
        self.assertEqual(client.proxy_handler.proxies, {})
        self.assertTrue(
            any(
                isinstance(item, handoff.NoRedirectHandler)
                for item in client.opener.handlers
            )
        )

    def test_debug_ocr_profile_is_not_an_operator_handoff_target(self) -> None:
        profiles = Path(__file__).resolve().parents[1] / "pdf_ocr_profiles.json"
        self.assertTrue(handoff.resolve_profile_capture("qa-debug", profiles))

    def test_ocr_adapter_rechecks_safe_settings_after_restart_and_records_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            env_path = self.make_env(tmp)
            args = self.parse(
                handoff.REPO_ROOT,
                env_path,
                tmp / "receipts",
                "--apply",
                "--operator",
                "cayman",
                "--approval-ref",
                "AGENTIC-2280",
                "--approve-provider-cost",
                "--confirm",
                "APPLY ocr-adapter default",
                "--retain",
                "--handoff-ref",
                "AGENTIC-2280",
                "--retain-confirm",
                "RETAIN ocr-adapter default",
                "ocr-adapter",
                "--profile",
                "default",
            )

            class SequenceAdapterClient:
                def __init__(self) -> None:
                    self.settings = iter(
                        [
                            safe_adapter_settings(),
                            safe_adapter_settings(),
                            safe_adapter_settings(capture=True),
                        ]
                    )

                def get_json(self, path: str) -> dict[str, object]:
                    if path != "/settings":
                        raise AssertionError(path)
                    return next(self.settings)

            adapter_client = SequenceAdapterClient()

            def client_factory(_url, **kwargs):
                self.assertEqual(kwargs["expected_port"], 31337)
                return adapter_client

            with self.assertRaises(handoff.HandoffError) as raised:
                self.run_transition(
                    args,
                    client=FakeClient(
                        [idle_queue(), idle_queue(), idle_queue(), idle_queue()]
                    ),
                    client_factory=client_factory,
                    sleeper=lambda _: None,
                    runner=lambda *_args, **_kwargs: None,
                    container_exists=lambda *_args: True,
                )

            self.assertIn("redacted receipt", str(raised.exception))
            receipt_paths = list((tmp / "receipts").glob("*.json"))
            self.assertEqual(len(receipt_paths), 1)
            persisted = json.loads(receipt_paths[0].read_text(encoding="utf-8"))
            self.assertEqual(persisted["compose_or_adapter_status"], "failed")

    def test_lifecycle_dry_run_is_body_free_and_performs_no_container_action(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            env_path = self.make_env(tmp)
            receipt_dir = tmp / "receipts"
            args = self.parse(
                handoff.REPO_ROOT,
                env_path,
                receipt_dir,
                "ocr-lifecycle",
                "--action",
                "ensure",
            )
            calls: list[list[str]] = []

            receipt, receipt_path = self.run_transition(
                args,
                client=FakeClient([idle_queue(), idle_queue()]),
                sleeper=lambda _: None,
                runner=lambda command, **_kwargs: calls.append(command),
                container_exists=lambda *_args: self.fail("dry run inspected Docker"),
                docling_inspector=lambda _root: self.fail("dry run inspected Docling"),
            )

            self.assertEqual(calls, [])
            self.assertEqual(receipt["mode"], "dry_run")
            self.assertEqual(receipt["final_state"], "planned")
            self.assertEqual(receipt["changed_keys"], [])
            self.assertIsNone(receipt["old_values"])
            self.assertIsNone(receipt["new_values"])
            self.assertEqual(receipt["body_retained_bytes"], 0)
            self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)

    def test_lifecycle_ensure_apply_uses_only_fixed_docling_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            env_path = self.make_env(tmp)
            receipt_dir = tmp / "receipts"
            args = self.parse(
                handoff.REPO_ROOT,
                env_path,
                receipt_dir,
                "--apply",
                "--operator",
                "cayman",
                "--approval-ref",
                "AGENTIC-2280",
                "--approve-provider-cost",
                "--confirm",
                "APPLY ocr-lifecycle ensure",
                "--retain",
                "--handoff-ref",
                "AGENTIC-2280",
                "--retain-confirm",
                "RETAIN ocr-lifecycle ensure",
                "ocr-lifecycle",
                "--action",
                "ensure",
            )
            calls: list[list[str]] = []
            docling = FakeDoclingClient()
            inspected: list[Path] = []

            def client_factory(url, **kwargs):
                self.assertEqual(url, handoff.DEFAULT_DOCLING_URL)
                self.assertEqual(kwargs["expected_port"], 5001)
                return docling

            with patch.dict(
                os.environ,
                {
                    "LOCAL_FIREPDF_DOCLING_IMAGE": "untrusted-image",
                    "LOCAL_FIREPDF_DOCLING_PORT": "9999",
                    "LOCAL_FIREPDF_CAPTURE_DOCLING_JSON": "true",
                },
                clear=False,
            ):
                receipt, receipt_path = self.run_transition(
                    args,
                    client=FakeClient([idle_queue() for _ in range(4)]),
                    client_factory=client_factory,
                    sleeper=lambda _: None,
                    runner=lambda command, **_kwargs: calls.append(command),
                    container_exists=lambda *_args: False,
                    docling_inspector=lambda root: inspected.append(root),
                )

            self.assertEqual(len(calls), 1)
            self.assertEqual(
                calls[0][0:5],
                ["docker", "run", "-d", "--name", handoff.DOCLING_CONTAINER],
            )
            self.assertIn("127.0.0.1:5001:5001", calls[0])
            self.assertIn(handoff.DOCLING_IMAGE, calls[0])
            self.assertNotIn("untrusted-image", calls[0])
            self.assertNotIn("9999", calls[0])
            self.assertEqual(docling.paths, ["/docs"])
            self.assertEqual(inspected, [handoff.REPO_ROOT])
            self.assertEqual(receipt["final_state"], "retained")
            self.assertEqual(receipt["ocr_adapter"], None)
            self.assertEqual(receipt["changed_keys"], [])
            self.assertEqual(receipt["body_retained_bytes"], 0)
            self.assertNotIn(
                "top-secret-value", receipt_path.read_text(encoding="utf-8")
            )

    def test_lifecycle_restart_bootstraps_fixed_adapter_and_reads_back_safe_settings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            env_path = self.make_env(tmp)
            args = self.parse(
                handoff.REPO_ROOT,
                env_path,
                tmp / "receipts",
                "--apply",
                "--operator",
                "cayman",
                "--approval-ref",
                "AGENTIC-2280",
                "--approve-provider-cost",
                "--confirm",
                "APPLY ocr-lifecycle restart",
                "--retain",
                "--handoff-ref",
                "AGENTIC-2280",
                "--retain-confirm",
                "RETAIN ocr-lifecycle restart",
                "ocr-lifecycle",
                "--action",
                "restart",
            )
            calls: list[list[str]] = []
            docling = FakeDoclingClient()
            adapter = FakeAdapterClient(safe_adapter_settings())

            def client_factory(url, **kwargs):
                if kwargs["expected_port"] == 5001:
                    self.assertEqual(url, handoff.DEFAULT_DOCLING_URL)
                    return docling
                self.assertEqual(kwargs["expected_port"], 31337)
                return adapter

            receipt, _ = self.run_transition(
                args,
                client=FakeClient([idle_queue() for _ in range(4)]),
                client_factory=client_factory,
                sleeper=lambda _: None,
                runner=lambda command, **_kwargs: calls.append(command),
                container_exists=lambda *_args: False,
                docling_inspector=lambda _root: None,
            )

            flattened = [item for command in calls for item in command]
            self.assertIn(handoff.DOCLING_IMAGE, flattened)
            self.assertIn("firecrawl-local-firepdf-adapter:latest", flattened)
            self.assertIn("LOCAL_FIREPDF_CAPTURE_DOCLING_JSON=false", flattened)
            self.assertNotIn("qa-debug", flattened)
            self.assertEqual(docling.paths, ["/docs"])
            self.assertEqual(
                receipt["ocr_adapter"],
                {
                    "active_ocr": 0,
                    "max_concurrent_ocr": 2,
                    "settings_fingerprint": "a" * 64,
                },
            )

    def test_missing_adapter_is_bootstrapped_without_a_failed_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            env_path = self.make_env(tmp)
            args = self.parse(
                handoff.REPO_ROOT,
                env_path,
                tmp / "receipts",
                "--apply",
                "--operator",
                "cayman",
                "--approval-ref",
                "AGENTIC-2280",
                "--approve-provider-cost",
                "--confirm",
                "APPLY ocr-adapter default",
                "--retain",
                "--handoff-ref",
                "AGENTIC-2280",
                "--retain-confirm",
                "RETAIN ocr-adapter default",
                "ocr-adapter",
                "--profile",
                "default",
            )
            calls: list[list[str]] = []

            class MissingAdapterClient:
                def get_json(self, _path: str) -> dict[str, object]:
                    raise handoff.HandoffError("loopback read is unavailable")

            clients = iter(
                [MissingAdapterClient(), FakeAdapterClient(safe_adapter_settings())]
            )

            receipt, _ = self.run_transition(
                args,
                client=FakeClient([idle_queue() for _ in range(4)]),
                client_factory=lambda _url, **_kwargs: next(clients),
                sleeper=lambda _: None,
                runner=lambda command, **_kwargs: calls.append(command),
                container_exists=lambda *_args: False,
            )

            self.assertFalse(
                any(command[:3] == ["docker", "rm", "-f"] for command in calls)
            )
            self.assertTrue(
                any(command[:2] == ["docker", "build"] for command in calls)
            )
            self.assertTrue(any(command[:2] == ["docker", "run"] for command in calls))
            self.assertEqual(receipt["final_state"], "retained")

    def test_lifecycle_failures_record_a_body_free_terminal_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            env_path = self.make_env(tmp)
            receipt_dir = tmp / "receipts"
            args = self.parse(
                handoff.REPO_ROOT,
                env_path,
                receipt_dir,
                "--apply",
                "--operator",
                "cayman",
                "--approval-ref",
                "AGENTIC-2280",
                "--approve-provider-cost",
                "--confirm",
                "APPLY ocr-lifecycle ensure",
                "--retain",
                "--handoff-ref",
                "AGENTIC-2280",
                "--retain-confirm",
                "RETAIN ocr-lifecycle ensure",
                "ocr-lifecycle",
                "--action",
                "ensure",
            )

            with self.assertRaises(handoff.HandoffError) as raised:
                self.run_transition(
                    args,
                    client=FakeClient([idle_queue() for _ in range(3)]),
                    client_factory=lambda *_args, **_kwargs: self.fail(
                        "unexpected readback"
                    ),
                    sleeper=lambda _: None,
                    runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        RuntimeError("Docker failed")
                    ),
                    container_exists=lambda *_args: False,
                    docling_inspector=lambda _root: self.fail("unexpected inspect"),
                )

            self.assertIn("redacted receipt", str(raised.exception))
            persisted = json.loads(
                next(receipt_dir.glob("*.json")).read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["final_state"], "manual-handoff-required")
            self.assertEqual(persisted["compose_or_adapter_status"], "failed")
            self.assertEqual(persisted["changed_keys"], [])
            self.assertEqual(persisted["body_retained_bytes"], 0)
            self.assertNotIn("top-secret-value", json.dumps(persisted))

    def test_lifecycle_rejects_profile_capture_image_and_port_options(self) -> None:
        parser = handoff.build_parser()
        for extra in (
            ("--profile", "qa-debug"),
            ("--capture-json",),
            ("--output-dir", "/tmp/raw"),
            ("--image", "untrusted-image"),
            ("--port", "9999"),
        ):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                parser.parse_args(["ocr-lifecycle", "--action", "ensure", *extra])

    def test_secret_like_operator_reference_is_rejected_without_echoing_it(
        self,
    ) -> None:
        secret = "sk-operator-secret"
        args = handoff.build_parser().parse_args(
            [
                "--apply",
                "--operator",
                secret,
                "--approval-ref",
                "AGENTIC-2280",
                "--approve-provider-cost",
                "--confirm",
                "APPLY ocr-lifecycle ensure",
                "--retain",
                "--handoff-ref",
                "AGENTIC-2280",
                "--retain-confirm",
                "RETAIN ocr-lifecycle ensure",
                "ocr-lifecycle",
                "--action",
                "ensure",
            ]
        )

        with self.assertRaises(handoff.HandoffError) as raised:
            handoff.validate_apply_attestation(args, "ocr-lifecycle", "ensure")

        self.assertNotIn(secret, str(raised.exception))

    def test_lifecycle_receipts_reject_environment_values_and_nonzero_bodies(
        self,
    ) -> None:
        receipt = handoff.make_receipt(
            operation="ocr-lifecycle",
            target="ensure",
            mode="apply",
            operator="cayman",
            approval_ref="AGENTIC-2280",
            handoff_ref="AGENTIC-2280",
            snapshots=[],
            adapter=None,
            old_values=None,
            new_values=None,
            env_before=None,
            env_transition=None,
            env_after=None,
            final_state="retained",
        )
        for update in (
            {
                "changed_keys": ["OPENAI_API_KEY"],
                "old_values": {"OPENAI_API_KEY": "x"},
                "new_values": {"OPENAI_API_KEY": "x"},
            },
            {"body_retained_bytes": 1},
        ):
            with self.subTest(update=update):
                invalid = dict(receipt)
                invalid.update(update)
                with self.assertRaises(handoff.HandoffError):
                    handoff.validate_receipt_schema(invalid)


if __name__ == "__main__":
    unittest.main()
