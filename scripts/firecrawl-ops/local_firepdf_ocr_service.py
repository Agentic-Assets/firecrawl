#!/usr/bin/env python3
"""Local FirePDF-compatible OCR adapter for self-hosted Firecrawl.

The Firecrawl API already knows how to call a FirePDF-like service at
`${FIRE_PDF_BASE_URL}/ocr`. This adapter implements that small contract and
delegates document understanding to a local Docling Serve instance.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


HOST = os.getenv("LOCAL_FIREPDF_HOST", "127.0.0.1")
PORT = int(os.getenv("LOCAL_FIREPDF_PORT", "31337"))
ENGINE = os.getenv("LOCAL_FIREPDF_ENGINE", "docling").strip().lower()
DOCLING_URL = os.getenv("LOCAL_FIREPDF_DOCLING_URL", "http://127.0.0.1:5001").rstrip("/")
TIMEOUT_SECONDS = float(os.getenv("LOCAL_FIREPDF_TIMEOUT_SECONDS", "600"))
KEEP_TEMP = os.getenv("LOCAL_FIREPDF_KEEP_TEMP", "").lower() in {"1", "true", "yes", "on"}
PROFILE_NAME = os.getenv("LOCAL_FIREPDF_PROFILE", "default").strip() or "default"
PROFILES_PATH = Path(
    os.getenv("LOCAL_FIREPDF_PROFILES_PATH", str(Path(__file__).with_name("pdf_ocr_profiles.json")))
).expanduser()


def parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        print(
            json.dumps(
                {"message": "Invalid float env value; using default", "name": name, "value": raw, "default": default}
            ),
            file=sys.stderr,
            flush=True,
        )
        return default


def parse_list_env(name: str) -> list[str] | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or None


CAPTURE_DOCLING_JSON = parse_bool_env("LOCAL_FIREPDF_CAPTURE_DOCLING_JSON", False)
OUTPUT_DIR = os.getenv("LOCAL_FIREPDF_OUTPUT_DIR", "").strip()

DEFAULT_DOCLING_OPTIONS: dict[str, Any] = {
    "from_formats": ["pdf"],
    "to_formats": ["md", "json", "html"],
    "do_ocr": True,
    "force_ocr": True,
    "ocr_preset": "auto",
    "pdf_backend": "docling_parse",
    "table_mode": "accurate",
    "table_cell_matching": True,
    "do_table_structure": True,
    "include_images": True,
    "images_scale": 2.0,
    "image_export_mode": "placeholder",
    "md_page_break_placeholder": "",
    "abort_on_error": False,
    "do_code_enrichment": False,
    "do_formula_enrichment": False,
    "do_picture_classification": False,
    "do_chart_extraction": False,
    "do_picture_description": False,
}

ENV_OPTION_MAP = {
    "LOCAL_FIREPDF_DOCLING_TO_FORMATS": ("to_formats", parse_list_env),
    "LOCAL_FIREPDF_DOCLING_DO_OCR": ("do_ocr", lambda name: parse_bool_env(name, True)),
    "LOCAL_FIREPDF_DOCLING_FORCE_OCR": ("force_ocr", lambda name: parse_bool_env(name, True)),
    "LOCAL_FIREPDF_DOCLING_OCR_PRESET": ("ocr_preset", lambda name: os.getenv(name, "").strip()),
    "LOCAL_FIREPDF_DOCLING_OCR_LANG": ("ocr_lang", parse_list_env),
    "LOCAL_FIREPDF_DOCLING_PDF_BACKEND": ("pdf_backend", lambda name: os.getenv(name, "").strip()),
    "LOCAL_FIREPDF_DOCLING_TABLE_MODE": ("table_mode", lambda name: os.getenv(name, "").strip()),
    "LOCAL_FIREPDF_DOCLING_TABLE_CELL_MATCHING": ("table_cell_matching", lambda name: parse_bool_env(name, True)),
    "LOCAL_FIREPDF_DOCLING_DO_TABLE_STRUCTURE": ("do_table_structure", lambda name: parse_bool_env(name, True)),
    "LOCAL_FIREPDF_DOCLING_INCLUDE_IMAGES": ("include_images", lambda name: parse_bool_env(name, True)),
    "LOCAL_FIREPDF_DOCLING_IMAGES_SCALE": ("images_scale", lambda name: parse_float_env(name, 2.0)),
    "LOCAL_FIREPDF_DOCLING_IMAGE_EXPORT_MODE": ("image_export_mode", lambda name: os.getenv(name, "").strip()),
    "LOCAL_FIREPDF_DOCLING_MD_PAGE_BREAK": ("md_page_break_placeholder", lambda name: os.getenv(name, "")),
    "LOCAL_FIREPDF_DOCLING_DO_CODE_ENRICHMENT": ("do_code_enrichment", lambda name: parse_bool_env(name, False)),
    "LOCAL_FIREPDF_DOCLING_DO_FORMULA_ENRICHMENT": ("do_formula_enrichment", lambda name: parse_bool_env(name, False)),
    "LOCAL_FIREPDF_DOCLING_DO_PICTURE_CLASSIFICATION": ("do_picture_classification", lambda name: parse_bool_env(name, False)),
    "LOCAL_FIREPDF_DOCLING_DO_CHART_EXTRACTION": ("do_chart_extraction", lambda name: parse_bool_env(name, False)),
    "LOCAL_FIREPDF_DOCLING_DO_PICTURE_DESCRIPTION": ("do_picture_description", lambda name: parse_bool_env(name, False)),
    "LOCAL_FIREPDF_DOCLING_VLM_PIPELINE_PRESET": ("vlm_pipeline_preset", lambda name: os.getenv(name, "").strip()),
    "LOCAL_FIREPDF_DOCLING_PICTURE_DESCRIPTION_PRESET": (
        "picture_description_preset",
        lambda name: os.getenv(name, "").strip(),
    ),
    "LOCAL_FIREPDF_DOCLING_CODE_FORMULA_PRESET": ("code_formula_preset", lambda name: os.getenv(name, "").strip()),
    "LOCAL_FIREPDF_DOCLING_TABLE_STRUCTURE_PRESET": (
        "table_structure_preset",
        lambda name: os.getenv(name, "").strip(),
    ),
    "LOCAL_FIREPDF_DOCLING_LAYOUT_PRESET": ("layout_preset", lambda name: os.getenv(name, "").strip()),
}


class AdapterError(Exception):
    def __init__(self, message: str, *, status: int = 500, details: Any | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.details = details


def log(message: str, **fields: Any) -> None:
    payload = {"message": message, **fields}
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)


def read_json_request(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        raise AdapterError("Missing JSON body", status=400)
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise AdapterError(f"Invalid JSON body: {exc}", status=400) from exc
    if not isinstance(data, dict):
        raise AdapterError("JSON body must be an object", status=400)
    return data


def write_json(handler: BaseHTTPRequestHandler, status: int, body: dict[str, Any]) -> None:
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def remaining_timeout(request_body: dict[str, Any]) -> float:
    timeout_ms = request_body.get("timeout")
    created_at = request_body.get("created_at")
    if isinstance(timeout_ms, (int, float)) and isinstance(created_at, (int, float)):
        elapsed = max(0.0, (time.time() * 1000.0) - float(created_at))
        remaining = (float(timeout_ms) - elapsed) / 1000.0
        return max(5.0, min(TIMEOUT_SECONDS, remaining))
    return TIMEOUT_SECONDS


def decode_pdf(request_body: dict[str, Any], target: Path) -> None:
    encoded = request_body.get("pdf") or request_body.get("pdf_b64")
    if not isinstance(encoded, str) or not encoded:
        raise AdapterError("Missing required base64 PDF field: pdf", status=400)
    try:
        target.write_bytes(base64.b64decode(encoded, validate=True))
    except Exception as exc:
        raise AdapterError(f"Invalid base64 PDF: {exc}", status=400) from exc
    if target.stat().st_size == 0:
        raise AdapterError("Decoded PDF is empty", status=400)


def count_pages(pdf_path: Path) -> int | None:
    pdfinfo = shutil.which("pdfinfo")
    if not pdfinfo:
        return None
    proc = subprocess.run(
        [pdfinfo, str(pdf_path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        log("pdfinfo failed", stderr=proc.stderr.strip())
        return None
    for line in proc.stdout.splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def capped_page_count(max_pages: int | None, total_pages: int | None) -> int | None:
    if not max_pages or max_pages <= 0:
        return total_pages
    if total_pages is None:
        return max_pages
    return min(max_pages, total_pages)


def limited_pdf_if_possible(pdf_path: Path, max_pages: int | None, total_pages: int | None, tmpdir: Path) -> tuple[Path, int | None]:
    if not max_pages or max_pages <= 0:
        return pdf_path, total_pages
    if total_pages is not None and total_pages <= max_pages:
        return pdf_path, total_pages

    pdfseparate = shutil.which("pdfseparate")
    pdfunite = shutil.which("pdfunite")
    if not pdfseparate or not pdfunite:
        log("Cannot enforce max_pages without pdfseparate/pdfunite", max_pages=max_pages)
        return pdf_path, capped_page_count(max_pages, total_pages)

    page_pattern = tmpdir / "page-%04d.pdf"
    limited_path = tmpdir / "limited.pdf"
    sep = subprocess.run(
        [pdfseparate, "-f", "1", "-l", str(max_pages), str(pdf_path), str(page_pattern)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if sep.returncode != 0:
        log("pdfseparate failed", stderr=sep.stderr.strip())
        return pdf_path, capped_page_count(max_pages, total_pages)
    page_files = sorted(tmpdir.glob("page-*.pdf"))
    if not page_files:
        return pdf_path, total_pages
    unite = subprocess.run(
        [pdfunite, *[str(path) for path in page_files], str(limited_path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if unite.returncode != 0:
        log("pdfunite failed", stderr=unite.stderr.strip())
        return pdf_path, capped_page_count(max_pages, total_pages)
    return limited_path, len(page_files)


def post_json(url: str, payload: dict[str, Any], timeout: float) -> Any:
    encoded = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise AdapterError(f"Docling returned HTTP {exc.code}", status=502, details=body) from exc
    except URLError as exc:
        raise AdapterError(f"Could not reach Docling Serve: {exc}", status=502) from exc
    except json.JSONDecodeError as exc:
        raise AdapterError(f"Docling returned invalid JSON: {exc}", status=502) from exc


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_profiles() -> dict[str, Any]:
    try:
        raw = json.loads(PROFILES_PATH.read_text())
    except FileNotFoundError as exc:
        raise AdapterError(f"Missing OCR profile registry: {PROFILES_PATH}", status=500) from exc
    except json.JSONDecodeError as exc:
        raise AdapterError(f"Invalid OCR profile registry {PROFILES_PATH}: {exc}", status=500) from exc
    if not isinstance(raw, dict):
        raise AdapterError(f"OCR profile registry must be a JSON object: {PROFILES_PATH}", status=500)
    return raw


def resolve_profile(profile_name: str, profiles: dict[str, Any] | None = None, stack: list[str] | None = None) -> dict[str, Any]:
    profiles = profiles if profiles is not None else load_profiles()
    stack = stack or []
    if profile_name not in profiles:
        available = ", ".join(sorted(profiles.keys()))
        raise AdapterError(f"Unknown LOCAL_FIREPDF_PROFILE={profile_name!r}. Available profiles: {available}", status=400)
    if profile_name in stack:
        raise AdapterError(f"OCR profile inheritance cycle: {' -> '.join([*stack, profile_name])}", status=500)

    current = profiles[profile_name]
    if not isinstance(current, dict):
        raise AdapterError(f"OCR profile {profile_name!r} must be an object", status=500)

    parent_name = current.get("extends")
    if isinstance(parent_name, str) and parent_name.strip():
        resolved = resolve_profile(parent_name.strip(), profiles, [*stack, profile_name])
    else:
        resolved = {}
    resolved = deep_merge(resolved, current)
    resolved.pop("extends", None)
    return resolved


def profile_capture_enabled(profile: dict[str, Any]) -> bool:
    value = profile.get("capture_docling_json")
    return bool(value) if isinstance(value, bool) else False


def env_docling_overrides() -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for env_name, (option_key, parser) in ENV_OPTION_MAP.items():
        if env_name not in os.environ:
            continue
        value = parser(env_name)
        if value is None:
            continue
        if isinstance(value, str) and value == "" and option_key != "md_page_break_placeholder":
            continue
        overrides[option_key] = value
    return overrides


def docling_options(timeout: float, max_pages: int | None) -> dict[str, Any]:
    profile = resolve_profile(PROFILE_NAME)
    options = deep_merge(DEFAULT_DOCLING_OPTIONS, profile.get("docling_options") or {})
    options = deep_merge(options, env_docling_overrides())
    options["document_timeout"] = timeout
    options["abort_on_error"] = False
    if max_pages and max_pages > 0:
        options["page_range"] = [1, max_pages]
    return options


def merge_docling_overrides(options: dict[str, Any], request_body: dict[str, Any]) -> dict[str, Any]:
    """Allow direct adapter callers to test Docling knobs without rebuilding.

    Firecrawl itself does not pass these fields today, so normal API use still
    follows container env settings. Direct `/ocr` smoke tests may include
    `docling_options` for fast experiments.
    """
    overrides = request_body.get("docling_options")
    if not isinstance(overrides, dict):
        return options

    allowed = {
        "to_formats",
        "do_ocr",
        "force_ocr",
        "ocr_preset",
        "ocr_lang",
        "ocr_custom_config",
        "pdf_backend",
        "table_mode",
        "table_cell_matching",
        "do_table_structure",
        "include_images",
        "images_scale",
        "image_export_mode",
        "md_page_break_placeholder",
        "do_code_enrichment",
        "do_formula_enrichment",
        "do_picture_classification",
        "do_chart_extraction",
        "do_picture_description",
        "vlm_pipeline_preset",
        "picture_description_preset",
        "code_formula_preset",
        "table_structure_preset",
        "layout_preset",
    }
    for key, value in overrides.items():
        if key in allowed:
            options[key] = value
    return options


def call_docling(pdf_path: Path, timeout: float, max_pages: int | None, request_body: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    options = merge_docling_overrides(docling_options(timeout, max_pages), request_body)
    payload = {
        "options": options,
        "sources": [
            {
                "kind": "file",
                "base64_string": base64.b64encode(pdf_path.read_bytes()).decode("ascii"),
                "filename": pdf_path.name,
            }
        ],
        "target": {"kind": "inbody"},
    }
    return post_json(f"{DOCLING_URL}/v1/convert/source", payload, timeout), options


def settings_payload() -> dict[str, Any]:
    profile = resolve_profile(PROFILE_NAME)
    capture_enabled = CAPTURE_DOCLING_JSON or profile_capture_enabled(profile)
    return {
        "engine": ENGINE,
        "profile": {
            "name": PROFILE_NAME,
            "description": profile.get("description"),
            "profiles_path": str(PROFILES_PATH),
            "capture_docling_json": capture_enabled,
            "output_dir": (OUTPUT_DIR or "/tmp/firecrawl-docling-debug") if capture_enabled else None,
            "available": sorted(load_profiles().keys()),
        },
        "adapter": {
            "host": HOST,
            "port": PORT,
            "timeout_seconds": TIMEOUT_SECONDS,
            "keep_temp": KEEP_TEMP,
        },
        "docling_url": DOCLING_URL,
        "docling_options": docling_options(TIMEOUT_SECONDS, None),
        "direct_request_overrides": {
            "field": "docling_options",
            "note": "POST /ocr may include this object for direct adapter tests; Firecrawl API calls use env/container settings.",
        },
    }


def iter_documents(docling_result: Any) -> list[dict[str, Any]]:
    if isinstance(docling_result, dict):
        if isinstance(docling_result.get("document"), dict):
            return [docling_result]
        data = docling_result.get("data")
        if isinstance(data, dict) and isinstance(data.get("document"), dict):
            return [data]
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        documents = docling_result.get("documents")
        if isinstance(documents, list):
            return [item for item in documents if isinstance(item, dict)]
        results = docling_result.get("results")
        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict)]
    if isinstance(docling_result, list):
        return [item for item in docling_result if isinstance(item, dict)]
    return []


def extract_markdown(docling_result: Any) -> tuple[str, list[Any]]:
    documents = iter_documents(docling_result)
    markdown_parts: list[str] = []
    errors: list[Any] = []
    for item in documents:
        status = item.get("status")
        if status in {"failure", "skipped"}:
            errors.extend(item.get("errors") or [status])
        elif item.get("errors"):
            errors.extend(item.get("errors") or [])

        document = item.get("document") if isinstance(item.get("document"), dict) else item
        content = (
            document.get("md_content")
            or document.get("markdown")
            or document.get("text_content")
            or document.get("text")
        )
        if not content and document.get("json_content") is not None:
            content = json.dumps(document["json_content"], indent=2, ensure_ascii=False)
        if isinstance(content, str) and content.strip():
            markdown_parts.append(content.rstrip())

    if not markdown_parts and isinstance(docling_result, dict):
        direct = docling_result.get("markdown") or docling_result.get("md_content")
        if isinstance(direct, str) and direct.strip():
            markdown_parts.append(direct.rstrip())

    return "\n\n".join(markdown_parts), errors


def resolve_json_ref(document: dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#/"):
        raise KeyError(ref)
    current: Any = document
    for part in ref[2:].split("/"):
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(ref)
    return current


def first_page_no(item: dict[str, Any]) -> int | None:
    prov = item.get("prov")
    if isinstance(prov, list) and prov and isinstance(prov[0], dict):
        page_no = prov[0].get("page_no")
        if isinstance(page_no, int):
            return page_no
    return None


def render_docling_json_item(document: dict[str, Any], item: dict[str, Any], visited: set[str]) -> str:
    self_ref = item.get("self_ref")
    if isinstance(self_ref, str):
        if self_ref in visited:
            return ""
        visited.add(self_ref)

    label = str(item.get("label") or "")
    if label in {"page_header", "page_footer"}:
        return ""

    text = str(item.get("text") or item.get("orig") or "").strip()
    if text:
        if label in {"title"}:
            return f"# {text}"
        if label in {"section_header"}:
            return f"## {text}"
        if label in {"list_item"}:
            return f"- {text}"
        return text

    if label in {"picture", "table", "group"}:
        children: list[str] = []
        for child in item.get("children") or []:
            if not isinstance(child, dict) or not isinstance(child.get("$ref"), str):
                continue
            try:
                child_text = render_docling_json_item(document, resolve_json_ref(document, child["$ref"]), visited)
            except Exception:
                continue
            if child_text:
                children.append(child_text)
        prefix = "[Figure]" if label == "picture" else "[Table]" if label == "table" else ""
        return "\n\n".join([part for part in [prefix, *children] if part])

    return ""


def extract_page_markdown_from_json(docling_result: Any, marker: str) -> str | None:
    documents = iter_documents(docling_result)
    for item in documents:
        document = item.get("document") if isinstance(item.get("document"), dict) else item
        json_content = document.get("json_content") if isinstance(document, dict) else None
        if not isinstance(json_content, dict):
            continue
        body = json_content.get("body")
        if not isinstance(body, dict) or not isinstance(body.get("children"), list):
            continue

        pages: dict[int, list[str]] = {}
        visited: set[str] = set()
        for child in body["children"]:
            if not isinstance(child, dict) or not isinstance(child.get("$ref"), str):
                continue
            try:
                resolved = resolve_json_ref(json_content, child["$ref"])
            except Exception:
                continue
            if not isinstance(resolved, dict):
                continue
            page_no = first_page_no(resolved)
            if page_no is None:
                continue
            rendered = render_docling_json_item(json_content, resolved, visited)
            if rendered:
                pages.setdefault(page_no, []).append(rendered)

        if len(pages) <= 1:
            continue
        page_markdown = ["\n\n".join(pages[page_no]).strip() for page_no in sorted(pages)]
        page_markdown = [part for part in page_markdown if part]
        if len(page_markdown) > 1:
            return f"\n\n{marker}\n\n".join(page_markdown)
    return None


def safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-_")[:80] or "item"


def save_debug(scrape_id: str, data: Any, settings: dict[str, Any]) -> None:
    profile = resolve_profile(PROFILE_NAME)
    if not (CAPTURE_DOCLING_JSON or profile_capture_enabled(profile)):
        return
    out_dir = Path(OUTPUT_DIR or "/tmp/firecrawl-docling-debug").expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    prefix = f"{timestamp}-{safe_slug(scrape_id or uuid4().hex)}-{safe_slug(PROFILE_NAME)}"
    (out_dir / f"{prefix}-docling.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    (out_dir / f"{prefix}-settings.json").write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")


def handle_ocr(request_body: dict[str, Any]) -> dict[str, Any]:
    if ENGINE != "docling":
        raise AdapterError(f"Unsupported LOCAL_FIREPDF_ENGINE={ENGINE!r}", status=400)

    scrape_id = str(request_body.get("scrape_id") or uuid4().hex)
    max_pages_raw = request_body.get("max_pages")
    max_pages = int(max_pages_raw) if isinstance(max_pages_raw, (int, float)) and max_pages_raw > 0 else None

    tmp_obj = tempfile.TemporaryDirectory(prefix="firepdf-local-")
    tmpdir = Path(tmp_obj.name)
    try:
        original_pdf = tmpdir / "input.pdf"
        decode_pdf(request_body, original_pdf)
        total_pages = count_pages(original_pdf)
        pdf_path, processed_pages = limited_pdf_if_possible(original_pdf, max_pages, total_pages, tmpdir)
        if processed_pages is None:
            processed_pages = count_pages(pdf_path) or max_pages or total_pages

        timeout = remaining_timeout(request_body)
        log(
            "Starting Docling OCR",
            scrape_id=scrape_id,
            profile=PROFILE_NAME,
            total_pages=total_pages,
            processed_pages=processed_pages,
            max_pages=max_pages,
            timeout=timeout,
        )
        docling_result, effective_options = call_docling(pdf_path, timeout, max_pages, request_body)
        save_debug(
            scrape_id,
            docling_result,
            {
                **settings_payload(),
                "effective_docling_options": effective_options,
                "scrape_id": scrape_id,
                "total_pages": total_pages,
                "processed_pages": processed_pages,
            },
        )
        markdown, errors = extract_markdown(docling_result)
        page_marker = str(effective_options.get("md_page_break_placeholder") or "").strip()
        if page_marker and page_marker not in markdown:
            page_markdown = extract_page_markdown_from_json(docling_result, page_marker)
            if page_markdown and len(page_markdown) >= max(500, int(len(markdown) * 0.5)):
                log(
                    "Using Docling JSON-derived page-aware markdown",
                    scrape_id=scrape_id,
                    profile=PROFILE_NAME,
                    marker=page_marker,
                    original_length=len(markdown),
                    page_markdown_length=len(page_markdown),
                    page_break_count=page_markdown.count(page_marker),
                )
                markdown = page_markdown
        if not markdown.strip():
            raise AdapterError("Docling returned no markdown/text content", status=502, details=docling_result)

        return {
            "markdown": markdown,
            "failed_pages": [],
            "pages_processed": processed_pages or 1,
            "errors": errors,
        }
    finally:
        if KEEP_TEMP:
            log("Keeping temp directory", path=str(tmpdir))
        else:
            tmp_obj.cleanup()


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalFirePDFAdapter/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        log("http", client=self.client_address[0], line=fmt % args)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/settings", "/config"}:
            write_json(self, 200, {"ok": True, **settings_payload()})
            return
        if path not in {"/health", "/healthz"}:
            write_json(self, 404, {"ok": False, "error": "not found"})
            return
        docling_ok = False
        docling_error = None
        try:
            with urlopen(f"{DOCLING_URL}/docs", timeout=5) as resp:
                docling_ok = 200 <= resp.status < 500
        except Exception as exc:
            docling_error = str(exc)
        write_json(
            self,
            200 if docling_ok else 503,
            {
                "ok": docling_ok,
                "engine": ENGINE,
                "docling_ok": docling_ok,
                "docling_error": docling_error,
                **settings_payload(),
            },
        )

    def do_POST(self) -> None:
        if self.path != "/ocr":
            write_json(self, 404, {"ok": False, "error": "not found"})
            return
        try:
            request_body = read_json_request(self)
            response = handle_ocr(request_body)
            write_json(
                self,
                200,
                {
                    "markdown": response["markdown"],
                    "failed_pages": response["failed_pages"],
                    "pages_processed": response["pages_processed"],
                },
            )
        except AdapterError as exc:
            log("adapter error", error=str(exc), details=exc.details)
            write_json(self, exc.status, {"error": str(exc), "details": exc.details})
        except Exception as exc:
            log("unhandled error", error=str(exc), traceback=traceback.format_exc())
            write_json(self, 500, {"error": str(exc)})


def main() -> int:
    try:
        settings_payload()
    except AdapterError as exc:
        log("adapter startup failed", error=str(exc), details=exc.details)
        return 2

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)

    def shutdown(signum: int, _frame: Any) -> None:
        log("shutting down", signal=signum)
        httpd.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log(
        "local FirePDF adapter listening",
        host=HOST,
        port=PORT,
        engine=ENGINE,
        profile=PROFILE_NAME,
        docling_url=DOCLING_URL,
    )
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
