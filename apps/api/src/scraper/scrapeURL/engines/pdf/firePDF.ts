import { Meta } from "../..";
import { config } from "../../../../config";
import { robustFetch } from "../../lib/fetch";
import { z } from "zod";
import type { PDFProcessorResult } from "./types";
import type { PDFMode } from "../../../../controllers/v2/types";
import { safeMarkdownToHtml } from "./markdownToHtml";
import {
  createPdfCacheKey,
  getPdfResultFromCache,
  savePdfResultToCache,
} from "../../../../lib/gcs-pdf-cache";
import {
  PDFLowQualityError,
  PDFOCRBackpressureError,
  PDFOCRTimeoutError,
} from "../../error";

/**
 * Reconcile an existing page count with what fire-pdf reported.
 *
 * Used after `scrapePDFWithFirePDF` returns. The original bug: when Rust
 * extraction (`processPdf`) threw on a malformed-but-still-renderable PDF,
 * `effectivePageCount` stayed at 0; fire-pdf would then process the PDF
 * fine but its `pages_processed` value was dropped on the floor, so
 * `pdfMetadata.numPages` shipped as 0 and billing under-counted.
 *
 * Semantics:
 *   - If fire-pdf didn't report a count (older fire-pdf builds, or stale
 *     cache hits), keep the current value — no signal to act on.
 *   - Otherwise take the max — never shrink a count that an upstream pass
 *     (detectPdf / processPdf) already established. fire-pdf can be
 *     called with `max_pages` capping its own processing below the true
 *     PDF length, and the upstream count is the authoritative one when
 *     both succeeded.
 *
 * Pure / synchronous so it's trivially unit-testable; the integration in
 * `index.ts` is just `effectivePageCount = reconcilePageCountWithFirePdf(...)`.
 */
export function reconcilePageCountWithFirePdf(
  current: number,
  firePdfResult: { pagesProcessed?: number } | null | undefined,
): number {
  const fromFirePdf = firePdfResult?.pagesProcessed;
  if (fromFirePdf === undefined) return current;
  return Math.max(current, fromFirePdf);
}

export function parseFirePdfFailure(
  error: unknown,
): { status?: number; body?: any; message?: string; code?: string } | null {
  const cause = (error as { cause?: any })?.cause;
  const response = cause?.response;
  if (!response) return null;

  let body: any = response.body;
  if (typeof body === "string") {
    try {
      body = JSON.parse(body);
    } catch {
      // Keep raw body.
    }
  }

  const details = body?.details;
  const detailCode =
    (typeof details === "object" && details !== null && details.code) ||
    body?.code;
  const message =
    body?.error ||
    (typeof details === "object" && details !== null
      ? details?.detail || details?.message
      : undefined) ||
    (typeof details === "string" ? details : undefined);

  return {
    status: response.status,
    body,
    message: typeof message === "string" ? message : undefined,
    code: typeof detailCode === "string" ? detailCode : undefined,
  };
}

export function throwTypedFirePdfFailure(error: unknown): never {
  const failure = parseFirePdfFailure(error);
  const status = failure?.status;
  const code = failure?.code;
  const message = failure?.message;

  if (status === 429 || code === "LOCAL_FIREPDF_BACKPRESSURE") {
    throw new PDFOCRBackpressureError(message);
  }

  if (
    status === 504 ||
    (status === 502 && /timeout|timed out/i.test(message ?? ""))
  ) {
    throw new PDFOCRTimeoutError(message);
  }

  if (status === 422 || code === "LOCAL_FIREPDF_LOW_QUALITY") {
    throw new PDFLowQualityError(message);
  }

  throw error;
}

export async function scrapePDFWithFirePDF(
  meta: Meta,
  base64Content: string,
  maxPages?: number,
  pagesProcessed?: number,
  mode?: PDFMode,
): Promise<PDFProcessorResult> {
  const logger = meta.logger;

  // Cache layout:
  //   - `ocr` mode reads/writes a dedicated `…-ocr.json` bucket. ocr
  //     requests explicitly want forced layout-mode OCR, so they must
  //     not be served a base-cache entry that was written by `auto`.
  //   - `auto` (and legacy undefined-mode) reads/writes the base
  //     `firepdf-<sha>.json` bucket — same key main has always used,
  //     so existing entries keep working. As a free upgrade, auto also
  //     reads the ocr bucket as a fallback: if some prior `ocr` run
  //     already produced markdown for this PDF, reuse it rather than
  //     running fire-pdf again.
  //   - `fast` is bypassed entirely (hard cost ceiling — must fail on
  //     scanned PDFs, not serve a cached OCR result).
  const cacheable =
    mode !== "fast" && !maxPages && !meta.internalOptions.zeroDataRetention;
  const ownVariant: string | undefined = mode === "ocr" ? "ocr" : undefined;
  const lookupVariants: (string | undefined)[] =
    mode === "ocr" ? ["ocr"] : [undefined, "ocr"];

  if (cacheable) {
    for (const variant of lookupVariants) {
      try {
        const cached = await getPdfResultFromCache(
          base64Content,
          "firepdf",
          variant,
        );
        if (cached) {
          logger.info("Using cached FirePDF result", {
            scrapeId: meta.id,
            requestedMode: mode,
            cacheVariant: variant ?? "base",
          });
          // Cache entries written before pagesProcessed existed don't carry
          // the field. Fall back to the caller's pagesProcessed argument so
          // billing on a stale hit doesn't silently regress to 0.
          return {
            ...cached,
            pagesProcessed: cached.pagesProcessed ?? pagesProcessed,
          };
        }
      } catch (error) {
        logger.warn("Error checking FirePDF cache, proceeding", {
          error,
          cacheVariant: variant ?? "base",
        });
      }
    }
  }

  meta.abort.throwIfAborted();

  const startedAt = Date.now();

  logger.info("FirePDF started", {
    scrapeId: meta.id,
    url: meta.rewrittenUrl ?? meta.url,
    maxPages,
    pagesProcessed,
  });

  const zdr = meta.internalOptions.zeroDataRetention === true;
  const pdfSha256 = createPdfCacheKey(base64Content);

  // Explicit deadline contract with fire-pdf (mirrors mineru-api):
  //   timeout    — remaining scrape-tier budget in ms (from AbortManager)
  //   created_at — epoch ms when we handed the budget over to fire-pdf
  //
  // fire-pdf computes remaining = timeout - (now - created_at) and can
  // return 503 if the budget is spent. Previously it only saw the abort
  // signal from the HTTP connection, which it didn't observe — so work
  // kept running past the caller's timeout and the user got a late
  // failure instead of a fast deadline-exceeded response.
  //
  // scrapeTimeout() returns undefined if no scrape-tier deadline is set
  // (e.g., internal tests, CLI). Don't send timeout in that case so
  // fire-pdf applies its own default.
  const fireScrapeTimeout = meta.abort.scrapeTimeout();
  const deadlineFields: { timeout?: number; created_at?: number } = {};
  if (fireScrapeTimeout !== undefined && fireScrapeTimeout > 0) {
    deadlineFields.timeout = Math.floor(fireScrapeTimeout);
    deadlineFields.created_at = Date.now();
  }

  let resp: {
    markdown: string;
    failed_pages: number[] | null;
    pages_processed?: number;
    metadata?: Record<string, unknown>;
  };
  try {
    resp = await robustFetch({
      url: `${config.FIRE_PDF_BASE_URL}/ocr`,
      method: "POST",
      headers: config.FIRE_PDF_API_KEY
        ? { Authorization: `Bearer ${config.FIRE_PDF_API_KEY}` }
        : undefined,
      body: {
        pdf: base64Content,
        scrape_id: meta.id,
        ...(maxPages !== undefined && { max_pages: maxPages }),
        ...(mode !== undefined && { mode }),
        // Enrichment for the fire-pdf jobs DB / dashboard. fire-pdf treats
        // these as optional — older fire-pdf builds will ignore unknown fields.
        team_id: meta.internalOptions.teamId,
        ...(meta.internalOptions.crawlId && {
          crawl_id: meta.internalOptions.crawlId,
        }),
        ...(zdr ? {} : { url: meta.rewrittenUrl ?? meta.url }),
        pdf_sha256: pdfSha256,
        source: "firecrawl",
        zdr,
        ...deadlineFields,
      },
      logger,
      schema: z.object({
        markdown: z.string(),
        failed_pages: z.array(z.number()).nullable(),
        pages_processed: z.number().optional(),
        metadata: z.record(z.string(), z.unknown()).optional(),
      }),
      mock: meta.mock,
      abort: meta.abort.asSignal(),
    });
  } catch (error) {
    throwTypedFirePdfFailure(error);
  }

  const durationMs = Date.now() - startedAt;
  const pages = resp.pages_processed ?? pagesProcessed;

  logger.info("FirePDF completed", {
    scrapeId: meta.id,
    url: meta.rewrittenUrl ?? meta.url,
    durationMs,
    markdownLength: resp.markdown.length,
    failedPages: resp.failed_pages,
    pagesProcessed: pages,
    ocrMetadata: resp.metadata,
    perPageMs: pages ? Math.round(durationMs / pages) : undefined,
  });

  const processorResult: PDFProcessorResult & { markdown: string } = {
    markdown: resp.markdown,
    html: await safeMarkdownToHtml(resp.markdown, logger, meta.id),
    pagesProcessed: pages,
    ocrMetadata: resp.metadata,
  };

  if (cacheable) {
    try {
      await savePdfResultToCache(
        base64Content,
        processorResult,
        "firepdf",
        ownVariant,
      );
    } catch (error) {
      logger.warn("Error saving FirePDF result to cache", { error });
    }
  }

  return processorResult;
}
