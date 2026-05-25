import type { Meta } from "../../..";
import type { PDFMode } from "../../../../../controllers/v2/types";
import type { PDFProcessorResult } from "../types";
import {
  getPdfResultFromCache,
  savePdfResultToCache,
} from "../../../../../lib/gcs-pdf-cache";

// Cache layout mirrors the sync `scrapePDFWithFirePDF` so async/sync share
// entries. `fast` and `ocr` bypass entirely: fast must fail on scanned PDFs
// instead of serving cached OCR, and OCR profile/env settings can change under
// identical PDF bytes. Calls with `maxPages` and zero-data-retention also
// bypass because those results must not be reused.
function cacheKeyShape(
  mode: PDFMode | undefined,
  maxPages: number | undefined,
) {
  const cacheable = mode !== "fast" && mode !== "ocr" && !maxPages;
  const ownVariant: string | undefined = undefined;
  const lookupVariants: (string | undefined)[] = [undefined];
  return { cacheable, ownVariant, lookupVariants };
}

export async function tryGetCached(
  meta: Meta,
  base64Content: string,
  mode: PDFMode | undefined,
  maxPages: number | undefined,
  pagesProcessed: number | undefined,
): Promise<PDFProcessorResult | null> {
  if (meta.internalOptions.zeroDataRetention) return null;
  const { cacheable, lookupVariants } = cacheKeyShape(mode, maxPages);
  if (!cacheable) return null;

  for (const variant of lookupVariants) {
    try {
      const cached = await getPdfResultFromCache(
        base64Content,
        "firepdf",
        variant,
      );
      if (cached) {
        meta.logger.info("Using cached FirePDF result (async path)", {
          scrapeId: meta.id,
          requestedMode: mode,
          cacheVariant: variant ?? "base",
        });
        return {
          ...cached,
          pagesProcessed: cached.pagesProcessed ?? pagesProcessed,
        };
      }
    } catch (error) {
      meta.logger.warn(
        "Error checking FirePDF cache (async path), proceeding",
        { error, cacheVariant: variant ?? "base" },
      );
    }
  }
  return null;
}

export async function maybeSaveResult(args: {
  meta: Meta;
  base64Content: string;
  mode: PDFMode | undefined;
  maxPages: number | undefined;
  result: PDFProcessorResult & { markdown: string };
}): Promise<void> {
  const { meta, base64Content, mode, maxPages, result } = args;
  if (meta.internalOptions.zeroDataRetention) return;
  const { cacheable, ownVariant } = cacheKeyShape(mode, maxPages);
  if (!cacheable) return;

  try {
    await savePdfResultToCache(base64Content, result, "firepdf", ownVariant);
  } catch (error) {
    meta.logger.warn(
      "Error saving FirePDF async result to cache (continuing)",
      { error },
    );
  }
}
