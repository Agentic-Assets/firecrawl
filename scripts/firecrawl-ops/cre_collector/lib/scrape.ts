// lib/scrape.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import Firecrawl from "@mendable/firecrawl-js";
import { API_URL } from "./config.js";
import { ScrapeOpts, ScrapedDoc } from "../types.js";

// Self-hosted with USE_DB_AUTHENTICATION=false accepts any non-empty key.
export const firecrawl = new Firecrawl({
  apiKey: process.env.FIRECRAWL_API_KEY || "local-self-hosted",
  apiUrl: API_URL,
});

/**
 * The API's scrape `timeout` is a server-side budget, not a guarantee that the
 * client promise settles. Keep source workers from being stranded behind a
 * stalled local API connection after that budget has elapsed.
 */
export async function withRequestDeadline<T>(request: Promise<T>, timeoutMs: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(
      () => reject(new Error(`Firecrawl scrape request timed out after ${timeoutMs}ms`)),
      timeoutMs
    );
  });
  try {
    return await Promise.race([request, deadline]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export async function scrapeRaw(url: string, opts: ScrapeOpts = {}): Promise<string> {
  let lastErr: unknown = null;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const timeout = opts.timeout ?? 90000;
      const doc = await withRequestDeadline(firecrawl.scrape(url, {
        formats: ["rawHtml"],
        ...(opts.waitFor ? { waitFor: opts.waitFor } : {}),
        ...(opts.proxy ? { proxy: opts.proxy } : {}),
        timeout,
      } as any), timeout);
      const body = (doc as any).rawHtml ?? "";
      if (!body) throw new Error("empty response body");
      return body;
    } catch (err) {
      lastErr = err;
      console.error(`scrape attempt ${attempt} failed for ${url}: ${err}`);
      await new Promise((r) => setTimeout(r, 2500 * attempt));
    }
  }
  throw lastErr;
}

export async function scrapeDoc(url: string, opts: ScrapeOpts = {}): Promise<ScrapedDoc> {
  let lastErr: unknown = null;
  for (let attempt = 1; attempt <= 3; attempt++) {
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
      return { rawHtml, markdown, links, images, attributes, metadata: data.metadata };
    } catch (err) {
      lastErr = err;
      console.error(`scrape-doc attempt ${attempt} failed for ${url}: ${err}`);
      await new Promise((r) => setTimeout(r, 2500 * attempt));
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

export async function scrapeJson(url: string, opts: ScrapeOpts = {}): Promise<any> {
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
    await new Promise((r) => setTimeout(r, backoffMs * attempt));
  }
  throw new Error(`response from ${url} contained no parseable JSON object`);
}
