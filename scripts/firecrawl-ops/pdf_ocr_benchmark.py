#!/usr/bin/env python3
"""Run a small local Firecrawl PDF parser matrix and save comparable outputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REQUEST_HELPER = SCRIPT_DIR / "firecrawl_request.py"
DEFAULT_ADAPTER_HEALTH_URL = "http://127.0.0.1:31337/health"


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def clean_slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value)
    return cleaned.strip(".-_")[:80] or "item"


def sample_slug(path: Path) -> str:
    if path.parent.name == "source" and path.parent.parent.name:
        stem = path.parent.parent.name
    else:
        stem = path.stem or path.name or "pdf"
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:8]
    return f"{clean_slug(stem)}-{digest}"


def basename_slug(value: str) -> str:
    stem = value or "pdf"
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in stem)
    return cleaned.strip(".-_")[:80] or "pdf"


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        return {"_load_error": str(exc)}


def command_json(cmd: list[str], *, timeout: float = 10.0) -> Any:
    try:
        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if proc.returncode != 0:
        return {"ok": False, "exit_code": proc.returncode, "stderr": proc.stderr[:1000]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": True, "stdout": proc.stdout[:1000]}


def payload(data: Any) -> dict[str, Any]:
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data if isinstance(data, dict) else {}


def summarize_response(data: Any) -> dict[str, Any]:
    body = payload(data)
    markdown = body.get("markdown") if isinstance(body.get("markdown"), str) else ""
    html = body.get("html") if isinstance(body.get("html"), str) else ""
    images = body.get("images") if isinstance(body.get("images"), list) else []
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    return {
        "success": data.get("success") if isinstance(data, dict) else None,
        "markdown_len": len(markdown),
        "html_len": len(html),
        "image_count": len(images),
        "num_pages": metadata.get("numPages"),
        "credits_used": metadata.get("creditsUsed"),
        "title": metadata.get("title"),
        "preview": markdown[:500],
    }


def expected_failure(mode: str, proc: subprocess.CompletedProcess[str]) -> bool:
    if proc.returncode == 0:
        return False
    return mode == "fast" and "SCRAPE_PDF_OCR_REQUIRED" in proc.stderr


def run_case(args: argparse.Namespace, pdf: Path, mode: str, out_dir: Path) -> dict[str, Any]:
    sample = sample_slug(pdf)
    case_slug = f"{sample}-{mode}"
    case_dir = out_dir / case_slug
    fields_dir = case_dir / "fields"
    response_path = case_dir / "response.json"
    case_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(REQUEST_HELPER),
        "parse",
        str(pdf),
        "--api-url",
        args.api_url,
        "--formats",
        args.formats,
        "--pdf-mode",
        mode,
        "--max-pages",
        str(args.max_pages),
        "--out",
        str(response_path),
        "--save-fields",
        str(fields_dir),
        "--pretty",
        "--quiet",
        "--timeout",
        str(args.timeout),
    ]
    if args.api_key:
        cmd.extend(["--api-key", args.api_key])

    started = time.time()
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    elapsed = time.time() - started

    (case_dir / "stdout.txt").write_text(proc.stdout)
    (case_dir / "stderr.txt").write_text(proc.stderr)
    (case_dir / "exit_code.txt").write_text(f"{proc.returncode}\n")

    data = load_json(response_path) if response_path.exists() else {}
    summary = {
        "sample": sample,
        "pdf": str(pdf),
        "mode": mode,
        "exit_code": proc.returncode,
        "expected_failure": expected_failure(mode, proc),
        "seconds": round(elapsed, 2),
        "response": str(response_path) if response_path.exists() else None,
        "fields": str(fields_dir) if fields_dir.exists() else None,
        "stderr_preview": proc.stderr[:1000],
        **summarize_response(data),
    }
    (case_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return summary


def write_markdown_summary(out_dir: Path, summaries: list[dict[str, Any]]) -> None:
    lines = [
        "# Firecrawl PDF OCR Benchmark",
        "",
        "| PDF | Mode | Exit | Expected | Seconds | Markdown | HTML | Pages | Images | Credits |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in summaries:
        lines.append(
            "| {pdf} | {mode} | {exit_code} | {expected_failure} | {seconds} | {markdown_len} | {html_len} | {num_pages} | {image_count} | {credits_used} |".format(
                pdf=f"{item.get('sample', basename_slug(Path(item['pdf']).name))}",
                mode=item["mode"],
                exit_code=item["exit_code"],
                expected_failure="yes" if item.get("expected_failure") else "",
                seconds=item["seconds"],
                markdown_len=item.get("markdown_len"),
                html_len=item.get("html_len"),
                num_pages=item.get("num_pages"),
                image_count=item.get("image_count"),
                credits_used=item.get("credits_used"),
            )
        )
    lines.append("")
    lines.append("Note: `creditsUsed` is Firecrawl's local per-page accounting field in this self-hosted run; it is not Firecrawl cloud credit spend.")
    lines.append("`Expected=yes` marks known-good failures such as `fast` mode refusing OCR-required PDFs.")
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")


def write_metadata(args: argparse.Namespace, out_dir: Path, summaries: list[dict[str, Any]], started: float) -> None:
    unexpected_failures = [
        item for item in summaries if item.get("exit_code") != 0 and not item.get("expected_failure")
    ]
    metadata = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(started)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "seconds": round(time.time() - started, 2),
        "command": sys.argv,
        "api_url": args.api_url,
        "modes": parse_csv(args.modes),
        "formats": parse_csv(args.formats),
        "max_pages": args.max_pages,
        "adapter_health_url": args.adapter_health_url,
        "adapter_health": command_json(["curl", "-fsS", args.adapter_health_url]),
        "docker_context": command_json(["docker", "context", "show"]),
        "case_count": len(summaries),
        "unexpected_failure_count": len(unexpected_failures),
        "unexpected_failures": unexpected_failures,
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdfs", nargs="+", type=Path)
    parser.add_argument("--modes", default="fast,auto,ocr", help="Comma-separated parser modes.")
    parser.add_argument("--formats", default="markdown,html,images")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--api-url", default="http://localhost:3002")
    parser.add_argument("--api-key")
    parser.add_argument("--adapter-health-url", default=DEFAULT_ADAPTER_HEALTH_URL)
    parser.add_argument("--strict", action="store_true", help="Exit 1 on unexpected parse failures.")
    parser.add_argument("--out-dir", type=Path, default=Path("tasks/tmp/firecrawl-pdf-ocr-benchmark"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    started = time.time()
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    modes = parse_csv(args.modes)
    summaries: list[dict[str, Any]] = []
    for pdf in args.pdfs:
        pdf = pdf.expanduser().resolve()
        if not pdf.is_file():
            print(f"Skipping missing PDF: {pdf}", file=sys.stderr)
            continue
        for mode in modes:
            print(f"Running {pdf.name} mode={mode}", file=sys.stderr)
            summaries.append(run_case(args, pdf, mode, out_dir))

    (out_dir / "summary.json").write_text(json.dumps(summaries, indent=2, ensure_ascii=False) + "\n")
    write_markdown_summary(out_dir, summaries)
    write_metadata(args, out_dir, summaries, started)
    print(out_dir)
    if args.strict:
        unexpected = [item for item in summaries if item.get("exit_code") != 0 and not item.get("expected_failure")]
        if unexpected:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
