#!/usr/bin/env python3
"""Validate the local Firecrawl CLI and MCP compatibility contract.

The default static check does not resolve packages or contact the local API.
The opt-in run probes only the loopback API and emits a redacted JSON result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import select
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

DOCTOR_SCHEMA_VERSION = 1
MANIFEST_PATH = Path(__file__).with_name("firecrawl_tooling_compatibility.json")
CLI_WRAPPER_PATH = Path(__file__).with_name("firecrawl_cli.sh")
MCP_WRAPPER_PATH = Path(__file__).with_name("firecrawl_mcp.sh")
DEFAULT_TIMEOUT_SECONDS = 45.0
PREFLIGHT_TIMEOUT_SECONDS = 5.0
MCP_MESSAGE_TIMEOUT_SECONDS = 15.0
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
LOOPBACK_NO_PROXY_HOSTS = ("localhost", "127.0.0.1", "::1")
SEMVER_PATTERN = re.compile(
    r"^(?P<name>firecrawl-(?:cli|mcp))@(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)$"
)
VERSION_PATTERN = re.compile(
    r"\b\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\b"
)


class CompatibilityError(RuntimeError):
    """A body-free compatibility failure suitable for a diagnostic code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ManifestRecord:
    content: dict[str, Any]
    sha256: str


def _require_mapping(value: object, code: str = "manifest") -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CompatibilityError(code)
    return value


def _require_string(value: object, code: str = "manifest") -> str:
    if not isinstance(value, str) or not value:
        raise CompatibilityError(code)
    return value


def load_manifest(path: Path = MANIFEST_PATH) -> ManifestRecord:
    try:
        raw = path.read_bytes()
        content = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise CompatibilityError("manifest") from error

    root = _require_mapping(content)
    if root.get("schema_version") != 1:
        raise CompatibilityError("manifest")

    normal = _require_mapping(root.get("normal"))
    for kind, expected_name in (("cli", "firecrawl-cli"), ("mcp", "firecrawl-mcp")):
        package = _require_mapping(normal.get(kind))
        name = _require_string(package.get("name"))
        version = _require_string(package.get("version"))
        spec = _require_string(package.get("spec"))
        if (
            name != expected_name
            or not SEMVER_PATTERN.fullmatch(spec)
            or spec != f"{name}@{version}"
        ):
            raise CompatibilityError("manifest")

    cli_probe = _require_mapping(_require_mapping(normal["cli"]).get("probe"))
    command = cli_probe.get("command")
    if not isinstance(command, list) or not all(
        isinstance(item, str) and item for item in command
    ):
        raise CompatibilityError("manifest")
    if command[:1] != ["map"] or "--json" not in command:
        raise CompatibilityError("manifest")

    mcp = _require_mapping(normal["mcp"])
    if mcp.get("protocol") != "jsonl":
        raise CompatibilityError("manifest")
    required_tools = mcp.get("required_tools")
    required_any_tools = mcp.get("required_any_tools")
    if not _valid_string_list(required_tools) or not _valid_string_list(
        required_any_tools
    ):
        raise CompatibilityError("manifest")

    upgrade = _require_mapping(root.get("upgrade_probe"))
    label = _require_string(upgrade.get("label"))
    if "HUMAN-ONLY" not in label:
        raise CompatibilityError("manifest")
    for key, expected_name in (
        ("cli_spec", "firecrawl-cli"),
        ("mcp_spec", "firecrawl-mcp"),
    ):
        if upgrade.get(key) != f"{expected_name}@latest":
            raise CompatibilityError("manifest")

    return ManifestRecord(content=dict(root), sha256=hashlib.sha256(raw).hexdigest())


def _valid_string_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and item for item in value)
    )


def package_spec(manifest: ManifestRecord, kind: str) -> str:
    try:
        return _require_string(
            _require_mapping(_require_mapping(manifest.content["normal"])[kind]).get(
                "spec"
            )
        )
    except (KeyError, CompatibilityError) as error:
        raise CompatibilityError("manifest") from error


def package_version(manifest: ManifestRecord, kind: str) -> str:
    try:
        return _require_string(
            _require_mapping(_require_mapping(manifest.content["normal"])[kind]).get(
                "version"
            )
        )
    except (KeyError, CompatibilityError) as error:
        raise CompatibilityError("manifest") from error


def validate_package_override(manifest: ManifestRecord, kind: str, spec: str) -> str:
    expected_name = {"cli": "firecrawl-cli", "mcp": "firecrawl-mcp"}.get(kind)
    if expected_name is None:
        raise CompatibilityError("package_spec")
    match = SEMVER_PATTERN.fullmatch(spec)
    if match is None or match.group("name") != expected_name:
        raise CompatibilityError("package_spec")
    return spec


def api_url_from_environment(environ: Mapping[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    return env.get("FIRECRAWL_API_URL") or env.get("API_URL") or "http://localhost:3002"


def validate_loopback_api_url(api_url: str) -> str:
    parsed = urlparse(api_url)
    try:
        port = parsed.port
    except ValueError as error:
        raise CompatibilityError("api") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname not in LOOPBACK_HOSTS
        or port != 3002
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise CompatibilityError("api")
    return api_url.rstrip("/")


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        raise CompatibilityError("api")


def preflight_api(api_url: str, timeout_seconds: float) -> None:
    try:
        opener = build_opener(ProxyHandler({}), _RejectRedirects())
        with opener.open(f"{api_url}/", timeout=timeout_seconds) as response:
            if not 200 <= response.status < 400:
                raise CompatibilityError("api")
    except CompatibilityError:
        raise
    except Exception as error:  # urllib has platform-specific connection errors.
        raise CompatibilityError("api") from error


def _command_environment(api_url: str) -> dict[str, str]:
    env = os.environ.copy()
    env["NPM_CONFIG_LOGLEVEL"] = "error"
    env["FIRECRAWL_API_URL"] = api_url
    env["FIRECRAWL_API_KEY"] = (
        env.get("FIRECRAWL_API_KEY") or env.get("TEST_API_KEY") or "local-dev"
    )
    env.pop("API_URL", None)
    env.pop("FIRECRAWL_CLI_PACKAGE", None)
    env.pop("FIRECRAWL_MCP_PACKAGE", None)
    env.pop("FIRECRAWL_HUMAN_UPGRADE_PROBE", None)
    no_proxy = _loopback_no_proxy_value(env)
    env["NO_PROXY"] = no_proxy
    env["no_proxy"] = no_proxy
    return env


def _loopback_no_proxy_value(environ: Mapping[str, str]) -> str:
    entries: list[str] = []
    seen: set[str] = set()
    for key in ("NO_PROXY", "no_proxy"):
        for entry in environ.get(key, "").split(","):
            normalized = entry.strip()
            if normalized and normalized.lower() not in seen:
                entries.append(normalized)
                seen.add(normalized.lower())
    for host in LOOPBACK_NO_PROXY_HOSTS:
        if host not in seen:
            entries.append(host)
            seen.add(host)
    return ",".join(entries)


def _bounded_timeout(deadline: float, maximum: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CompatibilityError("timeout")
    return min(remaining, maximum)


def _parse_json_object(value: bytes, code: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CompatibilityError(code) from error
    return _require_mapping(parsed, code)


def _extract_version(value: bytes, code: str) -> str:
    match = VERSION_PATTERN.search(value.decode("utf-8", errors="replace"))
    if match is None:
        raise CompatibilityError(code)
    return match.group(0)


def _require_normal_pin_version(
    manifest: ManifestRecord, kind: str, observed_version: str, mode: str
) -> None:
    if mode == "normal" and observed_version != package_version(manifest, kind):
        raise CompatibilityError("package_resolution")


def _cli_command(
    manifest: ManifestRecord, mode: str, api_url: str, arguments: list[str]
) -> list[str]:
    if mode == "upgrade_probe":
        return [
            "npx",
            "-y",
            manifest.content["upgrade_probe"]["cli_spec"],
            "--api-url",
            api_url,
            *arguments,
        ]
    return ["bash", str(CLI_WRAPPER_PATH), *arguments]


def _mcp_command(manifest: ManifestRecord, mode: str) -> list[str]:
    if mode == "upgrade_probe":
        return ["npx", "-y", manifest.content["upgrade_probe"]["mcp_spec"]]
    return ["bash", str(MCP_WRAPPER_PATH)]


def run_cli_version_probe(
    manifest: ManifestRecord,
    mode: str,
    api_url: str,
    deadline: float,
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> dict[str, Any]:
    """Verify the exact package version without invoking a Firecrawl tool."""
    command_environment = _command_environment(api_url)

    try:
        version_result = runner(
            _cli_command(manifest, mode, api_url, ["--version"]),
            env=command_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_bounded_timeout(deadline, MCP_MESSAGE_TIMEOUT_SECONDS),
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise CompatibilityError("timeout") from error
    except OSError as error:
        raise CompatibilityError("package_resolution") from error
    if version_result.returncode != 0:
        raise CompatibilityError("package_resolution")
    version = _extract_version(version_result.stdout, "package_resolution")
    _require_normal_pin_version(manifest, "cli", version, mode)
    return {
        "status": "pass",
        "spec": manifest.content["upgrade_probe"]["cli_spec"]
        if mode == "upgrade_probe"
        else package_spec(manifest, "cli"),
        "resolved_version": version,
        "body_bytes_persisted": 0,
    }


def run_cli_probe(
    manifest: ManifestRecord,
    mode: str,
    api_url: str,
    deadline: float,
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> dict[str, Any]:
    """Run the explicit operator map diagnostic after package-version verification."""
    version_result = run_cli_version_probe(
        manifest, mode, api_url, deadline, runner=runner
    )
    command_environment = _command_environment(api_url)

    command = _cli_command(
        manifest,
        mode,
        api_url,
        manifest.content["normal"]["cli"]["probe"]["command"],
    )
    try:
        map_result = runner(
            command,
            env=command_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_bounded_timeout(deadline, MCP_MESSAGE_TIMEOUT_SECONDS),
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise CompatibilityError("timeout") from error
    except OSError as error:
        raise CompatibilityError("cli_contract") from error
    if map_result.returncode != 0:
        raise CompatibilityError("cli_contract")
    payload = _parse_json_object(map_result.stdout, "cli_contract")
    data = payload.get("data")
    if (
        payload.get("success") is not True
        or not isinstance(data, Mapping)
        or not isinstance(data.get("links"), list)
    ):
        raise CompatibilityError("cli_contract")
    return {**version_result, "probe": "map"}


def mcp_frame(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def parse_jsonl_message(line: bytes) -> Mapping[str, Any]:
    if not line.strip():
        raise CompatibilityError("protocol")
    payload = _parse_json_object(line, "protocol")
    if payload.get("jsonrpc") != "2.0":
        raise CompatibilityError("protocol")
    return payload


def validate_mcp_transcript(
    messages: Iterable[Mapping[str, Any]], manifest: ManifestRecord, mode: str
) -> dict[str, Any]:
    responses: dict[int, Mapping[str, Any]] = {}
    for message in messages:
        if "id" not in message:
            continue
        response_id = message.get("id")
        if response_id not in (1, 2) or response_id in responses:
            raise CompatibilityError("protocol")
        responses[response_id] = message
    initialize = responses.get(1)
    tools = responses.get(2)
    if not isinstance(initialize, Mapping) or not isinstance(tools, Mapping):
        raise CompatibilityError("protocol")
    initialize_result = _require_mapping(initialize.get("result"), "protocol")
    if not isinstance(initialize_result.get("capabilities"), Mapping):
        raise CompatibilityError("protocol")
    tools_result = _require_mapping(tools.get("result"), "protocol")
    tool_values = tools_result.get("tools")
    if not isinstance(tool_values, list):
        raise CompatibilityError("protocol")
    names = {
        item.get("name")
        for item in tool_values
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    mcp = manifest.content["normal"]["mcp"]
    if not set(mcp["required_tools"]).issubset(names) or not set(
        mcp["required_any_tools"]
    ).intersection(names):
        raise CompatibilityError("inventory")
    server_info = initialize_result.get("serverInfo")
    version = server_info.get("version") if isinstance(server_info, Mapping) else None
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise CompatibilityError("protocol")
    _require_normal_pin_version(manifest, "mcp", version, mode)
    return {"resolved_version": version, "tool_count": len(names)}


def _read_mcp_message(
    process: subprocess.Popen[bytes], deadline: float
) -> Mapping[str, Any]:
    assert process.stdout is not None
    while True:
        timeout = _bounded_timeout(deadline, 0.25)
        ready, _, _ = select.select([process.stdout.fileno()], [], [], timeout)
        if not ready:
            continue
        line = process.stdout.readline()
        if not line:
            raise CompatibilityError("package_resolution")
        return parse_jsonl_message(line)


def _read_mcp_response(
    process: subprocess.Popen[bytes], expected_id: int, deadline: float
) -> Mapping[str, Any]:
    while True:
        message = _read_mcp_message(process, deadline)
        if "id" not in message:
            continue
        if message.get("id") != expected_id:
            raise CompatibilityError("protocol")
        return message


def _wait_until_deadline(process: subprocess.Popen[bytes], deadline: float) -> bool:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        return False
    return True


def _terminate_process(process: subprocess.Popen[bytes], deadline: float) -> None:
    if process.poll() is None:
        process.terminate()
        if not _wait_until_deadline(process, deadline):
            try:
                process.kill()
            except ProcessLookupError:
                pass
            _wait_until_deadline(process, deadline)
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(process, stream_name, None)
        if stream is not None:
            stream.close()


def run_mcp_probe(
    manifest: ManifestRecord,
    mode: str,
    api_url: str,
    deadline: float,
    *,
    popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> dict[str, Any]:
    command_environment = _command_environment(api_url)
    try:
        process = popen_factory(
            _mcp_command(manifest, mode),
            env=command_environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except OSError as error:
        raise CompatibilityError("package_resolution") from error
    assert process.stdin is not None
    try:
        process.stdin.write(
            mcp_frame(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "firecrawl-compatibility-doctor",
                            "version": "1",
                        },
                    },
                }
            )
        )
        process.stdin.flush()
        messages = [_read_mcp_response(process, 1, deadline)]
        process.stdin.write(
            mcp_frame(
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
            )
        )
        process.stdin.write(
            mcp_frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        )
        process.stdin.flush()
        messages.append(_read_mcp_response(process, 2, deadline))
        transcript = validate_mcp_transcript(messages, manifest, mode)
    except subprocess.TimeoutExpired as error:
        raise CompatibilityError("timeout") from error
    except OSError as error:
        raise CompatibilityError("package_resolution") from error
    finally:
        _terminate_process(process, deadline)
    return {
        "status": "pass",
        "spec": manifest.content["upgrade_probe"]["mcp_spec"]
        if mode == "upgrade_probe"
        else package_spec(manifest, "mcp"),
        "resolved_version": transcript["resolved_version"],
        "tool_count": transcript["tool_count"],
        "protocol": "jsonl",
        "body_bytes_persisted": 0,
    }


def doctor_result(
    manifest: ManifestRecord, *, mode: str, run: bool, api_url: str | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": DOCTOR_SCHEMA_VERSION,
        "kind": "firecrawl-compatibility-doctor",
        "mode": mode,
        "manifest_sha256": manifest.sha256,
        "body_bytes_persisted": 0,
        "status": "pass",
        "checks": {
            "manifest": "pass",
            "cli": {"status": "not_run", "spec": package_spec(manifest, "cli")},
            "mcp": {"status": "not_run", "spec": package_spec(manifest, "mcp")},
        },
    }
    if not run:
        result["observed_at"] = (
            datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        return result

    try:
        deadline = time.monotonic() + DEFAULT_TIMEOUT_SECONDS
        loopback_url = validate_loopback_api_url(api_url or api_url_from_environment())
        preflight_api(
            loopback_url, _bounded_timeout(deadline, PREFLIGHT_TIMEOUT_SECONDS)
        )
        result["checks"]["api"] = {"status": "pass", "url": loopback_url}
        result["checks"]["cli"] = run_cli_probe(manifest, mode, loopback_url, deadline)
        result["checks"]["mcp"] = run_mcp_probe(manifest, mode, loopback_url, deadline)
    except CompatibilityError as error:
        result["status"] = "fail"
        result["failure_code"] = error.code
        if "api" not in result["checks"]:
            result["checks"]["api"] = {"status": "fail", "code": error.code}
    result["observed_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    return result


def agent_safe_result(manifest: ManifestRecord, *, api_url: str) -> dict[str, Any]:
    """Run the agent prerequisite's strict read-only compatibility checks.

    Unlike the explicit operator `doctor --run` diagnostic, this never invokes
    the CLI's map command. It verifies the normal pinned CLI version, performs
    only the loopback root GET preflight, and checks the MCP JSONL initialize/
    tools-list protocol.
    """
    result: dict[str, Any] = {
        "schema_version": DOCTOR_SCHEMA_VERSION,
        "kind": "firecrawl-agent-safe-compatibility",
        "mode": "normal",
        "manifest_sha256": manifest.sha256,
        "body_bytes_persisted": 0,
        "status": "pass",
        "checks": {
            "manifest": "pass",
            "cli": {"status": "not_run", "spec": package_spec(manifest, "cli")},
            "mcp": {"status": "not_run", "spec": package_spec(manifest, "mcp")},
        },
    }
    try:
        deadline = time.monotonic() + DEFAULT_TIMEOUT_SECONDS
        loopback_url = validate_loopback_api_url(api_url)
        preflight_api(
            loopback_url, _bounded_timeout(deadline, PREFLIGHT_TIMEOUT_SECONDS)
        )
        result["checks"]["api"] = {"status": "pass", "url": loopback_url}
        result["checks"]["cli"] = run_cli_version_probe(
            manifest, "normal", loopback_url, deadline
        )
        result["checks"]["mcp"] = run_mcp_probe(
            manifest, "normal", loopback_url, deadline
        )
    except CompatibilityError as error:
        result["status"] = "fail"
        result["failure_code"] = error.code
        if "api" not in result["checks"]:
            result["checks"]["api"] = {"status": "fail", "code": error.code}
    result["observed_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    return result


def _write_json(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--print-default-spec", choices=("cli", "mcp"))
    actions.add_argument("--validate-package-spec", nargs=2, metavar=("KIND", "SPEC"))
    parser.add_argument(
        "--run", action="store_true", help="Run bounded loopback CLI and MCP checks."
    )
    parser.add_argument(
        "--upgrade-probe",
        action="store_true",
        help="Run the manifest's HUMAN-ONLY @latest probe.",
    )
    parser.add_argument(
        "--acknowledge-human-upgrade-probe",
        action="store_true",
        help="Required with --upgrade-probe; this does not update the normal pins.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest()
        if args.print_default_spec:
            sys.stdout.write(package_spec(manifest, args.print_default_spec) + "\n")
            return 0
        if args.validate_package_spec:
            kind, spec = args.validate_package_spec
            sys.stdout.write(validate_package_override(manifest, kind, spec) + "\n")
            return 0
        if args.upgrade_probe and not args.run:
            raise CompatibilityError("upgrade_probe_requires_run")
        if args.upgrade_probe and not args.acknowledge_human_upgrade_probe:
            raise CompatibilityError("upgrade_probe_acknowledgement")
        mode = "upgrade_probe" if args.upgrade_probe else "normal"
        result = doctor_result(manifest, mode=mode, run=args.run)
        _write_json(result)
        return 0 if result["status"] == "pass" else 1
    except CompatibilityError as error:
        if args.print_default_spec or args.validate_package_spec:
            sys.stderr.write(
                f"Firecrawl compatibility validation failed: {error.code}\n"
            )
        else:
            _write_json(
                {
                    "schema_version": DOCTOR_SCHEMA_VERSION,
                    "kind": "firecrawl-compatibility-doctor",
                    "status": "fail",
                    "failure_code": error.code,
                    "body_bytes_persisted": 0,
                }
            )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
