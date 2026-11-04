import { createHash } from "node:crypto";

import type { ScrapeResponseObservation } from "../lib/scrape.js";

function cbreDiagnosticUrl(value: unknown): string {
  if (typeof value !== "string") return "unknown";
  try {
    const parsed = new URL(value);
    if (
      parsed.protocol !== "https:" ||
      parsed.hostname !== "www.cbre.com" ||
      parsed.pathname !== "/listings-api/propertylistings/query"
    ) {
      return "redacted";
    }
    return `${parsed.origin}${parsed.pathname}`;
  } catch {
    return "unknown";
  }
}

function cbreEnvelopeShape(response: any): string {
  const root = Array.isArray(response)
    ? "array"
    : response === null
      ? "null"
      : typeof response;
  const documents = Array.isArray(response?.Documents)
    ? "array"
    : "missing-or-invalid";
  const count = Number.isInteger(response?.DocumentCount)
    ? "integer"
    : typeof response?.DocumentCount;
  const pageValue = [response?.Page, response?.PageNumber, response?.CurrentPage]
    .find((value) => value !== undefined);
  const page =
    pageValue === undefined
      ? "absent"
      : Number.isInteger(pageValue)
        ? "integer"
        : typeof pageValue;
  return `root=${root},documents=${documents},documentCount=${count},providerPage=${page}`;
}

export function logCbreValidationDiagnostic(
  response: any,
  page: number,
  pass: number,
  observation: ScrapeResponseObservation,
): void {
  try {
    const metadata = observation.metadata && typeof observation.metadata === "object"
      ? observation.metadata as Record<string, unknown>
      : {};
    const status = typeof metadata.statusCode === "number" &&
      Number.isInteger(metadata.statusCode) &&
      metadata.statusCode >= 100 &&
      metadata.statusCode <= 599
      ? String(metadata.statusCode)
      : "unknown";
    const rawHtmlBytes = Buffer.byteLength(observation.rawHtml, "utf8");
    const rawHtmlSha256 = createHash("sha256")
      .update(observation.rawHtml, "utf8")
      .digest("hex");
    console.error(
      `CBRE validation diagnostic page=${page} pass=${pass} ` +
        `envelope=${cbreEnvelopeShape(response)} firecrawlStatus=${status} ` +
        `firecrawlSourceURL=${cbreDiagnosticUrl(metadata.sourceURL)} ` +
        `firecrawlFinalURL=${cbreDiagnosticUrl(metadata.url)} ` +
        `firecrawlRawHtmlUtf8Bytes=${rawHtmlBytes} firecrawlRawHtmlSha256=${rawHtmlSha256}`,
    );
  } catch {
    // Diagnostic failures must not replace the original CBRE validation error.
  }
}
