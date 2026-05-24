import {
  parseFirePdfFailure,
  reconcilePageCountWithFirePdf,
  throwTypedFirePdfFailure,
} from "../firePDF";
import {
  PDFLowQualityError,
  PDFOCRBackpressureError,
  PDFOCRTimeoutError,
} from "../../../error";

describe("reconcilePageCountWithFirePdf", () => {
  it("uses fire-pdf's count when the upstream pass left it at 0", () => {
    // The original regression: processPdf threw "Invalid PDF structure" on a
    // malformed-but-still-renderable PDF, so effectivePageCount stayed 0.
    // fire-pdf processes it successfully and reports 15 pages. Billing must
    // see 15, not 0.
    expect(reconcilePageCountWithFirePdf(0, { pagesProcessed: 15 })).toBe(15);
  });

  it("never shrinks a count that an upstream pass already established", () => {
    // detectPdf / processPdf saw 20 pages; fire-pdf was called with
    // max_pages=10 and processed 10. We must keep 20 — fire-pdf's value
    // reflects its own cap, not the true PDF length.
    expect(reconcilePageCountWithFirePdf(20, { pagesProcessed: 10 })).toBe(20);
  });

  it("keeps current when both agree", () => {
    expect(reconcilePageCountWithFirePdf(15, { pagesProcessed: 15 })).toBe(15);
  });

  it("ignores undefined pagesProcessed (older fire-pdf or stale cache)", () => {
    // No signal — preserve whatever the upstream pass set, even if 0.
    expect(
      reconcilePageCountWithFirePdf(0, { pagesProcessed: undefined }),
    ).toBe(0);
    expect(reconcilePageCountWithFirePdf(7, {})).toBe(7);
  });

  it("ignores null/undefined result (fire-pdf didn't run)", () => {
    expect(reconcilePageCountWithFirePdf(7, null)).toBe(7);
    expect(reconcilePageCountWithFirePdf(7, undefined)).toBe(7);
  });

  it("treats fire-pdf's 0 as a real value (no special-casing)", () => {
    // If fire-pdf legitimately reports 0 (empty PDF that still rendered),
    // the max() semantic preserves whatever was already there. We only
    // skip when the field is *missing*, not when it's zero.
    expect(reconcilePageCountWithFirePdf(0, { pagesProcessed: 0 })).toBe(0);
    expect(reconcilePageCountWithFirePdf(5, { pagesProcessed: 0 })).toBe(5);
  });
});

describe("parseFirePdfFailure", () => {
  function failure(status: number, body: unknown) {
    return new Error("Request sent failure status", {
      cause: {
        response: {
          status,
          body: typeof body === "string" ? body : JSON.stringify(body),
        },
      },
    });
  }

  it("extracts local OCR backpressure details", () => {
    expect(
      parseFirePdfFailure(
        failure(429, {
          error: "Local OCR capacity is full; retry later.",
          code: "LOCAL_FIREPDF_BACKPRESSURE",
          details: { active_ocr: 2, max_concurrent_ocr: 2 },
        }),
      ),
    ).toMatchObject({
      status: 429,
      code: "LOCAL_FIREPDF_BACKPRESSURE",
      message: "Local OCR capacity is full; retry later.",
    });
  });

  it("extracts low-quality OCR details", () => {
    expect(
      parseFirePdfFailure(
        failure(422, {
          error: "Docling OCR output failed quality checks.",
          details: { code: "LOCAL_FIREPDF_LOW_QUALITY" },
        }),
      ),
    ).toMatchObject({
      status: 422,
      code: "LOCAL_FIREPDF_LOW_QUALITY",
      message: "Docling OCR output failed quality checks.",
    });
  });

  it("extracts Docling timeout details from nested response details", () => {
    expect(
      parseFirePdfFailure(
        failure(504, {
          error: "Docling returned HTTP 504",
          details: {
            detail:
              "Conversion is taking too long. The maximum wait time is configured as DOCLING_SERVE_MAX_SYNC_WAIT=120.",
          },
        }),
      ),
    ).toMatchObject({
      status: 504,
      message: "Docling returned HTTP 504",
    });
  });

  it("extracts timeout details from a plain details string", () => {
    expect(
      parseFirePdfFailure(
        failure(502, {
          details: "Docling timed out while converting the document.",
        }),
      ),
    ).toMatchObject({
      status: 502,
      message: "Docling timed out while converting the document.",
    });
  });

  it("extracts top-level adapter codes", () => {
    expect(
      parseFirePdfFailure(
        failure(429, {
          code: "LOCAL_FIREPDF_BACKPRESSURE",
          details: { active_ocr: 2 },
        }),
      ),
    ).toMatchObject({
      status: 429,
      code: "LOCAL_FIREPDF_BACKPRESSURE",
    });
  });

  it("keeps raw invalid JSON bodies inspectable", () => {
    expect(parseFirePdfFailure(failure(502, "upstream timeout"))).toEqual({
      status: 502,
      body: "upstream timeout",
      message: undefined,
      code: undefined,
    });
  });

  it("returns null when robustFetch did not expose a response", () => {
    expect(parseFirePdfFailure(new Error("network failure"))).toBeNull();
  });
});

describe("throwTypedFirePdfFailure", () => {
  function failure(status: number, body: unknown) {
    return new Error("Request sent failure status", {
      cause: {
        response: {
          status,
          body: typeof body === "string" ? body : JSON.stringify(body),
        },
      },
    });
  }

  it("maps local OCR backpressure to a typed transportable error", () => {
    expect(() =>
      throwTypedFirePdfFailure(
        failure(429, {
          error: "Local OCR capacity is full; retry later.",
          code: "LOCAL_FIREPDF_BACKPRESSURE",
        }),
      ),
    ).toThrow(PDFOCRBackpressureError);
    expect(() =>
      throwTypedFirePdfFailure(
        failure(429, {
          error: "Local OCR capacity is full; retry later.",
          code: "LOCAL_FIREPDF_BACKPRESSURE",
        }),
      ),
    ).toThrow("Local OCR capacity is full; retry later.");
  });

  it("maps Docling timeouts to a typed transportable error", () => {
    expect(() =>
      throwTypedFirePdfFailure(
        failure(504, {
          details: {
            detail:
              "Conversion is taking too long. The maximum wait time is configured as DOCLING_SERVE_MAX_SYNC_WAIT=120.",
          },
        }),
      ),
    ).toThrow(PDFOCRTimeoutError);
  });

  it("maps local low-quality OCR to a typed transportable error", () => {
    expect(() =>
      throwTypedFirePdfFailure(
        failure(422, {
          error: "Docling OCR output failed quality checks.",
          details: { code: "LOCAL_FIREPDF_LOW_QUALITY" },
        }),
      ),
    ).toThrow(PDFLowQualityError);
  });

  it("preserves unrelated FirePDF failures for the existing fallback path", () => {
    const original = failure(502, { error: "temporary upstream failure" });

    try {
      throwTypedFirePdfFailure(original);
      throw new Error("Expected throwTypedFirePdfFailure to throw");
    } catch (error) {
      expect(error).toBe(original);
    }
  });
});
