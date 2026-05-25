import {
  deserializeTransportableError,
  serializeTransportableError,
} from "../error-serde";
import {
  PDFLowQualityError,
  PDFOCRBackpressureError,
  PDFOCRTimeoutError,
} from "../../scraper/scrapeURL/error";

describe("PDF OCR transportable error serde", () => {
  it.each([
    {
      name: "backpressure",
      error: new PDFOCRBackpressureError("Local OCR capacity is full."),
      ErrorClass: PDFOCRBackpressureError,
      code: "SCRAPE_PDF_OCR_BACKPRESSURE",
    },
    {
      name: "timeout",
      error: new PDFOCRTimeoutError("Docling timed out."),
      ErrorClass: PDFOCRTimeoutError,
      code: "SCRAPE_PDF_OCR_TIMEOUT",
    },
    {
      name: "low-quality",
      error: new PDFLowQualityError(
        "Docling OCR output failed quality checks.",
      ),
      ErrorClass: PDFLowQualityError,
      code: "SCRAPE_PDF_LOW_QUALITY",
    },
  ])("round-trips $name", ({ error, ErrorClass, code }) => {
    const roundTripped = deserializeTransportableError(
      serializeTransportableError(error),
    );

    expect(roundTripped).toBeInstanceOf(ErrorClass);
    expect(roundTripped?.code).toBe(code);
    expect(roundTripped?.message).toBe(error.message);
  });
});
