// lib/scrape.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import Firecrawl from "@mendable/firecrawl-js";
import { API_URL } from "./config.js";
import { ScrapeOpts, ScrapedDoc } from "../types.js";
import {
  createClientRequestDeadlineError,
  recordClientAttemptCompleted,
  recordClientAttemptStarted,
  recordLogicalScrapeCall,
  recordRetry,
} from "./performance.js";

export type CollectorScrapeOpts = ScrapeOpts & {
  /**
   * Maximum age, in milliseconds, of a Firecrawl index result that may satisfy
   * this request. Full refresh callers use zero so a newly written artifact
   * cannot silently contain the local API's default cached response.
   */
  maxAge?: number;
};

const DEFAULT_SCRAPE_MAX_ATTEMPTS = 3;

/** Resolve the benchmark-only retry reduction without changing normal runs. */
export function configuredScrapeMaxAttempts(
  environment: NodeJS.ProcessEnv = process.env
): number {
  const configured = environment.CRE_SCRAPE_MAX_ATTEMPTS;
  if (configured === undefined || configured === "") {
    return DEFAULT_SCRAPE_MAX_ATTEMPTS;
  }
  if (!/^[1-3]$/.test(configured)) {
    throw new Error("CRE_SCRAPE_MAX_ATTEMPTS must be an integer from 1 through 3");
  }
  return Number(configured);
}

/** Return a delay only when another scrape attempt will actually run. */
export function scrapeRetryDelayMs(
  attempt: number,
  maxAttempts: number
): number | null {
  return attempt < maxAttempts ? 2500 * attempt : null;
}

export function createScrapeClient(
  environment: NodeJS.ProcessEnv = process.env
): Firecrawl {
  return new Firecrawl({
    // Self-hosted with USE_DB_AUTHENTICATION=false accepts any non-empty key.
    apiKey: environment.FIRECRAWL_API_KEY || "local-self-hosted",
    apiUrl: API_URL,
    // The pinned SDK calls this maxRetries, but it counts total HTTP attempts.
    maxRetries: configuredScrapeMaxAttempts(environment),
  });
}

export const firecrawl = createScrapeClient();

/**
 * The API's scrape `timeout` is a server-side budget, not a guarantee that the
 * client promise settles. Keep source workers from being stranded behind a
 * stalled local API connection after that budget has elapsed.
 */
export async function withRequestDeadline<T>(request: Promise<T>, timeoutMs: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(
      () => reject(createClientRequestDeadlineError(timeoutMs)),
      timeoutMs
    );
  });
  try {
    return await Promise.race([request, deadline]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export async function scrapeRaw(url: string, opts: CollectorScrapeOpts = {}): Promise<string> {
  recordLogicalScrapeCall("raw");
  let lastErr: unknown = null;
  const maxAttempts = configuredScrapeMaxAttempts();
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    const performanceAttempt = recordClientAttemptStarted({
      freshRequested: opts.maxAge === 0,
    });
    try {
      const timeout = opts.timeout ?? 90000;
      const doc = await withRequestDeadline(firecrawl.scrape(url, {
        formats: ["rawHtml"],
        ...(opts.waitFor ? { waitFor: opts.waitFor } : {}),
        ...(opts.proxy ? { proxy: opts.proxy } : {}),
        ...(opts.maxAge !== undefined ? { maxAge: opts.maxAge } : {}),
        timeout,
      } as any), timeout);
      const body = (doc as any).rawHtml ?? "";
      if (!body) throw new Error("empty response body");
      recordClientAttemptCompleted(performanceAttempt, { outcome: "succeeded" });
      return body;
    } catch (err) {
      recordClientAttemptCompleted(performanceAttempt, { outcome: "failed", error: err });
      lastErr = err;
      console.error(`scrape attempt ${attempt} failed for ${url}: ${err}`);
      const delayMs = scrapeRetryDelayMs(attempt, maxAttempts);
      if (delayMs !== null) {
        recordRetry("http_helper", delayMs, true);
        await new Promise((r) => setTimeout(r, delayMs));
      }
    }
  }
  throw lastErr;
}

export async function scrapeDoc(url: string, opts: CollectorScrapeOpts = {}): Promise<ScrapedDoc> {
  recordLogicalScrapeCall("doc");
  let lastErr: unknown = null;
  const maxAttempts = configuredScrapeMaxAttempts();
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    const performanceAttempt = recordClientAttemptStarted({
      freshRequested: opts.maxAge === 0,
    });
    try {
      const timeout = opts.timeout ?? 90000;
      const doc = await withRequestDeadline(firecrawl.scrape(url, {
        // Capture-everything format set: markdown (full page text), links + images
        // (full gallery, no truncation), rawHtml (regex fallback source), and an
        // `attributes` block that harvests video/iframe/anchor/source URLs for
        // harvestDetail(). onlyMainContent:false keeps links/iframes from being
        // stripped. The local fork returns data.images + data.attributes (verified
        // against POST /v2/scrape); when a fork omits either format the guards
        // below degrade it to undefined and harvestDetail() falls back to rawHtml.
        formats: [
          "markdown",
          "links",
          "images",
          "rawHtml",
          {
            type: "attributes",
            selectors: [
              { selector: "div[component=video]", attribute: "url" },
              { selector: "iframe", attribute: "src" },
              { selector: "a", attribute: "href" },
              { selector: "video source", attribute: "src" },
              { selector: "[data-video-url]", attribute: "data-video-url" },
            ],
          },
        ],
        onlyMainContent: false,
        ...(opts.waitFor ? { waitFor: opts.waitFor } : {}),
        ...(opts.proxy ? { proxy: opts.proxy } : {}),
        ...(opts.maxAge !== undefined ? { maxAge: opts.maxAge } : {}),
        timeout,
      } as any), timeout);
      const anyDoc = doc as any;
      const data = anyDoc.data ?? anyDoc;
      const rawHtml = data.rawHtml ?? "";
      const markdown = data.markdown ?? "";
      const links = Array.isArray(data.links) ? data.links : [];
      // images/attributes degrade to undefined when the fork omits the format,
      // so scrapeDoc NEVER hard-fails on an unsupported format; harvestDetail()
      // treats both as possibly-undefined and regex-falls-back over rawHtml.
      const images = Array.isArray(data.images) ? data.images : undefined;
      const attributes = Array.isArray(data.attributes) ? data.attributes : undefined;
      if (!rawHtml && !markdown) throw new Error("empty scraped document");
      recordClientAttemptCompleted(performanceAttempt, { outcome: "succeeded" });
      return { rawHtml, markdown, links, images, attributes, metadata: data.metadata };
    } catch (err) {
      recordClientAttemptCompleted(performanceAttempt, { outcome: "failed", error: err });
      lastErr = err;
      console.error(`scrape-doc attempt ${attempt} failed for ${url}: ${err}`);
      const delayMs = scrapeRetryDelayMs(attempt, maxAttempts);
      if (delayMs !== null) {
        recordRetry("http_helper", delayMs, true);
        await new Promise((r) => setTimeout(r, delayMs));
      }
    }
  }
  throw lastErr;
}

export function parseJsonBody(body: string): any | null {
  try {
    return JSON.parse(body);
  } catch {
    // JSON rendered inside an HTML wrapper (e.g. Chrome JSON viewer markup)
    const unescaped = body
      .replace(/<[^>]*>/g, "")
      .replace(/&quot;/g, '"')
      .replace(/&amp;/g, "&")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&#39;/g, "'");
    for (const candidate of [body, unescaped, repairUnescapedJsonStringQuotes(unescaped)]) {
      const spans = [
        { start: candidate.indexOf("{"), end: candidate.lastIndexOf("}") },
        { start: candidate.indexOf("["), end: candidate.lastIndexOf("]") },
      ].filter((s) => s.start !== -1 && s.end > s.start);
      spans.sort((a, b) => a.start - b.start);
      for (const { start, end } of spans) {
        try {
          return JSON.parse(candidate.slice(start, end + 1));
        } catch {
          /* try next */
        }
      }
    }
    return null;
  }
}

export function repairUnescapedJsonStringQuotes(body: string): string {
  let out = "";
  let inString = false;
  let escaped = false;
  for (let i = 0; i < body.length; i++) {
    const ch = body[i];
    if (!inString) {
      if (ch === '"') inString = true;
      out += ch;
      continue;
    }

    if (escaped) {
      out += ch;
      escaped = false;
      continue;
    }
    if (ch === "\\") {
      out += ch;
      escaped = true;
      continue;
    }
    if (ch === '"') {
      const rest = body.slice(i + 1);
      const next = rest.match(/\S/)?.[0] ?? "";
      if ([":", ",", "}", "]"].includes(next)) {
        inString = false;
        out += ch;
      } else {
        out += '\\"';
      }
      continue;
    }
    out += ch;
  }
  return out;
}

export async function scrapeJson(url: string, opts: CollectorScrapeOpts = {}): Promise<any> {
  recordLogicalScrapeCall("json");
  // A successful scrape can still return a non-JSON body (rate-limit or
  // challenge interstitial, e.g. Buildout under sustained paging). Retry the
  // whole scrape with growing backoff before giving up.
  const attempts = opts.jsonAttempts ?? 3;
  const backoffMs = opts.jsonBackoffMs ?? 8000;
  for (let attempt = 1; attempt <= attempts; attempt++) {
    const body = await scrapeRaw(url, opts);
    const parsed = parseJsonBody(body);
    if (parsed !== null) return parsed;
    console.error(`non-JSON body from ${url} (attempt ${attempt}); backing off`);
    const delayMs = backoffMs * attempt;
    recordRetry("json_parse", delayMs, attempt < attempts);
    await new Promise((r) => setTimeout(r, delayMs));
  }
  throw new Error(`response from ${url} contained no parseable JSON object`);
}
