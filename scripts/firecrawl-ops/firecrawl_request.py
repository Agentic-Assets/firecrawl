#!/usr/bin/env python3
"""Agent-friendly direct HTTP helper for the local Firecrawl API."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import mimetypes
import os
import re
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
    urlopen,
)

DEFAULT_API_URL = "http://localhost:3002"
QUEUE_STATUS_FIELDS = (
    "jobsInQueue",
    "activeJobsInQueue",
    "waitingJobsInQueue",
    "maxConcurrency",
)
CRAWL_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
CRAWL_FAILURE_STATUSES = {"failed", "cancelled"}
AGENT_SAFE_COMMANDS = {"health", "scrape", "map", "crawl", "parse"}
AGENT_SAFE_ORIGINS = {
    "localhost": "http://localhost:3002",
    "127.0.0.1": "http://127.0.0.1:3002",
    "::1": "http://[::1]:3002",
}
AGENT_SAFE_TARGET_URL = "https://example.com/"
AGENT_SAFE_PARSE_FILE = Path("apps/test-site/public/example.pdf")
AGENT_SAFE_PARSE_SHA256 = (
    "f6edcd8a1b4f7cb85486d0c6777f9174eadbc4d1d0d9e5aeba7132f30b34bc3e"
)
AGENT_SAFE_EVIDENCE_RELATIVE = Path("tasks/agentic-2279/evidence")
AGENT_SAFE_MAX_EVIDENCE_AGE_SECONDS = 45
AGENT_SAFE_REQUEST_TIMEOUT_SECONDS = 5.0
AGENT_SAFE_CRAWL_POLL_TIMEOUT_SECONDS = 30.0
AGENT_SAFE_CRAWL_POLL_INTERVAL_SECONDS = 1.0
AGENT_SAFE_CRAWL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
AGENT_SAFE_CRAWL_STATUSES = {"queued", "scraping", "completed", "failed", "cancelled"}
AGENT_SAFE_OUTCOMES = {
    "success",
    "http_rejected",
    "invalid_response",
    "unknown_submit",
    "poll_timeout",
    "crawl_failed",
    "crawl_cancelled",
    "queue_not_idle",
    "transport_unreachable",
}
AGENT_SAFE_REASON_CODES = {
    "success": "http_success",
    "http_rejected": "http_rejected",
    "invalid_response": "invalid_response",
    "unknown_submit": "unknown_submit",
    "poll_timeout": "poll_timeout",
    "crawl_failed": "crawl_failed",
    "crawl_cancelled": "crawl_cancelled",
    "queue_not_idle": "queue_not_idle",
    "transport_unreachable": "transport_unreachable",
}
AGENT_SAFE_SCRAPE_FORMATS = {"markdown"}
AGENT_SAFE_PARSE_FORMATS = {"markdown"}
AGENT_SAFE_RECIPE_EXAMPLE = (
    "crawl https://example.com/ --agent-safe --metrics-only --timeout 5 "
    "--receipt-dir tasks/agentic-2279/evidence "
    "--limit 1 --max-concurrency 1 --include-paths / --scrape-formats markdown "
    "--wait --poll-timeout 30 --poll-interval 1"
)
REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_SAFE_EVIDENCE_DIR = REPO_ROOT / AGENT_SAFE_EVIDENCE_RELATIVE
TOOLING_MANIFEST_PATH = (
    REPO_ROOT / "scripts/firecrawl-ops/firecrawl_tooling_compatibility.json"
)


class AgentSafeViolation(ValueError):
    """Raised before safe-mode commands can perform a side effect."""


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Expected a boolean, got {value!r}")


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Expected a positive integer, got {value!r}"
        ) from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"Expected a positive integer, got {value!r}")
    return parsed


def finite_positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Expected a finite positive number, got {value!r}"
        ) from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError(
            f"Expected a finite positive number, got {value!r}"
        )
    return parsed


def finite_nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Expected a finite nonnegative number, got {value!r}"
        ) from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError(
            f"Expected a finite nonnegative number, got {value!r}"
        )
    return parsed


def load_json_arg(value: str | None, *, label: str) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON for {label}: {exc}") from exc


def load_json_file(path: str | None, *, label: str) -> Any:
    if path is None:
        return None
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError as exc:
        raise SystemExit(f"Missing {label} file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {label} file {path}: {exc}") from exc


def load_headers_file(path: str | None) -> dict[str, Any] | None:
    headers = load_json_file(path, label="--headers-file")
    if headers is None:
        return None
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) for key in headers
    ):
        raise SystemExit(
            f"--headers-file must contain a JSON object with string keys: {path}"
        )
    return headers


def slugify(value: str) -> str:
    value = re.sub(r"https?://", "", value.strip())
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    value = value.strip(".-_")
    return value[:80] or "firecrawl"


def timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def build_url(api_url: str, path: str) -> str:
    if path.startswith(("http://", "https://")):
        return path
    return urljoin(api_url.rstrip("/") + "/", path.lstrip("/"))


def agent_safe_error(message: str) -> AgentSafeViolation:
    return AgentSafeViolation(f"{message}. Safe example: {AGENT_SAFE_RECIPE_EXAMPLE}")


def canonical_agent_safe_origin(value: str) -> str:
    """Validate an API origin without allowing a safe run to leave loopback."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise agent_safe_error(
            "--api-url must be a canonical loopback HTTP origin on port 3002"
        ) from exc

    if (
        parsed.scheme != "http"
        or parsed.hostname not in AGENT_SAFE_ORIGINS
        or port != 3002
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise agent_safe_error(
            "--api-url must be localhost, 127.0.0.1, or ::1 on HTTP port 3002"
        )
    return AGENT_SAFE_ORIGINS[parsed.hostname]


def require_agent_safe_target(value: str) -> None:
    """This temporary surface permits only the reviewed public fixture URL."""
    if value != AGENT_SAFE_TARGET_URL:
        raise agent_safe_error(
            "--agent-safe permits only the canonical https://example.com/ fixture URL"
        )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def agent_safe_format_values(
    value: str | None, *, allowed: set[str], label: str
) -> list[str]:
    formats = parse_csv(value)
    if len(allowed) != 1 or formats != [next(iter(allowed))]:
        allowed_text = ", ".join(sorted(allowed))
        raise agent_safe_error(f"{label} requires exactly the {allowed_text} format")
    return formats


def require_safe_crawl_id(value: str) -> None:
    if not AGENT_SAFE_CRAWL_ID_PATTERN.fullmatch(value):
        raise agent_safe_error("crawl id must contain only safe identifier characters")


def agent_safe_bounds(args: argparse.Namespace) -> dict[str, int | float]:
    bounds: dict[str, int | float] = {
        "request_timeout_seconds": AGENT_SAFE_REQUEST_TIMEOUT_SECONDS
    }
    if args.command == "map":
        bounds["limit"] = args.limit
    elif args.command == "crawl":
        bounds.update(
            {
                "limit": args.limit,
                "max_concurrency": args.max_concurrency,
                "include_path_count": len(parse_csv(args.include_paths)),
                "poll_timeout_seconds": args.poll_timeout,
                "poll_interval_seconds": args.poll_interval,
            }
        )
    elif args.command == "parse":
        bounds["max_pages"] = args.max_pages
    return bounds


def agent_safe_input_class(args: argparse.Namespace) -> str:
    """Validate the fixed input class without retaining a caller-controlled reference."""
    if args.command == "parse":
        if args.file != AGENT_SAFE_PARSE_FILE.as_posix():
            raise agent_safe_error(
                "safe parse permits only the tracked synthetic PDF fixture"
            )
        fixture = REPO_ROOT / AGENT_SAFE_PARSE_FILE
        if (
            not fixture.is_file()
            or fixture.is_symlink()
            or sha256_file(fixture) != AGENT_SAFE_PARSE_SHA256
        ):
            raise agent_safe_error(
                "the tracked synthetic PDF fixture is unavailable or has an unexpected digest"
            )
        return "synthetic_pdf_fixture"
    if args.command in {"scrape", "map", "crawl"}:
        return "public_example_fixture"
    return "loopback_health"


def strict_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value
    ):
        return None
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None


def require_fresh_observation(value: object, *, label: str) -> None:
    observed_at = strict_utc_timestamp(value)
    if observed_at is None:
        raise agent_safe_error(f"{label} must carry a strict UTC observed_at timestamp")
    age = (datetime.now(UTC) - observed_at).total_seconds()
    if age < 0 or age > AGENT_SAFE_MAX_EVIDENCE_AGE_SECONDS:
        raise agent_safe_error(
            f"{label} must be no more than {AGENT_SAFE_MAX_EVIDENCE_AGE_SECONDS} seconds old"
        )


def require_recorded_observation(value: object, *, label: str) -> None:
    """Validate durable provenance without expiring an historical receipt."""
    observed_at = strict_utc_timestamp(value)
    if observed_at is None or observed_at > datetime.now(UTC):
        raise ValueError(
            f"{label} must carry a nonfuture strict UTC observed_at timestamp"
        )


def is_zero_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == 0


def prefixed_sha256(value: bytes) -> str:
    return f"sha256:{sha256_bytes(value)}"


def canonical_json_digest(payload: dict[str, Any]) -> str:
    return prefixed_sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def local_ops_module(name: str) -> Any:
    """Load only the checked-in local prerequisite producer in the helper process."""
    ops_directory = str(Path(__file__).resolve().parent)
    if ops_directory not in sys.path:
        sys.path.insert(0, ops_directory)
    return importlib.import_module(name)


def run_agent_safe_prerequisites(args: argparse.Namespace) -> dict[str, dict[str, str]]:
    """Produce fresh local evidence; caller files are not an authorization input."""
    try:
        preflight = local_ops_module("local_agent_preflight")
        document = preflight.build_document(
            api_url=args.api_url,
            maximum_age_seconds=AGENT_SAFE_MAX_EVIDENCE_AGE_SECONDS,
            timeout_seconds=AGENT_SAFE_REQUEST_TIMEOUT_SECONDS,
        )
        preflight.validate_document(document)
        observed_at = document.get("observed_at")
        require_recorded_observation(observed_at, label="local preflight")
        capabilities = document.get("capabilities")
        observations = document.get("host_observations")
        queue = (
            observations.get("queue_status") if isinstance(observations, dict) else None
        )
        active = (
            observations.get("crawl_active") if isinstance(observations, dict) else None
        )
        queue_fields = queue.get("safe_fields") if isinstance(queue, dict) else None
        active_fields = active.get("safe_fields") if isinstance(active, dict) else None
        if (
            not isinstance(capabilities, dict)
            or not isinstance(capabilities.get("base_http"), dict)
            or capabilities["base_http"].get("state") != "ready"
            or not isinstance(queue, dict)
            or not isinstance(active, dict)
            or queue.get("result") != "success"
            or active.get("result") != "success"
            or not isinstance(queue_fields, dict)
            or not isinstance(active_fields, dict)
            or not is_zero_count(queue_fields.get("jobs_in_queue"))
            or not is_zero_count(active_fields.get("active_crawl_count"))
        ):
            raise agent_safe_error("fresh local preflight is not known ready and idle")
        evidence_digest = document.get("evidence_digest")
        if not isinstance(evidence_digest, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", evidence_digest
        ):
            raise agent_safe_error(
                "fresh local preflight has an invalid body-free digest"
            )

        doctor = local_ops_module("firecrawl_compatibility_doctor")
        manifest = doctor.load_manifest()
        doctor_result = doctor.agent_safe_result(manifest, api_url=args.api_url)
        doctor_observed_at = doctor_result.get("observed_at")
        require_recorded_observation(
            doctor_observed_at, label="agent-safe compatibility"
        )
        checks = doctor_result.get("checks")
        if (
            doctor_result.get("schema_version") != 1
            or doctor_result.get("kind") != "firecrawl-agent-safe-compatibility"
            or doctor_result.get("mode") != "normal"
            or doctor_result.get("status") != "pass"
            or doctor_result.get("body_bytes_persisted") != 0
            or doctor_result.get("manifest_sha256") != manifest.sha256
            or not isinstance(checks, dict)
            or not all(
                isinstance(checks.get(name), dict)
                and checks[name].get("status") == "pass"
                for name in ("api", "cli", "mcp")
            )
        ):
            raise agent_safe_error("agent-safe read-only compatibility did not pass")
    except AgentSafeViolation:
        raise
    except Exception as exc:
        raise agent_safe_error("fresh local prerequisite execution failed") from exc
    return {
        "preflight": {"evidence_digest": evidence_digest, "observed_at": observed_at},
        "compatibility_doctor": {
            "evidence_digest": canonical_json_digest(doctor_result),
            "observed_at": doctor_observed_at,
        },
    }


def validate_agent_safe_args(args: argparse.Namespace) -> None:
    """Enforce the narrow pilot contract before HTTP or output work."""
    if args.command not in AGENT_SAFE_COMMANDS:
        raise agent_safe_error(f"{args.command} is not available with --agent-safe")
    if not args.metrics_only:
        raise agent_safe_error("--agent-safe requires --metrics-only")
    if args.receipt_dir != AGENT_SAFE_EVIDENCE_RELATIVE.as_posix():
        raise agent_safe_error(
            "--agent-safe requires the fixed tasks/agentic-2279/evidence receipt directory"
        )
    # Inspect supplied values explicitly: false/zero/empty values must not bypass a fixed recipe.
    if (
        args.out is not None
        or args.out_dir is not None
        or args.save_fields is not None
        or args.unwrap is True
        or args.print_paths is True
    ):
        raise agent_safe_error(
            "--agent-safe rejects raw output, saved paths, and non-pilot controls"
        )
    strict_agent_safe_directory(create=False)
    if args.timeout != AGENT_SAFE_REQUEST_TIMEOUT_SECONDS:
        raise agent_safe_error(
            f"--agent-safe requires --timeout {int(AGENT_SAFE_REQUEST_TIMEOUT_SECONDS)}"
        )

    origin = canonical_agent_safe_origin(args.api_url)
    if args.command == "scrape":
        require_agent_safe_target(args.url)
        agent_safe_format_values(
            args.formats, allowed=AGENT_SAFE_SCRAPE_FORMATS, label="safe scrape"
        )
        if (
            args.schema is not None
            or args.schema_file is not None
            or args.query is not None
            or args.summary is True
            or args.wait_for is not None
            or args.proxy is not None
            or args.headers_file is not None
            or args.user_agent is not None
            or args.only_main_content is not None
            or args.country is not None
            or args.languages is not None
            or args.max_age is not None
        ):
            raise agent_safe_error(
                "--agent-safe scrape rejects AI, proxy, header, and delayed-render options"
            )
        if args.prompt != "Extract the requested fields.":
            raise agent_safe_error("--agent-safe scrape rejects prompt-backed options")
    elif args.command == "map":
        require_agent_safe_target(args.url)
        if (
            args.limit != 1
            or args.search is not None
            or args.sitemap is not None
            or args.include_subdomains is True
        ):
            raise agent_safe_error(
                "--agent-safe map requires exactly --limit 1 without discovery-expansion options"
            )
    elif args.command == "crawl":
        require_agent_safe_target(args.url)
        include_paths = parse_csv(args.include_paths)
        if (
            not args.wait
            or args.limit != 1
            or args.max_concurrency != 1
            or include_paths != ["/"]
            or args.poll_timeout != AGENT_SAFE_CRAWL_POLL_TIMEOUT_SECONDS
            or args.poll_interval != AGENT_SAFE_CRAWL_POLL_INTERVAL_SECONDS
        ):
            raise agent_safe_error(
                "--agent-safe crawl requires --wait, limit 1, concurrency 1, include path /, poll timeout 30, and interval 1"
            )
        agent_safe_format_values(
            args.scrape_formats, allowed=AGENT_SAFE_SCRAPE_FORMATS, label="safe crawl"
        )
        if (
            args.exclude_paths is not None
            or args.headers_file is not None
            or args.user_agent is not None
        ):
            raise agent_safe_error(
                "--agent-safe crawl rejects exclude paths and arbitrary request headers"
            )
    elif args.command == "parse":
        agent_safe_format_values(
            args.formats, allowed=AGENT_SAFE_PARSE_FORMATS, label="safe parse"
        )
        if (
            args.pdf_mode != "fast"
            or args.max_pages != 1
            or args.fire_pdf_async
            or args.no_pdf_parse
            or args.query is not None
            or args.only_main_content is not None
            or args.include_tags is not None
            or args.exclude_tags is not None
        ):
            raise agent_safe_error(
                "--agent-safe parse requires --pdf-mode fast, --max-pages, and no AI or OCR controls"
            )

    input_class = agent_safe_input_class(args)
    # All validation has passed. Only now can later commands use the canonical origin or write receipts.
    args.api_url = origin
    args.agent_safe_context = {
        "receipt_dir": AGENT_SAFE_EVIDENCE_DIR,
        "run_id": uuid.uuid4().hex,
        "started_at": time.monotonic(),
        "input_class": input_class,
        "bounds": agent_safe_bounds(args),
        "prerequisite_digests": {},
    }


def request_json(
    api_url: str,
    method: str,
    path: str,
    body: Any | None,
    api_key: str | None,
    timeout: float,
    *,
    agent_safe: bool = False,
) -> tuple[int, bytes]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = Request(
        build_url(api_url, path), data=data, headers=headers, method=method.upper()
    )
    if agent_safe:
        return open_request(req, timeout, agent_safe=True)
    return open_request(req, timeout)


def request_multipart(
    api_url: str,
    path: str,
    fields: dict[str, str],
    files: dict[str, Path],
    api_key: str | None,
    timeout: float,
    *,
    agent_safe: bool = False,
) -> tuple[int, bytes]:
    boundary = f"----firecrawl-local-{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\r\n")

    for name, path_obj in files.items():
        if not path_obj.is_file():
            raise SystemExit(f"Missing upload file: {path_obj}")
        content_type = (
            mimetypes.guess_type(str(path_obj))[0] or "application/octet-stream"
        )
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{path_obj.name}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
        )
        chunks.append(path_obj.read_bytes())
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}--\r\n".encode())
    data = b"".join(chunks)
    headers = {
        "Accept": "application/json",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(data)),
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = Request(build_url(api_url, path), data=data, headers=headers, method="POST")
    if agent_safe:
        return open_request(req, timeout, agent_safe=True)
    return open_request(req, timeout)


class AgentSafeNoRedirectHandler(HTTPRedirectHandler):
    """Reject a redirect rather than allowing a pilot request to leave loopback."""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def open_request(
    req: Request, timeout_value: float, *, agent_safe: bool = False
) -> tuple[int, bytes]:
    try:
        opener = (
            build_opener(ProxyHandler({}), AgentSafeNoRedirectHandler())
            if agent_safe
            else None
        )
        with (
            opener.open(req, timeout=timeout_value)
            if opener
            else urlopen(req, timeout=timeout_value)
        ) as resp:
            return resp.status, resp.read()
    except HTTPError as exc:
        body = exc.read()
        if not agent_safe:
            sys.stderr.write(f"HTTP {exc.code} from {req.full_url}\n")
        return exc.code, body
    except URLError as exc:
        if agent_safe:
            raise SystemExit("agent_safe_transport_error") from exc
        raise SystemExit(f"Could not reach {req.full_url}: {exc}") from exc


def decode_json_or_bytes(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body


def response_payload(result: Any) -> Any:
    if isinstance(result, dict) and isinstance(result.get("data"), dict):
        return result["data"]
    return result


def response_metrics(result: Any, http_status: int) -> dict[str, Any]:
    """Return stable response facts without serializing scraped source content."""
    root = result if isinstance(result, dict) else {}
    payload = response_payload(result)
    data = payload if isinstance(payload, dict) else {}
    metrics: dict[str, Any] = {
        "success": bool(root.get("success", http_status < 400)),
        "httpStatus": http_status,
    }
    identifier = (
        root.get("id") or root.get("jobId") or data.get("id") or data.get("jobId")
    )
    if isinstance(identifier, str):
        metrics["id"] = identifier
    for key in (
        "status",
        "total",
        "completed",
        "failed",
        "creditsUsed",
        "jobsInQueue",
        "activeJobsInQueue",
        "waitingJobsInQueue",
        "maxConcurrency",
        "apiHttpStatus",
        "queueHttpStatus",
    ):
        value = root.get(key, data.get(key))
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            metrics[key] = value
    if isinstance(data.get("markdown"), str):
        metrics["markdownChars"] = len(data["markdown"])
    for key, metric_key in (
        ("links", "linksCount"),
        ("images", "imagesCount"),
        ("data", "dataCount"),
    ):
        value = data.get(key, root.get(key))
        if isinstance(value, list):
            metrics[metric_key] = len(value)
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        for key in ("numPages", "totalPages"):
            value = metadata.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                metrics[key] = value
    return metrics


def agent_safe_http_status(value: int) -> int:
    """Keep only a bounded numeric status in the safe metrics contract."""
    return value if isinstance(value, int) and 100 <= value <= 599 else 0


def agent_safe_metrics(
    args: argparse.Namespace, result: Any, http_status: int
) -> dict[str, Any]:
    """Project a finite, per-recipe metric schema with no server strings or IDs."""
    outcome = getattr(args, "agent_safe_outcome", None)
    if outcome not in AGENT_SAFE_OUTCOMES:
        outcome = "success" if 200 <= http_status < 300 else "http_rejected"
    metrics: dict[str, Any] = {
        "schema_version": "agent-safe-metrics-v1",
        "recipe": args.command,
        "outcome": outcome,
        "http_status": agent_safe_http_status(http_status),
    }
    payload = response_payload(result)
    data = payload if isinstance(payload, dict) else {}
    if args.command in {"scrape", "parse"} and isinstance(data.get("markdown"), str):
        metrics["markdown_chars"] = len(data["markdown"])
    if args.command == "map":
        links = data.get(
            "links", result.get("links") if isinstance(result, dict) else None
        )
        if isinstance(links, list):
            metrics["links_count"] = len(links)
    if args.command == "parse":
        metadata = data.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("numPages"), int):
            metrics["pages"] = min(1, max(0, metadata["numPages"]))
    if args.command == "crawl":
        state = getattr(args, "agent_safe_crawl_state", None)
        if state in AGENT_SAFE_CRAWL_STATUSES:
            metrics["crawl_state"] = state
    return metrics


def agent_safe_failure(
    args: argparse.Namespace, outcome: str, *, status: int = 0
) -> None:
    """Stop after an allowed safe request with a finite, redacted failure code."""
    args.agent_safe_outcome = outcome
    write_response(args, {}, b"", status, args.command)
    raise SystemExit(f"agent_safe_{outcome}")


def ensure_agent_safe_post_ready(args: argparse.Namespace) -> None:
    """Run fresh trusted prerequisites, then recheck the shared host before POST."""
    if not getattr(args, "agent_safe", False):
        return
    try:
        args.agent_safe_context["prerequisite_digests"] = run_agent_safe_prerequisites(
            args
        )
        prerequisites = args.agent_safe_context["prerequisite_digests"]
        preflight = (
            prerequisites.get("preflight") if isinstance(prerequisites, dict) else None
        )
        compatibility = (
            prerequisites.get("compatibility_doctor")
            if isinstance(prerequisites, dict)
            else None
        )
        require_fresh_observation(
            preflight.get("observed_at") if isinstance(preflight, dict) else None,
            label="fresh local preflight at execution gate",
        )
        require_fresh_observation(
            compatibility.get("observed_at")
            if isinstance(compatibility, dict)
            else None,
            label="fresh agent-safe compatibility at execution gate",
        )
    except AgentSafeViolation as exc:
        raise SystemExit("agent_safe_prerequisite_failed") from exc
    args.agent_safe_request_started = True
    queue_status, queue_raw = request_json(
        args.api_url,
        "GET",
        "/v2/team/queue-status",
        None,
        args.api_key,
        args.timeout,
        agent_safe=True,
    )
    active_status, active_raw = request_json(
        args.api_url,
        "GET",
        "/v2/crawl/active",
        None,
        args.api_key,
        args.timeout,
        agent_safe=True,
    )
    queue = decode_json_or_bytes(queue_raw)
    active = decode_json_or_bytes(active_raw)
    queue_is_idle = (
        200 <= queue_status < 300
        and isinstance(queue, dict)
        and queue.get("success") is True
        and is_zero_count(queue.get("jobsInQueue"))
    )
    active_crawls = active.get("crawls") if isinstance(active, dict) else None
    active_is_idle = (
        200 <= active_status < 300
        and isinstance(active, dict)
        and active.get("success") is True
        and isinstance(active_crawls, list)
        and not active_crawls
    )
    if not queue_is_idle or not active_is_idle:
        agent_safe_failure(args, "queue_not_idle", status=0)


def write_outputs(
    result: Any,
    raw_body: bytes,
    *,
    out: str | None,
    out_dir: str | None,
    basename: str,
    pretty: bool,
    save_fields: str | None,
    quiet: bool,
) -> list[Path]:
    written: list[Path] = []
    output_bytes = format_result(result, raw_body, pretty=pretty)

    output_path: Path | None = None
    if out:
        output_path = Path(out)
    elif out_dir:
        output_path = Path(out_dir) / f"{timestamp()}-{slugify(basename)}.json"

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(output_bytes)
        written.append(output_path)
    elif not quiet:
        sys.stdout.buffer.write(output_bytes)
        if not output_bytes.endswith(b"\n"):
            sys.stdout.buffer.write(b"\n")

    if save_fields:
        field_dir = Path(save_fields)
        field_dir.mkdir(parents=True, exist_ok=True)
        payload = response_payload(result)
        if isinstance(payload, dict):
            field_specs = {
                "markdown": "markdown.md",
                "html": "html.html",
                "rawHtml": "raw.html",
                "links": "links.json",
                "images": "images.json",
                "metadata": "metadata.json",
                "json": "structured.json",
                "summary": "summary.txt",
                "query": "query.json",
            }
            for key, filename in field_specs.items():
                if key not in payload or payload[key] is None:
                    continue
                value = payload[key]
                target = field_dir / filename
                if isinstance(value, (dict, list)):
                    target.write_text(
                        json.dumps(value, indent=2, ensure_ascii=False) + "\n"
                    )
                else:
                    text = str(value)
                    target.write_text(text if text.endswith("\n") else text + "\n")
                written.append(target)
        else:
            target = field_dir / "response.bin"
            target.write_bytes(raw_body)
            written.append(target)

    return written


def write_response(
    args: argparse.Namespace,
    result: Any,
    raw: bytes,
    status: int,
    basename: str,
    crawl_id: str | None = None,
) -> None:
    metrics_only = getattr(args, "metrics_only", False)
    unwrap = getattr(args, "unwrap", False)

    if getattr(args, "agent_safe", False):
        output_result = agent_safe_metrics(args, result, status)
    elif metrics_only:
        output_result = response_metrics(result, status)
        if crawl_id:
            output_result["id"] = crawl_id
    elif unwrap:
        output_result = response_payload(result)
    else:
        output_result = result

    if (
        status >= 400
        and not metrics_only
        and not getattr(args, "agent_safe", False)
        and raw
    ):
        sys.stderr.write(raw.decode("utf-8", errors="replace") + "\n")

    written = write_outputs(
        output_result,
        raw,
        out=args.out,
        out_dir=args.out_dir,
        basename=args.basename or basename,
        pretty=args.pretty,
        save_fields=args.save_fields,
        quiet=args.quiet,
    )
    if args.print_paths:
        for item in written:
            print(item, file=sys.stderr)
    if getattr(args, "agent_safe", False):
        if not isinstance(output_result, dict):
            raise RuntimeError("agent-safe output must be projected metrics")
        write_agent_safe_receipt(args, output_result)


def format_result(result: Any, raw_body: bytes, *, pretty: bool) -> bytes:
    if isinstance(result, (dict, list)):
        if pretty:
            return (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode(
                "utf-8"
            )
        return (
            json.dumps(result, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
    return raw_body


def agent_safe_disposition(command: str, metrics: dict[str, Any]) -> str:
    outcome = metrics.get("outcome")
    if outcome in {
        "http_rejected",
        "invalid_response",
        "crawl_failed",
        "crawl_cancelled",
        "queue_not_idle",
        "transport_unreachable",
    }:
        return "reject"
    if outcome in {"unknown_submit", "poll_timeout"}:
        return "unknown"
    if command in {"scrape", "parse"}:
        return "accept" if metrics.get("markdown_chars", 0) > 0 else "manual_review"
    if command == "crawl":
        return (
            "accept" if metrics.get("crawl_state") == "completed" else "manual_review"
        )
    return "accept"


def agent_safe_reason_code(metrics: dict[str, Any]) -> str:
    outcome = metrics.get("outcome")
    if outcome not in AGENT_SAFE_REASON_CODES:
        raise ValueError("agent-safe metrics contain an unknown outcome")
    return AGENT_SAFE_REASON_CODES[outcome]


def agent_safe_interface_digests() -> dict[str, str]:
    return {
        "helper_sha256": prefixed_sha256(Path(__file__).resolve().read_bytes()),
        "tooling_manifest_sha256": prefixed_sha256(TOOLING_MANIFEST_PATH.read_bytes()),
    }


def strict_agent_safe_directory(*, create: bool) -> Path:
    """Keep fixed safe artifacts under a non-symlinked repository tasks path."""
    receipt_dir = AGENT_SAFE_EVIDENCE_DIR
    tasks_root = REPO_ROOT / "tasks"
    try:
        relative = receipt_dir.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise agent_safe_error(
            "agent-safe receipt directory is outside the repository"
        ) from exc
    if not relative.parts or relative.parts[0] != "tasks":
        raise agent_safe_error("agent-safe receipt directory must remain under tasks")

    current = REPO_ROOT
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise agent_safe_error("agent-safe receipt directory contains a symlink")
        if current.exists() and not current.is_dir():
            raise agent_safe_error("agent-safe receipt directory is not a directory")
        if create and not current.exists():
            current.mkdir()
    try:
        receipt_dir.resolve(strict=False).relative_to(tasks_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise agent_safe_error("agent-safe receipt directory escapes tasks") from exc
    return receipt_dir


def valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None
    )


def require_exact_keys(value: object, keys: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"agent-safe receipt schema mismatch: {label}")
    return value


def validate_agent_safe_metrics(metrics: dict[str, Any]) -> None:
    recipe = metrics.get("recipe")
    allowed = {"schema_version", "recipe", "outcome", "http_status"}
    if recipe in {"scrape", "parse"}:
        allowed.add("markdown_chars")
    elif recipe == "map":
        allowed.add("links_count")
    elif recipe == "crawl":
        allowed.add("crawl_state")
    if recipe == "parse":
        allowed.add("pages")
    if set(metrics) - allowed or not {
        "schema_version",
        "recipe",
        "outcome",
        "http_status",
    } <= set(metrics):
        raise ValueError("agent-safe metrics contain an unknown field")
    if (
        metrics.get("schema_version") != "agent-safe-metrics-v1"
        or recipe not in AGENT_SAFE_COMMANDS
        or metrics.get("outcome") not in AGENT_SAFE_OUTCOMES
        or not isinstance(metrics.get("http_status"), int)
        or isinstance(metrics.get("http_status"), bool)
        or not 0 <= metrics["http_status"] <= 599
    ):
        raise ValueError("agent-safe metrics have an invalid base schema")
    for key in ("markdown_chars", "links_count", "pages"):
        if key in metrics and (
            not isinstance(metrics[key], int)
            or isinstance(metrics[key], bool)
            or metrics[key] < 0
        ):
            raise ValueError("agent-safe metrics have an invalid count")
    if "pages" in metrics and metrics["pages"] > 1:
        raise ValueError("agent-safe metrics exceed the one-page pilot bound")
    if (
        "crawl_state" in metrics
        and metrics["crawl_state"] not in AGENT_SAFE_CRAWL_STATUSES
    ):
        raise ValueError("agent-safe metrics have an invalid crawl state")


def validate_agent_safe_bounds(recipe: str, bounds: object) -> None:
    """Keep every persisted recipe at the exact first-pilot limits."""
    expected: dict[str, dict[str, int | float]] = {
        "health": {"request_timeout_seconds": AGENT_SAFE_REQUEST_TIMEOUT_SECONDS},
        "scrape": {"request_timeout_seconds": AGENT_SAFE_REQUEST_TIMEOUT_SECONDS},
        "map": {
            "request_timeout_seconds": AGENT_SAFE_REQUEST_TIMEOUT_SECONDS,
            "limit": 1,
        },
        "crawl": {
            "request_timeout_seconds": AGENT_SAFE_REQUEST_TIMEOUT_SECONDS,
            "limit": 1,
            "max_concurrency": 1,
            "include_path_count": 1,
            "poll_timeout_seconds": AGENT_SAFE_CRAWL_POLL_TIMEOUT_SECONDS,
            "poll_interval_seconds": AGENT_SAFE_CRAWL_POLL_INTERVAL_SECONDS,
        },
        "parse": {
            "request_timeout_seconds": AGENT_SAFE_REQUEST_TIMEOUT_SECONDS,
            "max_pages": 1,
        },
    }
    if recipe not in expected or bounds != expected[recipe]:
        raise ValueError("agent-safe receipt has invalid fixed recipe bounds")


def receipt_prerequisite(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {"status": "not_required", "observed_at": None, "evidence_digest": None}
    observed_at = value.get("observed_at")
    digest = value.get("evidence_digest")
    require_recorded_observation(observed_at, label="agent-safe receipt prerequisite")
    if not valid_sha256(digest):
        raise ValueError("agent-safe receipt prerequisite has an invalid digest")
    return {"status": "passed", "observed_at": observed_at, "evidence_digest": digest}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def agent_safe_metrics_bytes(metrics: dict[str, Any]) -> bytes:
    return (json.dumps(metrics, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def validate_agent_safe_receipt(
    manifest: dict[str, Any], metrics: dict[str, Any]
) -> None:
    validate_agent_safe_metrics(metrics)
    root = require_exact_keys(
        manifest,
        {
            "schema_version",
            "kind",
            "receipt_id",
            "observed_at",
            "recipe",
            "api_scope",
            "producer",
            "preflight",
            "compatibility_doctor",
            "input",
            "bounds",
            "outcome",
            "redaction",
        },
        label="root",
    )
    if (
        root["schema_version"] != 1
        or root["kind"] != "firecrawl.agent-safe-receipt"
        or not isinstance(root["receipt_id"], str)
        or re.fullmatch(r"[0-9a-f]{32}", root["receipt_id"]) is None
        or root["recipe"] != metrics["recipe"]
        or root["api_scope"] != "loopback-http-3002"
    ):
        raise ValueError("agent-safe receipt has an invalid root schema")
    require_recorded_observation(root["observed_at"], label="agent-safe receipt")
    producer = require_exact_keys(
        root["producer"],
        {"interface", "helper_sha256", "tooling_manifest_sha256"},
        label="producer",
    )
    if producer["interface"] != "firecrawl_request.py --agent-safe" or not all(
        valid_sha256(producer[key])
        for key in ("helper_sha256", "tooling_manifest_sha256")
    ):
        raise ValueError("agent-safe receipt has invalid producer provenance")
    for name in ("preflight", "compatibility_doctor"):
        prerequisite = require_exact_keys(
            root[name], {"status", "observed_at", "evidence_digest"}, label=name
        )
        if prerequisite["status"] == "passed":
            require_recorded_observation(
                prerequisite["observed_at"], label=f"agent-safe receipt {name}"
            )
            if not valid_sha256(prerequisite["evidence_digest"]):
                raise ValueError(
                    "agent-safe receipt has invalid prerequisite provenance"
                )
        elif prerequisite != {
            "status": "not_required",
            "observed_at": None,
            "evidence_digest": None,
        }:
            raise ValueError("agent-safe receipt has invalid optional prerequisite")
    input_record = require_exact_keys(
        root["input"], {"kind", "retained", "content_sha256"}, label="input"
    )
    if input_record["kind"] not in {
        "loopback_health",
        "public_example_fixture",
        "synthetic_pdf_fixture",
    } or input_record != {
        "kind": input_record["kind"],
        "retained": False,
        "content_sha256": None,
    }:
        raise ValueError("agent-safe receipt has invalid input retention")
    validate_agent_safe_bounds(root["recipe"], root["bounds"])
    outcome = require_exact_keys(
        root["outcome"],
        {
            "terminal_disposition",
            "reason_code",
            "body_retained_bytes",
            "metrics_artifact",
        },
        label="outcome",
    )
    if (
        outcome["terminal_disposition"]
        not in {"accept", "manual_review", "reject", "unknown"}
        or outcome["reason_code"] != agent_safe_reason_code(metrics)
        or outcome["body_retained_bytes"] != 0
    ):
        raise ValueError("agent-safe receipt has invalid terminal semantics")
    artifact = require_exact_keys(
        outcome["metrics_artifact"],
        {"artifact_ref", "sha256", "bytes"},
        label="metrics artifact",
    )
    metrics_bytes = agent_safe_metrics_bytes(metrics)
    if (
        artifact["artifact_ref"]
        != f"firecrawl-agent-safe://v1/{root['receipt_id']}/metrics"
        or artifact["sha256"] != prefixed_sha256(metrics_bytes)
        or artifact["bytes"] != len(metrics_bytes)
    ):
        raise ValueError("agent-safe receipt has invalid metrics provenance")
    redaction = require_exact_keys(
        root["redaction"],
        {
            "source_body_retained",
            "request_value_retained",
            "absolute_path_retained",
            "secret_value_retained",
        },
        label="redaction",
    )
    if redaction != {key: False for key in redaction}:
        raise ValueError("agent-safe receipt has invalid redaction declaration")


def atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def write_agent_safe_receipt(args: argparse.Namespace, metrics: dict[str, Any]) -> None:
    """Atomically persist a validated terminal receipt after an allowed request."""
    context = getattr(args, "agent_safe_context", None)
    if not context:
        return
    args.agent_safe_receipt_attempted = True
    try:
        receipt_dir = strict_agent_safe_directory(create=True)
        receipt_id = context["run_id"]
        metrics_bytes = agent_safe_metrics_bytes(metrics)
        prerequisites = context["prerequisite_digests"]
        manifest = {
            "schema_version": 1,
            "kind": "firecrawl.agent-safe-receipt",
            "receipt_id": receipt_id,
            "observed_at": utc_now(),
            "recipe": args.command,
            "api_scope": "loopback-http-3002",
            "producer": {
                "interface": "firecrawl_request.py --agent-safe",
                **agent_safe_interface_digests(),
            },
            "preflight": receipt_prerequisite(prerequisites.get("preflight")),
            "compatibility_doctor": receipt_prerequisite(
                prerequisites.get("compatibility_doctor")
            ),
            "input": {
                "kind": context["input_class"],
                "retained": False,
                "content_sha256": None,
            },
            "bounds": context["bounds"],
            "outcome": {
                "terminal_disposition": agent_safe_disposition(args.command, metrics),
                "reason_code": agent_safe_reason_code(metrics),
                "body_retained_bytes": 0,
                "metrics_artifact": {
                    "artifact_ref": f"firecrawl-agent-safe://v1/{receipt_id}/metrics",
                    "sha256": prefixed_sha256(metrics_bytes),
                    "bytes": len(metrics_bytes),
                },
            },
            "redaction": {
                "source_body_retained": False,
                "request_value_retained": False,
                "absolute_path_retained": False,
                "secret_value_retained": False,
            },
        }
        validate_agent_safe_receipt(manifest, metrics)
        atomic_write(receipt_dir / f"{receipt_id}-metrics.json", metrics_bytes)
        # The manifest is the durable commit marker; write it only after the metrics are complete.
        atomic_write(
            receipt_dir / f"{receipt_id}-receipt.json",
            (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
        )
    except (AgentSafeViolation, KeyError, OSError, ValueError) as exc:
        raise SystemExit("agent_safe_receipt_write_failed") from exc
    args.agent_safe_receipt_written = True


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--api-url", default=os.getenv("FIRECRAWL_API_URL", DEFAULT_API_URL)
    )
    parser.add_argument(
        "--api-key", default=os.getenv("FIRECRAWL_API_KEY") or os.getenv("TEST_API_KEY")
    )
    parser.add_argument("--timeout", type=finite_positive_float, default=180.0)
    parser.add_argument(
        "--out", "-o", help="Write the full JSON response to this file."
    )
    parser.add_argument(
        "--out-dir",
        help="Write the full JSON response to a timestamped file in this directory.",
    )
    parser.add_argument("--basename", help="Filename label to use with --out-dir.")
    parser.add_argument(
        "--save-fields",
        help="Directory for extracted markdown/html/links/images/metadata fields.",
    )
    parser.add_argument(
        "--pretty", action="store_true", help="Pretty-print JSON responses."
    )
    parser.add_argument(
        "--unwrap",
        action="store_true",
        help="Write only a v2 response's data object when one is present.",
    )
    parser.add_argument(
        "--metrics-only",
        action="store_true",
        help="Write compact response metrics without source bodies or extracted fields.",
    )
    parser.add_argument(
        "--agent-safe",
        action="store_true",
        help="Restrict this command to the local, body-free agent pilot contract.",
    )
    parser.add_argument(
        "--receipt-dir",
        help="With --agent-safe, write body-free metrics and a receipt beneath tasks/.",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Do not print the response body to stdout."
    )
    parser.add_argument(
        "--print-paths", action="store_true", help="Print saved output paths to stderr."
    )


def scrape_body(args: argparse.Namespace) -> dict[str, Any]:
    body: dict[str, Any] = {"url": args.url}
    formats: list[Any] = parse_csv(args.formats) or ["markdown"]
    schema = load_json_arg(args.schema, label="--schema")
    schema_file = load_json_file(args.schema_file, label="--schema-file")
    if schema_file is not None:
        schema = schema_file
    if schema is not None:
        formats.append({"type": "json", "prompt": args.prompt, "schema": schema})
    elif args.query:
        formats.append({"type": "query", "prompt": args.query})
    elif args.summary:
        formats.append("summary")
    body["formats"] = formats
    if args.only_main_content is not None:
        body["onlyMainContent"] = args.only_main_content
    if args.wait_for is not None:
        body["waitFor"] = args.wait_for
    if args.country or args.languages:
        body["location"] = {}
        if args.country:
            body["location"]["country"] = args.country
        if args.languages:
            body["location"]["languages"] = parse_csv(args.languages)
    if args.proxy:
        body["proxy"] = args.proxy
    if args.max_age is not None:
        body["maxAge"] = args.max_age
    if args.headers_file:
        body["headers"] = load_headers_file(args.headers_file)
    if getattr(args, "user_agent", None):
        body.setdefault("headers", {})["User-Agent"] = args.user_agent
    return body


def parse_options(args: argparse.Namespace) -> dict[str, Any]:
    options: dict[str, Any] = {"formats": parse_csv(args.formats) or ["markdown"]}
    if args.no_pdf_parse:
        options["parsers"] = []
    elif args.pdf_mode or args.max_pages is not None or args.fire_pdf_async:
        parser: dict[str, Any] = {"type": "pdf"}
        if args.pdf_mode:
            parser["mode"] = args.pdf_mode
        if args.max_pages is not None:
            parser["maxPages"] = args.max_pages
        if args.fire_pdf_async:
            parser["__firePdfAsync"] = True
        options["parsers"] = [parser]
    if args.only_main_content is not None:
        options["onlyMainContent"] = args.only_main_content
    if args.include_tags:
        options["includeTags"] = parse_csv(args.include_tags)
    if args.exclude_tags:
        options["excludeTags"] = parse_csv(args.exclude_tags)
    if args.query:
        options["formats"].append({"type": "query", "prompt": args.query})
    return options


def run_and_write(
    args: argparse.Namespace,
    method: str,
    path: str,
    body: Any | None,
    basename: str,
) -> tuple[int, Any]:
    if getattr(args, "agent_safe", False):
        status, raw = request_json(
            args.api_url,
            method,
            path,
            body,
            args.api_key,
            args.timeout,
            agent_safe=True,
        )
    else:
        status, raw = request_json(
            args.api_url, method, path, body, args.api_key, args.timeout
        )
    result = decode_json_or_bytes(raw)
    if getattr(args, "agent_safe", False) and not 200 <= status < 300:
        args.agent_safe_outcome = "http_rejected"
    write_response(args, result, raw, status, basename)
    if getattr(args, "agent_safe", False) and not 200 <= status < 300:
        raise SystemExit("agent_safe_http_rejected")
    if status >= 400:
        raise SystemExit(1)
    return status, result


def cmd_scrape(args: argparse.Namespace) -> None:
    ensure_agent_safe_post_ready(args)
    run_and_write(args, "POST", "/v2/scrape", scrape_body(args), args.url)


def cmd_search(args: argparse.Namespace) -> None:
    body: dict[str, Any] = {"query": args.query, "limit": args.limit}
    if args.safe is not None:
        body["safe"] = args.safe
    scrape_formats = parse_csv(args.scrape_formats)
    if scrape_formats:
        body["scrapeOptions"] = {"formats": scrape_formats}
    run_and_write(args, "POST", "/v2/search", body, args.query)


def cmd_map(args: argparse.Namespace) -> None:
    body: dict[str, Any] = {"url": args.url}
    if args.limit is not None:
        body["limit"] = args.limit
    if args.search:
        body["search"] = args.search
    if args.sitemap:
        body["sitemap"] = args.sitemap
    if args.include_subdomains:
        body["includeSubdomains"] = True
    ensure_agent_safe_post_ready(args)
    run_and_write(args, "POST", "/v2/map", body, args.url)


def cmd_parse(args: argparse.Namespace) -> None:
    options = parse_options(args)
    ensure_agent_safe_post_ready(args)
    multipart_args = (
        args.api_url,
        "/v2/parse",
        {"options": json.dumps(options, separators=(",", ":"))},
        {"file": Path(args.file)},
        args.api_key,
        args.timeout,
    )
    if getattr(args, "agent_safe", False):
        status, raw = request_multipart(*multipart_args, agent_safe=True)
    else:
        status, raw = request_multipart(*multipart_args)
    result = decode_json_or_bytes(raw)
    if getattr(args, "agent_safe", False) and not 200 <= status < 300:
        args.agent_safe_outcome = "http_rejected"
    write_response(args, result, raw, status, Path(args.file).stem)
    if getattr(args, "agent_safe", False) and not 200 <= status < 300:
        raise SystemExit("agent_safe_http_rejected")
    if status >= 400:
        raise SystemExit(1)


def cmd_post(args: argparse.Namespace) -> None:
    body = load_json_file(args.body_file, label="--body-file")
    inline = load_json_arg(args.body_json, label="--body-json")
    if inline is not None:
        body = inline
    run_and_write(args, args.method, args.path, body, args.basename or args.path)


def cmd_health(args: argparse.Namespace) -> None:
    if getattr(args, "agent_safe", False):
        args.agent_safe_request_started = True
    root_args = (args.api_url, "GET", "/", None, args.api_key, args.timeout)
    if getattr(args, "agent_safe", False):
        root_status, root_raw = request_json(*root_args, agent_safe=True)
    else:
        root_status, root_raw = request_json(*root_args)
    root = decode_json_or_bytes(root_raw)
    if root_status >= 400 or (
        getattr(args, "agent_safe", False) and not 200 <= root_status < 300
    ):
        if getattr(args, "agent_safe", False):
            args.agent_safe_outcome = "http_rejected"
        write_response(args, root, root_raw, root_status, "health")
        raise SystemExit(1)

    queue_args = (
        args.api_url,
        "GET",
        "/v2/team/queue-status",
        None,
        args.api_key,
        args.timeout,
    )
    if getattr(args, "agent_safe", False):
        queue_status, queue_raw = request_json(*queue_args, agent_safe=True)
    else:
        queue_status, queue_raw = request_json(*queue_args)
    queue = decode_json_or_bytes(queue_raw)
    queue_data = queue if isinstance(queue, dict) else {}
    health: dict[str, Any] = {
        "success": queue_status < 400 and root_status < 400,
        "apiHttpStatus": root_status,
        "queueHttpStatus": queue_status,
    }
    for key in QUEUE_STATUS_FIELDS:
        if key in queue_data:
            health[key] = queue_data[key]
    health_raw = json.dumps(health, separators=(",", ":")).encode("utf-8")
    if (
        queue_status >= 400
        or (getattr(args, "agent_safe", False) and not 200 <= queue_status < 300)
    ) and getattr(args, "agent_safe", False):
        args.agent_safe_outcome = "http_rejected"
    write_response(
        args,
        health,
        health_raw,
        queue_status
        if queue_status >= 400
        or (getattr(args, "agent_safe", False) and not 200 <= queue_status < 300)
        else root_status,
        "health",
    )
    if queue_status >= 400 or (
        getattr(args, "agent_safe", False) and not 200 <= queue_status < 300
    ):
        if getattr(args, "agent_safe", False):
            raise SystemExit("agent_safe_http_rejected")
        raise SystemExit(1)


def crawl_body(args: argparse.Namespace) -> dict[str, Any]:
    body: dict[str, Any] = {"url": args.url}
    for argument, key in (("limit", "limit"), ("max_concurrency", "maxConcurrency")):
        value = getattr(args, argument, None)
        if value is not None:
            body[key] = value
    for argument, key in (
        ("include_paths", "includePaths"),
        ("exclude_paths", "excludePaths"),
    ):
        values = parse_csv(getattr(args, argument, None))
        if values:
            body[key] = values
    scrape_options: dict[str, Any] = {}
    formats = parse_csv(getattr(args, "scrape_formats", None))
    if formats:
        scrape_options["formats"] = formats
    headers_file = getattr(args, "headers_file", None)
    if headers_file:
        scrape_options["headers"] = load_headers_file(headers_file)
    if getattr(args, "user_agent", None):
        scrape_options.setdefault("headers", {})["User-Agent"] = args.user_agent
    if scrape_options:
        body["scrapeOptions"] = scrape_options
    return body


def get_crawl_id(result: Any) -> str:
    if not isinstance(result, dict):
        raise SystemExit("Crawl submit did not return a JSON object with an id.")
    identifier = result.get("id") or result.get("jobId")
    if not isinstance(identifier, str) or not identifier:
        raise SystemExit("Crawl submit did not return an id.")
    return identifier


def get_crawl_status(result: Any) -> str:
    if not isinstance(result, dict):
        return "unknown"
    return str(result.get("status", "unknown"))


def crawl_terminal_error(crawl_id: str, status: str) -> str:
    return f"Crawl {crawl_id} ended with status={status}."


def poll_crawl(
    args: argparse.Namespace,
    crawl_id: str,
) -> tuple[int, Any, bytes]:
    deadline = time.monotonic() + args.poll_timeout
    last_status = "unknown"
    last_http_status = 0

    def timeout_exit() -> None:
        if getattr(args, "agent_safe", False):
            agent_safe_failure(args, "poll_timeout", status=last_http_status or 0)
        message = (
            f"Timed out waiting for crawl {crawl_id}; last status={last_status}. "
            f"Poll it with: crawl-status {crawl_id}"
        )
        timeout_result = {
            "success": False,
            "id": crawl_id,
            "status": last_status,
            "error": message,
        }
        write_response(
            args,
            timeout_result,
            json.dumps(timeout_result).encode("utf-8"),
            last_http_status or 408,
            crawl_id,
            crawl_id,
        )
        raise SystemExit(message)

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timeout_exit()
        poll_args = (
            args.api_url,
            "GET",
            f"/v2/crawl/{crawl_id}",
            None,
            args.api_key,
            min(args.timeout, remaining),
        )
        if getattr(args, "agent_safe", False):
            status, raw = request_json(*poll_args, agent_safe=True)
        else:
            status, raw = request_json(*poll_args)
        last_http_status = status
        if time.monotonic() >= deadline:
            timeout_exit()
        result = decode_json_or_bytes(raw)
        if status >= 400 or (
            getattr(args, "agent_safe", False) and not 200 <= status < 300
        ):
            if getattr(args, "agent_safe", False):
                args.agent_safe_outcome = "http_rejected"
            write_response(args, result, raw, status, crawl_id, crawl_id)
            if getattr(args, "agent_safe", False):
                raise SystemExit("agent_safe_http_rejected")
            raise SystemExit(1)
        if not isinstance(result, dict):
            if getattr(args, "agent_safe", False):
                args.agent_safe_outcome = "invalid_response"
            write_response(args, result, raw, status, crawl_id, crawl_id)
            if getattr(args, "agent_safe", False):
                raise SystemExit("agent_safe_invalid_response")
            raise SystemExit(f"Crawl {crawl_id} returned a non-JSON status response.")
        last_status = get_crawl_status(result)
        if (
            getattr(args, "agent_safe", False)
            and last_status not in AGENT_SAFE_CRAWL_STATUSES
        ):
            agent_safe_failure(args, "invalid_response", status=status)
        if getattr(args, "agent_safe", False):
            args.agent_safe_crawl_state = last_status
        if last_status in CRAWL_TERMINAL_STATUSES:
            if last_status in CRAWL_FAILURE_STATUSES:
                if getattr(args, "agent_safe", False):
                    args.agent_safe_outcome = (
                        "crawl_failed" if last_status == "failed" else "crawl_cancelled"
                    )
                write_response(args, result, raw, status, crawl_id, crawl_id)
                if getattr(args, "agent_safe", False):
                    raise SystemExit(f"agent_safe_{args.agent_safe_outcome}")
                raise SystemExit(crawl_terminal_error(crawl_id, last_status))
            return status, result, raw
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timeout_exit()
        time.sleep(min(args.poll_interval, remaining))


def cmd_crawl(args: argparse.Namespace) -> None:
    ensure_agent_safe_post_ready(args)
    crawl_args = (
        args.api_url,
        "POST",
        "/v2/crawl",
        crawl_body(args),
        args.api_key,
        args.timeout,
    )
    if getattr(args, "agent_safe", False):
        status, raw = request_json(*crawl_args, agent_safe=True)
    else:
        status, raw = request_json(*crawl_args)
    result = decode_json_or_bytes(raw)
    if status >= 400 or (
        getattr(args, "agent_safe", False) and not 200 <= status < 300
    ):
        if getattr(args, "agent_safe", False):
            args.agent_safe_outcome = "http_rejected"
        write_response(args, result, raw, status, args.url)
        if getattr(args, "agent_safe", False):
            raise SystemExit("agent_safe_http_rejected")
        raise SystemExit(1)
    if not args.wait:
        write_response(args, result, raw, status, args.url)
        return
    try:
        crawl_id = get_crawl_id(result)
    except SystemExit:
        if getattr(args, "agent_safe", False):
            agent_safe_failure(args, "unknown_submit", status=status)
        raise
    if getattr(args, "agent_safe", False):
        try:
            require_safe_crawl_id(crawl_id)
        except AgentSafeViolation:
            agent_safe_failure(args, "unknown_submit", status=status)
    status, completed, raw = poll_crawl(args, crawl_id)
    write_response(args, completed, raw, status, crawl_id, crawl_id)


def cmd_crawl_status(args: argparse.Namespace) -> None:
    if args.wait:
        status, result, raw = poll_crawl(args, args.id)
        write_response(args, result, raw, status, args.id, args.id)
        return
    _status, result = run_and_write(args, "GET", f"/v2/crawl/{args.id}", None, args.id)
    status = get_crawl_status(result)
    if status in CRAWL_FAILURE_STATUSES:
        raise SystemExit(crawl_terminal_error(args.id, status))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Direct local Firecrawl API helper for agents. Use the upstream CLI "
            "for broad command coverage; use this helper for saved artifacts, "
            "advanced /v2/parse PDF settings, and arbitrary endpoint JSON."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    health = subparsers.add_parser(
        "health", help="Check the API root and queue-status endpoint."
    )
    add_common(health)
    health.set_defaults(func=cmd_health)

    scrape = subparsers.add_parser("scrape", help="POST /v2/scrape for one URL.")
    add_common(scrape)
    scrape.add_argument("url")
    scrape.add_argument("--formats", default="markdown")
    scrape.add_argument("--prompt", default="Extract the requested fields.")
    scrape.add_argument("--schema")
    scrape.add_argument("--schema-file")
    scrape.add_argument("--query")
    scrape.add_argument("--summary", action="store_true")
    scrape.add_argument("--only-main-content", type=parse_bool)
    scrape.add_argument("--wait-for", type=int)
    scrape.add_argument("--country")
    scrape.add_argument("--languages")
    scrape.add_argument("--proxy")
    scrape.add_argument("--max-age", type=int)
    scrape.add_argument("--headers-file")
    scrape.add_argument(
        "--user-agent", help="Set a descriptive User-Agent for this scrape."
    )
    scrape.set_defaults(func=cmd_scrape)

    search = subparsers.add_parser("search", help="POST /v2/search.")
    add_common(search)
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=3)
    search.add_argument(
        "--safe",
        type=parse_bool,
        help="Opt in or out of v2 safe-search filtering; omit to use the server default.",
    )
    search.add_argument(
        "--scrape-formats", help="Comma-separated formats for scrapeOptions."
    )
    search.set_defaults(func=cmd_search)

    map_cmd = subparsers.add_parser("map", help="POST /v2/map.")
    add_common(map_cmd)
    map_cmd.add_argument("url")
    map_cmd.add_argument("--limit", type=int)
    map_cmd.add_argument("--search")
    map_cmd.add_argument("--sitemap", choices=["only", "include", "skip"])
    map_cmd.add_argument("--include-subdomains", action="store_true")
    map_cmd.set_defaults(func=cmd_map)

    crawl = subparsers.add_parser(
        "crawl",
        help="POST /v2/crawl; use --wait for bounded HTTP status polling.",
    )
    add_common(crawl)
    crawl.add_argument("url")
    crawl.add_argument("--limit", type=int)
    crawl.add_argument("--max-concurrency", type=int)
    crawl.add_argument("--include-paths", help="Comma-separated crawl path allowlist.")
    crawl.add_argument("--exclude-paths", help="Comma-separated crawl path blocklist.")
    crawl.add_argument(
        "--scrape-formats", help="Comma-separated formats for each crawled page."
    )
    crawl.add_argument("--headers-file", help="JSON file of page request headers.")
    crawl.add_argument(
        "--user-agent", help="Set a descriptive User-Agent for each crawled page."
    )
    crawl.add_argument(
        "--wait",
        action="store_true",
        help="Poll HTTP status until the crawl reaches a terminal state.",
    )
    crawl.add_argument("--poll-interval", type=finite_nonnegative_float, default=1.0)
    crawl.add_argument("--poll-timeout", type=finite_positive_float, default=180.0)
    crawl.set_defaults(func=cmd_crawl)

    crawl_status = subparsers.add_parser("crawl-status", help="GET /v2/crawl/:id.")
    add_common(crawl_status)
    crawl_status.add_argument("id")
    crawl_status.add_argument(
        "--wait",
        action="store_true",
        help="Poll until the crawl reaches a terminal state.",
    )
    crawl_status.add_argument(
        "--poll-interval", type=finite_nonnegative_float, default=1.0
    )
    crawl_status.add_argument(
        "--poll-timeout", type=finite_positive_float, default=180.0
    )
    crawl_status.set_defaults(func=cmd_crawl_status)

    parse = subparsers.add_parser("parse", help="POST /v2/parse multipart upload.")
    add_common(parse)
    parse.add_argument("file")
    parse.add_argument("--formats", default="markdown")
    parse.add_argument("--pdf-mode", choices=["auto", "fast", "ocr"])
    parse.add_argument("--max-pages", type=positive_int, help="Positive PDF page cap.")
    parse.add_argument("--fire-pdf-async", action="store_true")
    parse.add_argument("--no-pdf-parse", action="store_true")
    parse.add_argument("--only-main-content", type=parse_bool)
    parse.add_argument("--include-tags")
    parse.add_argument("--exclude-tags")
    parse.add_argument("--query")
    parse.set_defaults(func=cmd_parse)

    post = subparsers.add_parser("post", help="POST/GET/etc. any JSON endpoint.")
    add_common(post)
    post.add_argument("path", help="Endpoint path, such as /v2/team/queue-status.")
    post.add_argument("--method", default="POST")
    group = post.add_mutually_exclusive_group()
    group.add_argument("--body-json")
    group.add_argument("--body-file")
    post.set_defaults(func=cmd_post)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "receipt_dir", None) and not getattr(args, "agent_safe", False):
        parser.error("--receipt-dir requires --agent-safe")
    if getattr(args, "agent_safe", False):
        try:
            validate_agent_safe_args(args)
        except AgentSafeViolation as exc:
            parser.error(str(exc))
    try:
        args.func(args)
    except SystemExit as exc:
        if (
            getattr(args, "agent_safe", False)
            and getattr(args, "agent_safe_request_started", False)
            and not getattr(args, "agent_safe_receipt_written", False)
            and not getattr(args, "agent_safe_receipt_attempted", False)
        ):
            if str(exc) == "agent_safe_transport_error":
                args.agent_safe_outcome = "transport_unreachable"
            elif getattr(args, "agent_safe_outcome", None) not in AGENT_SAFE_OUTCOMES:
                args.agent_safe_outcome = "invalid_response"
            write_response(args, {}, b"", 0, args.command)
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
