#!/usr/bin/env python3
"""Run a local Firecrawl API smoke matrix and save durable evidence."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


DEFAULT_API_URL = "http://localhost:3002"
TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled", "canceled"}


@dataclass
class ProbeResult:
    name: str
    status: str
    http_status: int | None = None
    duration_ms: int = 0
    detail: str = ""
    response: Any | None = None
    error: str | None = None


@dataclass
class SmokeContext:
    api_url: str
    api_key: str | None
    timeout: float
    poll_timeout: float
    poll_interval: float
    parse_file: Path
    crawl_url: str
    batch_url: str
    search_query: str
    out_dir: Path
    include_mutating_optional_probes: bool
    results: list[ProbeResult] = field(default_factory=list)


def build_url(api_url: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return urljoin(api_url.rstrip("/") + "/", path.lstrip("/"))


def auth_headers(ctx: SmokeContext) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if ctx.api_key:
        headers["Authorization"] = f"Bearer {ctx.api_key}"
    return headers


def request_json(
    ctx: SmokeContext,
    method: str,
    path: str,
    body: Any | None = None,
) -> tuple[int, Any]:
    data = None
    headers = auth_headers(ctx)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(build_url(ctx.api_url, path), data=data, headers=headers, method=method.upper())
    return open_request(req, ctx.timeout)


def request_multipart(
    ctx: SmokeContext,
    path: str,
    fields: dict[str, str],
    files: dict[str, Path],
) -> tuple[int, Any]:
    boundary = f"----firecrawl-smoke-{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\r\n")

    for name, file_path in files.items():
        if not file_path.is_file():
            raise FileNotFoundError(f"Missing upload file: {file_path}")
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{name}"; filename="{file_path.name}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(file_path.read_bytes())
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    data = b"".join(chunks)
    headers = auth_headers(ctx)
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    headers["Content-Length"] = str(len(data))
    req = Request(build_url(ctx.api_url, path), data=data, headers=headers, method="POST")
    return open_request(req, ctx.timeout)


def open_request(req: Request, timeout_value: float) -> tuple[int, Any]:
    try:
        with urlopen(req, timeout=timeout_value) as resp:
            return resp.status, decode_body(resp.read())
    except HTTPError as exc:
        return exc.code, decode_body(exc.read())
    except URLError as exc:
        raise RuntimeError(f"Could not reach {req.full_url}: {exc}") from exc


def decode_body(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return body.decode("utf-8", errors="replace")


def is_success_response(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("success") is False:
        return False
    return True


def payload_data(payload: Any) -> Any:
    if isinstance(payload, dict):
        return payload.get("data")
    return None


def json_text(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def require_markdown(payload: Any, label: str) -> str:
    data = payload_data(payload)
    markdown = data.get("markdown") if isinstance(data, dict) else None
    if not isinstance(markdown, str) or not markdown.strip():
        raise AssertionError(f"{label} response did not include markdown")
    return markdown


def extract_job_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise AssertionError("response is not a JSON object")
    for key in ("id", "jobId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("id", "jobId"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    raise AssertionError("response did not include a job id")


def response_status(payload: Any) -> str | None:
    if isinstance(payload, dict):
        value = payload.get("status")
        if isinstance(value, str):
            return value
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("status"), str):
            return data["status"]
    return None


def poll_job(ctx: SmokeContext, path: str) -> tuple[int, Any]:
    deadline = time.time() + ctx.poll_timeout
    last_status = None
    last_http_status = None
    last_payload: Any = None

    while time.time() <= deadline:
        last_http_status, last_payload = request_json(ctx, "GET", path)
        last_status = response_status(last_payload)
        if last_http_status >= 400:
            return last_http_status, last_payload
        if last_status in TERMINAL_JOB_STATUSES:
            return last_http_status, last_payload
        time.sleep(ctx.poll_interval)

    raise TimeoutError(f"Timed out polling {path}; last status={last_status!r}")


def add_probe(ctx: SmokeContext, name: str, func: Callable[[], tuple[int | None, Any, str]]) -> None:
    start = time.time()
    try:
        http_status, response, detail = func()
        status = "pass"
        error = None
    except AssertionError as exc:
        http_status = None
        response = None
        detail = ""
        status = "fail"
        error = str(exc)
    except Exception as exc:
        http_status = None
        response = None
        detail = ""
        status = "fail"
        error = f"{type(exc).__name__}: {exc}"

    ctx.results.append(
        ProbeResult(
            name=name,
            status=status,
            http_status=http_status,
            duration_ms=int((time.time() - start) * 1000),
            detail=detail,
            response=response,
            error=error,
        )
    )


def expect_http_success(http_status: int, payload: Any) -> None:
    if http_status >= 400:
        raise AssertionError(f"HTTP status was {http_status}")
    if not is_success_response(payload):
        raise AssertionError("response success flag was false")


def check_root(ctx: SmokeContext) -> tuple[int, Any, str]:
    http_status, payload = request_json(ctx, "GET", "/")
    if http_status >= 400:
        raise AssertionError(f"HTTP status was {http_status}")
    if not isinstance(payload, dict) or "Firecrawl" not in str(payload.get("message", "")):
        raise AssertionError("root response did not look like Firecrawl")
    return http_status, payload, "API root responded"


def check_scrape(ctx: SmokeContext) -> tuple[int, Any, str]:
    http_status, payload = request_json(
        ctx,
        "POST",
        "/v2/scrape",
        {"url": ctx.crawl_url, "formats": ["markdown", "links"]},
    )
    expect_http_success(http_status, payload)
    markdown = require_markdown(payload, "scrape")
    return http_status, payload, f"markdown_len={len(markdown)}"


def check_map(ctx: SmokeContext) -> tuple[int, Any, str]:
    http_status, payload = request_json(ctx, "POST", "/v2/map", {"url": ctx.crawl_url, "limit": 3})
    expect_http_success(http_status, payload)
    links = payload.get("links") if isinstance(payload, dict) else None
    if links is None:
        data = payload_data(payload)
        links = data.get("links") if isinstance(data, dict) else None
    if links is not None and not isinstance(links, list):
        raise AssertionError("map links field was not a list")
    return http_status, payload, f"links={len(links or [])}"


def check_search(ctx: SmokeContext) -> tuple[int, Any, str]:
    http_status, payload = request_json(
        ctx,
        "POST",
        "/v2/search",
        {"query": ctx.search_query, "limit": 2},
    )
    expect_http_success(http_status, payload)
    data = payload_data(payload)
    if isinstance(data, dict):
        count = sum(len(v) for v in data.values() if isinstance(v, list))
    elif isinstance(data, list):
        count = len(data)
    else:
        count = 0
    return http_status, payload, f"results={count}"


def check_parse(ctx: SmokeContext) -> tuple[int, Any, str]:
    options = {"formats": ["markdown"], "parsers": [{"type": "pdf", "mode": "fast", "maxPages": 2}]}
    http_status, payload = request_multipart(
        ctx,
        "/v2/parse",
        {"options": json.dumps(options, separators=(",", ":"))},
        {"file": ctx.parse_file},
    )
    expect_http_success(http_status, payload)
    markdown = require_markdown(payload, "parse")
    return http_status, payload, f"markdown_len={len(markdown)}"


def check_batch(ctx: SmokeContext) -> tuple[int, Any, str]:
    http_status, payload = request_json(
        ctx,
        "POST",
        "/v2/batch/scrape",
        {"urls": [ctx.batch_url], "formats": ["markdown"]},
    )
    expect_http_success(http_status, payload)
    job_id = extract_job_id(payload)
    status_http, status_payload = poll_job(ctx, f"/v2/batch/scrape/{job_id}")
    expect_http_success(status_http, status_payload)
    status = response_status(status_payload)
    if status != "completed":
        raise AssertionError(f"batch scrape ended with status {status!r}")
    return status_http, status_payload, f"job_id={job_id}"


def check_crawl(ctx: SmokeContext) -> tuple[int, Any, str]:
    http_status, payload = request_json(
        ctx,
        "POST",
        "/v2/crawl",
        {"url": ctx.crawl_url, "limit": 1, "scrapeOptions": {"formats": ["markdown"]}},
    )
    expect_http_success(http_status, payload)
    job_id = extract_job_id(payload)
    status_http, status_payload = poll_job(ctx, f"/v2/crawl/{job_id}")
    expect_http_success(status_http, status_payload)
    status = response_status(status_payload)
    if status != "completed":
        raise AssertionError(f"crawl ended with status {status!r}")
    return status_http, status_payload, f"job_id={job_id}"


def check_queue_status(ctx: SmokeContext) -> tuple[int, Any, str]:
    http_status, payload = request_json(ctx, "GET", "/v2/team/queue-status")
    expect_http_success(http_status, payload)
    if not isinstance(payload, dict) or "jobsInQueue" not in payload:
        raise AssertionError("queue status response did not include jobsInQueue")
    return http_status, payload, f"jobsInQueue={payload.get('jobsInQueue')}"


def check_active_crawls(ctx: SmokeContext) -> tuple[int, Any, str]:
    http_status, payload = request_json(ctx, "GET", "/v2/crawl/active")
    expect_http_success(http_status, payload)
    crawls = payload.get("crawls") if isinstance(payload, dict) else None
    if crawls is not None and not isinstance(crawls, list):
        raise AssertionError("active crawl response crawls field was not a list")
    return http_status, payload, f"active={len(crawls or [])}"


def check_browser_list(ctx: SmokeContext) -> tuple[int, Any, str]:
    http_status, payload = request_json(ctx, "GET", "/v2/browser")
    expect_http_success(http_status, payload)
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    return http_status, payload, f"sessions={len(sessions or [])}"


def check_optional_browser_create(ctx: SmokeContext) -> tuple[int, Any, str]:
    http_status, payload = request_json(ctx, "POST", "/v2/browser", {"ttl": 30, "activityTtl": 10})
    if http_status == 503 and "BROWSER_SERVICE_URL" in json_text(payload):
        return http_status, payload, "browser service not configured as expected"
    if http_status < 400:
        return http_status, payload, "browser service appears configured"
    raise AssertionError(f"unexpected browser create response HTTP {http_status}")


def check_optional_agent_create(ctx: SmokeContext) -> tuple[int, Any, str]:
    http_status, payload = request_json(
        ctx,
        "POST",
        "/v2/agent",
        {"prompt": "Smoke probe only.", "urls": [ctx.crawl_url], "maxCredits": 1},
    )
    if http_status >= 500 and "Agent beta is not enabled" in json_text(payload):
        return http_status, payload, "agent service not configured as expected"
    if http_status < 400:
        return http_status, payload, "agent service appears configured"
    raise AssertionError(f"unexpected agent response HTTP {http_status}")


def check_optional_support_proxy(ctx: SmokeContext) -> tuple[int, Any, str]:
    http_status, payload = request_json(ctx, "POST", "/v2/support/ask", {"message": "Smoke probe only."})
    if http_status == 503 and "support_agent_unavailable" in json_text(payload):
        return http_status, payload, "support service not configured as expected"
    if http_status < 400:
        return http_status, payload, "support service appears configured"
    raise AssertionError(f"unexpected support proxy response HTTP {http_status}")


def add_skipped(ctx: SmokeContext, name: str, detail: str) -> None:
    ctx.results.append(ProbeResult(name=name, status="skip", detail=detail))


CORE_PROBES: list[tuple[str, Callable[[SmokeContext], tuple[int | None, Any, str]]]] = [
    ("api_root", check_root),
    ("v2_scrape", check_scrape),
    ("v2_map", check_map),
    ("v2_search", check_search),
    ("v2_parse_pdf_fast", check_parse),
    ("v2_batch_scrape", check_batch),
    ("v2_crawl", check_crawl),
    ("v2_team_queue_status", check_queue_status),
    ("v2_crawl_active", check_active_crawls),
]

OPTIONAL_PROBES: list[tuple[str, Callable[[SmokeContext], tuple[int | None, Any, str]], str]] = [
    (
        "optional_browser_list",
        check_browser_list,
        "Skipped by default because browser routes depend on optional browser-service state.",
    ),
    (
        "optional_browser_create",
        check_optional_browser_create,
        "Skipped by default because it may create a browser session when configured.",
    ),
    (
        "optional_agent_create",
        check_optional_agent_create,
        "Skipped by default because it may enqueue an agent job when configured.",
    ),
    (
        "optional_support_proxy",
        check_optional_support_proxy,
        "Skipped by default because it may call an external support service when configured.",
    ),
]


def run_matrix(ctx: SmokeContext) -> None:
    for name, probe in CORE_PROBES:
        add_probe(ctx, name, lambda probe=probe: probe(ctx))

    if ctx.include_mutating_optional_probes:
        for name, probe, _skip_detail in OPTIONAL_PROBES:
            add_probe(ctx, name, lambda probe=probe: probe(ctx))
    else:
        for name, _probe, skip_detail in OPTIONAL_PROBES:
            add_skipped(ctx, name, skip_detail)


def result_to_dict(result: ProbeResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "status": result.status,
        "http_status": result.http_status,
        "duration_ms": result.duration_ms,
        "detail": result.detail,
        "error": result.error,
        "response": result.response,
    }


def write_artifacts(ctx: SmokeContext) -> tuple[Path, Path]:
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = ctx.out_dir / f"{stamp}-local-api-smoke.json"
    md_path = ctx.out_dir / f"{stamp}-local-api-smoke.md"
    passed = sum(1 for item in ctx.results if item.status == "pass")
    failed = sum(1 for item in ctx.results if item.status == "fail")
    skipped = sum(1 for item in ctx.results if item.status == "skip")
    summary = {
        "timestamp": stamp,
        "api_url": ctx.api_url,
        "parse_file": str(ctx.parse_file),
        "counts": {"pass": passed, "fail": failed, "skip": skipped},
        "results": [result_to_dict(item) for item in ctx.results],
    }
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")

    lines = [
        "# Local Firecrawl API Smoke Matrix",
        "",
        f"- Timestamp: `{stamp}`",
        f"- API URL: `{ctx.api_url}`",
        f"- Parse fixture: `{ctx.parse_file}`",
        f"- Passed: `{passed}`",
        f"- Failed: `{failed}`",
        f"- Skipped: `{skipped}`",
        "",
        "| Probe | Status | HTTP | Duration | Detail |",
        "|---|---:|---:|---:|---|",
    ]
    for item in ctx.results:
        http = "" if item.http_status is None else str(item.http_status)
        detail = item.error or item.detail
        detail = detail.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| `{item.name}` | `{item.status}` | `{http}` | `{item.duration_ms}ms` | {detail} |"
        )
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=os.getenv("FIRECRAWL_API_URL", DEFAULT_API_URL))
    parser.add_argument("--api-key", default=os.getenv("FIRECRAWL_API_KEY") or os.getenv("TEST_API_KEY"))
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--poll-timeout", type=float, default=90.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--crawl-url", default="https://example.com")
    parser.add_argument("--batch-url", default="https://example.com")
    parser.add_argument("--search-query", default="Firecrawl documentation")
    parser.add_argument("--parse-file", default="apps/test-site/public/example.pdf")
    parser.add_argument("--out-dir", default="tasks/tmp/local-api-smoke")
    parser.add_argument(
        "--include-mutating-optional-probes",
        action="store_true",
        help="Also probe optional browser, agent, and support services. These may create jobs when configured.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    parse_file = Path(args.parse_file)
    if not parse_file.is_file():
        print(f"Missing parse fixture: {parse_file}", file=sys.stderr)
        return 2

    ctx = SmokeContext(
        api_url=args.api_url,
        api_key=args.api_key,
        timeout=args.timeout,
        poll_timeout=args.poll_timeout,
        poll_interval=args.poll_interval,
        parse_file=parse_file,
        crawl_url=args.crawl_url,
        batch_url=args.batch_url,
        search_query=args.search_query,
        out_dir=Path(args.out_dir),
        include_mutating_optional_probes=args.include_mutating_optional_probes,
    )
    run_matrix(ctx)
    json_path, md_path = write_artifacts(ctx)
    for item in ctx.results:
        marker = "ok" if item.status == "pass" else item.status
        detail = item.error or item.detail
        print(f"[{marker}] {item.name}: {detail}")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 1 if any(item.status == "fail" for item in ctx.results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
