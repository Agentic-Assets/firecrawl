/** Bounded source-page observations with no canonical listing qualification. */
import { closeSync, constants, fstatSync, openSync, readSync } from "node:fs";
import { isIP } from "node:net";
import * as cheerio from "cheerio";
import { SOURCE_KEYS, type ScrapedDoc, type SourceKey } from "../../types.js";
import { scrapeDoc } from "../scrape.js";
import { observationNow } from "../observation-clock.js";
import { classifyPerformanceError } from "../performance.js";
import { captureEnrichmentEvidence, failedEnrichmentEvidence, limitEnrichmentObservations, preservesJsonValue } from "../enrichment-evidence/index.js";

const INPUT_BYTES = 4 * 1024 * 1024;
const CONTEXT_BYTES = 64 * 1024;
const MAX_ITEMS = 100;
type PageItem = { sourceKey: SourceKey; url: string; sourceId?: string | null; context?: unknown };

function possibleChallengeHeader(rawHtml: unknown): boolean {
  if (typeof rawHtml !== "string" || !rawHtml) return false;
  const $ = cheerio.load(rawHtml);
  const normalize = (value: string) => value.toLowerCase().replace(/\s+/g, " ").trim()
    .replace(/^[\p{P}\p{S}\s]+|[\p{P}\p{S}\s]+$/gu, "");
  const phrases = new Set(["access denied", "forbidden", "just a moment", "verify you are human",
    "please verify you are human", "checking your browser", "captcha"]);
  return [$("title").first().text(), $("h1").first().text()].some(text => phrases.has(normalize(text)));
}

function cleanHttpsUrl(value: unknown): value is string {
  if (typeof value !== "string" || value.length > 8192 || /[\s\x00-\x1f\x7f]/.test(value) || value.includes("#")) return false;
  try {
    const url = new URL(value);
    const authority = value.match(/^https:\/\/([^/?#]+)(?:[/?#]|$)/)?.[1];
    const host = url.hostname.toLowerCase();
    return url.protocol === "https:" && !!authority && authority.toLowerCase() === host &&
      !url.username && !url.password && !url.port && !isIP(host) &&
      host.includes(".") && !host.endsWith(".local") && !host.endsWith(".localhost") &&
      /^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/.test(host) && !host.includes("..");
  } catch { return false; }
}

export function validateObservationInput(value: unknown): PageItem[] {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      Object.keys(value).some(key => key !== "items")) throw new Error("invalid observation input");
  const items = (value as {items?: unknown}).items;
  if (!Array.isArray(items) || !items.length || items.length > MAX_ITEMS) throw new Error("invalid observation item count");
  const result: PageItem[] = [];
  for (const item of items) {
    if (!item || typeof item !== "object" || Array.isArray(item) ||
        Object.keys(item).some(key => !["sourceKey", "url", "sourceId", "context"].includes(key)) ||
        !SOURCE_KEYS.includes(item.sourceKey) || !cleanHttpsUrl(item.url) ||
        (Object.hasOwn(item, "sourceId") && item.sourceId !== null && (typeof item.sourceId !== "string" || !item.sourceId || item.sourceId.length > 512)) ||
        (Object.hasOwn(item, "context") && !preservesJsonValue(item.context))) throw new Error("invalid observation item");
    result.push(item as PageItem);
  }
  if (new Set(result.map(item => item.sourceKey)).size !== 1) throw new Error("observation input requires one source");
  return result;
}

export function readObservationInput(path: string): PageItem[] {
  let input: unknown;
  try {
    const fd = openSync(path, constants.O_RDONLY | constants.O_NONBLOCK);
    try {
      if (!fstatSync(fd).isFile()) throw new Error();
      const buffer = Buffer.alloc(INPUT_BYTES + 1);
      let length = 0;
      while (length < buffer.length) {
        const count = readSync(fd, buffer, length, buffer.length - length, null);
        if (!count) break;
        length += count;
      }
      if (length > INPUT_BYTES) throw new Error();
      input = JSON.parse(buffer.subarray(0, length).toString("utf8"));
    } finally { closeSync(fd); }
  } catch { throw new Error("invalid observation input file"); }
  return validateObservationInput(input);
}

export async function observePages(items: PageItem[], fetchDoc: typeof scrapeDoc = scrapeDoc) {
  const startedAt = observationNow();
  const observations: { value: unknown; attempted: number; returned: number; failed: number; notAttempted: number; omitted: boolean; stopReason: string | null }[] = [];
  let stop: "access_denial" | "uncertain_settlement" | "possible_challenge_header" | null = null;
  let attempted = 0, returned = 0, failed = 0, notAttempted = 0;
  let hasOmissions = false;
  for (const item of items) {
    const before = { attempted, returned, failed, notAttempted, stop };
    let omitted = false;
    const claim = { sourceKey: item.sourceKey, externalId: item.sourceId ?? null, url: item.url };
    let evidence: unknown;
    if (stop) {
      notAttempted++;
      evidence = { ...failedEnrichmentEvidence(claim), failure: `not_attempted_after_${stop}` };
    } else try {
      attempted++;
      const doc: ScrapedDoc = await fetchDoc(item.url, { maxAge: 0, allowEmptyBody: true, singleAttempt: true, quietErrors: true });
      returned++;
      let capturedAt: string | null = null;
      try { capturedAt = observationNow(); } catch { /* No source timestamp is invented. */ }
      evidence = captureEnrichmentEvidence(claim, doc, capturedAt);
      if (Object.keys((evidence as {omissions: object}).omissions).length) { hasOmissions = true; omitted = true; }
      const status = doc.metadata?.statusCode;
      if ([401, 403, 429, 451].includes(status)) stop = "access_denial";
      else if (possibleChallengeHeader(doc.rawHtml)) stop = "possible_challenge_header";
    } catch (error) {
      failed++;
      const status = (error as {status?: unknown})?.status;
      const classified = classifyPerformanceError(error);
      const settlementUnknown = classified.remoteSettlementUnknown || typeof status !== "number";
      evidence = { ...failedEnrichmentEvidence(claim), ...(typeof status === "number" ? { localApiStatus: status } : {}),
        errorCategory: classified.category, remoteSettlementUnknown: settlementUnknown };
      if (typeof status === "number" && [401, 403, 429, 451].includes(status)) stop = "access_denial";
      else if (settlementUnknown) stop = "uncertain_settlement";
    }
    const contextPresent = Object.hasOwn(item, "context");
    const contextTooLarge = contextPresent && Buffer.byteLength(JSON.stringify(item.context), "utf8") > CONTEXT_BYTES;
    if (contextTooLarge) { hasOmissions = true; omitted = true; }
    observations.push({ value: { input: { identityBasis: item.sourceId == null ? "url" : "source_id",
      ...(Object.hasOwn(item, "sourceId") ? { sourceId: item.sourceId } : {}),
      ...(contextPresent && !contextTooLarge ? { context: item.context } : {}),
      ...(contextTooLarge ? { contextOmission: "context_byte_limit" } : {}) }, evidence },
      attempted: attempted - before.attempted, returned: returned - before.returned,
      failed: failed - before.failed, notAttempted: notAttempted - before.notAttempted,
      omitted, stopReason: before.stop !== stop ? stop : null });
  }
  const batches: typeof observations[] = [];
  let remaining = observations;
  while (remaining.length) {
    const count = limitEnrichmentObservations(remaining.map(item => item.value)).retainedObservations.length;
    if (!count) throw new Error("single observation exceeds group limit");
    batches.push(remaining.slice(0, count));
    remaining = remaining.slice(count);
  }
  const finishedAt = observationNow();
  const parts = batches.map((group, index) => ({
    description: "Private unqualified source-page observations.",
    runMeta: { mode: "observe", evidenceOnly: true, evidencePurpose: "private_unqualified_source_observations", startedAt, finishedAt,
      countsScope: "part", partNumber: index + 1, partCount: batches.length },
    sources: [{ sourceKey: items[0].sourceKey, supported: true, requested: group.length,
      attempted: group.reduce((n, x) => n + x.attempted, 0), returned: group.reduce((n, x) => n + x.returned, 0),
      failed: group.reduce((n, x) => n + x.failed, 0), notAttempted: group.reduce((n, x) => n + x.notAttempted, 0),
      stopReason: group.find(x => x.stopReason)?.stopReason ?? null, listingsCollected: 0,
      retainedObservations: group.map(x => x.value),
      evidenceIncomplete: group.some(x => x.omitted || x.failed > 0 || x.notAttempted > 0 || x.stopReason !== null) }],
    listings: [], brokers: [], totalListings: 0,
  }));
  return { parts, summary: { kind: "observation_run_summary", countsScope: "run", requested: items.length, attempted, returned, failed,
    notAttempted, stopReason: stop, evidenceIncomplete: hasOmissions || failed > 0 || notAttempted > 0 || stop !== null,
    partCount: parts.length } };
}
