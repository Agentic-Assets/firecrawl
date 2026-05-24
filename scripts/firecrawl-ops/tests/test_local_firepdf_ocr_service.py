#!/usr/bin/env python3
"""Unit tests for local FirePDF adapter helpers.

Run from the repo root:

    python3 scripts/firecrawl-ops/tests/test_local_firepdf_ocr_service.py
"""

from __future__ import annotations

import importlib.util
import threading
import unittest
from pathlib import Path


SERVICE_PATH = Path(__file__).resolve().parents[1] / "local_firepdf_ocr_service.py"


def load_service_module():
    spec = importlib.util.spec_from_file_location("local_firepdf_ocr_service", SERVICE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


service = load_service_module()


class MarkdownQualityTests(unittest.TestCase):
    def test_flat_markdown_without_page_marker_is_not_treated_as_empty_pages(self) -> None:
        markdown = "This is substantive OCR text for a research paper. " * 400

        quality = service.analyze_markdown_quality(markdown, processed_pages=20, marker="")

        self.assertEqual(quality["page_count_observed"], 1)
        self.assertEqual(quality["empty_page_ratio"], 0.0)
        self.assertFalse(quality["low_quality"])

    def test_page_marker_preserves_empty_page_detection(self) -> None:
        markdown = "Only the first page has text." + ("\n\nFIRECRAWLPAGEBREAK\n\n" * 8)

        quality = service.analyze_markdown_quality(
            markdown,
            processed_pages=9,
            marker="FIRECRAWLPAGEBREAK",
        )

        self.assertIn("most_pages_empty", quality["warnings"])
        self.assertTrue(quality["low_quality"])

    def test_publisher_boilerplate_is_rejected(self) -> None:
        markdown = (
            "Downloaded from Wiley Online Library. Terms and conditions license.\n"
            * 200
        )

        quality = service.analyze_markdown_quality(markdown, processed_pages=10, marker="")

        self.assertIn("publisher_boilerplate_dominates", quality["warnings"])
        self.assertTrue(quality["low_quality"])

    def test_invalid_concurrency_env_falls_back_to_default(self) -> None:
        self.assertEqual(service.parse_int_env("__MISSING_LOCAL_FIREPDF_TEST_ENV__", 2), 2)


class BackpressureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_semaphore = service.OCR_SEMAPHORE
        self.original_active_count = service.OCR_ACTIVE_COUNT
        self.original_max_concurrent = service.MAX_CONCURRENT_OCR
        self.original_decode_pdf = service.decode_pdf
        self.original_count_pages = service.count_pages
        self.original_limited_pdf = service.limited_pdf_if_possible
        self.original_call_docling = service.call_docling

        service.MAX_CONCURRENT_OCR = 1
        service.OCR_SEMAPHORE = threading.BoundedSemaphore(1)
        service.OCR_ACTIVE_COUNT = 0

    def tearDown(self) -> None:
        service.OCR_SEMAPHORE = self.original_semaphore
        service.OCR_ACTIVE_COUNT = self.original_active_count
        service.MAX_CONCURRENT_OCR = self.original_max_concurrent
        service.decode_pdf = self.original_decode_pdf
        service.count_pages = self.original_count_pages
        service.limited_pdf_if_possible = self.original_limited_pdf
        service.call_docling = self.original_call_docling

    def test_second_concurrent_ocr_request_gets_backpressure(self) -> None:
        entered_call_docling = threading.Event()
        release_call_docling = threading.Event()
        first_result: list[dict[str, object]] = []
        first_error: list[BaseException] = []

        def fake_decode_pdf(_request_body, target: Path) -> None:
            target.write_bytes(b"%PDF-1.4\n")

        def fake_limited_pdf(pdf_path: Path, _max_pages, _total_pages, _tmpdir):
            return pdf_path, 1

        def fake_call_docling(_pdf_path, _timeout, _max_pages, _request_body):
            entered_call_docling.set()
            release_call_docling.wait(timeout=5)
            return {"markdown": "Substantive OCR text. " * 300}, {}

        service.decode_pdf = fake_decode_pdf
        service.count_pages = lambda _pdf_path: 1
        service.limited_pdf_if_possible = fake_limited_pdf
        service.call_docling = fake_call_docling

        def run_first_request() -> None:
            try:
                first_result.append(service.handle_ocr({"pdf": "ZmFrZQ==", "scrape_id": "first"}))
            except BaseException as exc:
                first_error.append(exc)

        worker = threading.Thread(target=run_first_request)
        worker.start()
        self.assertTrue(entered_call_docling.wait(timeout=5))

        with self.assertRaises(service.AdapterError) as raised:
            service.handle_ocr({"pdf": "ZmFrZQ==", "scrape_id": "second"})

        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(raised.exception.details["code"], "LOCAL_FIREPDF_BACKPRESSURE")
        self.assertEqual(raised.exception.details["max_concurrent_ocr"], 1)

        release_call_docling.set()
        worker.join(timeout=5)
        self.assertFalse(first_error)
        self.assertEqual(first_result[0]["pages_processed"], 1)


if __name__ == "__main__":
    unittest.main()
