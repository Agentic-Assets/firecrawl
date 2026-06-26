#!/usr/bin/env python3
"""Run local Firecrawl PDF parse canaries across parser modes."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


DEFAULT_API_URL = "http://localhost:3002"
DEFAULT_PDF = Path("apps/test-site/public/example.pdf")
DEFAULT_OUT_DIR = Path("tasks/tmp/pdf-parse-canary")


@dataclass
class CanaryResult:
    mode: str
    status: str
    http_status: int | None
    duration_ms: int
    markdown_len: int
    error: str | None
    response: Any | None


def build_url(api_url: str, path: str) -> str:
    return urljoin(api_url.rstrip("/") + "/", path.lstrip("/"))


def decode_body(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return body.decode("utf-8", errors="replace")


def parse_pdf(api_url: str, pdf_path: Path, mode: str, max_pages: int, timeout: float) -> tuple[int, Any]:
    boundary = f"----firecrawl-parse-canary-{uuid.uuid4().hex}"
    options = {
        "formats": ["markdown"],
        "parsers": [{"type": "pdf", "mode": mode, "maxPages": max_pages}],
    }
    content_type = mimetypes.guess_type(str(pdf_path))[0] or "application/pdf"
    chunks = [
        f"--{boundary}\r\n".encode("utf-8"),
        b'Content-Disposition: form-data; name="options"\r\n\r\n',
        json.dumps(options, separators=(",", ":")).encode("utf-8"),
        b"\r\n",
        f"--{boundary}\r\n".encode("utf-8"),
        (
            f'Content-Disposition: form-data; name="file"; filename="{pdf_path.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8"),
        pdf_path.read_bytes(),
        b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    body = b"".join(chunks)
    req = Request(
        build_url(api_url, "/v2/parse"),
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, decode_body(resp.read())
    except HTTPError as exc:
        return exc.code, decode_body(exc.read())
    except URLError as exc:
        raise RuntimeError(f"Could not reach {req.full_url}: {exc}") from exc


def markdown_len(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    data = payload.get("data")
    if not isinstance(data, dict):
        return 0
    markdown = data.get("markdown")
    return len(markdown) if isinstance(markdown, str) else 0


def run_mode(api_url: str, pdf_path: Path, mode: str, max_pages: int, timeout: float) -> CanaryResult:
    started = time.time()
    try:
        http_status, payload = parse_pdf(api_url, pdf_path, mode, max_pages, timeout)
        length = markdown_len(payload)
        success = isinstance(payload, dict) and payload.get("success") is not False and http_status < 400
        status = "pass" if success and length > 0 else "fail"
        error = None if status == "pass" else f"HTTP {http_status}, markdown_len={length}"
        return CanaryResult(mode, status, http_status, int((time.time() - started) * 1000), length, error, payload)
    except Exception as exc:
        return CanaryResult(mode, "fail", None, int((time.time() - started) * 1000), 0, str(exc), None)


def write_artifacts(results: list[CanaryResult], out_dir: Path, api_url: str, pdf_path: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"{stamp}-pdf-parse-canary.json"
    md_path = out_dir / f"{stamp}-pdf-parse-canary.md"
    payload = {
        "timestamp": stamp,
        "api_url": api_url,
        "pdf_path": str(pdf_path),
        "results": [asdict(result) for result in results],
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n")
    lines = [
        "# Firecrawl PDF Parse Canary",
        "",
        f"- Timestamp: `{stamp}`",
        f"- API URL: `{api_url}`",
        f"- PDF: `{pdf_path}`",
        "",
        "| Mode | Status | HTTP | Duration | Markdown chars | Error |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for result in results:
        error = (result.error or "").replace("|", "\\|")
        http = "" if result.http_status is None else str(result.http_status)
        lines.append(
            f"| `{result.mode}` | `{result.status}` | `{http}` | `{result.duration_ms}ms` | `{result.markdown_len}` | {error} |"
        )
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def parse_modes(value: str, include_ocr: bool) -> list[str]:
    modes = [mode.strip() for mode in value.split(",") if mode.strip()]
    if include_ocr and "ocr" not in modes:
        modes.append("ocr")
    invalid = [mode for mode in modes if mode not in {"fast", "auto", "ocr"}]
    if invalid:
        raise ValueError(f"Invalid mode(s): {', '.join(invalid)}")
    return modes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=os.getenv("FIRECRAWL_API_URL", DEFAULT_API_URL))
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--modes", default="fast,auto", help="Comma-separated parser modes.")
    parser.add_argument("--include-ocr", action="store_true", help="Add ocr mode to the mode list.")
    parser.add_argument("--max-pages", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.pdf.is_file():
        print(f"Missing PDF canary fixture: {args.pdf}")
        return 2
    try:
        modes = parse_modes(args.modes, args.include_ocr)
    except ValueError as exc:
        print(exc)
        return 2
    results = [run_mode(args.api_url, args.pdf, mode, args.max_pages, args.timeout) for mode in modes]
    json_path, md_path = write_artifacts(results, args.out_dir, args.api_url, args.pdf)
    for result in results:
        marker = "ok" if result.status == "pass" else "fail"
        print(f"[{marker}] {result.mode}: markdown_len={result.markdown_len}, duration_ms={result.duration_ms}")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 1 if any(result.status == "fail" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
