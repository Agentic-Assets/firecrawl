#!/usr/bin/env python3
"""Run a small local Firecrawl PDF parser matrix and save comparable outputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REQUEST_HELPER = SCRIPT_DIR / "firecrawl_request.py"
OCR_HELPER = SCRIPT_DIR / "local_firepdf_ocr.sh"
DEFAULT_ADAPTER_HEALTH_URL = "http://127.0.0.1:31337/health"
PAGE_BREAK_MARKER = "FIRECRAWLPAGEBREAK"


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


def is_pdf_like(path: Path) -> bool:
    try:
        return b"%PDF-" in path.read_bytes()[:1024]
    except OSError:
        return False


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


def count_table_signals(markdown: str, html: str = "") -> int:
    table_lines = sum(1 for line in markdown.splitlines() if line.count("|") >= 2)
    html_tables = len(re.findall(r"<table\b", html, flags=re.IGNORECASE))
    return table_lines + html_tables


def count_figure_signals(markdown: str, html: str = "", images: list[Any] | None = None) -> int:
    image_count = len(images or [])
    figure_words = len(re.findall(r"\b(fig(?:ure)?\.?|chart|image|caption|diagram)\b", markdown, flags=re.IGNORECASE))
    html_figures = len(re.findall(r"<(?:figure|img)\b", html, flags=re.IGNORECASE))
    return image_count + figure_words + html_figures


def repeated_line_ratio(markdown: str) -> float:
    lines = [line.strip() for line in markdown.splitlines() if len(line.strip()) >= 8]
    if not lines:
        return 0.0
    counts: dict[str, int] = {}
    for line in lines:
        counts[line] = counts.get(line, 0) + 1
    repeated = sum(count - 1 for count in counts.values() if count > 1)
    return round(repeated / max(1, len(lines)), 4)


def page_records(markdown: str, html: str, images: list[Any], expected_pages: int | None) -> list[dict[str, Any]]:
    if PAGE_BREAK_MARKER in markdown:
        chunks = markdown.split(PAGE_BREAK_MARKER)
        marker_based = True
    else:
        chunks = [markdown]
        marker_based = False

    records: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        text = chunk.strip()
        words = len(text.split())
        warnings: list[str] = []
        if not text:
            warnings.append("empty_page")
        elif words < 25:
            warnings.append("low_text")
        records.append(
            {
                "page_index": index,
                "markdown": text,
                "char_count": len(text),
                "word_count": words,
                "table_signal_count": count_table_signals(text),
                "figure_signal_count": count_figure_signals(text),
                "warnings": warnings,
            }
        )

    if expected_pages and not marker_based and expected_pages > 1:
        records[0]["warnings"].append("page_breaks_missing")
    return records


def write_pages_jsonl(fields_dir: Path, markdown: str, html: str, images: list[Any], expected_pages: int | None) -> list[dict[str, Any]]:
    records = page_records(markdown, html, images, expected_pages)
    fields_dir.mkdir(parents=True, exist_ok=True)
    target = fields_dir / "pages.jsonl"
    with target.open("w") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return records


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
    pdf_ocr = metadata.get("pdfOcr") if isinstance(metadata.get("pdfOcr"), dict) else {}
    pdf_ocr_quality = pdf_ocr.get("quality") if isinstance(pdf_ocr.get("quality"), dict) else {}
    pdf_ocr_boundaries = pdf_ocr.get("page_boundaries") if isinstance(pdf_ocr.get("page_boundaries"), dict) else {}
    pdf_ocr_docling = pdf_ocr.get("docling") if isinstance(pdf_ocr.get("docling"), dict) else {}
    docling_json_summary = (
        pdf_ocr_docling.get("json_summary")
        if isinstance(pdf_ocr_docling.get("json_summary"), dict)
        else {}
    )
    page_break_count = markdown.count(PAGE_BREAK_MARKER)
    return {
        "success": data.get("success") if isinstance(data, dict) else None,
        "markdown_len": len(markdown),
        "word_count": len(markdown.split()),
        "line_count": len(markdown.splitlines()),
        "chars_per_page": round(len(markdown) / metadata.get("numPages"), 1)
        if isinstance(metadata.get("numPages"), (int, float)) and metadata.get("numPages")
        else None,
        "html_len": len(html),
        "image_count": len(images),
        "num_pages": metadata.get("numPages"),
        "page_break_count": page_break_count,
        "page_boundary_source": pdf_ocr_boundaries.get("source"),
        "page_record_count": page_break_count + 1 if markdown else 0,
        "table_signal_count": count_table_signals(markdown, html),
        "figure_signal_count": count_figure_signals(markdown, html, images),
        "docling_table_count": docling_json_summary.get("table_count"),
        "docling_picture_count": docling_json_summary.get("picture_count"),
        "replacement_char_count": markdown.count("\ufffd"),
        "repeated_line_ratio": repeated_line_ratio(markdown),
        "ocr_low_quality": pdf_ocr_quality.get("low_quality"),
        "ocr_boilerplate_score": pdf_ocr_quality.get("boilerplate_score"),
        "ocr_nonempty_pages": pdf_ocr_quality.get("populated_pages"),
        "ocr_empty_page_ratio": pdf_ocr_quality.get("empty_page_ratio"),
        "ocr_warnings": pdf_ocr_quality.get("warnings") if isinstance(pdf_ocr_quality.get("warnings"), list) else [],
        "ocr_settings_fingerprint": pdf_ocr.get("settings_fingerprint"),
        "ocr_profile": pdf_ocr.get("profile"),
        "credits_used": metadata.get("creditsUsed"),
        "title": metadata.get("title"),
        "preview": markdown[:500],
    }


def expected_failure(mode: str, proc: subprocess.CompletedProcess[str]) -> bool:
    if proc.returncode == 0:
        return False
    return mode == "fast" and "SCRAPE_PDF_OCR_REQUIRED" in proc.stderr


def preflight_failure(pdf: Path, reason: str) -> dict[str, Any]:
    return {
        "sample": sample_slug(pdf),
        "pdf": str(pdf),
        "mode": "preflight",
        "exit_code": 2,
        "expected_failure": False,
        "seconds": 0.0,
        "response": None,
        "fields": None,
        "stderr_preview": reason,
        "success": False,
        "markdown_len": 0,
        "word_count": 0,
        "line_count": 0,
        "chars_per_page": None,
        "html_len": 0,
        "image_count": 0,
        "num_pages": None,
        "credits_used": None,
        "title": None,
        "preview": "",
        "preflight_error": reason,
    }


def build_qa_report(
    *,
    case_dir: Path,
    fields_dir: Path,
    data: Any,
    summary: dict[str, Any],
    adapter_settings: Any,
) -> dict[str, Any]:
    body = payload(data)
    markdown = body.get("markdown") if isinstance(body.get("markdown"), str) else ""
    html = body.get("html") if isinstance(body.get("html"), str) else ""
    images = body.get("images") if isinstance(body.get("images"), list) else []
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    pdf_ocr = metadata.get("pdfOcr") if isinstance(metadata.get("pdfOcr"), dict) else {}
    pdf_ocr_quality = pdf_ocr.get("quality") if isinstance(pdf_ocr.get("quality"), dict) else {}
    pdf_ocr_boundaries = pdf_ocr.get("page_boundaries") if isinstance(pdf_ocr.get("page_boundaries"), dict) else {}
    pdf_ocr_pages = pdf_ocr.get("pages") if isinstance(pdf_ocr.get("pages"), list) else []
    expected_pages = metadata.get("numPages") if isinstance(metadata.get("numPages"), int) else None
    pages = write_pages_jsonl(fields_dir, markdown, html, images, expected_pages) if markdown else []
    low_text_pages = [page["page_index"] for page in pages if page["word_count"] < 25]
    empty_pages = [page["page_index"] for page in pages if page["word_count"] == 0]
    page_break_count = markdown.count(PAGE_BREAK_MARKER)
    missing_page_breaks = bool(expected_pages and expected_pages > 1 and page_break_count == 0)
    abstract_signal = bool(re.search(r"\babstract\b", markdown[:5000], flags=re.IGNORECASE))
    tail = markdown[-12000:]
    references_signal = bool(re.search(r"\b(references|bibliography|works cited)\b", tail, flags=re.IGNORECASE))
    elapsed_per_page = None
    if expected_pages and summary.get("seconds") is not None:
        elapsed_per_page = round(float(summary["seconds"]) / max(1, expected_pages), 3)

    warnings: list[str] = []
    if missing_page_breaks:
        warnings.append("missing_page_breaks")
    if low_text_pages:
        warnings.append("low_text_pages")
    if summary.get("replacement_char_count", 0):
        warnings.append("replacement_characters_present")
    if float(summary.get("repeated_line_ratio") or 0.0) > 0.15:
        warnings.append("high_repeated_line_ratio")
    if not abstract_signal:
        warnings.append("abstract_not_detected_early")
    for warning in pdf_ocr_quality.get("warnings") or []:
        if isinstance(warning, str) and warning not in warnings:
            warnings.append(warning)
    if pdf_ocr_quality.get("low_quality") is True and "adapter_low_quality" not in warnings:
        warnings.append("adapter_low_quality")

    profile_info = adapter_settings.get("profile") if isinstance(adapter_settings, dict) else None
    recommendation = "accept"
    if not summary.get("success") or summary.get("exit_code") != 0:
        recommendation = "reject"
    elif pdf_ocr_quality.get("low_quality") is True:
        recommendation = "reject"
    elif warnings:
        recommendation = "manual_review"

    qa = {
        "profile": summary.get("profile"),
        "mode": summary.get("mode"),
        "recommendation": recommendation,
        "expected_pages": expected_pages,
        "page_break_count": page_break_count,
        "page_boundary_source": pdf_ocr_boundaries.get("source"),
        "page_record_count": len(pages),
        "adapter_page_summary_count": len(pdf_ocr_pages),
        "missing_page_breaks": missing_page_breaks,
        "low_text_pages": low_text_pages,
        "empty_pages": empty_pages,
        "replacement_chars": summary.get("replacement_char_count"),
        "repeated_line_ratio": summary.get("repeated_line_ratio"),
        "table_signal_count": summary.get("table_signal_count"),
        "figure_signal_count": summary.get("figure_signal_count"),
        "docling_table_count": summary.get("docling_table_count"),
        "docling_picture_count": summary.get("docling_picture_count"),
        "ocr_settings_fingerprint": summary.get("ocr_settings_fingerprint"),
        "ocr_boilerplate_score": summary.get("ocr_boilerplate_score"),
        "ocr_empty_page_ratio": summary.get("ocr_empty_page_ratio"),
        "ocr_warnings": summary.get("ocr_warnings"),
        "abstract_signal": abstract_signal,
        "references_signal": references_signal,
        "elapsed_per_page": elapsed_per_page,
        "adapter_profile": profile_info,
        "raw_docling_json_capture_enabled": bool(profile_info and profile_info.get("capture_docling_json")),
        "warnings": warnings,
    }
    (case_dir / "qa.json").write_text(json.dumps(qa, indent=2, ensure_ascii=False) + "\n")
    write_qa_markdown(case_dir / "qa.md", qa)
    return qa


def write_qa_markdown(path: Path, qa: dict[str, Any]) -> None:
    warnings = ", ".join(qa["warnings"]) if qa["warnings"] else "none"
    lines = [
        "# OCR QA",
        "",
        f"- Mode: `{qa.get('mode')}`",
        f"- Profile: `{qa.get('profile') or 'none'}`",
        f"- Recommendation: `{qa.get('recommendation')}`",
        f"- Expected pages: `{qa.get('expected_pages')}`",
        f"- Page records: `{qa.get('page_record_count')}`",
        f"- Page boundary source: `{qa.get('page_boundary_source')}`",
        f"- Page breaks: `{qa.get('page_break_count')}`",
        f"- Low-text pages: `{qa.get('low_text_pages')}`",
        f"- Table signals: `{qa.get('table_signal_count')}`",
        f"- Docling table count: `{qa.get('docling_table_count')}`",
        f"- Figure signals: `{qa.get('figure_signal_count')}`",
        f"- OCR boilerplate score: `{qa.get('ocr_boilerplate_score')}`",
        f"- OCR settings fingerprint: `{qa.get('ocr_settings_fingerprint')}`",
        f"- Repeated-line ratio: `{qa.get('repeated_line_ratio')}`",
        f"- Raw Docling JSON capture: `{qa.get('raw_docling_json_capture_enabled')}`",
        f"- Warnings: {warnings}",
        "",
    ]
    path.write_text("\n".join(lines))


def run_case(args: argparse.Namespace, pdf: Path, mode: str, profile: str | None, out_dir: Path) -> dict[str, Any]:
    sample = sample_slug(pdf)
    case_slug = f"{sample}-{mode}" if not profile else f"{sample}-{mode}-{clean_slug(profile)}"
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
    adapter_settings = command_json(["curl", "-fsS", args.adapter_settings_url], timeout=10.0)
    summary = {
        "sample": sample,
        "pdf": str(pdf),
        "mode": mode,
        "profile": profile,
        "exit_code": proc.returncode,
        "expected_failure": expected_failure(mode, proc),
        "seconds": round(elapsed, 2),
        "response": str(response_path) if response_path.exists() else None,
        "fields": str(fields_dir) if fields_dir.exists() else None,
        "stderr_preview": proc.stderr[:1000],
        **summarize_response(data),
    }
    qa = build_qa_report(case_dir=case_dir, fields_dir=fields_dir, data=data, summary=summary, adapter_settings=adapter_settings)
    summary["qa"] = str(case_dir / "qa.json")
    summary["qa_warnings"] = qa["warnings"]
    (case_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return summary


def choose_recommendations(summaries: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    by_sample: dict[str, list[dict[str, Any]]] = {}
    for item in summaries:
        by_sample.setdefault(str(item.get("sample")), []).append(item)

    recommendations: dict[str, dict[str, str]] = {}
    for sample, items in by_sample.items():
        preflight = next((item for item in items if item.get("preflight_error")), None)
        if preflight:
            recommendations[sample] = {
                "mode": "none",
                "profile": "none",
                "decision": "reject",
                "reason": str(preflight["preflight_error"]),
            }
            continue

        viable = [
            item
            for item in items
            if item.get("exit_code") == 0 and isinstance(item.get("markdown_len"), int) and item.get("markdown_len", 0) > 0
            and item.get("ocr_low_quality") is not True
        ]
        if not viable:
            low_quality = [
                item
                for item in items
                if item.get("exit_code") == 0 and item.get("ocr_low_quality") is True
            ]
            if low_quality:
                recommendations[sample] = {
                    "mode": "none",
                    "profile": "none",
                    "decision": "reject",
                    "reason": "Only low-quality OCR output was produced; reject or send to manual review.",
                }
                continue
            recommendations[sample] = {
                "mode": "none",
                "profile": "none",
                "decision": "reject",
                "reason": "No parser mode produced markdown.",
            }
            continue
        if len(viable) == 1:
            only = viable[0]
            failed_modes = sorted(
                {
                    str(item.get("mode"))
                    for item in items
                    if item.get("exit_code") != 0 and not item.get("preflight_error")
                }
            )
            reason = "Only one parser mode produced markdown."
            if failed_modes:
                reason = f"Only {only.get('mode')} produced markdown; failed modes: {', '.join(failed_modes)}."
            recommendations[sample] = {
                "mode": str(only.get("mode")),
                "profile": str(only.get("profile") or "none"),
                "decision": "manual_review" if only.get("qa_warnings") else "accept",
                "reason": reason,
            }
            continue

        by_mode = {f"{item.get('mode')}:{item.get('profile') or 'none'}": item for item in viable}
        fast_candidates = [item for item in viable if item.get("mode") == "fast"]
        auto_candidates = [item for item in viable if item.get("mode") == "auto"]
        ocr_candidates = [item for item in viable if item.get("mode") == "ocr"]
        fast = max(fast_candidates, key=lambda item: (item.get("markdown_len", 0), -item.get("seconds", 0)), default=None)
        ocr_best = max(
            [*auto_candidates, *ocr_candidates],
            key=lambda item: (
                item.get("markdown_len", 0),
                item.get("page_break_count", 0),
                item.get("table_signal_count", 0),
                -item.get("seconds", 0),
            ),
            default=None,
        )

        if fast and ocr_best:
            fast_len = int(fast.get("markdown_len", 0))
            ocr_len = int(ocr_best.get("markdown_len", 0))
            fast_words = int(fast.get("word_count", 0))
            ocr_words = int(ocr_best.get("word_count", 0))
            if fast_len >= max(1000, int(ocr_len * 1.5)) and fast_words >= int(ocr_words * 1.25):
                recommendations[sample] = {
                    "mode": "fast",
                    "profile": "none",
                    "decision": "accept" if not fast.get("qa_warnings") else "manual_review",
                    "reason": "Born-digital text extraction produced much more text and was faster than OCR.",
                }
                continue
            if ocr_len >= int(fast_len * 1.1) or ocr_words >= int(fast_words * 1.1):
                recommendations[sample] = {
                    "mode": str(ocr_best.get("mode")),
                    "profile": str(ocr_best.get("profile") or "none"),
                    "decision": "accept" if not ocr_best.get("qa_warnings") else "manual_review",
                    "reason": "OCR/layout extraction produced more recoverable text than fast mode; inspect tables, page breaks, and layout.",
                }
                continue

        fastest = min(viable, key=lambda item: item.get("seconds", 0))
        recommendations[sample] = {
            "mode": str(fastest.get("mode")),
            "profile": str(fastest.get("profile") or "none"),
            "decision": "accept" if not fastest.get("qa_warnings") else "manual_review",
            "reason": "Outputs were similar enough; prefer the fastest successful mode.",
        }
    return recommendations


def write_markdown_summary(out_dir: Path, summaries: list[dict[str, Any]]) -> None:
    recommendations = choose_recommendations(summaries)
    lines = [
        "# Firecrawl PDF OCR Benchmark",
        "",
        "## Recommended Mode",
        "",
        "| PDF | Mode | Profile | Why |",
        "| --- | --- | --- | --- |",
    ]
    for sample, item in sorted(recommendations.items()):
        decision = item.get("decision", "manual_review")
        lines.append(f"| {sample} | {item['mode']} | {item.get('profile', 'none')} | {decision}: {item['reason']} |")
    lines.extend([
        "",
        "## Raw Results",
        "",
        "| PDF | Mode | Profile | Exit | Expected | Seconds | Markdown | Boundary | Pages | Tables | Docling Tables | Boilerplate | OCR Quality | Warnings |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- | --- |",
    ])
    for item in summaries:
        lines.append(
            "| {pdf} | {mode} | {profile} | {exit_code} | {expected_failure} | {seconds} | {markdown_len} | {boundary} | {num_pages} | {table_signal_count} | {docling_table_count} | {boilerplate} | {ocr_quality} | {warnings} |".format(
                pdf=f"{item.get('sample', basename_slug(Path(item['pdf']).name))}",
                mode=item["mode"],
                profile=item.get("profile") or "none",
                exit_code=item["exit_code"],
                expected_failure="yes" if item.get("expected_failure") else "",
                seconds=item["seconds"],
                markdown_len=item.get("markdown_len"),
                boundary=item.get("page_boundary_source") or f"markers:{item.get('page_break_count')}",
                num_pages=item.get("num_pages"),
                table_signal_count=item.get("table_signal_count"),
                docling_table_count=item.get("docling_table_count"),
                boilerplate=item.get("ocr_boilerplate_score"),
                ocr_quality="low" if item.get("ocr_low_quality") is True else "ok" if item.get("ocr_low_quality") is False else "",
                warnings=", ".join(item.get("qa_warnings") or []),
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
        "profiles": parse_csv(args.profiles) if args.profiles else [],
        "formats": parse_csv(args.formats),
        "max_pages": args.max_pages,
        "adapter_health_url": args.adapter_health_url,
        "adapter_health": command_json(["curl", "-fsS", args.adapter_health_url]),
        "adapter_settings": command_json(["curl", "-fsS", args.adapter_settings_url]),
        "docker_context": command_json(["docker", "context", "show"]),
        "case_count": len(summaries),
        "recommendations": choose_recommendations(summaries),
        "unexpected_failure_count": len(unexpected_failures),
        "unexpected_failures": unexpected_failures,
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdfs", nargs="+", type=Path)
    parser.add_argument("--modes", default="fast,auto,ocr", help="Comma-separated parser modes.")
    parser.add_argument(
        "--profiles",
        default="",
        help="Comma-separated local Docling OCR profiles for auto/ocr cases. Fast mode runs once without a profile.",
    )
    parser.add_argument("--formats", default="markdown,html,images")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--api-url", default="http://localhost:3002")
    parser.add_argument("--api-key")
    parser.add_argument("--adapter-health-url", default=DEFAULT_ADAPTER_HEALTH_URL)
    parser.add_argument("--adapter-settings-url", default="http://127.0.0.1:31337/settings")
    parser.add_argument(
        "--no-profile-restart",
        action="store_true",
        help="Do not restart the adapter between profiles; assumes the caller already set the desired process-level profile.",
    )
    parser.add_argument(
        "--capture-json",
        action="store_true",
        help="When restarting profile cases, ask the adapter to capture raw Docling JSON/settings.",
    )
    parser.add_argument("--strict", action="store_true", help="Exit 1 on unexpected parse failures.")
    parser.add_argument("--out-dir", type=Path, default=Path("tasks/tmp/firecrawl-pdf-ocr-benchmark"))
    return parser


def restart_adapter_for_profile(args: argparse.Namespace, profile: str, out_dir: Path) -> None:
    if args.no_profile_restart:
        return
    cmd = [str(OCR_HELPER), "restart-adapter", "--profile", profile]
    if args.capture_json:
        cmd.append("--capture-json")
        cmd.extend(["--output-dir", str(out_dir / "docling-debug")])
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    profile_dir = out_dir / "_adapter-restarts"
    profile_dir.mkdir(parents=True, exist_ok=True)
    target_prefix = profile_dir / clean_slug(profile)
    (target_prefix.with_suffix(".stdout.txt")).write_text(proc.stdout)
    (target_prefix.with_suffix(".stderr.txt")).write_text(proc.stderr)
    if proc.returncode != 0:
        raise SystemExit(f"Failed to restart local FirePDF adapter for profile {profile!r}. See {profile_dir}.")


def cases_for_modes_and_profiles(modes: list[str], profiles: list[str]) -> list[tuple[str, str | None]]:
    cases: list[tuple[str, str | None]] = []
    for mode in modes:
        if mode == "fast" or not profiles:
            cases.append((mode, None))
            continue
        for profile in profiles:
            cases.append((mode, profile))
    return cases


def main() -> int:
    args = build_parser().parse_args()
    started = time.time()
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    modes = parse_csv(args.modes)
    profiles = parse_csv(args.profiles)
    summaries: list[dict[str, Any]] = []
    for pdf in args.pdfs:
        pdf = pdf.expanduser().resolve()
        if not pdf.is_file():
            print(f"Preflight failed for {pdf}: file does not exist", file=sys.stderr)
            summaries.append(preflight_failure(pdf, "File does not exist."))
            continue
        if not is_pdf_like(pdf):
            print(f"Preflight failed for {pdf}: missing PDF header", file=sys.stderr)
            summaries.append(preflight_failure(pdf, "File is not a PDF; it is missing a %PDF header."))
            continue
        last_profile: str | None = None
        for mode, profile in cases_for_modes_and_profiles(modes, profiles):
            if profile and profile != last_profile:
                print(f"Applying local OCR profile={profile}", file=sys.stderr)
                restart_adapter_for_profile(args, profile, out_dir)
                last_profile = profile
            print(f"Running {pdf.name} mode={mode} profile={profile or 'none'}", file=sys.stderr)
            summaries.append(run_case(args, pdf, mode, profile, out_dir))

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
