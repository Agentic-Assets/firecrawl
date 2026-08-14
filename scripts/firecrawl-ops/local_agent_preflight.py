"""Emit a read-only, body-free local Firecrawl capability preflight for agents.

Examples:
  scripts/firecrawl-ops/local_agent_preflight.py
  scripts/firecrawl-ops/local_agent_preflight.py --require base_http
  scripts/firecrawl-ops/local_agent_preflight.py --offline --smoke-file task/smoke.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from local_capability_matrix import extract_routes

SCHEMA_VERSION = "1"
DEFAULT_API_URL = "http://localhost:3002"
DEFAULT_MAX_EVIDENCE_AGE_SECONDS = 24 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 2.0
MAX_SMOKE_BYTES = 4 * 1024 * 1024
FUTURE_CLOCK_SKEW = timedelta(minutes=5)
CAPABILITIES = ("base_http", "async_jobs", "cli", "mcp", "ai_formats", "pdf_ocr")
STATES = {"ready", "degraded", "unavailable", "stale", "unknown"}
REPO_ROOT = Path(__file__).resolve().parents[2]
V2_ROUTE_FILE = REPO_ROOT / "apps/api/src/routes/v2.ts"
ROOT_ROUTE_FILE = REPO_ROOT / "apps/api/src/index.ts"
CLI_WRAPPER = REPO_ROOT / "scripts/firecrawl-ops/firecrawl_cli.sh"
MCP_WRAPPER = REPO_ROOT / "scripts/firecrawl-ops/firecrawl_mcp.sh"
ENV_FILE = REPO_ROOT / ".env"
DEFAULT_SMOKE_DIR = REPO_ROOT / "tasks/tmp/local-api-smoke"
PACKAGE_PATTERNS = {
    "cli": re.compile(r"^firecrawl-cli@\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$"),
    "mcp": re.compile(r"^firecrawl-mcp@\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$"),
}
RFC3339_UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


class NoRedirectHandler(HTTPRedirectHandler):
    """Treat redirects as HTTP failures so a local probe cannot leave loopback."""

    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def build_read_only_opener() -> Any:
    """Build an opener that cannot use ambient proxies or follow redirects."""

    return build_opener(ProxyHandler({}), NoRedirectHandler())


READ_ONLY_OPENER = build_read_only_opener()


def open_get(request: Request, timeout_seconds: float) -> Any:
    return READ_ONLY_OPENER.open(request, timeout=timeout_seconds)


@dataclass(frozen=True)
class SmokeEvidence:
    state: str
    digest: str
    checked_at: str | None
    passed: frozenset[str]


@dataclass(frozen=True)
class EnvEvidence:
    state: str
    model_configured: bool
    pdf_ocr_configured: bool


def timestamp(value: datetime | None = None) -> str:
    moment = value or datetime.now(UTC)
    return (
        moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not RFC3339_UTC_PATTERN.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def validate_local_api_url(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme != "http"
        or parts.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise argparse.ArgumentTypeError(
            "--api-url must be an http loopback origin without a path."
        )
    return value.rstrip("/")


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected a nonnegative integer.") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("Expected a nonnegative integer.")
    return parsed


def bounded_timeout(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Expected a timeout between 0 and 10 seconds."
        ) from exc
    if not 0 < parsed <= 10:
        raise argparse.ArgumentTypeError("Expected a timeout between 0 and 10 seconds.")
    return parsed


def digest_bytes(parts: list[bytes]) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part)
        hasher.update(b"\0")
    return f"sha256:{hasher.hexdigest()}"


def read_static(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        return b""
    return path.read_bytes()


def static_route_classes() -> tuple[dict[str, list[str]], list[bytes]]:
    v2_bytes = read_static(V2_ROUTE_FILE)
    root_bytes = read_static(ROOT_ROUTE_FILE)
    routes = (
        {(route.method, route.path) for route in extract_routes(V2_ROUTE_FILE)}
        if v2_bytes
        else set()
    )
    root_declared = bool(re.search(rb'app\.get\(\s*"/"', root_bytes))

    def declared(*items: tuple[str, str]) -> list[str]:
        return [
            f"{method} /v2{path}" for method, path in items if (method, path) in routes
        ]

    return (
        {
            "base_http": ["GET /"] if root_declared else [],
            "async_jobs": declared(
                ("GET", "/team/queue-status"),
                ("GET", "/crawl/active"),
                ("GET", "/crawl/:jobId"),
                ("GET", "/batch/scrape/:jobId"),
            ),
            "cli": ["local CLI wrapper"] if read_static(CLI_WRAPPER) else [],
            "mcp": ["local MCP wrapper"] if read_static(MCP_WRAPPER) else [],
            "ai_formats": declared(("POST", "/scrape"), ("POST", "/extract")),
            "pdf_ocr": declared(("POST", "/parse")),
        },
        [v2_bytes, root_bytes, read_static(CLI_WRAPPER), read_static(MCP_WRAPPER)],
    )


def read_redacted_environment(path: Path | None = None) -> EnvEvidence:
    path = path or ENV_FILE
    if not path.exists() or path.is_symlink() or not path.is_file():
        return EnvEvidence("unknown", False, False)
    try:
        raw = path.read_bytes()
    except OSError:
        return EnvEvidence("unknown", False, False)
    if len(raw) > MAX_SMOKE_BYTES:
        return EnvEvidence("unknown", False, False)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return EnvEvidence("unknown", False, False)

    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")

    model_configured = all(
        values.get(key, "")
        for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "MODEL_NAME")
    )
    pdf_ocr_configured = values.get("FIRE_PDF_ENABLE", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    } and bool(values.get("FIRE_PDF_BASE_URL", ""))
    return EnvEvidence("configured", model_configured, pdf_ocr_configured)


def canonical_env_evidence(env: EnvEvidence) -> bytes:
    """Return non-secret environment evidence suitable for a stable digest."""

    return json.dumps(
        {
            "model_configured": env.model_configured,
            "pdf_ocr_configured": env.pdf_ocr_configured,
            "state": env.state,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def declared_package_spec(kind: str, value: str | None) -> tuple[bool, str | None]:
    if value is None:
        return False, None
    return bool(PACKAGE_PATTERNS[kind].fullmatch(value)), value


def select_smoke_evidence(
    smoke_file: Path | None,
    smoke_dir: Path,
    maximum_age_seconds: int,
    now: datetime,
) -> SmokeEvidence:
    candidates = (
        [smoke_file]
        if smoke_file
        else sorted(smoke_dir.glob("*-local-api-smoke.json"))
        if smoke_dir.is_dir()
        else []
    )
    candidates = [
        path for path in candidates if path and path.is_file() and not path.is_symlink()
    ]
    if not candidates:
        return SmokeEvidence("unknown", digest_bytes([]), None, frozenset())
    candidate = max(candidates, key=lambda path: path.stat().st_mtime)
    try:
        raw = candidate.read_bytes()
    except OSError:
        return SmokeEvidence("unknown", digest_bytes([]), None, frozenset())
    digest = digest_bytes([raw])
    if len(raw) > MAX_SMOKE_BYTES:
        return SmokeEvidence("unknown", digest, None, frozenset())
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return SmokeEvidence("unknown", digest, None, frozenset())
    observed_at = parse_timestamp(
        payload.get("observed_at") if isinstance(payload, dict) else None
    )
    if observed_at is None or observed_at > now + FUTURE_CLOCK_SKEW:
        return SmokeEvidence("unknown", digest, None, frozenset())
    passed = frozenset(
        item.get("name")
        for item in payload.get("results", [])
        if isinstance(item, dict)
        and item.get("status") == "pass"
        and isinstance(item.get("name"), str)
    )
    age = (now - observed_at).total_seconds()
    return SmokeEvidence(
        "fresh" if age <= maximum_age_seconds else "stale",
        digest,
        timestamp(observed_at),
        passed,
    )


def request_get(
    api_url: str, path: str, timeout_seconds: float, checked_at: str
) -> dict[str, Any]:
    url = urljoin(api_url + "/", path.lstrip("/"))
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    observation: dict[str, Any] = {
        "checked_at": checked_at,
        "result": "unreachable",
        "http_status": None,
        "safe_fields": {},
    }
    try:
        with open_get(request, timeout_seconds) as response:
            observation["http_status"] = response.status
            if response.status >= 400:
                observation["result"] = "http_error"
                return observation
            body = response.read(65_537)
    except HTTPError as exc:
        observation["result"] = "http_error"
        observation["http_status"] = exc.code
        return observation
    except (OSError, URLError, TimeoutError):
        return observation
    if len(body) > 65_536:
        observation["result"] = "invalid_response"
        return observation
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        observation["result"] = "invalid_response"
        return observation
    if path != "/" and (
        not isinstance(payload, dict) or payload.get("success") is not True
    ):
        observation["result"] = "invalid_response"
        return observation
    observation["result"] = "success"
    if path == "/":
        observation["safe_fields"] = {
            "firecrawl_identity": bool(
                isinstance(payload, dict) and payload.get("message") == "Firecrawl API"
            )
        }
    elif path == "/v2/team/queue-status":
        fields = {}
        if isinstance(payload, dict):
            for key, output_key in (
                ("jobsInQueue", "jobs_in_queue"),
                ("activeJobsInQueue", "active_jobs_in_queue"),
                ("waitingJobsInQueue", "waiting_jobs_in_queue"),
                ("maxConcurrency", "max_concurrency"),
            ):
                value = payload.get(key)
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                ):
                    fields[output_key] = value
        observation["safe_fields"] = fields
    else:
        crawls = payload.get("crawls") if isinstance(payload, dict) else None
        observation["safe_fields"] = (
            {"active_crawl_count": len(crawls)} if isinstance(crawls, list) else {}
        )
    return observation


def not_checked(checked_at: str) -> dict[str, Any]:
    return {
        "checked_at": checked_at,
        "result": "not_checked",
        "http_status": None,
        "safe_fields": {},
    }


def host_observations(
    api_url: str, timeout_seconds: float, offline: bool, checked_at: str
) -> dict[str, dict[str, Any]]:
    if offline:
        return {
            "api_root": not_checked(checked_at),
            "queue_status": not_checked(checked_at),
            "crawl_active": not_checked(checked_at),
        }
    return {
        "api_root": request_get(api_url, "/", timeout_seconds, checked_at),
        "queue_status": request_get(
            api_url, "/v2/team/queue-status", timeout_seconds, checked_at
        ),
        "crawl_active": request_get(
            api_url, "/v2/crawl/active", timeout_seconds, checked_at
        ),
    }


def capability(
    state: str,
    evidence_kind: str,
    checked_at: str,
    route_class: list[str],
    reason_code: str,
    smoke_state: str,
    optional_service_state: str = "not_applicable",
    model_capability_state: str = "not_applicable",
) -> dict[str, Any]:
    if state not in STATES:
        raise ValueError(f"Invalid state: {state}")
    return {
        "state": state,
        "evidence_kind": evidence_kind,
        "checked_at": checked_at,
        "route_class": route_class,
        "reason_code": reason_code,
        "smoke_evidence_state": smoke_state,
        "optional_service_state": optional_service_state,
        "model_capability_state": model_capability_state,
    }


def build_document(
    *,
    api_url: str = DEFAULT_API_URL,
    smoke_file: Path | None = None,
    smoke_dir: Path = DEFAULT_SMOKE_DIR,
    maximum_age_seconds: int = DEFAULT_MAX_EVIDENCE_AGE_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    offline: bool = False,
    cli_package_spec: str | None = None,
    mcp_package_spec: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    checked_at = timestamp(moment)
    route_classes, static_bytes = static_route_classes()
    smoke = select_smoke_evidence(smoke_file, smoke_dir, maximum_age_seconds, moment)
    env = read_redacted_environment()
    observations = host_observations(api_url, timeout_seconds, offline, checked_at)
    root_ready = observations["api_root"]["result"] == "success" and observations[
        "api_root"
    ]["safe_fields"].get("firecrawl_identity")
    queue_ready = (
        observations["queue_status"]["result"] == "success"
        and "jobs_in_queue" in observations["queue_status"]["safe_fields"]
    )
    active_ready = (
        observations["crawl_active"]["result"] == "success"
        and "active_crawl_count" in observations["crawl_active"]["safe_fields"]
    )
    cli_valid, cli_spec = declared_package_spec("cli", cli_package_spec)
    mcp_valid, mcp_spec = declared_package_spec("mcp", mcp_package_spec)

    if not route_classes["base_http"]:
        base = capability(
            "unavailable",
            "static_route",
            checked_at,
            [],
            "root_route_not_declared",
            smoke.state,
        )
    elif observations["api_root"]["result"] == "not_checked":
        base = capability(
            "unknown",
            "static_route",
            checked_at,
            route_classes["base_http"],
            "host_get_not_checked",
            smoke.state,
        )
    elif root_ready:
        base = capability(
            "ready",
            "host_get",
            checked_at,
            route_classes["base_http"],
            "root_get_confirmed",
            smoke.state,
        )
    else:
        base = capability(
            "unavailable",
            "host_get",
            checked_at,
            route_classes["base_http"],
            "root_get_not_confirmed",
            smoke.state,
        )

    required_async_routes = 4
    if len(route_classes["async_jobs"]) != required_async_routes:
        async_jobs = capability(
            "unavailable",
            "static_route",
            checked_at,
            route_classes["async_jobs"],
            "async_routes_not_declared",
            smoke.state,
        )
    elif smoke.state == "stale":
        async_jobs = capability(
            "stale",
            "smoke",
            checked_at,
            route_classes["async_jobs"],
            "smoke_evidence_stale",
            smoke.state,
        )
    elif smoke.state != "fresh":
        async_jobs = capability(
            "unknown",
            "smoke",
            checked_at,
            route_classes["async_jobs"],
            "smoke_evidence_unknown",
            smoke.state,
        )
    elif not (queue_ready and active_ready):
        async_jobs = capability(
            "unavailable",
            "smoke_and_host_get",
            checked_at,
            route_classes["async_jobs"],
            "queue_get_not_confirmed",
            smoke.state,
        )
    elif observations["crawl_active"]["safe_fields"].get("active_crawl_count") != 0:
        async_jobs = capability(
            "degraded",
            "smoke_and_host_get",
            checked_at,
            route_classes["async_jobs"],
            "active_crawls_present",
            smoke.state,
        )
    else:
        async_jobs = capability(
            "degraded",
            "smoke_and_host_get",
            checked_at,
            route_classes["async_jobs"],
            "smoke_producer_contract_untrusted",
            smoke.state,
        )

    def package_capability(kind: str, spec: str | None, valid: bool) -> dict[str, Any]:
        routes = route_classes[kind]
        if not routes:
            return capability(
                "unavailable",
                "static_route",
                checked_at,
                routes,
                "wrapper_not_declared",
                "not_applicable",
            )
        if spec is None:
            return capability(
                "unknown",
                "static_route",
                checked_at,
                routes,
                "immutable_package_spec_missing",
                "not_applicable",
            )
        if not valid:
            return capability(
                "unavailable",
                "declared_package_spec",
                checked_at,
                routes,
                "immutable_package_spec_invalid",
                "not_applicable",
            )
        return capability(
            "degraded",
            "declared_package_spec",
            checked_at,
            routes,
            "immutable_package_spec_declared_not_doctor_verified",
            "not_applicable",
        )

    cli = package_capability("cli", cli_spec, cli_valid)
    mcp = package_capability("mcp", mcp_spec, mcp_valid)

    if not route_classes["ai_formats"]:
        ai_formats = capability(
            "unavailable",
            "static_route",
            checked_at,
            [],
            "ai_routes_not_declared",
            "not_applicable",
            "not_applicable",
            "unavailable",
        )
    elif env.state != "configured":
        ai_formats = capability(
            "unknown",
            "redacted_env",
            checked_at,
            route_classes["ai_formats"],
            "model_configuration_not_readable",
            "not_applicable",
            "unknown",
            "unknown",
        )
    elif env.model_configured:
        ai_formats = capability(
            "degraded",
            "redacted_env",
            checked_at,
            route_classes["ai_formats"],
            "model_configured_but_not_read_only_verified",
            "not_applicable",
            "configured",
            "configured",
        )
    else:
        ai_formats = capability(
            "unavailable",
            "redacted_env",
            checked_at,
            route_classes["ai_formats"],
            "model_configuration_missing",
            "not_applicable",
            "unconfigured",
            "unconfigured",
        )

    if not route_classes["pdf_ocr"]:
        pdf_ocr = capability(
            "unavailable",
            "static_route",
            checked_at,
            [],
            "pdf_parse_route_not_declared",
            "not_applicable",
            "unavailable",
        )
    elif env.state != "configured":
        pdf_ocr = capability(
            "unknown",
            "redacted_env",
            checked_at,
            route_classes["pdf_ocr"],
            "ocr_configuration_not_readable",
            "not_applicable",
            "unknown",
        )
    elif env.pdf_ocr_configured:
        pdf_ocr = capability(
            "degraded",
            "redacted_env",
            checked_at,
            route_classes["pdf_ocr"],
            "ocr_configured_but_not_read_only_verified",
            "not_applicable",
            "configured",
        )
    else:
        pdf_ocr = capability(
            "unavailable",
            "redacted_env",
            checked_at,
            route_classes["pdf_ocr"],
            "ocr_optional_service_unconfigured",
            "not_applicable",
            "unconfigured",
        )

    declared_specs = {}
    if cli_valid and cli_spec:
        declared_specs["cli"] = cli_spec
    if mcp_valid and mcp_spec:
        declared_specs["mcp"] = mcp_spec
    document = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": checked_at,
        "max_evidence_age_seconds": maximum_age_seconds,
        "evidence_digest": digest_bytes(
            static_bytes + [smoke.digest.encode("ascii"), canonical_env_evidence(env)]
        ),
        "capabilities": {
            "base_http": base,
            "async_jobs": async_jobs,
            "cli": cli,
            "mcp": mcp,
            "ai_formats": ai_formats,
            "pdf_ocr": pdf_ocr,
        },
        "host_observations": observations,
        "declared_package_specs": declared_specs,
    }
    validate_document(document)
    return document


def schema_path() -> Path:
    return Path(__file__).with_suffix(".schema.json")


def validate_document(document: Any) -> None:
    schema = json.loads(schema_path().read_text(encoding="utf-8"))

    def validate(value: Any, rule: dict[str, Any], location: str) -> None:
        if "$ref" in rule:
            reference = rule["$ref"]
            if not reference.startswith("#/$defs/"):
                raise ValueError(f"Unsupported schema reference at {location}")
            validate(value, schema["$defs"][reference.rsplit("/", 1)[1]], location)
            return
        expected_type = rule.get("type")
        if expected_type is not None:
            expected = (
                expected_type if isinstance(expected_type, list) else [expected_type]
            )
            type_matches = {
                "object": isinstance(value, dict),
                "array": isinstance(value, list),
                "string": isinstance(value, str),
                "integer": isinstance(value, int) and not isinstance(value, bool),
                "boolean": isinstance(value, bool),
                "null": value is None,
            }
            if not any(type_matches.get(item, False) for item in expected):
                raise ValueError(f"Schema type mismatch at {location}")
        if "const" in rule and value != rule["const"]:
            raise ValueError(f"Schema const mismatch at {location}")
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError(f"Schema enum mismatch at {location}")
        if isinstance(value, int) and "minimum" in rule and value < rule["minimum"]:
            raise ValueError(f"Schema minimum mismatch at {location}")
        if isinstance(value, str):
            if "pattern" in rule and re.fullmatch(rule["pattern"], value) is None:
                raise ValueError(f"Schema pattern mismatch at {location}")
            if rule.get("format") == "date-time" and parse_timestamp(value) is None:
                raise ValueError(f"Schema date-time mismatch at {location}")
        if isinstance(value, list):
            if len(value) < rule.get("minItems", 0):
                raise ValueError(f"Schema minItems mismatch at {location}")
            item_rule = rule.get("items")
            if item_rule:
                for index, item in enumerate(value):
                    validate(item, item_rule, f"{location}[{index}]")
        if isinstance(value, dict):
            properties = rule.get("properties", {})
            for name in rule.get("required", []):
                if name not in value:
                    raise ValueError(
                        f"Schema required key missing at {location}.{name}"
                    )
            if rule.get("additionalProperties") is False and set(value) - set(
                properties
            ):
                raise ValueError(f"Schema unexpected key at {location}")
            for name, item in value.items():
                if name in properties:
                    validate(item, properties[name], f"{location}.{name}")

    validate(document, schema, "$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--api-url", default=DEFAULT_API_URL, type=validate_local_api_url
    )
    parser.add_argument(
        "--smoke-file",
        type=Path,
        help="Read one existing smoke JSON artifact without executing it.",
    )
    parser.add_argument(
        "--smoke-dir",
        type=Path,
        default=DEFAULT_SMOKE_DIR,
        help="Directory containing existing smoke artifacts.",
    )
    parser.add_argument(
        "--max-evidence-age-seconds",
        type=nonnegative_int,
        default=DEFAULT_MAX_EVIDENCE_AGE_SECONDS,
    )
    parser.add_argument(
        "--timeout-seconds", type=bounded_timeout, default=DEFAULT_TIMEOUT_SECONDS
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Perform static/evidence inspection only; make no HTTP request.",
    )
    parser.add_argument(
        "--cli-package-spec",
        help="Optional immutable firecrawl-cli@x.y.z declaration; never resolved.",
    )
    parser.add_argument(
        "--mcp-package-spec",
        help="Optional immutable firecrawl-mcp@x.y.z declaration; never resolved.",
    )
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="CAPABILITY",
        help="Fail closed unless a named capability is ready. May repeat.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    unknown = sorted(set(args.require) - set(CAPABILITIES))
    if unknown:
        parser.error(f"Unknown capability for --require: {', '.join(unknown)}")
    document = build_document(
        api_url=args.api_url,
        smoke_file=args.smoke_file,
        smoke_dir=args.smoke_dir,
        maximum_age_seconds=args.max_evidence_age_seconds,
        timeout_seconds=args.timeout_seconds,
        offline=args.offline,
        cli_package_spec=args.cli_package_spec,
        mcp_package_spec=args.mcp_package_spec,
    )
    print(json.dumps(document, separators=(",", ":"), sort_keys=True))
    unmet = [
        name
        for name in args.require
        if document["capabilities"][name]["state"] != "ready"
    ]
    if unmet:
        print(f"Required capability is not ready: {', '.join(unmet)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
