#!/usr/bin/env python3
"""Agent-friendly direct HTTP helper for the local Firecrawl API."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

DEFAULT_API_URL = "http://localhost:3002"
MODEL_PROFILES = ["budget", "escalated", "gateway", "gateway-codex", "openai-direct"]
QUEUE_STATUS_FIELDS = (
    "jobsInQueue",
    "activeJobsInQueue",
    "waitingJobsInQueue",
    "maxConcurrency",
)
CRAWL_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
CRAWL_FAILURE_STATUSES = {"failed", "cancelled"}


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
        raise argparse.ArgumentTypeError(f"Expected a positive integer, got {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"Expected a positive integer, got {value!r}")
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


def slugify(value: str) -> str:
    value = re.sub(r"https?://", "", value.strip())
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    value = value.strip(".-_")
    return value[:80] or "firecrawl"


def timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def build_url(api_url: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return urljoin(api_url.rstrip("/") + "/", path.lstrip("/"))


def resolve_fc_dir(value: str | None = None) -> Path:
    candidates = [
        value,
        os.getenv("FC_DIR"),
        str(Path(__file__).resolve().parents[2]),
        str(Path.cwd()),
        str(Path.home() / "Github" / "agentic-assets" / "firecrawl"),
        str(Path.home() / "Documents" / "GitHub" / "agentic-assets" / "firecrawl"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if (path / "docker-compose.yaml").exists() and (path / "scripts" / "firecrawl-ops").is_dir():
            return path
    raise SystemExit("Could not find the Firecrawl repo. Pass --firecrawl-dir or set FC_DIR.")


def apply_model_profile(args: argparse.Namespace) -> None:
    profile = getattr(args, "model_profile", None)
    if not profile:
        return
    fc_dir = resolve_fc_dir(getattr(args, "firecrawl_dir", None))
    script = fc_dir / "scripts" / "firecrawl-ops" / "set_model_profile.sh"
    subprocess.run([str(script), profile], check=True)
    if getattr(args, "no_recreate_api", False):
        print(
            "Profile written, but running api was not recreated. Recreate api before AI-backed calls.",
            file=sys.stderr,
        )
        return
    subprocess.run(
        ["docker", "compose", "--project-directory", str(fc_dir), "up", "-d", "--force-recreate", "api"],
        check=True,
    )
    if getattr(args, "healthcheck", False):
        subprocess.run([str(fc_dir / "scripts" / "firecrawl-ops" / "firecrawl_healthcheck.sh")], check=True)


def request_json(
    api_url: str,
    method: str,
    path: str,
    body: Any | None,
    api_key: str | None,
    timeout: float,
) -> tuple[int, bytes]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = Request(build_url(api_url, path), data=data, headers=headers, method=method.upper())
    return open_request(req, timeout)


def request_multipart(
    api_url: str,
    path: str,
    fields: dict[str, str],
    files: dict[str, Path],
    api_key: str | None,
    timeout: float,
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
        content_type = mimetypes.guess_type(str(path_obj))[0] or "application/octet-stream"
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
    return open_request(req, timeout)


def open_request(req: Request, timeout_value: float) -> tuple[int, bytes]:
    try:
        with urlopen(req, timeout=timeout_value) as resp:
            return resp.status, resp.read()
    except HTTPError as exc:
        body = exc.read()
        sys.stderr.write(f"HTTP {exc.code} from {req.full_url}\n")
        if body:
            sys.stderr.write(body.decode("utf-8", errors="replace") + "\n")
        return exc.code, body
    except URLError as exc:
        raise SystemExit(f"Could not reach {req.full_url}: {exc}") from exc


def decode_json_or_bytes(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
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
    identifier = root.get("id") or root.get("jobId") or data.get("id") or data.get("jobId")
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
    for key, metric_key in (("links", "linksCount"), ("images", "imagesCount"), ("data", "dataCount")):
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
                    target.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
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

    if metrics_only:
        output_result = response_metrics(result, status)
        if crawl_id:
            output_result["id"] = crawl_id
    elif unwrap:
        output_result = response_payload(result)
    else:
        output_result = result

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


def format_result(result: Any, raw_body: bytes, *, pretty: bool) -> bytes:
    if isinstance(result, (dict, list)):
        if pretty:
            return (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        return (json.dumps(result, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    return raw_body


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-url", default=os.getenv("FIRECRAWL_API_URL", DEFAULT_API_URL))
    parser.add_argument("--api-key", default=os.getenv("FIRECRAWL_API_KEY") or os.getenv("TEST_API_KEY"))
    parser.add_argument("--model-profile", choices=MODEL_PROFILES, help="Apply a local model profile before the request.")
    parser.add_argument("--firecrawl-dir", help="Firecrawl repo root for --model-profile.")
    parser.add_argument(
        "--no-recreate-api",
        action="store_true",
        help="With --model-profile, update .env but do not recreate the api container.",
    )
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="With --model-profile, run the local healthcheck after api recreation.",
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--out", "-o", help="Write the full JSON response to this file.")
    parser.add_argument("--out-dir", help="Write the full JSON response to a timestamped file in this directory.")
    parser.add_argument("--basename", help="Filename label to use with --out-dir.")
    parser.add_argument("--save-fields", help="Directory for extracted markdown/html/links/images/metadata fields.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON responses.")
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
    parser.add_argument("--quiet", action="store_true", help="Do not print the response body to stdout.")
    parser.add_argument("--print-paths", action="store_true", help="Print saved output paths to stderr.")


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
        body["headers"] = load_json_file(args.headers_file, label="--headers-file")
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
    status, raw = request_json(args.api_url, method, path, body, args.api_key, args.timeout)
    result = decode_json_or_bytes(raw)
    write_response(args, result, raw, status, basename)
    if status >= 400:
        raise SystemExit(1)
    return status, result


def cmd_scrape(args: argparse.Namespace) -> None:
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
    run_and_write(args, "POST", "/v2/map", body, args.url)


def cmd_parse(args: argparse.Namespace) -> None:
    options = parse_options(args)
    status, raw = request_multipart(
        args.api_url,
        "/v2/parse",
        {"options": json.dumps(options, separators=(",", ":"))},
        {"file": Path(args.file)},
        args.api_key,
        args.timeout,
    )
    result = decode_json_or_bytes(raw)
    write_response(args, result, raw, status, Path(args.file).stem)
    if status >= 400:
        raise SystemExit(1)


def cmd_post(args: argparse.Namespace) -> None:
    body = load_json_file(args.body_file, label="--body-file")
    inline = load_json_arg(args.body_json, label="--body-json")
    if inline is not None:
        body = inline
    run_and_write(args, args.method, args.path, body, args.basename or args.path)


def cmd_health(args: argparse.Namespace) -> None:
    root_status, root_raw = request_json(args.api_url, "GET", "/", None, args.api_key, args.timeout)
    root = decode_json_or_bytes(root_raw)
    if root_status >= 400:
        write_response(args, root, root_raw, root_status, "health")
        raise SystemExit(1)

    queue_status, queue_raw = request_json(
        args.api_url,
        "GET",
        "/v2/team/queue-status",
        None,
        args.api_key,
        args.timeout,
    )
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
    write_response(args, health, health_raw, root_status, "health")
    if queue_status >= 400:
        raise SystemExit(1)


def crawl_body(args: argparse.Namespace) -> dict[str, Any]:
    body: dict[str, Any] = {"url": args.url}
    for argument, key in (("limit", "limit"), ("max_concurrency", "maxConcurrency")):
        value = getattr(args, argument, None)
        if value is not None:
            body[key] = value
    for argument, key in (("include_paths", "includePaths"), ("exclude_paths", "excludePaths")):
        values = parse_csv(getattr(args, argument, None))
        if values:
            body[key] = values
    scrape_options: dict[str, Any] = {}
    formats = parse_csv(getattr(args, "scrape_formats", None))
    if formats:
        scrape_options["formats"] = formats
    headers_file = getattr(args, "headers_file", None)
    if headers_file:
        scrape_options["headers"] = load_json_file(headers_file, label="--headers-file")
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
    while True:
        status, raw = request_json(args.api_url, "GET", f"/v2/crawl/{crawl_id}", None, args.api_key, args.timeout)
        result = decode_json_or_bytes(raw)
        if status >= 400:
            write_response(args, result, raw, status, crawl_id, crawl_id)
            raise SystemExit(1)
        if not isinstance(result, dict):
            write_response(args, result, raw, status, crawl_id, crawl_id)
            raise SystemExit(f"Crawl {crawl_id} returned a non-JSON status response.")
        last_status = get_crawl_status(result)
        if last_status in CRAWL_TERMINAL_STATUSES:
            if last_status in CRAWL_FAILURE_STATUSES:
                write_response(args, result, raw, status, crawl_id, crawl_id)
                raise SystemExit(crawl_terminal_error(crawl_id, last_status))
            return status, result, raw
        if time.monotonic() >= deadline:
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
            write_response(args, timeout_result, json.dumps(timeout_result).encode("utf-8"), status, crawl_id, crawl_id)
            raise SystemExit(message)
        time.sleep(args.poll_interval)


def cmd_crawl(args: argparse.Namespace) -> None:
    status, raw = request_json(args.api_url, "POST", "/v2/crawl", crawl_body(args), args.api_key, args.timeout)
    result = decode_json_or_bytes(raw)
    if status >= 400:
        write_response(args, result, raw, status, args.url)
        raise SystemExit(1)
    if not args.wait:
        write_response(args, result, raw, status, args.url)
        return
    crawl_id = get_crawl_id(result)
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

    health = subparsers.add_parser("health", help="Check the API root and queue-status endpoint.")
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
    scrape.add_argument("--user-agent", help="Set a descriptive User-Agent for this scrape.")
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
    search.add_argument("--scrape-formats", help="Comma-separated formats for scrapeOptions.")
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
    crawl.add_argument("--scrape-formats", help="Comma-separated formats for each crawled page.")
    crawl.add_argument("--headers-file", help="JSON file of page request headers.")
    crawl.add_argument("--user-agent", help="Set a descriptive User-Agent for each crawled page.")
    crawl.add_argument("--wait", action="store_true", help="Poll HTTP status until the crawl reaches a terminal state.")
    crawl.add_argument("--poll-interval", type=float, default=1.0)
    crawl.add_argument("--poll-timeout", type=float, default=180.0)
    crawl.set_defaults(func=cmd_crawl)

    crawl_status = subparsers.add_parser("crawl-status", help="GET /v2/crawl/:id.")
    add_common(crawl_status)
    crawl_status.add_argument("id")
    crawl_status.add_argument("--wait", action="store_true", help="Poll until the crawl reaches a terminal state.")
    crawl_status.add_argument("--poll-interval", type=float, default=1.0)
    crawl_status.add_argument("--poll-timeout", type=float, default=180.0)
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
    apply_model_profile(args)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
