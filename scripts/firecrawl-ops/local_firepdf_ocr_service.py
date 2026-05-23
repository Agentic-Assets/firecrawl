#!/usr/bin/env python3
"""Local FirePDF-compatible OCR adapter for self-hosted Firecrawl.

The Firecrawl API already knows how to call a FirePDF-like service at
`${FIRE_PDF_BASE_URL}/ocr`. This adapter implements that small contract and
delegates document understanding to a local Docling Serve instance.
"""

from __future__ import annotations

import base64
import json
import os
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
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


HOST = os.getenv("LOCAL_FIREPDF_HOST", "127.0.0.1")
PORT = int(os.getenv("LOCAL_FIREPDF_PORT", "31337"))
ENGINE = os.getenv("LOCAL_FIREPDF_ENGINE", "docling").strip().lower()
DOCLING_URL = os.getenv("LOCAL_FIREPDF_DOCLING_URL", "http://127.0.0.1:5001").rstrip("/")
TIMEOUT_SECONDS = float(os.getenv("LOCAL_FIREPDF_TIMEOUT_SECONDS", "240"))
KEEP_TEMP = os.getenv("LOCAL_FIREPDF_KEEP_TEMP", "").lower() in {"1", "true", "yes", "on"}
OUTPUT_DIR = os.getenv("LOCAL_FIREPDF_OUTPUT_DIR", "").strip()
TO_FORMATS = [
    item.strip()
    for item in os.getenv("LOCAL_FIREPDF_DOCLING_TO_FORMATS", "md,json,html").split(",")
    if item.strip()
]


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
        log("Invalid float env value; using default", name=name, value=raw, default=default)
        return default


def parse_list_env(name: str) -> list[str] | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or None


DO_OCR = parse_bool_env("LOCAL_FIREPDF_DOCLING_DO_OCR", True)
FORCE_OCR = parse_bool_env("LOCAL_FIREPDF_DOCLING_FORCE_OCR", True)
OCR_PRESET = os.getenv("LOCAL_FIREPDF_DOCLING_OCR_PRESET", "auto").strip() or "auto"
OCR_LANG = parse_list_env("LOCAL_FIREPDF_DOCLING_OCR_LANG")
PDF_BACKEND = os.getenv("LOCAL_FIREPDF_DOCLING_PDF_BACKEND", "docling_parse").strip() or "docling_parse"
TABLE_MODE = os.getenv("LOCAL_FIREPDF_DOCLING_TABLE_MODE", "accurate").strip() or "accurate"
TABLE_CELL_MATCHING = parse_bool_env("LOCAL_FIREPDF_DOCLING_TABLE_CELL_MATCHING", True)
DO_TABLE_STRUCTURE = parse_bool_env("LOCAL_FIREPDF_DOCLING_DO_TABLE_STRUCTURE", True)
INCLUDE_IMAGES = parse_bool_env("LOCAL_FIREPDF_DOCLING_INCLUDE_IMAGES", True)
IMAGES_SCALE = parse_float_env("LOCAL_FIREPDF_DOCLING_IMAGES_SCALE", 2.0)
IMAGE_EXPORT_MODE = os.getenv("LOCAL_FIREPDF_DOCLING_IMAGE_EXPORT_MODE", "placeholder").strip() or "placeholder"
MD_PAGE_BREAK_PLACEHOLDER = os.getenv("LOCAL_FIREPDF_DOCLING_MD_PAGE_BREAK", "").strip()
DO_CODE_ENRICHMENT = parse_bool_env("LOCAL_FIREPDF_DOCLING_DO_CODE_ENRICHMENT", False)
DO_FORMULA_ENRICHMENT = parse_bool_env("LOCAL_FIREPDF_DOCLING_DO_FORMULA_ENRICHMENT", False)
DO_PICTURE_CLASSIFICATION = parse_bool_env("LOCAL_FIREPDF_DOCLING_DO_PICTURE_CLASSIFICATION", False)
DO_CHART_EXTRACTION = parse_bool_env("LOCAL_FIREPDF_DOCLING_DO_CHART_EXTRACTION", False)
DO_PICTURE_DESCRIPTION = parse_bool_env("LOCAL_FIREPDF_DOCLING_DO_PICTURE_DESCRIPTION", False)
VLM_PIPELINE_PRESET = os.getenv("LOCAL_FIREPDF_DOCLING_VLM_PIPELINE_PRESET", "").strip()
PICTURE_DESCRIPTION_PRESET = os.getenv("LOCAL_FIREPDF_DOCLING_PICTURE_DESCRIPTION_PRESET", "").strip()
CODE_FORMULA_PRESET = os.getenv("LOCAL_FIREPDF_DOCLING_CODE_FORMULA_PRESET", "").strip()
TABLE_STRUCTURE_PRESET = os.getenv("LOCAL_FIREPDF_DOCLING_TABLE_STRUCTURE_PRESET", "").strip()
LAYOUT_PRESET = os.getenv("LOCAL_FIREPDF_DOCLING_LAYOUT_PRESET", "").strip()


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


def docling_options(timeout: float, max_pages: int | None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "from_formats": ["pdf"],
        "to_formats": TO_FORMATS,
        "do_ocr": DO_OCR,
        "force_ocr": FORCE_OCR,
        "ocr_preset": OCR_PRESET,
        "pdf_backend": PDF_BACKEND,
        "table_mode": TABLE_MODE,
        "table_cell_matching": TABLE_CELL_MATCHING,
        "do_table_structure": DO_TABLE_STRUCTURE,
        "include_images": INCLUDE_IMAGES,
        "images_scale": IMAGES_SCALE,
        "image_export_mode": IMAGE_EXPORT_MODE,
        "md_page_break_placeholder": MD_PAGE_BREAK_PLACEHOLDER,
        "document_timeout": timeout,
        "abort_on_error": False,
        "do_code_enrichment": DO_CODE_ENRICHMENT,
        "do_formula_enrichment": DO_FORMULA_ENRICHMENT,
        "do_picture_classification": DO_PICTURE_CLASSIFICATION,
        "do_chart_extraction": DO_CHART_EXTRACTION,
        "do_picture_description": DO_PICTURE_DESCRIPTION,
    }
    if max_pages and max_pages > 0:
        options["page_range"] = [1, max_pages]
    if OCR_LANG:
        options["ocr_lang"] = OCR_LANG
    if VLM_PIPELINE_PRESET:
        options["vlm_pipeline_preset"] = VLM_PIPELINE_PRESET
    if PICTURE_DESCRIPTION_PRESET:
        options["picture_description_preset"] = PICTURE_DESCRIPTION_PRESET
    if CODE_FORMULA_PRESET:
        options["code_formula_preset"] = CODE_FORMULA_PRESET
    if TABLE_STRUCTURE_PRESET:
        options["table_structure_preset"] = TABLE_STRUCTURE_PRESET
    if LAYOUT_PRESET:
        options["layout_preset"] = LAYOUT_PRESET
    return options


def call_docling(pdf_path: Path, timeout: float, max_pages: int | None) -> Any:
    payload = {
        "options": docling_options(timeout, max_pages),
        "sources": [
            {
                "kind": "file",
                "base64_string": base64.b64encode(pdf_path.read_bytes()).decode("ascii"),
                "filename": pdf_path.name,
            }
        ],
        "target": {"kind": "inbody"},
    }
    return post_json(f"{DOCLING_URL}/v1/convert/source", payload, timeout)


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


def save_debug(tmpdir: Path, scrape_id: str, data: Any) -> None:
    if not OUTPUT_DIR:
        return
    out_dir = Path(OUTPUT_DIR).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{scrape_id or uuid4().hex}.docling.json"
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


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
            total_pages=total_pages,
            processed_pages=processed_pages,
            max_pages=max_pages,
            timeout=timeout,
        )
        docling_result = call_docling(pdf_path, timeout, max_pages)
        save_debug(tmpdir, scrape_id, docling_result)
        markdown, errors = extract_markdown(docling_result)
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
        if self.path not in {"/health", "/healthz"}:
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
                "docling_url": DOCLING_URL,
                "docling_ok": docling_ok,
                "docling_error": docling_error,
                "docling_options": {
                    "to_formats": TO_FORMATS,
                    "do_ocr": DO_OCR,
                    "force_ocr": FORCE_OCR,
                    "ocr_preset": OCR_PRESET,
                    "ocr_lang": OCR_LANG,
                    "pdf_backend": PDF_BACKEND,
                    "table_mode": TABLE_MODE,
                    "do_table_structure": DO_TABLE_STRUCTURE,
                    "include_images": INCLUDE_IMAGES,
                },
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
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)

    def shutdown(signum: int, _frame: Any) -> None:
        log("shutting down", signal=signum)
        httpd.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log("local FirePDF adapter listening", host=HOST, port=PORT, engine=ENGINE, docling_url=DOCLING_URL)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
