#!/usr/bin/env python3
"""Agent-friendly direct HTTP helper for the local Firecrawl API."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


DEFAULT_API_URL = "http://localhost:3002"


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
        raise SystemExit(1) from exc
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


def format_result(result: Any, raw_body: bytes, *, pretty: bool) -> bytes:
    if isinstance(result, (dict, list)):
        if pretty:
            return (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        return (json.dumps(result, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    return raw_body


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-url", default=os.getenv("FIRECRAWL_API_URL", DEFAULT_API_URL))
    parser.add_argument("--api-key", default=os.getenv("FIRECRAWL_API_KEY") or os.getenv("TEST_API_KEY"))
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--out", "-o", help="Write the full JSON response to this file.")
    parser.add_argument("--out-dir", help="Write the full JSON response to a timestamped file in this directory.")
    parser.add_argument("--basename", help="Filename label to use with --out-dir.")
    parser.add_argument("--save-fields", help="Directory for extracted markdown/html/links/images/metadata fields.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON responses.")
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
    return body


def parse_options(args: argparse.Namespace) -> dict[str, Any]:
    options: dict[str, Any] = {"formats": parse_csv(args.formats) or ["markdown"]}
    if args.no_pdf_parse:
        options["parsers"] = []
    elif args.pdf_mode or args.max_pages or args.fire_pdf_async:
        parser: dict[str, Any] = {"type": "pdf"}
        if args.pdf_mode:
            parser["mode"] = args.pdf_mode
        if args.max_pages:
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


def run_and_write(args: argparse.Namespace, method: str, path: str, body: Any | None, basename: str) -> None:
    status, raw = request_json(args.api_url, method, path, body, args.api_key, args.timeout)
    result = decode_json_or_bytes(raw)
    written = write_outputs(
        result,
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
    if status >= 400:
        raise SystemExit(1)


def cmd_scrape(args: argparse.Namespace) -> None:
    run_and_write(args, "POST", "/v2/scrape", scrape_body(args), args.url)


def cmd_search(args: argparse.Namespace) -> None:
    body: dict[str, Any] = {"query": args.query, "limit": args.limit}
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
    written = write_outputs(
        result,
        raw,
        out=args.out,
        out_dir=args.out_dir,
        basename=args.basename or Path(args.file).stem,
        pretty=args.pretty,
        save_fields=args.save_fields,
        quiet=args.quiet,
    )
    if args.print_paths:
        for item in written:
            print(item, file=sys.stderr)
    if status >= 400:
        raise SystemExit(1)


def cmd_post(args: argparse.Namespace) -> None:
    body = load_json_file(args.body_file, label="--body-file")
    inline = load_json_arg(args.body_json, label="--body-json")
    if inline is not None:
        body = inline
    run_and_write(args, args.method, args.path, body, args.basename or args.path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Direct local Firecrawl API helper for agents. Use the upstream CLI "
            "for broad command coverage; use this helper for saved artifacts, "
            "advanced /v2/parse PDF settings, and arbitrary endpoint JSON."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

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
    scrape.set_defaults(func=cmd_scrape)

    search = subparsers.add_parser("search", help="POST /v2/search.")
    add_common(search)
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=3)
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

    parse = subparsers.add_parser("parse", help="POST /v2/parse multipart upload.")
    add_common(parse)
    parse.add_argument("file")
    parse.add_argument("--formats", default="markdown")
    parse.add_argument("--pdf-mode", choices=["auto", "fast", "ocr"])
    parse.add_argument("--max-pages", type=int)
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
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
