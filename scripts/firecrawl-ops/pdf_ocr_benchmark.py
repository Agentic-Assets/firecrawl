#!/usr/bin/env python3
"""Run a small local Firecrawl PDF parser matrix and save comparable outputs."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REQUEST_HELPER = SCRIPT_DIR / "firecrawl_request.py"


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def slugify(path: Path) -> str:
    stem = path.stem or "pdf"
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in stem)
    return cleaned.strip(".-_")[:80] or "pdf"


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        return {"_load_error": str(exc)}


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


def run_case(args: argparse.Namespace, pdf: Path, mode: str, out_dir: Path) -> dict[str, Any]:
    case_slug = f"{slugify(pdf)}-{mode}"
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
        "pdf": str(pdf),
        "mode": mode,
        "exit_code": proc.returncode,
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
        "| PDF | Mode | Exit | Seconds | Markdown | HTML | Pages | Images | Credits |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in summaries:
        lines.append(
            "| {pdf} | {mode} | {exit_code} | {seconds} | {markdown_len} | {html_len} | {num_pages} | {image_count} | {credits_used} |".format(
                pdf=Path(item["pdf"]).name,
                mode=item["mode"],
                exit_code=item["exit_code"],
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
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdfs", nargs="+", type=Path)
    parser.add_argument("--modes", default="fast,auto,ocr", help="Comma-separated parser modes.")
    parser.add_argument("--formats", default="markdown,html,images")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--api-url", default="http://localhost:3002")
    parser.add_argument("--api-key")
    parser.add_argument("--out-dir", type=Path, default=Path("tasks/tmp/firecrawl-pdf-ocr-benchmark"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
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
    print(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
