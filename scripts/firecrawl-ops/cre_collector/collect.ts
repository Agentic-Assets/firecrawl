// =============================================================================
// collect.ts - multi-source CRE listing collector (local Firecrawl edition)
//
// Adapted from scripts/firecrawl-ops/prometheus/multi_source/script.ts
// (the original Prometheus cloud collector, preserved unmodified there).
//
// Differences from the original:
//   - Runs against the self-hosted Firecrawl API (FIRECRAWL_API_URL,
//     default http://localhost:3002) instead of cloud Firecrawl.
//   - Per-source proxy/waitFor settings (CBRE requires stealth locally).
//   - Collects BOTH for-sale and for-lease listings (--transaction).
//   - Full pagination: --max-items=0 means "everything the source exposes".
//   - Writes output to a file (--out) and prints a run summary to stderr.
//
// Usage:
//   npx tsx collect.ts --source=all --transaction=both --max-items=0 --out=./out/run.json
//   npx tsx collect.ts --source=cbre,svn --transaction=sale --max-items=25
// =============================================================================

import Firecrawl from "@mendable/firecrawl-js";
import * as cheerio from "cheerio";
import { parseArgs } from "node:util";
import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { createHash } from "node:crypto";

const API_URL = process.env.FIRECRAWL_API_URL ?? "http://localhost:3002";
// Self-hosted with USE_DB_AUTHENTICATION=false accepts any non-empty key.
const firecrawl = new Firecrawl({
  apiKey: process.env.FIRECRAWL_API_KEY || "local-self-hosted",
  apiUrl: API_URL,
});

// ---------- CLI ----------

const SOURCE_KEYS = [
  "cbre",
  "cbre-dealflow",
  "jll",
  "jll-investor",
  "cushman-wakefield",
  "colliers",
  "newmark",
  "marcus-millichap",
  "avison-young",
  "savills",
  "svn",
  "nai-global",
  "lee-associates",
  "transwestern",
] as const;
type SourceKey = (typeof SOURCE_KEYS)[number];

const { values: flags } = parseArgs({
  strict: true,
  options: {
    source: { type: "string" }, // all | comma-separated keys
    transaction: { type: "string" }, // sale | lease | both (default both)
    "max-items": { type: "string" }, // per source per transaction; 0 = unlimited
    "page-cap": { type: "string" }, // page-scrape sources: max rendered pages per tx
    out: { type: "string" }, // output JSON path (default stdout)
    concurrency: { type: "string" }, // concurrent page fetches within a source
  },
});

const sourceArg = (flags.source ?? "all").toLowerCase();
const requestedSources: SourceKey[] =
  sourceArg === "all"
    ? [...SOURCE_KEYS]
    : (sourceArg.split(",").map((s) => s.trim()) as SourceKey[]);
for (const s of requestedSources) {
  if (!SOURCE_KEYS.includes(s)) {
    console.error(`unknown source '${s}'. Valid: all, ${SOURCE_KEYS.join(", ")}`);
    process.exit(1);
  }
}
const txArg = (flags.transaction ?? "both").toLowerCase();
if (!["sale", "lease", "both"].includes(txArg)) {
  console.error(`--transaction must be sale|lease|both, got '${txArg}'`);
  process.exit(1);
}
const TRANSACTIONS: Tx[] = txArg === "both" ? ["sale", "lease"] : [txArg as Tx];
const rawMax = Number(flags["max-items"] ?? "0");
const MAX_ITEMS = rawMax <= 0 ? Number.POSITIVE_INFINITY : rawMax;
const PAGE_CAP = Math.max(1, Number(flags["page-cap"] ?? "60"));
const CONCURRENCY = Math.max(1, Math.min(6, Number(flags.concurrency ?? "3")));
const OUT_PATH = flags.out ?? null;

type Tx = "sale" | "lease";

// ---------- shared helpers ----------

function clean(s: any): string | null {
  if (typeof s !== "string") return null;
  const t = s.replace(/\s+/g, " ").trim();
  return t || null;
}

function num(v: any): number | null {
  return typeof v === "number" && isFinite(v) && v !== 0 ? v : null;
}

function boundedInt(value: string | undefined, fallback: number, lo: number, hi: number): number {
  const parsed = value === undefined ? fallback : Number(value);
  const finite = Number.isFinite(parsed) ? parsed : fallback;
  return Math.max(lo, Math.min(hi, Math.trunc(finite)));
}

function moneyToNumber(t: string | null): number | null {
  if (!t) return null;
  const m = t.replace(/,/g, "").match(/\$\s*([0-9]+(?:\.[0-9]+)?)/);
  return m ? Number(m[1]) : null;
}

function prune(v: any): any {
  if (v === null || v === undefined || v === false || v === "") return undefined;
  if (Array.isArray(v)) {
    const arr = v.map(prune).filter((x) => x !== undefined);
    return arr.length ? arr : undefined;
  }
  if (typeof v === "object") {
    const out: Record<string, any> = {};
    for (const [k, val] of Object.entries(v)) {
      const p = prune(val);
      if (p !== undefined) out[k] = p;
    }
    return Object.keys(out).length ? out : undefined;
  }
  return v;
}

type ScrapeOpts = {
  waitFor?: number;
  proxy?: "stealth" | "basic" | "auto";
  timeout?: number;
  jsonAttempts?: number;
  jsonBackoffMs?: number;
};
type ScrapedDoc = {
  rawHtml: string;
  markdown: string;
  links: string[];
  metadata?: Record<string, any>;
};

async function scrapeRaw(url: string, opts: ScrapeOpts = {}): Promise<string> {
  let lastErr: unknown = null;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const doc = await firecrawl.scrape(url, {
        formats: ["rawHtml"],
        ...(opts.waitFor ? { waitFor: opts.waitFor } : {}),
        ...(opts.proxy ? { proxy: opts.proxy } : {}),
        timeout: opts.timeout ?? 90000,
      } as any);
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

async function scrapeDoc(url: string, opts: ScrapeOpts = {}): Promise<ScrapedDoc> {
  let lastErr: unknown = null;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const doc = await firecrawl.scrape(url, {
        formats: ["rawHtml", "markdown", "links"],
        onlyMainContent: false,
        ...(opts.waitFor ? { waitFor: opts.waitFor } : {}),
        ...(opts.proxy ? { proxy: opts.proxy } : {}),
        timeout: opts.timeout ?? 90000,
      } as any);
      const anyDoc = doc as any;
      const data = anyDoc.data ?? anyDoc;
      const rawHtml = data.rawHtml ?? "";
      const markdown = data.markdown ?? "";
      const links = Array.isArray(data.links) ? data.links : [];
      if (!rawHtml && !markdown) throw new Error("empty scraped document");
      return { rawHtml, markdown, links, metadata: data.metadata };
    } catch (err) {
      lastErr = err;
      console.error(`scrape-doc attempt ${attempt} failed for ${url}: ${err}`);
      await new Promise((r) => setTimeout(r, 2500 * attempt));
    }
  }
  throw lastErr;
}

function parseJsonBody(body: string): any | null {
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

function repairUnescapedJsonStringQuotes(body: string): string {
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

async function scrapeJson(url: string, opts: ScrapeOpts = {}): Promise<any> {
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

// Bounded-concurrency map that preserves input order in the result.
async function pmap<T, R>(items: T[], limit: number, fn: (t: T, i: number) => Promise<R>): Promise<R[]> {
  const results: R[] = new Array(items.length);
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const i = next++;
      results[i] = await fn(items[i], i);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return results;
}

// ---------- broker dedupe table (shared across sources) ----------

const brokerIndex = new Map<string, number>();
const brokers: any[] = [];

function brokerRef(b: {
  name: string | null;
  email?: string | null;
  phone?: string | null;
  office?: string | null;
  avatarUrl?: string | null;
  company: string;
}): number | null {
  if (!b.name && !b.email) return null;
  const key = (b.email ?? "") + "|" + (b.name ?? "") + "|" + b.company;
  const existing = brokerIndex.get(key);
  if (existing !== undefined) {
    const rec = brokers[existing];
    if (!rec.phone && b.phone) rec.phone = b.phone;
    if (!rec.office && b.office) rec.office = b.office;
    if (!rec.avatarUrl && b.avatarUrl) rec.avatarUrl = b.avatarUrl;
    return existing;
  }
  const idx = brokers.length;
  brokers.push({
    name: b.name ?? null,
    email: b.email ?? null,
    phone: b.phone ?? null,
    office: b.office ?? null,
    avatarUrl: b.avatarUrl ?? null,
    company: b.company,
  });
  brokerIndex.set(key, idx);
  return idx;
}

// ---------- source adapters ----------
// Each adapter returns { company, sourceUrl, method, totalAvailable, listings, note? }.
// Listings share one field vocabulary (prune() drops what a source lacks):
// id, name, headline, transactionType, assetType, description, street, city, state,
// postalCode, country, latitude, longitude, salePriceUsd, salePriceText, capRatePct,
// leaseRateText, sizeText, buildingSizeSqft, lotSizeAcres, brokerIds, brochures,
// photos, url, lastUpdated

type SourceResult = {
  company: string;
  sourceUrl: string;
  method: string;
  totalAvailable: number | null;
  listings: any[];
  note?: string;
};

// --- CBRE: internal listings JSON API, paginated, behind Cloudflare (stealth) ---

async function srcCbre(tx: Tx, max: number): Promise<SourceResult> {
  const aspect = tx === "sale" ? "isSale" : "isLetting";
  const opts: ScrapeOpts = { proxy: "stealth", waitFor: 4000, timeout: 120000 };
  const base = `https://www.cbre.com/listings-api/propertylistings/query?site=us-comm&Common.Aspects=${aspect}&PageSize=200`;
  const first = await scrapeJson(`${base}&Page=1`, opts);
  if (typeof first.DocumentCount !== "number" || !Array.isArray(first.Documents)) {
    throw new Error("CBRE listings API response is missing DocumentCount/Documents fields");
  }
  const total: number = first.DocumentCount;
  const want = Math.min(max, total);
  const pages = Math.ceil(want / 200);
  console.error(`  cbre/${tx}: ${total} total, fetching ${pages} page(s)`);
  const docsArr: any[][] = [first.Documents.flat()];
  if (pages > 1) {
    const pageNums = Array.from({ length: pages - 1 }, (_, i) => i + 2);
    const rest = await pmap(pageNums, CONCURRENCY, async (p) => {
      const d = await scrapeJson(`${base}&Page=${p}`, opts);
      console.error(`  cbre/${tx}: page ${p}/${pages} (${(d.Documents ?? []).flat().length} docs)`);
      return Array.isArray(d.Documents) ? d.Documents.flat() : [];
    });
    docsArr.push(...rest);
  }
  const docs = docsArr.flat().slice(0, want);
  const text = (loc: any) =>
    Array.isArray(loc) && loc.length ? clean(loc[0]["Common.Text"]) : null;
  const listings = docs.map((d: any) => {
    const addr = d["Common.ActualAddress"] ?? {};
    const charges: any[] = Array.isArray(d["Common.Charges"]) ? d["Common.Charges"] : [];
    const sale = charges.find(
      (c: any) => c["Common.ChargeKind"] === "SalePrice" && num(c["Common.Amount"])
    );
    const rent = charges.find(
      (c: any) => c["Common.ChargeKind"] === "Rent" && num(c["Common.Amount"])
    );
    const coord = d["Common.Coordinate"] ?? {};
    const aspects: string[] = Array.isArray(d["Common.Aspects"]) ? d["Common.Aspects"] : [];
    const name = clean(addr["Common.Line1"]);
    const street = clean(addr["Common.Line2"]);
    const city = clean(addr["Common.Locallity"]);
    const state = clean(addr["Common.Region"]);
    const zip = clean(addr["Common.PostCode"]);
    const slug = [name, street, city, state, zip]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "");
    const brokerIds = (Array.isArray(d["Common.Agents"]) ? d["Common.Agents"] : [])
      .map((a: any) =>
        brokerRef({
          name: clean(a["Common.AgentName"]),
          email: clean(a["Common.EmailAddress"]),
          phone: clean(a["Common.TelephoneNumber"]),
          office: clean(a["Common.AgentOffice"]),
          company: "CBRE",
        })
      )
      .filter((x: number | null): x is number => x !== null);
    const isSale = aspects.includes("isSale");
    const isLet = aspects.includes("isLetting");
    return {
      id: d["Common.PrimaryKey"],
      name,
      headline: text(d["Common.Strapline"]),
      transactionType: isSale && isLet ? "Sale/Lease" : isLet ? "Lease" : "Sale",
      assetType: clean(d["Common.UsageType"]),
      description: text(d["Common.LongDescription"]),
      street,
      city,
      state,
      postalCode: zip,
      country: clean(addr["Common.Country"]),
      latitude: typeof coord.lat === "number" ? coord.lat : null,
      longitude: typeof coord.lon === "number" ? coord.lon : null,
      salePriceUsd: sale ? sale["Common.Amount"] : null,
      salePriceText: sale || tx === "lease" ? null : "Contact broker for pricing",
      leaseRateText: rent
        ? `${rent["Common.Amount"]} ${clean(rent["Common.ChargeCurrency"]) ?? "USD"}/${clean(rent["Common.ChargeInterval"]) ?? ""} ${clean(rent["Common.ChargeBasis"]) ?? ""}`.trim()
        : null,
      buildingSizeSqft: num(d["Dynamic.TotalArea"]),
      brokerIds,
      brochures: (Array.isArray(d["Common.Brochures"]) ? d["Common.Brochures"] : []).map(
        (b: any) => ({
          name: clean(b["Common.BrochureName"]),
          url: clean(b["Common.Uri"])?.startsWith("http")
            ? clean(b["Common.Uri"])
            : `https://www.cbre.com${clean(b["Common.Uri"]) ?? ""}`,
        })
      ),
      photos: (Array.isArray(d["Common.Photos"]) ? d["Common.Photos"] : [])
        .map((p: any) => {
          const r =
            (p["Common.ImageResources"] ?? []).find(
              (x: any) => x["Common.Breakpoint"] === "original"
            ) ?? (p["Common.ImageResources"] ?? [])[0];
          const u = r && clean(r["Common.Resource.Uri"]);
          return u ? (u.startsWith("http") ? u : `https://www.cbre.com${u}`) : null;
        })
        .filter(Boolean),
      url: `https://www.cbre.com/properties/properties-for-lease/commercial-space/details/${d["Common.PrimaryKey"]}/${slug}`,
      lastUpdated: clean(d["Common.LastUpdated"])?.slice(0, 10) ?? null,
    };
  });
  return {
    company: "CBRE",
    sourceUrl: `https://www.cbre.com/properties (${aspect})`,
    method: "CBRE public listings API (JSON, paginated, stealth proxy)",
    totalAvailable: total,
    listings,
  };
}

// --- Buildout platform (SVN, Lee & Associates): inventory JSON API, paginated ---
// The inventory feed has no server-side sale/lease filter; items carry a `sale`
// boolean (false = lease availability). Fetch the full inventory once per
// brokerage (cached across the sale and lease passes) and partition client-side.

const buildoutCache = new Map<string, { items: any[]; total: number | null }>();
const buildoutFailureCache = new Map<string, Error>();

type BuildoutInventoryOpts = {
  preferDirectJson?: boolean;
  directReferer?: string;
  pageConcurrency?: number;
  requireCompletePages?: boolean;
  cacheSlug?: string;
  usePageCache?: boolean;
  recoveryPasses?: number;
  recoveryCooldownMs?: number;
  maxRecoveryPages?: number;
  jsonAttempts?: number;
  jsonBackoffMs?: number;
};

function buildoutInventoryUrl(pluginKey: string, page: number): string {
  return `https://buildout.com/plugins/${pluginKey}/inventory.json?page=${page}`;
}

function envBool(name: string): boolean {
  return ["1", "true", "yes", "on"].includes((process.env[name] ?? "").toLowerCase());
}

function envInt(name: string): number | null {
  const raw = process.env[name];
  if (raw === undefined || raw.trim() === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) ? Math.max(0, Math.trunc(n)) : null;
}

function buildoutCacheSlug(company: string, pluginKey: string, opts: BuildoutInventoryOpts): string {
  return (
    opts.cacheSlug ??
    company
      .toLowerCase()
      .replace(/&/g, "and")
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "") ??
    pluginKey.slice(0, 12)
  );
}

function buildoutCacheDir(): string {
  return process.env.BUILDOUT_CACHE_DIR ?? "out/cache/buildout";
}

function buildoutPageCachePath(company: string, pluginKey: string, page: number, opts: BuildoutInventoryOpts): string {
  return `${buildoutCacheDir()}/${buildoutCacheSlug(company, pluginKey, opts)}/page-${String(page).padStart(4, "0")}.json`;
}

function readBuildoutPageCache(
  company: string,
  pluginKey: string,
  page: number,
  opts: BuildoutInventoryOpts
): any | null {
  const path = buildoutPageCachePath(company, pluginKey, page, opts);
  if (!existsSync(path)) return null;
  try {
    const cached = JSON.parse(readFileSync(path, "utf8"));
    if (cached.pluginKey !== pluginKey || cached.page !== page) return null;
    const data = cached.data;
    if (!data || !Array.isArray(data.inventory)) return null;
    return data;
  } catch {
    return null;
  }
}

function writeBuildoutPageCache(
  company: string,
  pluginKey: string,
  page: number,
  opts: BuildoutInventoryOpts,
  data: any
): void {
  if (!data || !Array.isArray(data.inventory)) return;
  const path = buildoutPageCachePath(company, pluginKey, page, opts);
  mkdirSync(dirname(path), { recursive: true });
  const tmp = `${path}.tmp-${process.pid}-${Date.now()}`;
  writeFileSync(
    tmp,
    JSON.stringify(
      {
        pluginKey,
        page,
        cachedAt: new Date().toISOString(),
        data,
      },
      null,
      2
    )
  );
  renameSync(tmp, path);
}

function buildoutPageWindow(pages: number): { start: number; end: number } | null {
  const start = envInt("BUILDOUT_PAGE_START");
  const end = envInt("BUILDOUT_PAGE_END");
  if (start === null && end === null) return null;
  const lo = Math.max(0, start ?? 0);
  const hi = Math.min(pages - 1, end ?? lo);
  if (hi < lo) throw new Error(`invalid Buildout page window ${lo}-${hi}`);
  return { start: lo, end: hi };
}

function buildoutJitterMs(): [number, number] | null {
  const raw = process.env.BUILDOUT_PAGE_JITTER_MS;
  if (!raw) return null;
  const parts = raw.split(",").map((p) => Number(p.trim()));
  if (!parts.every(Number.isFinite)) return null;
  const lo = Math.max(0, Math.trunc(parts[0] ?? 0));
  const hi = Math.max(lo, Math.trunc(parts[1] ?? parts[0] ?? 0));
  return [lo, hi];
}

async function sleepBuildoutJitter(): Promise<void> {
  const jitter = buildoutJitterMs();
  if (!jitter || jitter[1] <= 0) return;
  const [lo, hi] = jitter;
  const ms = lo + Math.floor(Math.random() * (hi - lo + 1));
  await new Promise((r) => setTimeout(r, ms));
}

async function directBuildoutJson(url: string, referer: string, timeoutMs = 15000): Promise<any> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      signal: controller.signal,
      headers: {
        accept: "application/json,text/plain,*/*",
        "user-agent":
          "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
        referer,
      },
    });
    const body = await res.text();
    const parsed = parseJsonBody(body);
    if (res.ok && parsed !== null) return parsed;
    const contentType = res.headers.get("content-type") ?? "unknown";
    const shape = parsed === null ? "non-json" : "json-error-status";
    throw new Error(`direct GET ${res.status} ${contentType} (${shape})`);
  } finally {
    clearTimeout(timer);
  }
}

async function fetchBuildoutInventoryPage(
  company: string,
  pluginKey: string,
  page: number,
  opts: BuildoutInventoryOpts
): Promise<any> {
  const url = buildoutInventoryUrl(pluginKey, page);
  const useCache = opts.usePageCache || envBool("BUILDOUT_USE_PAGE_CACHE") || envBool("BUILDOUT_CACHE_ONLY") || envBool("BUILDOUT_ASSEMBLE_FROM_CACHE");
  if (useCache) {
    const cached = readBuildoutPageCache(company, pluginKey, page, opts);
    if (cached) return cached;
    if (envBool("BUILDOUT_ASSEMBLE_FROM_CACHE")) {
      throw new Error(`missing Buildout cache page ${page} for ${buildoutCacheSlug(company, pluginKey, opts)}`);
    }
  }
  await sleepBuildoutJitter();
  let data: any;
  if (opts.preferDirectJson) {
    try {
      data = await directBuildoutJson(url, opts.directReferer ?? "https://buildout.com/");
      if (page === 0) console.error(`  ${company}: direct Buildout JSON available`);
    } catch (err) {
      console.error(`  ${company}: direct Buildout JSON failed for page ${page} (${err}); falling back to Firecrawl`);
    }
  }
  data ??= await scrapeJson(url, {
    timeout: 60000,
    jsonAttempts: opts.jsonAttempts,
    jsonBackoffMs: opts.jsonBackoffMs,
  });
  if (useCache) writeBuildoutPageCache(company, pluginKey, page, opts, data);
  return data;
}

async function buildoutInventory(
  company: string,
  pluginKey: string,
  opts: BuildoutInventoryOpts = {}
): Promise<{ items: any[]; total: number | null }> {
  const cached = buildoutCache.get(pluginKey);
  if (cached) return cached;
  const cachedFailure = buildoutFailureCache.get(pluginKey);
  if (cachedFailure) throw cachedFailure;
  const first = await fetchBuildoutInventoryPage(company, pluginKey, 0, opts);
  const total: number | null = first.meta?.total ?? null;
  const limit: number = first.meta?.limit ?? 30;
  const pages = total && total > limit ? Math.min(Math.ceil(total / limit), 1200) : 1;
  const pageWindow = buildoutPageWindow(pages);
  const cacheOnly = envBool("BUILDOUT_CACHE_ONLY");
  const assembleFromCache = envBool("BUILDOUT_ASSEMBLE_FROM_CACHE");
  if (pageWindow) {
    console.error(
      `  ${company}: Buildout page window ${pageWindow.start}-${pageWindow.end}/${pages - 1}${
        cacheOnly ? " (cache-only)" : assembleFromCache ? " (assembling from cache)" : ""
      }`
    );
  }
  const inventoryByPage = new Map<number, any[]>();
  inventoryByPage.set(0, first.inventory ?? []);
  const failedPages = new Set<number>();
  const attemptedPages = new Set<number>([0]);
  const unattemptedPages = new Set<number>();
  if (total && total > limit) {
    const failureLimit = Math.max(3, Math.floor(pages * 0.03));
    const allPageNums = Array.from({ length: pages - 1 }, (_, i) => i + 1);
    const pageNums =
      pageWindow && !assembleFromCache
        ? allPageNums.filter((p) => p >= pageWindow.start && p <= pageWindow.end)
        : allPageNums;
    if (pageWindow && !assembleFromCache) {
      for (const p of allPageNums) {
        if (!pageNums.includes(p)) unattemptedPages.add(p);
      }
    }
    const pageConcurrency = Math.max(1, Math.min(opts.pageConcurrency ?? Math.min(CONCURRENCY, 2), pageNums.length));
    let done = 1;
    let aborting = false;
    await pmap(pageNums, pageConcurrency, async (p) => {
      if (aborting) {
        unattemptedPages.add(p);
        return;
      }
      try {
        attemptedPages.add(p);
        const d = await fetchBuildoutInventoryPage(company, pluginKey, p, opts);
        inventoryByPage.set(p, d.inventory ?? []);
        done++;
        if (done % 25 === 0) console.error(`  ${company}: inventory page ${done}/${pages}`);
      } catch (err) {
        failedPages.add(p);
        console.error(`  ${company}: inventory page ${p} FAILED after retries: ${err}`);
        if (failedPages.size > failureLimit) {
          aborting = true;
        }
      }
    });

    const recoveryPasses = assembleFromCache ? 0 : opts.recoveryPasses ?? 0;
    const maxRecoveryPages = opts.maxRecoveryPages ?? failureLimit;
    for (let pass = 1; pass <= recoveryPasses && failedPages.size > 0; pass++) {
      if (failedPages.size > maxRecoveryPages) break;
      const retryPages = [...failedPages].sort((a, b) => a - b);
      console.error(
        `  ${company}: retrying ${retryPages.length} failed Buildout page(s), recovery pass ${pass}/${recoveryPasses}`
      );
      await new Promise((r) => setTimeout(r, opts.recoveryCooldownMs ?? 15000));
      for (const p of retryPages) {
        try {
          let d: any;
          if (opts.preferDirectJson) {
            try {
              d = await directBuildoutJson(
                buildoutInventoryUrl(pluginKey, p),
                opts.directReferer ?? "https://buildout.com/"
              );
            } catch {
              d = await fetchBuildoutInventoryPage(company, pluginKey, p, {
                ...opts,
                jsonAttempts: opts.jsonAttempts ?? 4,
                jsonBackoffMs: opts.jsonBackoffMs ?? 12000,
              });
            }
          } else {
            d = await fetchBuildoutInventoryPage(company, pluginKey, p, {
              ...opts,
              jsonAttempts: opts.jsonAttempts ?? 4,
              jsonBackoffMs: opts.jsonBackoffMs ?? 12000,
            });
          }
          inventoryByPage.set(p, d.inventory ?? []);
          if (opts.usePageCache || envBool("BUILDOUT_USE_PAGE_CACHE") || envBool("BUILDOUT_CACHE_ONLY")) {
            writeBuildoutPageCache(company, pluginKey, p, opts, d);
          }
          failedPages.delete(p);
          console.error(`  ${company}: recovered Buildout inventory page ${p}`);
        } catch (err) {
          console.error(`  ${company}: inventory page ${p} still failed on recovery pass ${pass}: ${err}`);
        }
      }
    }

    if (cacheOnly) {
      const missingAttempted = [...pageNums].filter((p) => !inventoryByPage.has(p));
      const cacheError = new Error(
        `${company}: cache-only Buildout window complete (${pageWindow?.start ?? 0}-${pageWindow?.end ?? pages - 1}); ` +
          `${attemptedPages.size} page(s) attempted, ${missingAttempted.length} selected page(s) missing; not producing listing artifact`
      );
      buildoutFailureCache.set(pluginKey, cacheError);
      throw cacheError;
    }

    if (pageWindow && !assembleFromCache) {
      const windowError = new Error(
        `${company}: Buildout page window was requested without BUILDOUT_CACHE_ONLY=1 or BUILDOUT_ASSEMBLE_FROM_CACHE=1; refusing partial listing artifact`
      );
      buildoutFailureCache.set(pluginKey, windowError);
      throw windowError;
    }

    if (assembleFromCache) {
      for (let p = 0; p < pages; p++) {
        if (!inventoryByPage.has(p)) failedPages.add(p);
      }
    }

    // A few rate-limited pages are tolerable (gap fills on the next run);
    // a large gap means the feed is unusable and must not be cached or
    // ingested (mark-missing on a gappy run would soft-delete live rows).
    if (opts.requireCompletePages ? failedPages.size > 0 : failedPages.size > failureLimit) {
      const failed = [...failedPages].sort((a, b) => a - b);
      const shown = failed.slice(0, 20).join(",");
      const suffix = failed.length > 20 ? `... (+${failed.length - 20} more)` : "";
      const unattempted = unattemptedPages.size ? `, ${unattemptedPages.size} unattempted page(s)` : "";
      const abortError = new Error(
        `${company}: ${failedPages.size}/${pages} inventory pages failed (${shown}${suffix}${unattempted}); aborting this source`
      );
      buildoutFailureCache.set(pluginKey, abortError);
      throw abortError;
    }
  }
  const items: any[] = [];
  for (let p = 0; p < pages; p++) items.push(...(inventoryByPage.get(p) ?? []));
  const result = { items, total };
  buildoutCache.set(pluginKey, result);
  console.error(
    `  ${company}: full inventory cached (${items.length} items, total ${total ?? "?"}${failedPages.size ? `, ${failedPages.size} pages skipped` : ""})`
  );
  return result;
}

async function srcBuildout(
  company: string,
  pluginKey: string,
  listingsPage: string,
  tx: Tx,
  max: number,
  inventoryOpts: BuildoutInventoryOpts = {}
): Promise<SourceResult> {
  const { items, total } = await buildoutInventory(company, pluginKey, inventoryOpts);
  const listings: any[] = [];
  for (const x of items) {
    if (listings.length >= max) break;
    if (x.closed === true) continue;
    const isSale = x.sale === true;
    const isLease = x.sale !== true; // inventory rows are active availabilities: not-for-sale = for-lease
    if (tx === "sale" && !isSale) continue;
    if (tx === "lease" && !isLease) continue;
    {
      const attrs = new Map<string, string>(
        (x.index_attributes ?? []).map((p: any) => [String(p[0]), String(p[1])])
      );
      const priceText = attrs.get("Price") ?? null;
      const leaseRateText = attrs.get("Lease Rate") ?? attrs.get("Rate") ?? null;
      const sizeText =
        attrs.get("Building Size") ?? attrs.get("Lot Size") ?? clean(x.size_summary);
      const brokerIds = (x.broker_contacts ?? [])
        .map((b: any) =>
          brokerRef({
            name: clean(b.name),
            email: clean(b.email),
            phone: clean(b.phone),
            avatarUrl: clean(b.photo_url),
            company,
          })
        )
        .filter((v: number | null): v is number => v !== null);
      listings.push({
        id: x.id != null ? String(x.id) : null,
        name: clean(x.display_name) ?? clean(x.name),
        transactionType: x.also_for_sale_or_lease ? "Sale/Lease" : isLease ? "Lease" : "Sale",
        assetType: clean(x.property_sub_type_name) ?? attrs.get("Property Type") ?? null,
        street: clean(x.address),
        city: clean(x.city),
        state: clean(x.state),
        postalCode: clean(x.zip),
        country: "US",
        latitude: num(x.latitude),
        longitude: num(x.longitude),
        salePriceUsd: tx === "sale" ? moneyToNumber(priceText) : null,
        salePriceText: tx === "sale" ? priceText : null,
        leaseRateText,
        sizeText,
        brokerIds,
        brochures: x.pdf_url ? [{ name: "Listing brochure (PDF)", url: x.pdf_url }] : [],
        photos: [x.photo_url, x.large_thumbnail_url].filter(Boolean).slice(0, 1),
        url: clean(x.show_link),
        underContract: x.under_contract === true,
      });
    }
  }
  return {
    company,
    sourceUrl: listingsPage,
    method: "Buildout plugin inventory API (JSON, paginated)",
    totalAvailable: total,
    listings,
  };
}

// --- Newmark: Algolia search API, credentials read from the page ---

let newmarkCreds: { appId: string; searchKey: string; indexName: string } | null = null;
const newmarkPeopleCache = new Map<string, Promise<any | null>>();

async function newmarkAlgoliaJson(url: string): Promise<any> {
  const res = await fetch(url, {
    headers: {
      accept: "application/json",
      "user-agent": "Mozilla/5.0 CRE collector",
    },
  });
  if (!res.ok) throw new Error(`Newmark Algolia HTTP ${res.status}`);
  return res.json();
}

function normalizePersonName(value: any): string | null {
  return clean(value)?.toLowerCase().replace(/\s+/g, " ") ?? null;
}

function newmarkState(hit: any): string | null {
  const state = clean(hit.state);
  if (state) return state;
  const stateCode = clean(hit.state_code);
  if (stateCode && /^[A-Za-z]{2}$/.test(stateCode)) return stateCode.toUpperCase();
  const city = clean(hit.city)?.toLowerCase();
  const zip = clean(hit.zip);
  if (city === "washington" && zip?.startsWith("200")) return "DC";
  return null;
}

function newmarkAbsoluteUrl(value: any): string | null {
  const url = clean(value);
  if (!url) return null;
  try {
    return new URL(url, "https://www.nmrk.com").toString();
  } catch {
    return null;
  }
}

async function srcNewmark(tx: Tx, max: number): Promise<SourceResult> {
  const sourceUrl = "https://www.nmrk.com/properties";
  if (!newmarkCreds) {
    for (const waitFor of [3000, 8000]) {
      const html = await scrapeRaw(sourceUrl, { waitFor });
      const appId = html.match(/algoliaAppId='([^']+)'/)?.[1];
      const searchKey = html.match(/algoliaSearchApiKey='([^']+)'/)?.[1];
      const indexName = html.match(/algoliaIndexName='([^']+)'/)?.[1] ?? "prod_entries";
      if (appId && searchKey) {
        newmarkCreds = { appId, searchKey, indexName };
        break;
      }
    }
    if (!newmarkCreds) throw new Error("could not extract Algolia credentials from nmrk.com/properties");
  }
  const { appId, searchKey, indexName } = newmarkCreds;
  const facetVal = tx === "sale" ? "Sale" : "Lease";

  const query = async (
    extraFacets: string[],
    hitsPerPage: number,
    page = 0,
    facetsField = "state"
  ) => {
    const facetFilters = encodeURIComponent(
      JSON.stringify([
        "sectionGroup:Properties",
        `saleOrLease:${facetVal}`,
        "country_code:US",
        "siteHandle:enUs",
        ...extraFacets,
      ])
    );
    return newmarkAlgoliaJson(
      `https://${appId}-dsn.algolia.net/1/indexes/${indexName}?x-algolia-application-id=${appId}&x-algolia-api-key=${searchKey}&query=&hitsPerPage=${hitsPerPage}&page=${page}&facets=${encodeURIComponent(facetsField)}&facetFilters=${facetFilters}`
    );
  };
  const queryFilters = async (filters: string, hitsPerPage: number, page = 0) =>
    newmarkAlgoliaJson(
      `https://${appId}-dsn.algolia.net/1/indexes/${indexName}?x-algolia-application-id=${appId}&x-algolia-api-key=${searchKey}&query=&hitsPerPage=${hitsPerPage}&page=${page}&filters=${encodeURIComponent(filters)}`
    );
  const lookupPerson = (name: string | null): Promise<any | null> => {
    const key = normalizePersonName(name);
    if (!key) return Promise.resolve(null);
    if (!newmarkPeopleCache.has(key)) {
      const run = (async () => {
        const facetFilters = encodeURIComponent(JSON.stringify(["sectionGroup:People", "siteHandle:enUs"]));
        const result = await newmarkAlgoliaJson(
          `https://${appId}-dsn.algolia.net/1/indexes/${indexName}?x-algolia-application-id=${appId}&x-algolia-api-key=${searchKey}&query=${encodeURIComponent(name ?? "")}&hitsPerPage=5&page=0&facetFilters=${facetFilters}`
        );
        const hits = Array.isArray(result.hits) ? result.hits : [];
        return (
          hits.find((h: any) => {
            const fullName = normalizePersonName(h.fullName ?? h.title);
            return fullName === key;
          }) ?? null
        );
      })().catch((err) => {
        console.error(`  newmark: people lookup failed for ${name}: ${err}`);
        return null;
      });
      newmarkPeopleCache.set(key, run);
    }
    return newmarkPeopleCache.get(key)!;
  };

  const first = await query([], 1000);
  if (!Array.isArray(first.hits)) throw new Error("Newmark Algolia response has no hits array");
  const total: number = first.nbHits ?? first.hits.length;
  const hitMap = new Map<string, any>();
  for (const h of first.hits) hitMap.set(h.objectID ?? h.slug ?? JSON.stringify(h), h);

  // Algolia caps retrievable hits (~1000/query). Split by state facet for full coverage.
  if (total > first.hits.length && hitMap.size < Math.min(max, total)) {
    const states: string[] = Object.keys(first.facets?.state ?? {});
    console.error(`  newmark/${tx}: ${total} total > single-query cap, splitting across ${states.length} states`);
    await pmap(states, CONCURRENCY, async (st) => {
      if (hitMap.size >= max) return;
      const r = await query([`state:${st}`], 1000, 0, "property_types");
      for (const h of r.hits ?? []) hitMap.set(h.objectID ?? h.slug ?? JSON.stringify(h), h);
      if ((r.nbHits ?? 0) > 1000) {
        // Sub-split the over-cap state by property type to stay under the cap.
        const types: string[] = Object.keys(r.facets?.property_types ?? {});
        if (!types.length) {
          console.error(`  newmark/${tx}: WARNING state ${st} has ${r.nbHits} hits, >1000 cap and no property_types facet; coverage truncated`);
          return;
        }
        console.error(`  newmark/${tx}: state ${st} has ${r.nbHits} hits, sub-splitting across ${types.length} property types`);
        await pmap(types, 2, async (pt) => {
          const r2 = await query([`state:${st}`, `property_types:${pt}`], 1000);
          for (const h of r2.hits ?? []) hitMap.set(h.objectID ?? h.slug ?? JSON.stringify(h), h);
          if ((r2.nbHits ?? 0) > 1000) {
            console.error(`  newmark/${tx}: WARNING ${st}/${pt} still ${r2.nbHits} hits, >1000 cap; coverage truncated`);
          }
        });
      }
    });
    if (hitMap.size < Math.min(max, total) && states.length) {
      const baseFilters = [
        'sectionGroup:"Properties"',
        `saleOrLease:"${facetVal}"`,
        'country_code:"US"',
        'siteHandle:"enUs"',
      ];
      const noStateFilters = [
        ...baseFilters,
        ...states.map((st) => `NOT state:"${String(st).replace(/"/g, '\\"')}"`),
      ].join(" AND ");
      const r = await queryFilters(noStateFilters, 1000);
      const before = hitMap.size;
      for (const h of r.hits ?? []) hitMap.set(h.objectID ?? h.slug ?? JSON.stringify(h), h);
      if (hitMap.size > before) {
        console.error(`  newmark/${tx}: recovered ${hitMap.size - before} no-state Algolia hit(s)`);
      }
      if ((r.nbHits ?? 0) > 1000) {
        console.error(`  newmark/${tx}: WARNING no-state recovery returned ${r.nbHits} hits, >1000 cap; coverage may be truncated`);
      }
    }
  }

  const hits = [...hitMap.values()].slice(0, Math.min(max, Number.MAX_SAFE_INTEGER));
  const listings = await pmap(hits, Math.min(CONCURRENCY, 4), async (h: any) => {
    const person = await lookupPerson(clean(h.broker_name));
    const contactsDetailed = person
      ? [
          {
            name: clean(person.fullName) ?? clean(person.title),
            title: clean(person.positionJobTitle),
            email: clean(person.email),
            phone: stripHtmlText(person.phone) ?? stripHtmlText(person.mobilePhoneNumber),
            company: "Newmark",
            office: Array.isArray(person.offices) ? clean(person.offices.join(", ")) : clean(person.offices),
            profileUrl: newmarkAbsoluteUrl(person.url),
            avatarUrl: Array.isArray(person.thumbnails) ? clean(person.thumbnails.at(-1)?.url) : null,
          },
        ]
      : [];
    return {
    id: clean(h.slug),
    name: clean(h.title),
    headline: clean(h.content),
    transactionType: facetVal,
    assetType: Array.isArray(h.property_types)
      ? h.property_types.join(", ")
      : clean(h.property_type),
    street: clean(h.address),
    city: clean(h.city),
    state: newmarkState(h),
    postalCode: clean(h.zip),
    county: clean(h.county),
    submarket: clean(h.submarket),
    country: clean(h.country_code) ?? "US",
    latitude: num(h.latitude),
    longitude: num(h.longitude),
    salePriceUsd: tx === "sale" ? num(h.sale_price) : null,
    salePriceText: tx === "sale" && !h.sale_price ? "Contact broker for pricing" : null,
    buildingSizeSqft: num(h.building_size_sf),
    lotSizeAcres: num(h.lot_size_acres),
    brokerIds: [],
    contactsDetailed,
    newmarkBrokerProvenance: {
      broker_name: clean(h.broker_name),
      broker_id: h.broker_id ?? null,
      broker_ids: Array.isArray(h.broker_ids) ? h.broker_ids : [],
      second_broker_id: h.second_broker_id ?? null,
      third_broker_id: h.third_broker_id ?? null,
    },
    rawNewmarkHit: h,
    photos: (h.thumbnails ?? []).slice(-1).map((t: any) => t.url),
    url: h.url ? `https://www.nmrk.com${h.url}` : null,
    lastUpdated: clean(h.updateDate)?.slice(0, 10) ?? null,
    };
  });
  return {
    company: "Newmark",
    sourceUrl,
    method: "Newmark Algolia search API (JSON; credentials read from the page; state/property-type split plus no-state recovery)",
    totalAvailable: total,
    listings,
  };
}

// --- JLL: rendered search pages ---

const JLL_PROPERTY_TYPES = [
  "office",
  "industrial",
  "retail",
  "land",
  "medical",
  "multifamily",
  "lab",
  "coworking",
  "data-center",
] as const;
const JLL_DETAIL_CONCURRENCY = boundedInt(
  process.env.JLL_DETAIL_CONCURRENCY,
  Math.min(CONCURRENCY, 3),
  1,
  10
);

function jllPropertyTypeLabel(propertyType: string): string {
  return propertyType
    .split("-")
    .map((part) => part[0].toUpperCase() + part.slice(1))
    .join(" ");
}

function normalizedJllListingUrl(href: string): string {
  const abs = href.startsWith("http") ? href : `https://property.jll.com${href}`;
  const url = new URL(abs);
  url.hash = "";
  url.search = "";
  return url.toString().replace(/\/$/, "");
}

function jllFilteredSearchUrl(tenure: "sale" | "rent", propertyType: string, page: number): string {
  const url = new URL("https://property.jll.com/search");
  url.searchParams.set("tenureTypes", tenure);
  url.searchParams.set("propertyTypes", propertyType);
  url.searchParams.set("page", String(page));
  return url.toString();
}

function parseJllSearchPage(html: string, tx: Tx, propertyType: string, page: number): {
  total: number | null;
  listings: any[];
} {
  const $ = cheerio.load(html);
  const total =
    Number(
      (($("h2").text() || html).match(/([0-9][0-9,]*)\s+propert(?:y|ies)/i) ?? [])[1]?.replace(/,/g, "")
    ) || null;
  const seenHere = new Set<string>();
  const listings: any[] = [];
  $('a.text-base[href*="/listings/"]').each((_, el) => {
    const href = $(el).attr("href");
    if (!href) return;
    const url = normalizedJllListingUrl(href);
    if (seenHere.has(url)) return;
    seenHere.add(url);
    const lines: string[] = [];
    $(el)
      .find("*")
      .addBack()
      .contents()
      .each((__, n) => {
        if (n.type === "text") {
          const t = clean((n as any).data);
          if (t && t !== "&nbsp;") lines.push(t);
        }
      });
    const flat = lines.join(" | ");
    const priceText = (flat.match(/\$[0-9][0-9,.]*(?:\s*-\s*\$[0-9][0-9,.]*)?/) ?? [])[0] ?? null;
    const sizeText = (flat.match(/([0-9][0-9,.]*\s*(?:SF|Acres?))/i) ?? [])[1] ?? null;
    const addr =
      lines.find(
        (l) => /,\s*[A-Z]{2}[, ]/.test(l) || /,\s*[A-Z]{2}$/.test(l.replace(/,?\s*\d{5}$/, ""))
      ) ?? lines[1] ?? null;
    const m = (addr ?? "").match(/^(.*?),\s*([A-Z]{2}),?\s*(\d{5})?/);
    listings.push({
      id: url.split("/listings/")[1] ?? null,
      name: lines[0] ?? null,
      transactionType: tx === "sale" ? "Sale" : "Lease",
      assetType: jllPropertyTypeLabel(propertyType),
      city: m ? clean(m[1]) : null,
      state: m ? m[2] : null,
      postalCode: m?.[3] ?? null,
      country: "US",
      salePriceUsd: tx === "sale" ? moneyToNumber(priceText) : null,
      salePriceText: tx === "sale" ? priceText : null,
      leaseRateText: tx === "lease" ? priceText : null,
      sizeText,
      brokerIds: [],
      url,
      jllPropertyTypeFilters: [propertyType],
      jllSearchPages: [page],
      jllFilterTotals: total === null ? {} : { [propertyType]: total },
    });
  });
  return { total, listings };
}

async function fetchJllSearchPage(tx: Tx, propertyType: string, page: number): Promise<{
  total: number | null;
  listings: any[];
}> {
  const searchUrl = jllFilteredSearchUrl(tx === "sale" ? "sale" : "rent", propertyType, page);
  const waits = [8000, 12000, 16000];
  let lastParsed: { total: number | null; listings: any[] } | null = null;
  for (const waitFor of waits) {
    const html = await scrapeRaw(searchUrl, { waitFor });
    const parsed = parseJllSearchPage(html, tx, propertyType, page);
    lastParsed = parsed;
    if (parsed.listings.length > 0 || parsed.total === 0) return parsed;
    console.error(
      `  jll/${tx}/${propertyType}: page ${page} rendered 0 cards (total ${parsed.total ?? "?"}); retrying with waitFor=${waitFor}`
    );
  }
  return lastParsed ?? { total: null, listings: [] };
}

function mergeJllListing(existing: any, candidate: any, propertyType: string, page: number) {
  existing.jllPropertyTypeFilters = Array.from(
    new Set([...(existing.jllPropertyTypeFilters ?? []), propertyType])
  );
  existing.jllSearchPages = Array.from(new Set([...(existing.jllSearchPages ?? []), page]));
  existing.jllFilterTotals = {
    ...(existing.jllFilterTotals ?? {}),
    ...(candidate.jllFilterTotals ?? {}),
  };
  const labels = existing.jllPropertyTypeFilters.map(jllPropertyTypeLabel);
  existing.assetType = labels.join(", ");
}

function jllNextData(rawHtml: string): any | null {
  const $ = cheerio.load(rawHtml);
  const text = $("#__NEXT_DATA__").first().text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function jllDetailCacheDir(): string {
  return process.env.JLL_DETAIL_CACHE_DIR ?? "out/cache/jll-detail";
}

function jllDetailCachePath(url: string): string {
  const key = createHash("sha1").update(normalizedJllListingUrl(url)).digest("hex");
  return `${jllDetailCacheDir()}/${key}.json`;
}

function readJllDetailCache(url: string): ScrapedDoc | null {
  const path = jllDetailCachePath(url);
  if (!existsSync(path)) return null;
  try {
    const cached = JSON.parse(readFileSync(path, "utf8"));
    if (cached.url !== normalizedJllListingUrl(url)) return null;
    if (typeof cached.rawHtml !== "string") return null;
    return {
      rawHtml: cached.rawHtml,
      markdown: typeof cached.markdown === "string" ? cached.markdown : "",
      links: Array.isArray(cached.links) ? cached.links.filter((link: any) => typeof link === "string") : [],
      metadata: cached.metadata,
    };
  } catch {
    return null;
  }
}

function writeJllDetailCache(url: string, doc: ScrapedDoc): void {
  const path = jllDetailCachePath(url);
  mkdirSync(dirname(path), { recursive: true });
  const tmp = `${path}.${process.pid}.tmp`;
  writeFileSync(
    tmp,
    JSON.stringify(
      {
        url: normalizedJllListingUrl(url),
        cachedAt: new Date().toISOString(),
        rawHtml: doc.rawHtml,
        markdown: doc.markdown,
        links: doc.links,
        metadata: doc.metadata,
      },
      null,
      2
    )
  );
  renameSync(tmp, path);
}

async function scrapeJllDetailDoc(url: string): Promise<ScrapedDoc> {
  const cached = readJllDetailCache(url);
  if (cached) return cached;
  const doc = await scrapeDoc(url, { waitFor: 8000, timeout: 120000 });
  writeJllDetailCache(url, doc);
  return doc;
}

function jllPublicProfileUrl(pageUrl: any): string | null {
  const slug = clean(pageUrl);
  if (!slug) return null;
  if (/^https?:\/\//i.test(slug)) return slug;
  return `https://www.us.jll.com/en/people/${slug.replace(/^\/+/, "")}`;
}

function jllStringUrls(values: any): string[] {
  if (!Array.isArray(values)) return [];
  return dedupeStrings(values.map((value) => clean(value))).filter((url) => /^https?:\/\//i.test(url));
}

function jllSurfaceAreaSqft(property: any): number | null {
  const direct = num(property?.surfaceArea);
  if (direct) return direct;
  const areas = Array.isArray(property?.surfaceAreas) ? property.surfaceAreas : [];
  const feet = areas
    .flatMap((area: any) => [area, ...(Array.isArray(area?.metrics) ? area.metrics : [])])
    .find((area: any) => clean(area?.unit)?.toLowerCase() === "feet");
  const value = feet?.value;
  if (typeof value === "number") return num(value);
  if (value && typeof value === "object") return num(value.max) ?? num(value.min);
  return null;
}

function jllDescription(property: any): string | null {
  const sections = Array.isArray(property?.descriptionSections) ? property.descriptionSections : [];
  const pieces = sections
    .flatMap((section: any) => [stripHtmlText(section?.title), stripHtmlText(section?.content)])
    .filter(Boolean);
  const highlights = Array.isArray(property?.highlights)
    ? property.highlights.map((item: any) => stripHtmlText(item)).filter(Boolean)
    : [];
  return clean([...pieces, ...highlights].join("\n\n"));
}

function jllContacts(brokersRaw: any[]): any[] {
  const contacts = (Array.isArray(brokersRaw) ? brokersRaw : [])
    .map((broker: any) =>
      prune({
        name: clean(broker?.name),
        title: clean(broker?.jobTitle),
        email: clean(broker?.email),
        phone: clean(broker?.telephone),
        company: "JLL",
        office: clean(broker?.office ?? broker?.city),
        profileUrl: jllPublicProfileUrl(broker?.pageUrl),
        avatarUrl: clean(broker?.photo),
        linkedInUrl: clean(broker?.linkedin),
        licenses: broker?.brokerLicenses,
        entityLicenses: broker?.entityLicenses,
      })
    )
    .filter(Boolean);
  const seen = new Set<string>();
  return contacts.filter((contact: any) => {
    const key = contact.email ?? contact.profileUrl ?? contact.name ?? JSON.stringify(contact);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

async function enrichJllListing(base: any): Promise<any> {
  if (!base.url) return base;
  try {
    const doc = await scrapeJllDetailDoc(base.url);
    const next = jllNextData(doc.rawHtml);
    const pageProps = next?.props?.pageProps;
    const property = pageProps?.property;
    if (!property) return prune({ ...base, detailError: "missing property in __NEXT_DATA__" });

    const contactsDetailed = jllContacts(Array.isArray(pageProps?.brokers) ? pageProps.brokers : property?.brokers);
    const brokerIds = contactsDetailed
      .map((contact: any) =>
        brokerRef({
          name: clean(contact.name),
          email: clean(contact.email),
          phone: clean(contact.phone),
          office: clean(contact.office),
          avatarUrl: clean(contact.avatarUrl),
          company: "JLL",
        })
      )
      .filter((id: number | null): id is number => id !== null);
    const brochures = [...jllStringUrls(property.brochures), ...jllStringUrls(property.floorPlans)];
    const images = jllStringUrls(property.images);
    const detailUrl = clean(property.pageUrl) ?? clean(pageProps?.relativeUrl);
    const url = detailUrl ? normalizedJllListingUrl(detailUrl) : base.url;

    return prune({
      ...base,
      id: clean(property.id) ?? base.id,
      name: clean(property.title) ?? base.name,
      assetType: Array.isArray(property.propertyTypes)
        ? property.propertyTypes.map(jllPropertyTypeLabel).join(", ")
        : clean(property.propertyType) ?? base.assetType,
      description: jllDescription(property) ?? base.description,
      street: clean(property.address) ?? base.street,
      city: clean(property.city) ?? base.city,
      state: clean(property.state) ?? base.state,
      postalCode: clean(property.postcode) ?? base.postalCode,
      latitude: num(property.latitude) ?? base.latitude,
      longitude: num(property.longitude) ?? base.longitude,
      salePriceText: clean(property.salePrice) ?? base.salePriceText,
      leaseRateText: clean(property.rentPrice) ?? base.leaseRateText,
      sizeText: clean(property.surfaceArea) ?? base.sizeText,
      buildingSizeSqft: jllSurfaceAreaSqft(property) ?? base.buildingSizeSqft,
      brokerIds,
      contactsDetailed,
      brochures: brochures.map((docUrl) => ({ name: titleFromFilename(docUrl), url: docUrl })),
      photos: images.length ? images : base.photos ?? [],
      url,
      lastUpdated: base.lastUpdated,
      jllDetail: {
        id: clean(property.id),
        refId: clean(property.refId),
        pageUrl: clean(property.pageUrl),
        relativeUrl: clean(pageProps?.relativeUrl),
        tenureTypes: property.tenureTypes,
        propertyTypes: property.propertyTypes,
        labels: property.labels,
        amenities: property.amenities,
        amenitiesData: property.amenitiesData,
        highlights: property.highlights,
        customRefId: clean(property.customRefId),
        buildingClass: clean(property.buildingClass),
        parkingDetails: property.parkingDetails,
        locationDescription: stripHtmlText(property.locationDescription),
        submarket: clean(property.submarket),
        videos: property.videos,
        virtualTours: property.virtualTours,
        view360URLs: property.view360URLs,
        brokerCount: contactsDetailed.length,
        brochureCount: brochures.length,
        imageCount: images.length,
        scrape: {
          markdownLength: doc.markdown.length,
          rawHtmlLength: doc.rawHtml.length,
          linkCount: doc.links.length,
        },
      },
    });
  } catch (err) {
    console.error(`  jll: detail failed for ${base.url}: ${err}`);
    return prune({ ...base, detailError: String(err) });
  }
}

async function srcJll(tx: Tx, max: number): Promise<SourceResult> {
  const tenure = tx === "sale" ? "sale" : "rent";
  const sourceUrl = `https://property.jll.com/search?tenureTypes=${tenure}`;
  const listings: any[] = [];
  const byUrl = new Map<string, any>();
  const filterTotals: Record<string, number | null> = {};
  const maxByFilterPage: Record<string, number | null> = {};

  for (let page = 1; listings.length < max && page <= PAGE_CAP; page++) {
    const activePropertyTypes = JLL_PROPERTY_TYPES.filter((propertyType) => {
      const maxPage = maxByFilterPage[propertyType];
      return maxPage === undefined || maxPage === null || page <= maxPage;
    });
    if (!activePropertyTypes.length) break;

    const pageResults = await pmap(activePropertyTypes, CONCURRENCY, async (propertyType) => {
      const parsed = await fetchJllSearchPage(tx, propertyType, page);
      if (filterTotals[propertyType] === undefined) {
        filterTotals[propertyType] = parsed.total;
        maxByFilterPage[propertyType] =
          parsed.total === null ? null : Math.max(1, Math.ceil(parsed.total / 50));
      }
      console.error(
        `  jll/${tx}/${propertyType}: page ${page}, ${parsed.listings.length} cards (filter total ${parsed.total ?? "?"})`
      );
      return { propertyType, ...parsed };
    });

    let addedOrSeenOnPage = 0;
    for (let offset = 0; ; offset++) {
      let advanced = false;
      for (const result of pageResults) {
        const candidate = result.listings[offset];
        if (!candidate) continue;
        advanced = true;
        addedOrSeenOnPage++;
        const existing = byUrl.get(candidate.url);
        if (existing) {
          mergeJllListing(existing, candidate, result.propertyType, page);
          continue;
        }
        if (listings.length >= max) continue;
        byUrl.set(candidate.url, candidate);
        listings.push(candidate);
      }
      if (!advanced) break;
    }

    console.error(
      `  jll/${tx}: page ${page}, ${listings.length} unique collected across ${activePropertyTypes.length} property filters`
    );
    if (addedOrSeenOnPage === 0) break;
  }
  if (!listings.length) throw new Error("no listing cards found on JLL search page");
  const enriched = await pmap(listings, JLL_DETAIL_CONCURRENCY, enrichJllListing);

  const knownTotals = Object.values(filterTotals).filter((n): n is number => typeof n === "number");
  const total = knownTotals.length ? knownTotals.reduce((sum, n) => sum + n, 0) : null;
  const totalEvidence = JLL_PROPERTY_TYPES.map(
    (propertyType) => `${propertyType}=${filterTotals[propertyType] ?? "?"}`
  ).join(", ");
  return {
    company: "JLL",
    sourceUrl,
    method:
      "Rendered search pages parsed across public propertyTypes filters, then detail __NEXT_DATA__ enrichment with URL-only assets",
    totalAvailable: total,
    listings: enriched,
    note: `Per-filter source totals before cross-filter de-dupe: ${totalEvidence}. Detail enrichment stores public brochure/image/profile URLs only and retains per-row detailError if a detail scrape fails.`,
  };
}

// --- JLL Investor Center: rendered page (sale-only by nature) ---

const JLL_INVESTOR_HOST = "https://invest.jll.com";
const JLL_INVESTOR_SEARCH_URL =
  "https://invest.jll.com/us/en/property-search?filter=%7B%22location%22%3A%5B%22United%20States%22%5D%7D";
const JLL_INVESTOR_DETAIL_CONCURRENCY = Math.min(CONCURRENCY, 2);

function jllInvestorNextData(rawHtml: string): any | null {
  const $ = cheerio.load(rawHtml);
  const text = $("#__NEXT_DATA__").first().text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function jllInvestorUrlFromAlias(alias: string | null): string | null {
  const cleaned = clean(alias);
  if (!cleaned) return null;
  if (/^https?:\/\//i.test(cleaned)) return cleaned;
  const path = cleaned.startsWith("/us/en/listings/")
    ? cleaned
    : `/us/en/listings/${cleaned.replace(/^\/+/, "")}`;
  return `${JLL_INVESTOR_HOST}${path}`;
}

function jllInvestorStatus(row: any): string {
  if (row?.isUnderContract) return "Under Contract";
  const status = clean(row?.stageName ?? row?.status);
  return status ?? "Active";
}

function jllInvestorSearchListing(row: any): any {
  const url = jllInvestorUrlFromAlias(row?.alias);
  const id = clean(row?.id) ?? clean(row?.alias)?.split("/").slice(-1)[0] ?? null;
  return prune({
    id,
    name: clean(row?.name),
    transactionType: "Sale (investment)",
    assetType:
      clean(row?.assetType) ??
      clean(row?.rawAssetType) ??
      (Array.isArray(row?.assetTypesPrimaryList) ? row.assetTypesPrimaryList.map(clean).filter(Boolean).join(", ") : null),
    status: jllInvestorStatus(row),
    street: clean(row?.displayAddress),
    city: clean(row?.city),
    state: clean(row?.state),
    country: clean(row?.country) === "United States" ? "US" : clean(row?.country),
    latitude: num(row?.latitude),
    longitude: num(row?.longitude),
    sizeText: clean(row?.numberOfUnits),
    brokerIds: [],
    photos: clean(row?.image) ? [clean(row.image)] : [],
    url,
    jllInvestorSearchRow: row,
  });
}

function jllInvestorSearchFallback(rawHtml: string, max: number): any[] {
  const $ = cheerio.load(rawHtml);
  const seen = new Set<string>();
  const listings: any[] = [];
  $('a[href*="/us/en/listings/"]').each((_, el) => {
    if (listings.length >= max) return;
    const href = $(el).attr("href")!;
    const abs = href.startsWith("http") ? href : `${JLL_INVESTOR_HOST}${href}`;
    if (seen.has(abs)) return;
    seen.add(abs);
    const card = $(el).closest("li,article,div[class]");
    const txt = clean(card.text()) ?? "";
    const img = card.find("img").attr("src") ?? null;
    const slugParts = abs.split("/listings/")[1]?.split("/") ?? [];
    listings.push(
      prune({
        id: slugParts.slice(-1)[0] ?? null,
        name:
          clean(card.find("h3,h4").first().text()) ??
          clean(slugParts.slice(-1)[0]?.replace(/-/g, " ")) ??
          null,
        transactionType: "Sale (investment)",
        assetType: clean(slugParts.length > 1 ? slugParts[0]?.replace(/-/g, " ") : null),
        status: /under contract/i.test(txt)
          ? "Under Contract"
          : /closed/i.test(txt)
            ? "Closed"
            : "Active",
        brokerIds: [],
        photos: img ? [img] : [],
        url: abs,
      })
    );
  });
  return listings;
}

function jllInvestorDocumentUrls(listing: any): string[] {
  const docs = listing?.documents;
  const candidates: string[] = [];
  const visit = (value: any) => {
    if (!value) return;
    if (typeof value === "string") {
      if (/^https?:\/\//i.test(value)) candidates.push(value);
      return;
    }
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (typeof value === "object") {
      visit(value.url);
      for (const nested of Object.values(value)) visit(nested);
    }
  };
  visit(docs);
  return dedupeStrings(candidates);
}

function jllInvestorImageUrls(listing: any, fallback: string[] = []): string[] {
  const images = [
    clean(listing?.image),
    ...(Array.isArray(listing?.multimedia?.images) ? listing.multimedia.images.map(clean) : []),
    ...fallback,
  ];
  return dedupeStrings(images).filter((url) => /^https?:\/\//i.test(url));
}

function jllInvestorContacts(listing: any): any[] {
  if (!Array.isArray(listing?.brokers)) return [];
  const contacts = listing.brokers
    .map((broker: any) =>
      prune({
        name: clean(broker?.name),
        title: clean(broker?.title),
        email: clean(broker?.email),
        phone: clean(broker?.phone),
        company: "JLL",
        avatarUrl: clean(broker?.image),
        linkedInUrl: clean(broker?.linkedInURL),
        licensedEntity: broker?.licensedEntity,
        licenses: broker?.licenses,
      })
    )
    .filter(Boolean);
  const seen = new Set<string>();
  return contacts.filter((contact: any) => {
    const key = contact.email ?? contact.name ?? JSON.stringify(contact);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

async function enrichJllInvestorListing(base: any): Promise<any> {
  if (!base.url) return base;
  try {
    const doc = await scrapeDoc(base.url, { waitFor: 8000, timeout: 120000 });
    const next = jllInvestorNextData(doc.rawHtml);
    const listing = next?.props?.pageProps?.initialState?.pdp?.listing;
    if (!listing) {
      return prune({ ...base, detailError: "missing pdp listing in __NEXT_DATA__" });
    }
    const contactsDetailed = jllInvestorContacts(listing);
    const brokerIds = contactsDetailed
      .map((contact: any) =>
        brokerRef({
          name: clean(contact.name),
          email: clean(contact.email),
          phone: clean(contact.phone),
          office: clean(contact.office),
          avatarUrl: clean(contact.avatarUrl),
          company: "JLL",
        })
      )
      .filter((id: number | null): id is number => id !== null);
    const documentUrls = jllInvestorDocumentUrls(listing);
    return prune({
      ...base,
      id: clean(listing.id) ?? base.id,
      name: clean(listing.name) ?? base.name,
      assetType:
        clean(listing.assetType) ??
        clean(listing.rawAssetType) ??
        (Array.isArray(listing.assetTypesPrimaryList)
          ? listing.assetTypesPrimaryList.map(clean).filter(Boolean).join(", ")
          : base.assetType),
      description: clean(listing.description) ?? base.description,
      street: clean(listing.fullLocation) ?? base.street,
      city: clean(listing.city) ?? base.city,
      state: clean(listing.state) ?? base.state,
      country: clean(listing.country) === "United States" ? "US" : clean(listing.country) ?? base.country,
      latitude: num(listing.latitude) ?? base.latitude,
      longitude: num(listing.longitude) ?? base.longitude,
      status: jllInvestorStatus(listing),
      sizeText: clean(listing.numberOfUnits ? `${listing.numberOfUnits} units` : null) ?? base.sizeText,
      brokerIds,
      contactsDetailed,
      brochures: documentUrls.map((url) => ({ name: titleFromFilename(url), url })),
      photos: jllInvestorImageUrls(listing, base.photos ?? []),
      lastUpdated: clean(listing.dateModified ?? listing.datePublished),
      jllInvestorDetail: {
        id: clean(listing.id),
        alias: clean(listing.alias),
        dealType: clean(listing.dealType),
        stageName: clean(listing.stageName),
        isUnderContract: Boolean(listing.isUnderContract),
        highlights: listing.highlights,
        customAttributes: listing.customAttributes,
        documentsCA: listing.documentsCA,
        rawPriceRange: listing.priceRange,
        datePublished: clean(listing.datePublished),
        dateModified: clean(listing.dateModified),
        scrape: {
          markdownLength: doc.markdown.length,
          rawHtmlLength: doc.rawHtml.length,
          linkCount: doc.links.length,
        },
      },
    });
  } catch (err) {
    console.error(`  jll-investor: detail failed for ${base.url}: ${err}`);
    return prune({ ...base, detailError: String(err) });
  }
}

async function srcJllInvestor(tx: Tx, max: number): Promise<SourceResult> {
  if (tx === "lease") {
    return {
      company: "JLL Investor Center",
      sourceUrl: JLL_INVESTOR_HOST,
      method: "skipped",
      totalAvailable: 0,
      listings: [],
      note: "Investment-sale platform; no lease inventory.",
    };
  }
  const html = await scrapeRaw(JLL_INVESTOR_SEARCH_URL, { waitFor: 8000, timeout: 120000 });
  const next = jllInvestorNextData(html);
  const search = next?.props?.pageProps?.initialState?.advancedSearch;
  const rows = Array.isArray(search?.listings) ? search.listings : [];
  const total = typeof search?.count === "number" ? search.count : null;
  const baseListings = rows.length
    ? rows.slice(0, Math.min(max, rows.length)).map(jllInvestorSearchListing)
    : jllInvestorSearchFallback(html, max);
  const listings = await pmap(baseListings, JLL_INVESTOR_DETAIL_CONCURRENCY, enrichJllInvestorListing);
  if (!listings.length) throw new Error("no listing cards found on JLL Investor Center search page");
  return {
    company: "JLL Investor Center",
    sourceUrl: JLL_INVESTOR_SEARCH_URL,
    method: "Rendered search page __NEXT_DATA__ plus public detail-page enrichment",
    totalAvailable: total,
    listings,
    note:
      "Still bounded to the first rendered search page. Detail enrichment stores public teaser document URLs, image URLs, and broker contact fields only; CA/NDA document URLs remain in raw detail metadata.",
  };
}

// --- Cushman & Wakefield: public JSON search API plus detail enrichment ---

const CUSHMAN_HOST = "https://www.cushmanwakefield.com";
const CUSHMAN_API_BASE = `${CUSHMAN_HOST}/api/properties/search`;
const CUSHMAN_PAGE_SIZE = 100;
const CUSHMAN_QUERY = clean(process.env.CUSHMAN_QUERY ?? null);
const CUSHMAN_API_CONCURRENCY = boundedInt(process.env.CUSHMAN_API_CONCURRENCY, 1, 1, CONCURRENCY);
const CUSHMAN_DETAIL_CONCURRENCY = boundedInt(
  process.env.CUSHMAN_DETAIL_CONCURRENCY,
  CONCURRENCY,
  1,
  CONCURRENCY
);

function decodeHtmlEntities(s: string): string {
  return s
    .replace(/\\u0026/g, "&")
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .replace(/&#34;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">");
}

function titleFromFilename(url: string): string {
  try {
    const u = new URL(url);
    const last = decodeURIComponent(u.pathname.split("/").filter(Boolean).slice(-1)[0] ?? "");
    return (
      clean(
        last
          .replace(/\.[a-z0-9]+$/i, "")
          .replace(/[-_]+/g, " ")
          .replace(/\s+/g, " ")
      ) ?? "Document"
    );
  } catch {
    return "Document";
  }
}

function canonicalCushmanUrl(url: string | null): string | null {
  if (!url) return null;
  const decoded = decodeHtmlEntities(url).trim();
  if (!decoded || /^javascript:/i.test(decoded)) return null;
  const abs = decoded.startsWith("http")
    ? decoded
    : decoded.startsWith("/")
      ? `${CUSHMAN_HOST}${decoded}`
      : `${CUSHMAN_HOST}/${decoded}`;
  try {
    const u = new URL(abs);
    if (u.hostname === "sitecore-www.cushmanwakefield.com") u.hostname = "www.cushmanwakefield.com";
    return u.toString();
  } catch {
    return abs;
  }
}

function canonicalCushmanAssetUrl(url: string): string | null {
  const decoded = decodeHtmlEntities(url)
    .replace(/[)"'\]>]+$/g, "")
    .replace(/,$/, "");
  if (!/^https?:\/\/assets\.cushmanwakefield\.com\//i.test(decoded)) return null;
  try {
    const u = new URL(decoded);
    u.searchParams.delete("sc");
    u.searchParams.delete("hash");
    return u.toString();
  } catch {
    return null;
  }
}

function dedupeAssetsBestWidth(urls: string[]): string[] {
  const best = new Map<string, string>();
  const score = (url: string) => {
    try {
      const u = new URL(url);
      const width = Number(u.searchParams.get("w") ?? "0");
      return Number.isFinite(width) ? width : 0;
    } catch {
      return 0;
    }
  };
  for (const url of urls) {
    try {
      const u = new URL(url);
      const key = `${u.origin}${u.pathname}?rev=${u.searchParams.get("rev") ?? ""}`;
      const prev = best.get(key);
      if (!prev || score(url) > score(prev)) best.set(key, url);
    } catch {
      if (!best.has(url)) best.set(url, url);
    }
  }
  return [...best.values()];
}

function extractCushmanAssetUrls(doc: ScrapedDoc): string[] {
  const text = [doc.rawHtml, doc.markdown, ...(doc.links ?? [])].join("\n");
  const candidates = text.match(/https?:\/\/assets\.cushmanwakefield\.com\/[^"'<>\s\])]+/gi) ?? [];
  return dedupeAssetsBestWidth(
    candidates
      .map((u) => canonicalCushmanAssetUrl(u))
      .filter((u: string | null): u is string => Boolean(u))
  );
}

function pmediaId(url: string): string | null {
  return url.match(/\/pmedia\/([^/]+)\//i)?.[1] ?? null;
}

function extractCushmanDocuments(assetUrls: string[]): any[] {
  const seen = new Set<string>();
  const docs: any[] = [];
  for (const url of assetUrls) {
    if (!/\.pdf(?:\?|$)/i.test(url) || seen.has(url)) continue;
    seen.add(url);
    docs.push({ name: titleFromFilename(url), url });
  }
  return docs;
}

function extractCushmanPhotos(assetUrls: string[]): string[] {
  const pdfIds = new Set(assetUrls.filter((u) => /\.pdf(?:\?|$)/i.test(u)).map(pmediaId).filter(Boolean));
  const imageUrls = assetUrls.filter(
    (u) =>
      /\/pmedia\//i.test(u) &&
      /\.(?:webp|png|jpe?g)(?:\?|$)/i.test(u) &&
      !/\/people\//i.test(u)
  );
  const firstImageId = imageUrls.map(pmediaId).find(Boolean) ?? null;
  const targetIds = pdfIds.size ? pdfIds : firstImageId ? new Set([firstImageId]) : new Set<string>();
  const selected = imageUrls.filter((u) => {
    const id = pmediaId(u);
    return id ? targetIds.has(id) : false;
  });
  if (selected.length || !firstImageId) return selected;
  return imageUrls.filter((u) => pmediaId(u) === firstImageId);
}

function jsonLdObjects(rawHtml: string): any[] {
  const $ = cheerio.load(rawHtml);
  const out: any[] = [];
  const visit = (value: any) => {
    if (!value) return;
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (typeof value === "object") {
      out.push(value);
      if (value["@graph"]) visit(value["@graph"]);
    }
  };
  $('script[type="application/ld+json"]').each((_, el) => {
    const txt = clean($(el).text());
    if (!txt) return;
    try {
      visit(JSON.parse(txt));
    } catch {
      /* ignore malformed embedded JSON-LD */
    }
  });
  return out;
}

function firstJsonLd(rawHtml: string, type: string): any | null {
  const wanted = type.toLowerCase();
  return (
    jsonLdObjects(rawHtml).find((obj) => {
      const t = obj["@type"];
      return Array.isArray(t)
        ? t.map((x: any) => String(x).toLowerCase()).includes(wanted)
        : String(t ?? "").toLowerCase() === wanted;
    }) ?? null
  );
}

function markdownLabel(markdown: string, label: string): string | null {
  const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const m = markdown.match(new RegExp(`${escaped}:?\\s*\\n+\\s*([^\\n]+)`, "i"));
  return clean(m?.[1] ?? null);
}

function firstNumberText(text: string | null): number | null {
  if (!text) return null;
  const m = text.replace(/,/g, "").match(/([0-9]+(?:\.[0-9]+)?)/);
  return m ? Number(m[1]) : null;
}

function sqftFromText(text: string | null): number | null {
  if (!text) return null;
  const m = text.replace(/,/g, "").match(/([0-9]+(?:\.[0-9]+)?)\s*(?:SF|sq\.?\s*ft\.?)/i);
  return m ? Number(m[1]) : null;
}

function acresFromText(text: string | null): number | null {
  if (!text) return null;
  const acres = text.replace(/,/g, "").match(/([0-9]*\.?[0-9]+)\s*Acres?/i);
  if (acres) return Number(acres[1]);
  const sqft = sqftFromText(text);
  return sqft ? sqft / 43560 : null;
}

function extractCushmanContacts(doc: ScrapedDoc): any[] {
  const $ = cheerio.load(doc.rawHtml);
  const contactsByKey = new Map<string, any>();
  const listing = firstJsonLd(doc.rawHtml, "RealEstateListing");
  const offeredBy = Array.isArray(listing?.offeredBy) ? listing.offeredBy : listing?.offeredBy ? [listing.offeredBy] : [];

  for (const person of offeredBy) {
    if (!person || String(person["@type"] ?? "").toLowerCase() !== "person") continue;
    const profileUrl = canonicalCushmanUrl(clean(person.url));
    const key = profileUrl ?? clean(person.name) ?? JSON.stringify(person);
    contactsByKey.set(key, {
      name: clean(person.name),
      title: clean(person.jobTitle),
      phone: clean(person.telephone),
      profileUrl,
      company: "Cushman & Wakefield",
    });
  }

  $('a[href*="/people/"], a[href*="/api/GetVCard"]').each((_, el) => {
    const href = canonicalCushmanUrl($(el).attr("href") ?? null);
    if (!href) return;
    const block = $(el).closest("li, article, section, div").first();
    const profileHref =
      href.includes("/people/") ? href : canonicalCushmanUrl(block.find('a[href*="/people/"]').first().attr("href") ?? null);
    const key = profileHref ?? href;
    const text = clean(block.text()) ?? "";
    const existing = contactsByKey.get(key) ?? { company: "Cushman & Wakefield" };
    const phone =
      clean(block.find('a[href^="tel:"]').first().text()) ??
      clean(text.match(/(\+?1?\s*\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4})/)?.[1] ?? null);
    const name =
      existing.name ??
      clean(block.find('a[href*="/people/"]').last().text()) ??
      clean($(el).text());
    const avatar = canonicalCushmanAssetUrl(block.find("img").first().attr("src") ?? "");
    contactsByKey.set(key, {
      ...existing,
      name,
      phone: existing.phone ?? phone,
      profileUrl: existing.profileUrl ?? profileHref,
      avatarUrl: existing.avatarUrl ?? avatar,
      vcardUrl: existing.vcardUrl ?? (href.includes("/api/GetVCard") ? href : null),
    });
  });

  return [...contactsByKey.values()].filter((c) => c.name || c.phone || c.profileUrl || c.vcardUrl);
}

function cushmanSearchApiUrl(tx: Tx, offset: number): string {
  const listingType = tx === "sale" ? "Buy" : "Lease";
  const params = new URLSearchParams({
    rfkId: "property_search",
    view: "pins",
    site_country: "US",
    listing_type: listingType,
    language: "en",
    limit: String(CUSHMAN_PAGE_SIZE),
    offset: String(offset),
  });
  if (CUSHMAN_QUERY) params.set("q", CUSHMAN_QUERY);
  return `${CUSHMAN_API_BASE}?${params.toString()}`;
}

function baseCushmanListing(row: any, tx: Tx): any {
  const url = canonicalCushmanUrl(row.url ?? row.relative_url);
  const street = clean(row.property_street);
  const city = clean(row.property_city);
  const state = clean(row.state_or_province)?.toUpperCase() ?? null;
  const zip = clean(row.property_postal_code);
  return {
    id: clean(row.id) ?? clean(row.url) ?? clean(row.relative_url),
    name: clean(row.nav_title) ?? street,
    headline: clean(row.attribute1),
    transactionType: tx === "sale" ? "Sale" : "Lease",
    assetType: clean(row.property_type),
    street,
    city,
    state,
    postalCode: zip,
    country: clean(row.property_country) ?? "US",
    latitude: row.property_latitude != null ? Number(row.property_latitude) : null,
    longitude: row.property_longitude != null ? Number(row.property_longitude) : null,
    salePriceText: tx === "sale" ? "Contact broker for pricing" : null,
    brokerIds: [],
    photos: clean(row.image_url) ? [canonicalCushmanAssetUrl(row.image_url) ?? clean(row.image_url)] : [],
    url,
    listingStatus: clean(row.listing_status),
    rawCushmanApi: row,
  };
}

async function enrichCushmanListing(row: any, tx: Tx): Promise<any> {
  const base = baseCushmanListing(row, tx);
  if (!base.url) return base;
  try {
    const doc = await scrapeDoc(base.url, { waitFor: 1000, timeout: 60000 });
    const listingLd = firstJsonLd(doc.rawHtml, "RealEstateListing");
    const assetUrls = extractCushmanAssetUrls(doc);
    const documents = extractCushmanDocuments(assetUrls);
    const photos = extractCushmanPhotos(assetUrls);
    const contacts = extractCushmanContacts(doc);
    const brokerIds = contacts
      .map((c) =>
        brokerRef({
          name: clean(c.name),
          phone: clean(c.phone),
          avatarUrl: clean(c.avatarUrl),
          office: clean(c.office),
          company: "Cushman & Wakefield",
        })
      )
      .filter((v: number | null): v is number => v !== null);
    const buildingSizeText =
      markdownLabel(doc.markdown, "Building Size") ?? markdownLabel(doc.markdown, "Available Space");
    const lotSizeText = markdownLabel(doc.markdown, "Lot Size");
    const salePriceText = markdownLabel(doc.markdown, "Sale Price");
    const leaseRateText =
      markdownLabel(doc.markdown, "Rental Price") ?? markdownLabel(doc.markdown, "Lease Rate");
    const yearText =
      markdownLabel(doc.markdown, "Year Built/Renovated") ??
      markdownLabel(doc.markdown, "Built") ??
      markdownLabel(doc.markdown, "Year Built");
    return prune({
      ...base,
      name: clean(listingLd?.name) ?? base.name,
      description: clean(listingLd?.description) ?? clean(doc.markdown.match(/Overview\s*-+\s*([\s\S]{1,2500}?)(?:\n[A-Z][A-Za-z ]+\n-+|\n#{1,6}\s|\nCONTACT|\nLOCATION|$)/i)?.[1]),
      salePriceUsd: tx === "sale" ? moneyToNumber(salePriceText) : null,
      salePriceText: tx === "sale" ? salePriceText ?? base.salePriceText : null,
      leaseRateText: tx === "lease" ? leaseRateText : null,
      sizeText: buildingSizeText ?? lotSizeText,
      buildingSizeSqft: sqftFromText(buildingSizeText),
      lotSizeAcres: acresFromText(lotSizeText),
      yearBuilt: firstNumberText(yearText),
      brokerIds,
      brochures: documents,
      photos: photos.length ? photos : base.photos,
      lastUpdated: clean(listingLd?.datePosted)?.slice(0, 10) ?? null,
      contactsDetailed: contacts,
      documentCount: documents.length,
      photoCount: photos.length || base.photos.length,
      detailScrape: {
        url: base.url,
        markdownLength: doc.markdown.length,
        rawHtmlLength: doc.rawHtml.length,
        linkCount: doc.links.length,
        assetCount: assetUrls.length,
      },
    });
  } catch (err) {
    console.error(`  cushman-wakefield/${tx}: detail failed for ${base.url}: ${err}`);
    return prune({
      ...base,
      detailError: String(err),
    });
  }
}

async function srcCushman(tx: Tx, max: number): Promise<SourceResult> {
  const sourceUrl =
    tx === "sale"
      ? "https://www.cushmanwakefield.com/en/united-states/properties/invest/search"
      : "https://www.cushmanwakefield.com/en/united-states/properties/lease/search";
  const apiOpts: ScrapeOpts = { timeout: 90000, jsonAttempts: 8, jsonBackoffMs: 12000 };
  const first = await scrapeJson(cushmanSearchApiUrl(tx, 0), apiOpts);
  const content = Array.isArray(first.content) ? first.content : [];
  const total: number = Number(first.total_item ?? content.length);
  if (!content.length) throw new Error(`Cushman & Wakefield API returned no ${tx} content`);
  const want = Math.min(max, total);
  const pages = Math.ceil(want / CUSHMAN_PAGE_SIZE);
  console.error(`  cushman-wakefield/${tx}: ${total} total, fetching ${pages} API page(s)`);
  const chunks: any[][] = [content];
  if (pages > 1) {
    const offsets = Array.from({ length: pages - 1 }, (_, i) => (i + 1) * CUSHMAN_PAGE_SIZE);
    const rest = await pmap(offsets, CUSHMAN_API_CONCURRENCY, async (offset) => {
      const d = await scrapeJson(cushmanSearchApiUrl(tx, offset), apiOpts);
      const rows = Array.isArray(d.content) ? d.content : [];
      console.error(`  cushman-wakefield/${tx}: API offset ${offset}, ${rows.length} rows`);
      return rows;
    });
    chunks.push(...rest);
  }
  const rows = chunks.flat().slice(0, want);
  let done = 0;
  const listings = await pmap(rows, CUSHMAN_DETAIL_CONCURRENCY, async (row) => {
    const enriched = await enrichCushmanListing(row, tx);
    done++;
    if (done % 25 === 0 || done === rows.length) {
      console.error(`  cushman-wakefield/${tx}: detail enriched ${done}/${rows.length}`);
    }
    return enriched;
  });
  return {
    company: "Cushman & Wakefield",
    sourceUrl,
    method: "Cushman public /api/properties/search JSON pagination plus detail-page raw HTML enrichment",
    totalAvailable: total,
    listings,
  };
}

// --- Marcus & Millichap: public contentsearch API + public detail pages (sale-only platform) ---

const MARCUS_BASE = "https://www.marcusmillichap.com";
const MARCUS_PROPERTIES_URL = `${MARCUS_BASE}/properties`;

function marcusHeaders(): Record<string, string> {
  return {
    accept: "application/json, text/javascript, */*; q=0.01",
    "content-type": "application/json",
    origin: MARCUS_BASE,
    referer: MARCUS_PROPERTIES_URL,
    "user-agent": "Mozilla/5.0 CRE collector",
  };
}

function marcusSearchBody(pageSize: number): Record<string, any> {
  return {
    pageNumber: 1,
    pageSize,
    sortOrder: "DESC",
    indexFieldName: "orderdate",
    facets: [],
    rangeFacets: [],
    geoFacet: { Polygons: [], Circles: [], FieldName: "customdraw" },
    savedSearchId: null,
    allowedFacets: ["propertytype", "location", "advisors", "listingprice", "caprate"],
  };
}

function marcusMapDetailBody(activityId: string): Record<string, any> {
  return { activityId };
}

async function marcusPost(path: string, body: Record<string, any>): Promise<any> {
  const res = await fetch(`${MARCUS_BASE}${path}`, {
    method: "POST",
    headers: marcusHeaders(),
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Marcus & Millichap ${path} HTTP ${res.status}`);
  return res.json();
}

function marcusUrl(href: string | null | undefined): string | null {
  const h = clean(href ?? null);
  if (!h) return null;
  try {
    return new URL(h, MARCUS_BASE).toString();
  } catch {
    return null;
  }
}

function extractCssUrl(style: string | null | undefined): string | null {
  const match = (style ?? "").match(/url\((['"]?)(.*?)\1\)/i);
  return match ? match[2] : null;
}

function parseMarcusLocation(location: string | null): {
  city: string | null;
  state: string | null;
  postalCode: string | null;
} {
  const m = (location ?? "").match(/^(.*?),\s*([A-Z]{2})(?:\s+(\d{5}(?:-\d{4})?))?$/);
  return {
    city: m ? clean(m[1]) : location,
    state: m ? m[2] : null,
    postalCode: m?.[3] ?? null,
  };
}

function parseMarcusAddress(address: string | null): {
  street: string | null;
  city: string | null;
  state: string | null;
  postalCode: string | null;
} {
  const m = (address ?? "").match(/^(.*?),\s*([^,]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$/);
  return {
    street: m ? clean(m[1]) : null,
    city: m ? clean(m[2]) : null,
    state: m ? m[3] : null,
    postalCode: m ? m[4] : null,
  };
}

function parseMarcusTileHtml(tileHtml: string | null | undefined, row: any = {}): any {
  const $ = cheerio.load(tileHtml ?? "");
  const tile = $(".mm-tile").first();
  const href = tile.find('a[href^="/properties/"], a[href*="marcusmillichap.com/properties/"]').first().attr("href");
  const location = parseMarcusLocation(clean(tile.find(".mm-location").first().text()));
  const priceText =
    clean(
      tile
        .find(".mm-listing-price, .starting-bid")
        .first()
        .text()
    )?.replace(/^Listing Price:\s*/i, "") ??
    clean(row.ListingPrice) ??
    null;
  const capRateText = clean(tile.find(".mm-cap-rate").first().text()) ?? clean(String(row.CapRate ?? ""));
  const capRate = (capRateText ?? "").match(/([0-9.]+)%?/)?.[1];
  const img = marcusUrl(tile.find('img[src*="mmimageservice"]').first().attr("src"));
  const rowUrl = marcusUrl(row.PropertyUrl);
  return prune({
    id: clean(String(row.DealId ?? tile.attr("data-dealid") ?? "")),
    activityId: clean(row.ActivityId ?? tile.attr("data-activityid")),
    propertyId: clean(String(row.PropertyId ?? "")),
    name: clean(row.PropertyName) ?? clean(tile.find("h2").first().text()),
    transactionType: "Sale",
    assetType: clean(row.PropertyType) ?? clean(tile.find("h3").first().text()),
    city: clean(row.City) ?? location.city,
    state: clean(row.StateProvince) ?? location.state,
    postalCode: clean(row.PostalCode) ?? location.postalCode,
    country: "US",
    latitude: num(Number(row.Latitude)),
    longitude: num(Number(row.Longitude)),
    salePriceUsd: moneyToNumber(priceText),
    salePriceText: priceText,
    capRatePct: capRate ? Number(capRate) : null,
    brokerIds: [],
    photos: img ? [img] : [],
    url: rowUrl ?? marcusUrl(href),
    marcusFlags: {
      newlyListed: Boolean(row.NewlyListed),
      newlyReduced: Boolean(row.NewlyReduced),
    },
    rawMarcusSearchRow: row,
  });
}

async function fetchMarcusMapRows(): Promise<any[]> {
  const map = await marcusPost("/api/contentsearch/mapproperties", marcusSearchBody(1));
  const results = map.Results ?? map;
  const rows = Array.isArray(results.Properties) ? results.Properties : Array.isArray(results) ? results : [];
  const seen = new Set<string>();
  return rows.filter((row: any) => {
    const activityId = clean(row.ActivityId);
    if (!activityId || seen.has(activityId)) return false;
    seen.add(activityId);
    return true;
  });
}

async function fetchMarcusMapListing(mapRow: any): Promise<any | null> {
  const activityId = clean(mapRow.ActivityId);
  if (!activityId) return null;
  try {
    const detail = await marcusPost("/api/contentsearch/mappropertydetail", marcusMapDetailBody(activityId));
    const results = detail.Results ?? detail;
    return parseMarcusTileHtml(results.PropertyDetail, {
      ...mapRow,
      ActivityId: activityId,
      PropertyUrl: results.PropertyUrl,
      rawMarcusMapDetail: results,
    });
  } catch (err) {
    console.error(`  marcus-millichap/sale: map detail failed for ${activityId}: ${err}`);
    return prune({
      activityId,
      latitude: num(Number(mapRow.Latitude)),
      longitude: num(Number(mapRow.Longitude)),
      country: "US",
      transactionType: "Sale",
      marcusFlags: {
        newlyListed: Boolean(mapRow.NewlyListed),
        newlyReduced: Boolean(mapRow.NewlyReduced),
      },
      detailError: String(err),
      rawMarcusSearchRow: mapRow,
    });
  }
}

async function fetchMarcusDetailHtml(url: string): Promise<string> {
  const res = await fetch(url, {
    headers: {
      accept: "text/html,*/*",
      referer: MARCUS_PROPERTIES_URL,
      "user-agent": "Mozilla/5.0 CRE collector",
    },
  });
  if (!res.ok) throw new Error(`Marcus & Millichap detail HTTP ${res.status}`);
  return res.text();
}

function extractMarcusDetailImages($: cheerio.CheerioAPI, seed: string[]): string[] {
  const urls: Array<string | null> = [...seed];
  $('img[src*="mmimageservice.azurewebsites.net/api/image/property"]').each((_, el) => {
    urls.push(marcusUrl($(el).attr("src")));
  });
  $('[style*="mmimageservice.azurewebsites.net/api/image/property"]').each((_, el) => {
    urls.push(marcusUrl(extractCssUrl($(el).attr("style"))));
  });
  return dedupeStrings(urls);
}

function extractMarcusContacts($: cheerio.CheerioAPI): any[] {
  const contactsByKey = new Map<string, any>();
  $('li .mm-tile, .mm-advisor .mm-tile, .mm-advisor-card .mm-tile')
    .has('a[href^="/advisors/"]')
    .each((_, el) => {
      const tile = $(el);
      const profileUrl = marcusUrl(tile.find('a[href^="/advisors/"]').first().attr("href"));
      const email = clean(tile.find('a[href^="mailto:"]').first().attr("href")?.replace(/^mailto:/i, "").split("?")[0]);
      const phone = clean(
        tile.find('a[href^="tel:"]').first().text() ??
          tile.find('a[href^="tel:"]').first().attr("href")?.replace(/^tel:/i, "")
      );
      const avatarUrl = marcusUrl(extractCssUrl(tile.find(".mm-image-wrapper").first().attr("style")));
      const name = clean(tile.find("h3").first().text());
      const key = email ?? profileUrl ?? name;
      if (!key) return;
      contactsByKey.set(key, {
        name,
        title: clean(tile.find(".ipa-subtitle").first().text()),
        email,
        phone,
        company: "Marcus & Millichap",
        profileUrl,
        avatarUrl,
        license: clean(tile.find(".ipa-license").first().text()),
        office: clean(tile.find(".ipa-location").first().text()),
      });
    });
  return [...contactsByKey.values()].filter((c) => c.name || c.email || c.phone || c.profileUrl);
}

function parseMarcusSpecifications($: cheerio.CheerioAPI): Record<string, string> {
  const specs: Record<string, string> = {};
  $(".specification-outer").each((_, el) => {
    const key = clean($(el).find(".specification-name").first().text());
    const value = clean($(el).find(".specification-value").first().text());
    if (key && value && specs[key] === undefined) specs[key] = value;
  });
  return specs;
}

function marcusListingCacheKey(listing: any): string | null {
  return clean(String(listing?.id ?? listing?.activityId ?? listing?.url ?? ""));
}

function marcusDetailCachePath(): string {
  return OUT_PATH ? `${OUT_PATH}.marcus-detail-cache.jsonl` : "out/marcus-millichap-detail-cache.jsonl";
}

function readMarcusDetailCache(path: string): Map<string, any> {
  const cached = new Map<string, any>();
  if (!existsSync(path)) return cached;
  for (const line of readFileSync(path, "utf8").split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const listing = JSON.parse(line);
      if (listing?.detailError) continue;
      const key = marcusListingCacheKey(listing);
      if (key) cached.set(key, listing);
    } catch {
      // Ignore a partial final line if a prior run was interrupted mid-write.
    }
  }
  return cached;
}

function appendMarcusDetailCache(path: string, listing: any): void {
  if (listing?.detailError) return;
  mkdirSync(dirname(path), { recursive: true });
  appendFileSync(path, `${JSON.stringify(listing)}\n`);
}

async function enrichMarcusListing(base: any): Promise<any> {
  if (!base.url) return base;
  try {
    const html = await fetchMarcusDetailHtml(base.url);
    const $ = cheerio.load(html);
    const address = parseMarcusAddress(clean($(".score-hero-body p").first().text()));
    const specs = parseMarcusSpecifications($);
    const priceText =
      clean($(".mm-property-price").first().text())?.replace(/^Listing Price:\s*/i, "") ??
      base.salePriceText ??
      null;
    const capRate = specs["Cap Rate"]?.match(/([0-9.]+)%/)?.[1];
    const yearBuilt = specs["Year Built"]?.match(/\b(18|19|20)\d{2}\b/)?.[0];
    const dealRoomUrl = marcusUrl($(".mm-property-documents-button a[href]").first().attr("href"));
    return prune({
      ...base,
      name: clean($("h1").first().text()) ?? base.name,
      street: address.street ?? base.street,
      city: address.city ?? base.city,
      state: address.state ?? base.state,
      postalCode: address.postalCode ?? base.postalCode,
      salePriceUsd: moneyToNumber(priceText) ?? base.salePriceUsd,
      salePriceText: priceText,
      capRatePct: capRate ? Number(capRate) : base.capRatePct,
      yearBuilt: yearBuilt ? Number(yearBuilt) : null,
      description:
        clean($(".mm-property-investment-overview p").first().text()) ??
        clean($('meta[name="description"]').attr("content")) ??
        base.description,
      contactsDetailed: extractMarcusContacts($),
      photos: extractMarcusDetailImages($, base.photos ?? []),
      marcusSpecifications: specs,
      gatedDocuments: dealRoomUrl
        ? [
            {
              name: clean($(".mm-property-documents-button a[href]").first().text()) ?? "Offering Memorandum & Deal Room",
              url: dealRoomUrl,
              gated: true,
            },
          ]
        : [],
      detailScrape: {
        url: base.url,
        rawHtmlLength: html.length,
      },
    });
  } catch (err) {
    console.error(`  marcus-millichap/sale: detail failed for ${base.url}: ${err}`);
    return prune({ ...base, detailError: String(err) });
  }
}

async function srcMarcusMillichap(tx: Tx, max: number): Promise<SourceResult> {
  if (tx === "lease") {
    return {
      company: "Marcus & Millichap",
      sourceUrl: MARCUS_PROPERTIES_URL,
      method: "skipped",
      totalAvailable: 0,
      listings: [],
      note:
        "Sale-only in the public property UI. The documented public bundle exposes property and auction endpoints, but no public lease search mode or lease endpoint was found.",
    };
  }
  const search = await marcusPost("/api/contentsearch/properties", marcusSearchBody(2));
  const results = search.Results ?? search;
  const rows = Array.isArray(results.Properties) ? results.Properties : [];
  const total = typeof results.TotalCount === "number" ? results.TotalCount : null;
  if (!rows.length) throw new Error("Marcus & Millichap public properties API sanity check returned no rows");
  console.error(
    `  marcus-millichap/sale: public properties API sanity check returned ${rows.length} row(s), total ${
      total ?? "?"
    }`
  );
  const mapRows = await fetchMarcusMapRows();
  if (!mapRows.length) throw new Error("Marcus & Millichap public mapproperties API returned no rows");
  const want = Math.min(max, mapRows.length);
  const selectedMapRows = mapRows.slice(0, Number.isFinite(want) ? want : mapRows.length);
  console.error(
    `  marcus-millichap/sale: public map API returned ${mapRows.length} ActivityId row(s), expanding ${selectedMapRows.length}`
  );
  const baseRows = await pmap(selectedMapRows, CONCURRENCY, fetchMarcusMapListing);
  const baseListings = baseRows.filter((l: any) => l?.url);
  const cachePath = marcusDetailCachePath();
  const cachedDetails = readMarcusDetailCache(cachePath);
  if (cachedDetails.size) {
    console.error(`  marcus-millichap/sale: loaded ${cachedDetails.size} cached detail row(s) from ${cachePath}`);
  }
  let done = 0;
  const listings = await pmap(baseListings, CONCURRENCY, async (row) => {
    const key = marcusListingCacheKey(row);
    const cached = key ? cachedDetails.get(key) : null;
    const enriched = cached ?? (await enrichMarcusListing(row));
    if (!cached) appendMarcusDetailCache(cachePath, enriched);
    done++;
    if (done % 10 === 0 || done === baseListings.length) {
      console.error(`  marcus-millichap/sale: detail enriched ${done}/${baseListings.length}`);
    }
    return enriched;
  });
  return {
    company: "Marcus & Millichap",
    sourceUrl: MARCUS_PROPERTIES_URL,
    method:
      "Public POST /api/contentsearch/mapproperties ActivityIds, mappropertydetail tiles, and direct public detail HTML enrichment",
    totalAvailable: total,
    listings,
    note:
      "Public sale inventory only. The list endpoint still caps unfiltered visible rows at the newest 100, so discovery uses public map ActivityIds plus mappropertydetail tiles. Lease remains skipped because no public lease UI mode or endpoint has been proven.",
  };
}

// --- Avison Young: SharpLaunch search app ---

const AVISON_YOUNG_PAGE_URL =
  "https://www.avisonyoung.us/properties/#/?transaction=sale&view=sidebar&status=active";
const AVISON_YOUNG_API_BASE = "https://pse-api.sharplaunch.com/data";
const AVISON_YOUNG_FALLBACK_API_KEY = "b9fda00f3d4d7f623665270841e32176";
const AVISON_YOUNG_CDN_BASE = "https://cdn.sharplaunch.com";
const AVISON_YOUNG_HOST = "https://www.avisonyoung.us";
const AVISON_YOUNG_DETAIL_CONCURRENCY = boundedInt(
  process.env.AVISON_YOUNG_DETAIL_CONCURRENCY,
  CONCURRENCY,
  1,
  CONCURRENCY
);

let avisonYoungCache:
  | {
      apiKey: string;
      websiteRows: any[];
      teamMembers: Map<string, any>;
    }
  | null = null;

async function fetchAvisonYoungApiKey(): Promise<string> {
  try {
    const res = await fetch(AVISON_YOUNG_PAGE_URL, {
      headers: { "User-Agent": "Mozilla/5.0 CRE collector" },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const html = await res.text();
    const key = html.match(/SharpLaunch\.PSE\.create\(\s*['"]([a-f0-9]{32})['"]/i)?.[1];
    if (key) return key;
    console.error("  avison-young: SharpLaunch key not found on page; using documented fallback");
  } catch (err) {
    console.error(`  avison-young: failed to fetch page key (${err}); using documented fallback`);
  }
  return AVISON_YOUNG_FALLBACK_API_KEY;
}

async function fetchAvisonYoungEntity(entity: string, apiKey: string): Promise<any[]> {
  const url = new URL(AVISON_YOUNG_API_BASE);
  url.searchParams.set("entity", entity);
  if (entity === "website") url.searchParams.set("status", "active");
  const res = await fetch(url, { headers: { "X-Api-Key": apiKey } });
  if (!res.ok) throw new Error(`Avison Young SharpLaunch ${entity} API HTTP ${res.status}`);
  const data = await res.json();
  const items = Array.isArray((data as any).items) ? (data as any).items : [];
  if (!items.length) throw new Error(`Avison Young SharpLaunch ${entity} API returned no items`);
  return items;
}

async function getAvisonYoungFeed(): Promise<{
  apiKey: string;
  websiteRows: any[];
  teamMembers: Map<string, any>;
}> {
  if (avisonYoungCache) return avisonYoungCache;
  const apiKey = await fetchAvisonYoungApiKey();
  const [websiteRows, teamRows] = await Promise.all([
    fetchAvisonYoungEntity("website", apiKey),
    fetchAvisonYoungEntity("team_member", apiKey),
  ]);
  const teamMembers = new Map<string, any>();
  for (const member of teamRows) {
    if (member?.id != null) teamMembers.set(String(member.id), member);
  }
  avisonYoungCache = { apiKey, websiteRows, teamMembers };
  console.error(
    `  avison-young: cached SharpLaunch feed (${websiteRows.length} active rows, ${teamMembers.size} team members)`
  );
  return avisonYoungCache;
}

function stripHtmlText(html: any): string | null {
  if (typeof html !== "string") return null;
  return clean(cheerio.load(`<body>${html}</body>`).text());
}

function sharpLaunchCdnUrl(path: any): string | null {
  const p = clean(path);
  if (!p) return null;
  if (/^https?:\/\//i.test(p)) return p;
  return `${AVISON_YOUNG_CDN_BASE}/${p.replace(/^\/+/, "")}`;
}

function avisonYoungAbsoluteUrl(value: any, base = AVISON_YOUNG_HOST): string | null {
  const raw = clean(value);
  if (!raw || /^javascript:/i.test(raw) || /^mailto:/i.test(raw) || /^tel:/i.test(raw)) return null;
  try {
    return new URL(decodeHtmlEntities(raw), base).toString();
  } catch {
    return null;
  }
}

function avisonYoungDetailLimit(max: number, selectedCount: number): number {
  if (process.env.AVISON_YOUNG_DETAIL_LIMIT !== undefined) {
    return boundedInt(process.env.AVISON_YOUNG_DETAIL_LIMIT, 0, 0, selectedCount);
  }
  return Number.isFinite(max) ? selectedCount : 0;
}

function extractAvisonYoungUrls(doc: ScrapedDoc, baseUrl: string): string[] {
  const $ = cheerio.load(doc.rawHtml);
  const candidates: Array<string | null> = [...doc.links];
  $("a[href], img[src], source[src], [data-src], [data-href]").each((_, el) => {
    candidates.push(
      $(el).attr("href") ?? $(el).attr("src") ?? $(el).attr("data-src") ?? $(el).attr("data-href") ?? null
    );
  });
  for (const match of doc.rawHtml.match(/https?:\/\/[^"'<>\\\s)]+/gi) ?? []) {
    candidates.push(match);
  }
  return dedupeStrings(
    candidates
      .map((url) => avisonYoungAbsoluteUrl(url, baseUrl))
      .filter((url: string | null): url is string => Boolean(url))
      .map((url) => url.replace(/&amp;/g, "&").replace(/[)"'\]>]+$/g, ""))
  );
}

function extractAvisonYoungDocuments(docs: Array<{ doc: ScrapedDoc; url: string }>): any[] {
  const urls = dedupeStrings(
    docs
      .flatMap(({ doc, url }) => extractAvisonYoungUrls(doc, url))
      .filter((url) => {
        try {
          const u = new URL(url);
          return /\.pdf(?:[?#].*)?$/i.test(u.pathname + u.search) && /(^|\.)sharplaunch\.com$/i.test(u.hostname);
        } catch {
          return false;
        }
      })
  );
  return urls.map((url) => ({ name: titleFromFilename(url), url }));
}

function extractAvisonYoungPhotos(docs: Array<{ doc: ScrapedDoc; url: string }>, fallback: string[]): string[] {
  const urls = docs
    .flatMap(({ doc, url }) => extractAvisonYoungUrls(doc, url))
    .filter((url) => {
      try {
        const u = new URL(url);
        return (
          u.hostname === "cdn.sharplaunch.com" &&
          /\.(?:jpe?g|png|webp)(?:[?#].*)?$/i.test(u.pathname + u.search) &&
          !/\/media\//i.test(u.pathname) &&
          (/\/website-\d+\//i.test(u.pathname) || /\/v2\/client-\d+\//i.test(u.pathname))
        );
      } catch {
        return false;
      }
    });
  return dedupeStrings([...urls, ...fallback]);
}

function extractAvisonYoungJsonLd(docs: Array<{ doc: ScrapedDoc; url: string }>): any | null {
  for (const { doc } of docs) {
    const listing = firstJsonLd(doc.rawHtml, "RealEstateListing");
    if (listing) return listing;
  }
  return null;
}

function avisonYoungNameSlug(name: string | null): string | null {
  if (!name) return null;
  return clean(
    name
      .toLowerCase()
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
  );
}

function extractAvisonYoungContactUrls(docs: Array<{ doc: ScrapedDoc; url: string }>): {
  profileLinks: Array<{ url: string; text: string | null; slug: string | null }>;
  vcardLinks: string[];
} {
  const profileLinks: Array<{ url: string; text: string | null; slug: string | null }> = [];
  const vcardLinks: string[] = [];
  for (const { doc, url: baseUrl } of docs) {
    const $ = cheerio.load(doc.rawHtml);
    $('a[href*="/professionals/-/ayp/view/"]').each((_, el) => {
      const href = avisonYoungAbsoluteUrl($(el).attr("href"), baseUrl);
      if (!href) return;
      try {
        const u = new URL(href);
        if (u.hostname !== "www.avisonyoung.us") return;
        const slug = clean(u.pathname.match(/\/professionals\/-\/ayp\/view\/([^/]+)/i)?.[1] ?? null);
        profileLinks.push({ url: u.toString(), text: clean($(el).text()), slug });
      } catch {
        /* ignore malformed contact URL */
      }
    });
    $('a[href*="vcard"], a[href*="vcf"], a[href*="GetVCard"]').each((_, el) => {
      const href = avisonYoungAbsoluteUrl($(el).attr("href"), baseUrl);
      if (href) vcardLinks.push(href);
    });
  }
  const seenProfiles = new Map<string, { url: string; text: string | null; slug: string | null }>();
  for (const link of profileLinks) seenProfiles.set(link.url, link);
  return {
    profileLinks: [...seenProfiles.values()],
    vcardLinks: dedupeStrings(vcardLinks),
  };
}

function enrichAvisonYoungContacts(contacts: any[], docs: Array<{ doc: ScrapedDoc; url: string }>): any[] {
  if (!contacts.length || !docs.length) return contacts;
  const { profileLinks, vcardLinks } = extractAvisonYoungContactUrls(docs);
  if (!profileLinks.length && !vcardLinks.length) return contacts;
  return contacts.map((contact) => {
    const nameSlug = avisonYoungNameSlug(clean(contact?.name));
    const profile =
      profileLinks.find((link) => nameSlug && link.slug === nameSlug) ??
      profileLinks.find((link) => nameSlug && link.slug?.includes(nameSlug)) ??
      (contacts.length === 1 && profileLinks.length === 1 ? profileLinks[0] : null);
    const vcardUrl = contacts.length === 1 && vcardLinks.length === 1 ? vcardLinks[0] : null;
    return prune({
      ...contact,
      profileUrl: clean(contact?.profileUrl) ?? profile?.url,
      vcardUrl: clean(contact?.vcardUrl) ?? vcardUrl,
    });
  });
}

function isAvisonYoungUsCompatible(row: any): boolean {
  const country = clean(row.country)?.toLowerCase();
  if (country) return ["us", "usa", "united states", "united states of america"].includes(country);
  const state = clean(row.state);
  return !!state && /^[A-Z]{2}$/.test(state);
}

function avisonYoungTransactions(row: any): string[] {
  return (Array.isArray(row.transaction) ? row.transaction : [row.transaction])
    .map((t: any) => clean(String(t ?? ""))?.toLowerCase())
    .filter((t: string | null): t is string => !!t);
}

function avisonYoungMatchesTx(row: any, tx: Tx): boolean {
  const transactions = avisonYoungTransactions(row);
  if (tx === "sale") return transactions.some((t) => t.includes("sale"));
  return transactions.some((t) => t.includes("lease") || t.includes("sublease"));
}

function avisonYoungTransactionType(row: any): string {
  const transactions = avisonYoungTransactions(row);
  const hasSale = transactions.some((t) => t.includes("sale"));
  const hasLease = transactions.some((t) => t.includes("lease") || t.includes("sublease"));
  if (hasSale && hasLease) return "Sale/Lease";
  if (transactions.some((t) => t.includes("sublease"))) return "Sublease";
  return hasLease ? "Lease" : "Sale";
}

function avisonYoungSizeText(row: any): string | null {
  const parts: string[] = [];
  if (num(row.total_surface_sqft)) parts.push(`${row.total_surface_sqft} SF total`);
  if (num(row.availabilities_min_surface_sqft) || num(row.availabilities_max_surface_sqft)) {
    const min = num(row.availabilities_min_surface_sqft);
    const max = num(row.availabilities_max_surface_sqft);
    parts.push(
      min && max && min !== max
        ? `${min} - ${max} SF available`
        : `${min ?? max} SF available`
    );
  }
  return clean(parts.join("; "));
}

function avisonYoungLeaseRateText(row: any): string | null {
  const min = num(row.availabilities_min_rent);
  const max = num(row.availabilities_max_rent);
  if (!min && !max) return null;
  const value = min && max && min !== max ? `$${min} - $${max}` : `$${min ?? max}`;
  return `${value}/SF/YR`;
}

function avisonYoungContact(member: any): any | null {
  if (!member) return null;
  const name = clean([member.first_name, member.last_name].map(clean).filter(Boolean).join(" "));
  const avatarUrl =
    member.media_id != null ? sharpLaunchCdnUrl(`media/${String(member.media_id)}`) : null;
  return prune({
    name,
    title: clean(member.title),
    email: clean(member.email),
    phone: clean(member.phone) ?? clean(member.phone_2),
    company: clean(member.company) ?? clean(member.location) ?? "Avison Young",
    avatarUrl,
  });
}

function avisonYoungBaseListing(row: any, teamMembers: Map<string, any>): any {
  const contactsDetailed = (Array.isArray(row.team_member_ids) ? row.team_member_ids : [])
    .map((id: any) => avisonYoungContact(teamMembers.get(String(id))))
    .filter(Boolean);
  const brokerIds = contactsDetailed
    .map((c: any) =>
      brokerRef({
        name: clean(c.name),
        email: clean(c.email),
        phone: clean(c.phone),
        avatarUrl: clean(c.avatarUrl),
        company: "Avison Young",
      })
    )
    .filter((id: number | null): id is number => id !== null);
  const imageUrl = sharpLaunchCdnUrl(row.image_path);
  const externalUrl = clean(row.external_url);
  const sharpLaunchUrl = clean(row.url);
  const rawTypes = Array.isArray(row.type) ? row.type.map(clean).filter(Boolean) : [];
  return prune({
    id: row.id != null ? String(row.id) : null,
    name: clean(row.name) ?? clean(row.meta_title),
    headline: clean(row.meta_title),
    transactionType: avisonYoungTransactionType(row),
    assetType: rawTypes.length ? rawTypes.join(", ") : null,
    description: stripHtmlText(row.description) ?? clean(row.meta_description),
    street: clean(row.address),
    city: clean(row.city),
    state: clean(row.state),
    postalCode: clean(row.zip),
    country: clean(row.country) ?? "US",
    latitude: num(row.location?.lat),
    longitude: num(row.location?.lng),
    salePriceUsd: num(row.sale_price),
    salePriceText: row.sale_price ? `$${Number(row.sale_price).toLocaleString("en-US")}` : null,
    capRatePct: num(row.cap_rate),
    leaseRateText: avisonYoungLeaseRateText(row),
    sizeText: avisonYoungSizeText(row),
    buildingSizeSqft: num(row.total_surface_sqft),
    yearBuilt: num(row.yearbuilt),
    brokerIds,
    contactsDetailed,
    photos: imageUrl ? [imageUrl] : [],
    url: externalUrl ?? sharpLaunchUrl,
    externalUrl,
    sharpLaunchUrl,
    sourceFeedUrl: `${AVISON_YOUNG_API_BASE}?entity=website&status=active`,
    lastUpdated: clean(row.updated_at)?.slice(0, 10) ?? clean(row.on_market_at)?.slice(0, 10),
    rawSubtypes: rawTypes,
    saleUnitPrice: num(row.sale_unit_price),
    availableMinSqft: num(row.availabilities_min_surface_sqft),
    availableMaxSqft: num(row.availabilities_max_surface_sqft),
    rawSharpLaunch: row,
  });
}

async function enrichAvisonYoungListing(base: any): Promise<any> {
  const detailUrls = dedupeStrings([clean(base.sharpLaunchUrl), clean(base.externalUrl)]).filter((url) =>
    /^https?:\/\//i.test(url)
  );
  if (!detailUrls.length) return prune({ ...base, detailError: "missing public detail URLs" });

  const docs: Array<{ doc: ScrapedDoc; url: string }> = [];
  const errors: string[] = [];
  for (const url of detailUrls) {
    try {
      docs.push({ url, doc: await scrapeDoc(url, { waitFor: 1000, timeout: 60000 }) });
    } catch (err) {
      errors.push(`${url}: ${String(err)}`);
    }
  }
  if (!docs.length) {
    return prune({ ...base, detailError: errors.join("; ") || "no detail pages scraped" });
  }

  const documents = extractAvisonYoungDocuments(docs);
  const photos = extractAvisonYoungPhotos(docs, Array.isArray(base.photos) ? base.photos : []);
  const listingLd = extractAvisonYoungJsonLd(docs);
  const contactsDetailed = enrichAvisonYoungContacts(
    Array.isArray(base.contactsDetailed) ? base.contactsDetailed : [],
    docs
  );
  const brokerIds = contactsDetailed
    .map((c: any) =>
      brokerRef({
        name: clean(c.name),
        email: clean(c.email),
        phone: clean(c.phone),
        avatarUrl: clean(c.avatarUrl),
        company: "Avison Young",
      })
    )
    .filter((id: number | null): id is number => id !== null);

  return prune({
    ...base,
    name: clean(listingLd?.name) ?? base.name,
    description: clean(listingLd?.description) ?? base.description,
    lastUpdated: clean(listingLd?.datePosted)?.slice(0, 10) ?? base.lastUpdated,
    brokerIds: brokerIds.length ? brokerIds : base.brokerIds,
    contactsDetailed,
    brochures: documents,
    photos: photos.length ? photos : base.photos,
    documentCount: documents.length,
    photoCount: photos.length || base.photos?.length,
    detailJsonLd: listingLd,
    detailScrape: {
      urls: docs.map(({ url }) => url),
      markdownLength: docs.reduce((sum, item) => sum + item.doc.markdown.length, 0),
      rawHtmlLength: docs.reduce((sum, item) => sum + item.doc.rawHtml.length, 0),
      linkCount: docs.reduce((sum, item) => sum + item.doc.links.length, 0),
      documentCount: documents.length,
      photoCount: photos.length,
      profileUrlCount: contactsDetailed.filter((c: any) => clean(c?.profileUrl)).length,
      vcardUrlCount: contactsDetailed.filter((c: any) => clean(c?.vcardUrl)).length,
    },
    detailError: errors.length ? errors.join("; ") : undefined,
  });
}

async function srcAvisonYoung(tx: Tx, max: number): Promise<SourceResult> {
  const sourceUrl = `https://www.avisonyoung.us/properties/#/?transaction=${tx}&view=sidebar&status=active`;
  const { websiteRows, teamMembers } = await getAvisonYoungFeed();
  const rows = websiteRows
    .filter((row) => row?.status === "active")
    .filter(isAvisonYoungUsCompatible)
    .filter((row) => avisonYoungMatchesTx(row, tx))
    .sort((a, b) => Number(a.order_id ?? a.id ?? 0) - Number(b.order_id ?? b.id ?? 0));
  const want = Math.min(max, rows.length);
  const baseListings = rows.slice(0, want).map((row) => avisonYoungBaseListing(row, teamMembers));
  const detailLimit = avisonYoungDetailLimit(max, baseListings.length);
  const enrichedListings = detailLimit
    ? await pmap(baseListings.slice(0, detailLimit), AVISON_YOUNG_DETAIL_CONCURRENCY, async (listing, idx) => {
        const enriched = await enrichAvisonYoungListing(listing);
        if ((idx + 1) % 10 === 0 || idx + 1 === detailLimit) {
          console.error(`  avison-young/${tx}: detail enriched ${idx + 1}/${detailLimit}`);
        }
        return enriched;
      })
    : [];
  const listings = [...enrichedListings, ...baseListings.slice(detailLimit)];
  if (!listings.length) throw new Error(`no ${tx} listings found in Avison Young SharpLaunch feed`);
  return {
    company: "Avison Young (US)",
    sourceUrl,
    method:
      "SharpLaunch public website/team_member API with bounded public detail-page enrichment for selected rows",
    totalAvailable: rows.length,
    listings,
    note:
      detailLimit > 0
        ? `Detail enrichment fetched public SharpLaunch/Avison pages for ${detailLimit} selected row(s); documents, images, profile URLs, VCard URLs, and JSON-LD are stored as URLs/raw public metadata only.`
        : "Full-feed run preserved as SharpLaunch-only by default. Set AVISON_YOUNG_DETAIL_LIMIT to enrich a bounded number of selected rows.",
  };
}

// --- Savills: server-rendered list pages ---

const US_STATE_NAME_TO_ABBR: Record<string, string> = {
  alabama: "AL",
  alaska: "AK",
  arizona: "AZ",
  arkansas: "AR",
  california: "CA",
  colorado: "CO",
  connecticut: "CT",
  delaware: "DE",
  florida: "FL",
  georgia: "GA",
  hawaii: "HI",
  idaho: "ID",
  illinois: "IL",
  indiana: "IN",
  iowa: "IA",
  kansas: "KS",
  kentucky: "KY",
  louisiana: "LA",
  maine: "ME",
  maryland: "MD",
  massachusetts: "MA",
  michigan: "MI",
  minnesota: "MN",
  mississippi: "MS",
  missouri: "MO",
  montana: "MT",
  nebraska: "NE",
  nevada: "NV",
  "new hampshire": "NH",
  "new jersey": "NJ",
  "new mexico": "NM",
  "new york": "NY",
  "north carolina": "NC",
  "north dakota": "ND",
  ohio: "OH",
  oklahoma: "OK",
  oregon: "OR",
  pennsylvania: "PA",
  "rhode island": "RI",
  "south carolina": "SC",
  "south dakota": "SD",
  tennessee: "TN",
  texas: "TX",
  utah: "UT",
  vermont: "VT",
  virginia: "VA",
  washington: "WA",
  "west virginia": "WV",
  wisconsin: "WI",
  wyoming: "WY",
};

function inferStateFromZip(zip: string | null): string | null {
  if (!zip) return null;
  const prefix = Number(zip.slice(0, 3));
  if (!Number.isFinite(prefix)) return null;
  if (prefix >= 6 && prefix <= 9) return "PR";
  if (prefix >= 10 && prefix <= 27) return "MA";
  if (prefix >= 28 && prefix <= 29) return "RI";
  if (prefix >= 30 && prefix <= 38) return "NH";
  if (prefix >= 39 && prefix <= 49) return "ME";
  if (prefix >= 50 && prefix <= 59) return "IA";
  if (prefix >= 60 && prefix <= 149) return "NY";
  if (prefix >= 150 && prefix <= 196) return "PA";
  if (prefix >= 197 && prefix <= 199) return "DE";
  if (prefix >= 200 && prefix <= 205) return "DC";
  if (prefix >= 206 && prefix <= 219) return "MD";
  if (prefix >= 220 && prefix <= 246) return "VA";
  if (prefix >= 247 && prefix <= 268) return "WV";
  if (prefix >= 270 && prefix <= 289) return "NC";
  if (prefix >= 290 && prefix <= 299) return "SC";
  if (prefix >= 300 && prefix <= 319) return "GA";
  if (prefix >= 320 && prefix <= 349) return "FL";
  if (prefix >= 350 && prefix <= 369) return "AL";
  if (prefix >= 370 && prefix <= 385) return "TN";
  if (prefix >= 386 && prefix <= 397) return "MS";
  if (prefix >= 398 && prefix <= 399) return "GA";
  if (prefix >= 400 && prefix <= 427) return "KY";
  if (prefix >= 430 && prefix <= 459) return "OH";
  if (prefix >= 460 && prefix <= 479) return "IN";
  if (prefix >= 480 && prefix <= 499) return "MI";
  if (prefix >= 500 && prefix <= 528) return "IA";
  if (prefix >= 530 && prefix <= 549) return "WI";
  if (prefix >= 550 && prefix <= 567) return "MN";
  if (prefix >= 570 && prefix <= 577) return "SD";
  if (prefix >= 580 && prefix <= 588) return "ND";
  if (prefix >= 590 && prefix <= 599) return "MT";
  if (prefix >= 600 && prefix <= 629) return "IL";
  if (prefix >= 630 && prefix <= 658) return "MO";
  if (prefix >= 660 && prefix <= 679) return "KS";
  if (prefix >= 680 && prefix <= 693) return "NE";
  if (prefix >= 700 && prefix <= 714) return "LA";
  if (prefix >= 716 && prefix <= 729) return "AR";
  if (prefix >= 730 && prefix <= 749) return "OK";
  if (prefix >= 750 && prefix <= 799) return "TX";
  if (prefix >= 800 && prefix <= 816) return "CO";
  if (prefix >= 820 && prefix <= 831) return "WY";
  if (prefix >= 832 && prefix <= 838) return "ID";
  if (prefix >= 840 && prefix <= 847) return "UT";
  if (prefix >= 850 && prefix <= 865) return "AZ";
  if (prefix >= 870 && prefix <= 884) return "NM";
  if (prefix >= 889 && prefix <= 898) return "NV";
  if (prefix >= 900 && prefix <= 961) return "CA";
  if (prefix >= 967 && prefix <= 968) return "HI";
  if (prefix >= 970 && prefix <= 979) return "OR";
  if (prefix >= 980 && prefix <= 994) return "WA";
  if (prefix >= 995 && prefix <= 999) return "AK";
  return null;
}

function parseSavillsUsLocation(address2: string | null): {
  city: string | null;
  state: string | null;
  postalCode: string | null;
} | null {
  if (!address2) return null;
  const postalCode = (address2.match(/\b\d{5}(?:-\d{4})?\b/) ?? [])[0] ?? null;
  const parts = address2
    .split(",")
    .map((p) => p.trim())
    .filter(Boolean);
  const state =
    parts
      .map((p) => p.match(/\b([A-Z]{2})\b/)?.[1] ?? US_STATE_NAME_TO_ABBR[p.toLowerCase()] ?? null)
      .find((s): s is string => s !== null) ?? inferStateFromZip(postalCode);
  if (!state && !postalCode) return null;
  const city =
    parts.find((p) => {
      const lower = p.toLowerCase();
      return !/^\d{5}(?:-\d{4})?$/.test(p) && !/\b[A-Z]{2}\b/.test(p) && !US_STATE_NAME_TO_ABBR[lower];
    }) ??
    clean(address2.replace(new RegExp(`\\b${state}\\b.*$`), "").replace(/\d{5}(?:-\d{4})?.*$/, "").replace(/,+$/, "")) ??
    null;
  return { city: clean(city), state, postalCode };
}

function parseSavillsNextData(html: string): any | null {
  const raw = html.match(/<script id="__NEXT_DATA__" type="application\/json">([\s\S]*?)<\/script>/)?.[1];
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function savillsNextDataProperties(html: string): any[] {
  const data = parseSavillsNextData(html);
  const props = data?.props?.initialReduxState?.properties;
  return props && typeof props === "object" ? Object.values(props) : [];
}

function savillsTotalItems(html: string, fallback: number): number | null {
  const data = parseSavillsNextData(html);
  const total = data?.props?.initialReduxState?.listPage?.totalItems;
  if (typeof total === "number" && total > 0) return Math.max(total, fallback);
  const headingTotal = Number((html.match(/([0-9][0-9,]*)\s+Properties for (?:let|sale|rent)/i) ?? [])[1]?.replace(/,/g, ""));
  return Number.isFinite(headingTotal) && headingTotal > 0 ? Math.max(headingTotal, fallback) : fallback || null;
}

function savillsSqft(text: string | null): number | null {
  const match = text?.match(/\(([0-9][0-9,.]*)\s*sq ?ft\)/i) ?? text?.match(/([0-9][0-9,.]*)\s*sq ?ft/i);
  return match ? Number(match[1].replace(/,/g, "")) : null;
}

function savillsImageUrls(row: any): string[] {
  const urls = new Set<string>();
  for (const img of [...(row.ImagesGallery ?? []), ...(row.PropertyCardImagesGallery ?? [])]) {
    for (const key of ["ImageUrl_L", "ImageUrl_M", "ImageUrl_S", "ImageUrl"]) {
      const url = clean(img?.[key]);
      if (url?.startsWith("http")) urls.add(url);
    }
  }
  return [...urls];
}

function savillsDocumentUrls(row: any): { name: string | null; url: string }[] {
  const docs: { name: string | null; url: string }[] = [];
  const add = (name: string | null, url: string | null) => {
    if (url?.startsWith("http") && /\.pdf(?:$|\?)/i.test(url) && !docs.some((d) => d.url === url)) {
      docs.push({ name, url });
    }
  };
  add("Floor plan", clean(row.FloorplanPDFUrl));
  for (const doc of row.BrochureGallery ?? []) {
    add(clean(doc?.Caption) ?? "Brochure", clean(doc?.ImageUrl));
  }
  return docs;
}

function savillsContact(agent: any): any | null {
  const name = clean(agent?.AgentName);
  const email = clean(agent?.EmailAddress);
  const phone = clean(agent?.AgentPhoneNumber);
  if (!name && !email && !phone) return null;
  return {
    name,
    email,
    phone,
    office: clean(agent?.Office?.OfficeName),
    company: "Savills",
    avatarUrl: clean(agent?.AgentImageUrl),
  };
}

async function srcSavillsCommercialLease(max: number): Promise<SourceResult> {
  const sourceUrl = "https://search.savills.com/com/en/list/commercial/property-to-let/united-states-of-america";
  const html = await scrapeRaw(sourceUrl, { waitFor: 6000 });
  const rows = savillsNextDataProperties(html).filter((row) => row?.IsCommercial === true);
  const selected = rows.slice(0, Math.min(max, rows.length));
  const listings: any[] = [];
  let nonUsFiltered = 0;
  for (const row of selected) {
    const location = parseSavillsUsLocation(clean(row.AddressLine2));
    if (!location) {
      nonUsFiltered++;
      continue;
    }
    const contactsDetailed = [savillsContact(row.PrimaryAgent), savillsContact(row.SecondaryAgent)].filter(Boolean);
    const brokerIds = contactsDetailed
      .map((contact) => brokerRef(contact))
      .filter((id): id is number => id !== null);
    const propertyType = clean(row.PropertyTypes?.[0]?.Caption);
    const detailId = clean(row.ExternalPropertyIDFormatted) ?? clean(row.ExternalPropertyID)?.toLowerCase();
    const url = detailId ? `https://search.savills.com/com/en/property-detail/${detailId}` : sourceUrl;
    listings.push({
      id: clean(row.ExternalPropertyID) ?? detailId,
      name: clean(row.AddressLine1) ?? clean(row.PropertyPageTitle),
      transactionType: "Lease",
      assetType: propertyType,
      street: clean(row.AddressLine1),
      city: location.city,
      state: location.state,
      postalCode: location.postalCode,
      country: "US",
      latitude: num(row.Latitude),
      longitude: num(row.Longitude),
      leaseRateText: clean(row.GuidePriceText) ?? clean(row.DisplayPriceText),
      sizeText: clean(row.SizeFormatted) ?? clean(row.FooterSizeFormatted),
      buildingSizeSqft: savillsSqft(clean(row.SizeFormatted) ?? clean(row.FooterSizeFormatted)),
      description: clean((row.LongDescription ?? []).map((part: any) => [part.Head, part.Body].filter(Boolean).join("\n")).join("\n\n")),
      brokerIds,
      contactsDetailed,
      brochures: savillsDocumentUrls(row),
      photos: savillsImageUrls(row),
      url,
      rawSavillsProperty: row,
    });
  }
  return {
    company: "Savills",
    sourceUrl,
    method: "Server-rendered commercial lease page parsed from public __NEXT_DATA__ property objects",
    totalAvailable: savillsTotalItems(html, listings.length + nonUsFiltered),
    listings,
    note: nonUsFiltered
      ? `${nonUsFiltered} non-US or non-US-office commercial lease row(s) filtered out`
      : "Commercial sale route was checked separately; the only public commercial sale object observed was Toronto, Canada.",
  };
}

async function srcSavills(tx: Tx, max: number): Promise<SourceResult> {
  if (tx === "lease") return srcSavillsCommercialLease(max);

  const base = "https://search.savills.com/com/en/list/property-for-sale/united-states-of-america";
  const listings: any[] = [];
  let total: number | null = null;
  let nonUsFiltered = 0;
  let emptyStreak = 0;
  for (let page = 1; listings.length < max && page <= Math.max(PAGE_CAP, 10); page++) {
    const before = listings.length;
    const url = page === 1 ? base : `${base}/page/${page}`;
    const html = await scrapeRaw(url, { waitFor: 6000 });
    const $ = cheerio.load(html);
    total =
      total ??
      (Number(
        (html.match(/([0-9][0-9,]*)\s+Properties for (?:sale|rent)/i) ?? [])[1]?.replace(/,/g, "")
      ) || null);
    const seenHere = new Set<string>();
    $('a[href*="/property-detail/"]').each((_, el) => {
      if (listings.length >= max) return;
      const href = $(el).attr("href")!;
      const abs = href.startsWith("http") ? href : `https://search.savills.com${href}`;
      if (seenHere.has(abs) || listings.some((l) => l.url === abs)) return;
      seenHere.add(abs);
      let card = $(el);
      if (!card.find("[class*='sv-details__address1']").length) {
        const parent = card
          .parents()
          .filter((__, p) => $(p).find("[class*='sv-details__address1']").length > 0)
          .first();
        if (parent.length) card = parent;
      }
      const name = clean(card.find("[class*='sv-details__address1']").first().text());
      const address2 = clean(card.find("[class*='sv-details__address2']").first().text());
      const priceBlock = clean(card.find(".sv-property-price").first().text());
      const priceText =
        (priceBlock?.match(/(?:US\$|\$|€|£)\s?[0-9][0-9,.]*(?:\s?million)?/i) ?? [])[0] ??
        priceBlock ??
        null;
      const sizeText =
        (clean(card.text())?.match(/\(([0-9][0-9,.]*\s*sq ?ft)\)/i) ?? [])[1] ??
        (clean(card.text())?.match(/([0-9][0-9,.]*\s*(?:sq ?ft|acres?|m²))/i) ?? [])[1] ??
        null;
      const brokerIds = [
        brokerRef({
          name: clean(card.find("[class*='sv-details__contacts-name']").first().text()),
          phone: clean(card.find("[class*='sv-details__contacts-phone']").first().text()),
          company: "Savills",
        }),
      ].filter((x): x is number => x !== null);
      const location = parseSavillsUsLocation(address2);
      // When the US filter has no inventory (lease), Savills renders foreign
      // fallback cards (e.g. Cyprus, EUR-priced). US-only feed: drop them.
      if (!location) {
        nonUsFiltered++;
        return;
      }
      const img = card.find("img").attr("src") ?? card.find("img").attr("data-src") ?? null;
      listings.push({
        id: abs.split("/property-detail/")[1] ?? null,
        name,
        transactionType: "Sale",
        city: location.city,
        state: location.state,
        postalCode: location.postalCode,
        country: "US",
        salePriceUsd: /\$/.test(priceText ?? "") ? moneyToNumber(priceText) : null,
        salePriceText: priceText,
        sizeText,
        brokerIds,
        photos: img && !img.startsWith("data:") ? [img] : [],
        url: abs,
      });
    });
    if (!seenHere.size) break;
    // Savills shuffles sort order between requests, so a page can be all
    // duplicates without meaning the end of the result set. Stop only after
    // several consecutive pages contribute nothing new.
    if (listings.length === before) {
      if (++emptyStreak >= 3) break;
    } else {
      emptyStreak = 0;
    }
    console.error(`  savills/sale: page ${page}, ${listings.length} collected (total ${total ?? "?"})`);
  }
  if (!listings.length && !nonUsFiltered) {
    throw new Error("no property-detail links found on Savills list page");
  }
  return {
    company: "Savills",
    sourceUrl: base,
    method: "Server-rendered list pages parsed (cards), paginated via /page/N",
    totalAvailable: total,
    listings,
    note: nonUsFiltered
      ? `${nonUsFiltered} non-US fallback card(s) filtered out`
      : undefined,
  };
}

// --- NAI Global: Infabode public GraphQL feed ---

const NAI_WIDGET_URL = "https://ab.infabode.com/nai-global/listings3";
const NAI_PUBLIC_API_URL = "https://infabode.com/public_api";
const NAI_PUBLIC_POST_URL = "https://infabode.com/graphql";
const NAI_LISTING_URL_BASE = "https://infabode.com/services/listings";
const NAI_PAGE_SIZE = 18;
const NAI_DETAIL_CONCURRENCY = Math.min(CONCURRENCY, 2);
const NAI_CONTENT_TYPE_BY_TX: Record<Tx, number> = { sale: 4, lease: 10 };
const NAI_SOURCE_IDS = [
  99487, 99571, 99491, 99492, 84593, 99494, 99495, 84587, 99573, 161338, 84617, 268182,
  84557, 99574, 99499, 268184, 99500, 85394, 99501, 99502, 99503, 99577, 84594, 209408,
  99505, 99506, 77674, 99507, 99508, 85523, 99509, 85516, 99510, 77668, 99511, 99513,
  99514, 99516, 99517, 99518, 99519, 84585, 92844, 99520, 99581, 99521, 99522, 84591,
  99523, 77643, 99524, 99525, 77682, 85417, 99526, 77670, 99527, 99530, 99532, 200927,
  99533, 99534, 87675, 194245, 99536, 99537, 87673, 84622, 99538, 99540, 210201, 194610,
  99543, 77675, 86241, 87997, 149117, 234516, 99545, 99546, 92845, 99548, 99549, 99550,
  99583, 182876, 99551, 99531, 99552, 84621, 99486, 99554, 99555, 99556, 83286, 294858,
  268194, 99557, 92846, 77680, 99558, 99559, 99560, 268195, 99561, 99535, 99584, 99562,
  99563, 109852, 99498, 99566, 99567, 99569, 99585, 92843,
];

const NAI_FEED_QUERY =
  "query GET_LISTINGS_POSTS($filter: PostFilter, $offset: Int, $limit: Int) { posts(filter: $filter, offset: $offset, limit: $limit) { id title summary publishedAt locations { id path } contentType { id name } source { id name logoS3(format: LOGO_300X300) bannerS3 } postImages { id url } } }";
const NAI_DETAIL_QUERY =
  "query publicPost($id: Int!) { publicPost(id: $id) { id title summary content tags currency listingStatus price landSize sizeTotal sizeRangeH sizeRangeL urlOriginal contactEmail urlDocument documentPreview contentType { id name } postImages { id url index } locations { id name geometry path } source { id socialLinks name bannerS3 logoS3(format: LOGO_100X100) } } }";

const naiFeedPageCache = new Map<number, any[]>();

async function naiGraphqlPost(url: string, body: any, referer: string): Promise<any> {
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      origin: new URL(referer).origin,
      referer,
      "user-agent": "Mozilla/5.0 CRE collector",
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(30000),
  });
  const text = await res.text();
  let parsed: any = null;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error(`Infabode GraphQL returned non-JSON HTTP ${res.status}`);
  }
  if (!res.ok || parsed.errors?.length) {
    const msg = parsed.errors?.map((e: any) => e?.message).filter(Boolean).join("; ");
    throw new Error(`Infabode GraphQL HTTP ${res.status}${msg ? `: ${msg}` : ""}`);
  }
  return parsed.data;
}

async function fetchNaiFeedPage(offset: number): Promise<any[]> {
  const cached = naiFeedPageCache.get(offset);
  if (cached) return cached;
  const data = await naiGraphqlPost(
    NAI_PUBLIC_API_URL,
    {
      query: NAI_FEED_QUERY,
      variables: {
        offset,
        limit: NAI_PAGE_SIZE,
        filter: {
          content_types_ids: [NAI_CONTENT_TYPE_BY_TX.sale, NAI_CONTENT_TYPE_BY_TX.lease],
          indSectorsIds: [],
          sourcesIds: NAI_SOURCE_IDS,
          locationsIds: [],
          title: "",
        },
      },
    },
    NAI_WIDGET_URL
  );
  const rows = Array.isArray(data?.posts) ? data.posts : [];
  naiFeedPageCache.set(offset, rows);
  return rows;
}

async function fetchNaiPublicPost(id: number): Promise<any> {
  const data = await naiGraphqlPost(
    NAI_PUBLIC_POST_URL,
    { query: NAI_DETAIL_QUERY, variables: { id } },
    `${NAI_LISTING_URL_BASE}/${id}`
  );
  return data?.publicPost ?? null;
}

function naiLocation(partsSource: any[]): { city: string | null; state: string | null; country: string | null } {
  const path = clean(partsSource[0]?.path ?? partsSource[0]?.name);
  const parts = path ? path.split(",").map((p) => clean(p)).filter(Boolean) : [];
  return {
    city: parts[0] ?? null,
    state: parts.find((p) => /^[A-Z]{2}$/.test(p ?? "")) ?? null,
    country: parts.find((p) => /^United States$/i.test(p ?? "")) ?? "US",
  };
}

function naiImageUrls(row: any, detail: any): string[] {
  const urls = [...(detail?.postImages ?? []), ...(row?.postImages ?? [])]
    .map((img: any) => clean(img?.url))
    .filter((url: string | null): url is string => !!url && /^https?:\/\//i.test(url));
  return [...new Set(urls)];
}

function naiDocumentUrls(detail: any): string[] {
  const urls = [detail?.urlDocument, detail?.documentPreview]
    .map((url) => clean(url))
    .filter((url: string | null): url is string => !!url && /^https?:\/\//i.test(url));
  return [...new Set(urls)];
}

function naiPriceText(detail: any): string | null {
  if (detail?.price === null || detail?.price === undefined) return null;
  const value = String(detail.price);
  const currency = clean(detail.currency);
  return currency ? `${currency} ${value}` : value;
}

function naiSizeText(detail: any): string | null {
  const pieces = [
    num(detail?.sizeTotal) ? `${detail.sizeTotal} SF` : null,
    num(detail?.sizeRangeL) || num(detail?.sizeRangeH)
      ? `${detail.sizeRangeL ?? "?"}-${detail.sizeRangeH ?? "?"} SF`
      : null,
    num(detail?.landSize) ? `${detail.landSize} acres land` : null,
  ].filter(Boolean);
  return pieces.length ? pieces.join("; ") : null;
}

function naiListingStatus(detail: any): string | null {
  const value = detail?.listingStatus;
  if (Array.isArray(value)) {
    const statuses = value.map((status) => clean(status)).filter(Boolean);
    return statuses.length ? statuses.join(",") : null;
  }
  return clean(value);
}

function naiListingFromFeed(row: any, tx: Tx, detail: any, detailError: string | null): any {
  const id = Number(row?.id);
  const sourceLocations = Array.isArray(detail?.locations) && detail.locations.length ? detail.locations : row?.locations;
  const loc = naiLocation(Array.isArray(sourceLocations) ? sourceLocations : []);
  const coords = detail?.locations?.[0]?.geometry?.coordinates;
  const priceText = naiPriceText(detail);
  const docs = detailError ? [] : naiDocumentUrls(detail);
  const contacts =
    !detailError && clean(detail?.contactEmail)
      ? [
          {
            name: null,
            email: clean(detail.contactEmail),
            company: clean(detail?.source?.name ?? row?.source?.name) ?? "NAI Global",
            isPrimary: true,
          },
        ]
      : [];
  return {
    id: Number.isFinite(id) ? `infabode:${id}` : null,
    name: clean(detail?.title ?? row?.title),
    transactionType: tx === "sale" ? "Sale" : "Lease",
    assetType: clean(detail?.contentType?.name ?? row?.contentType?.name),
    description: stripHtmlText(detail?.content) ?? clean(detail?.summary ?? row?.summary),
    city: loc.city,
    state: loc.state,
    country: loc.country,
    latitude: Array.isArray(coords) ? num(coords[1]) : null,
    longitude: Array.isArray(coords) ? num(coords[0]) : null,
    salePriceText: tx === "sale" ? priceText : null,
    leaseRateText: tx === "lease" ? priceText : null,
    sizeText: naiSizeText(detail),
    buildingSizeSqft: num(detail?.sizeTotal),
    lotSizeAcres: num(detail?.landSize),
    listingOffice: clean(detail?.source?.name ?? row?.source?.name),
    sourceCompany: clean(detail?.source?.name ?? row?.source?.name),
    brokerIds: [],
    contactsDetailed: contacts,
    brochures: docs.map((url) => ({ name: titleFromFilename(url), url })),
    photos: naiImageUrls(row, detail),
    url: Number.isFinite(id) ? `${NAI_LISTING_URL_BASE}/${id}` : NAI_WIDGET_URL,
    lastUpdated: clean(row?.publishedAt),
    feedRow: row,
    publicPost: detail ?? undefined,
    detailError: detailError ?? undefined,
    sourceOrganization: detail?.source ?? row?.source,
    sourceWebsiteUrl: clean(detail?.urlOriginal),
    sourceSocialLinks: Array.isArray(detail?.source?.socialLinks) ? detail.source.socialLinks : undefined,
    listingStatus: naiListingStatus(detail),
    tags: Array.isArray(detail?.tags) ? detail.tags : undefined,
    providerCurrency: clean(detail?.currency),
  };
}

async function srcNaiGlobal(tx: Tx, max: number): Promise<SourceResult> {
  const targetContentType = NAI_CONTENT_TYPE_BY_TX[tx];
  const rows: any[] = [];
  let stoppedOnShortPage = false;
  for (let offset = 0; offset < PAGE_CAP * NAI_PAGE_SIZE && rows.length < max; offset += NAI_PAGE_SIZE) {
    const page = await fetchNaiFeedPage(offset);
    const matching = page.filter((row: any) => Number(row?.contentType?.id) === targetContentType);
    rows.push(...matching.slice(0, Math.max(0, max - rows.length)));
    console.error(
      `  nai-global/${tx}: API offset ${offset}, ${page.length} feed rows, ${rows.length} ${tx} collected`
    );
    if (page.length < NAI_PAGE_SIZE) {
      stoppedOnShortPage = true;
      break;
    }
  }
  if (!rows.length) throw new Error(`no ${tx} listing rows found in NAI Global Infabode feed`);
  let detailFailures = 0;
  const listings = await pmap(rows, NAI_DETAIL_CONCURRENCY, async (row) => {
    const id = Number(row?.id);
    if (!Number.isFinite(id)) {
      detailFailures++;
      return naiListingFromFeed(row, tx, null, "missing numeric Infabode post id");
    }
    try {
      const detail = await fetchNaiPublicPost(id);
      return naiListingFromFeed(row, tx, detail, null);
    } catch (err) {
      detailFailures++;
      return naiListingFromFeed(row, tx, null, String(err));
    }
  });
  const activeListings = listings.filter((listing) => listing.listingStatus === "FOR_SALE_ON_MARKET");
  const skippedInactiveOrUnknown = listings.length - activeListings.length;
  return {
    company: "NAI Global",
    sourceUrl: NAI_WIDGET_URL,
    method: "Infabode public GraphQL feed plus publicPost detail enrichment, offset paginated, filtered to FOR_SALE_ON_MARKET",
    totalAvailable: stoppedOnShortPage ? activeListings.length : null,
    listings: activeListings,
    note:
      `${NAI_SOURCE_IDS.length} documented NAI source organization ids; stable Infabode IDs and detail URLs captured. ` +
      `Documents and contacts remain URL-only when public fields exist. ` +
      `Scanned ${rows.length} public feed rows for ${tx}; retained ${activeListings.length} on-market rows, ` +
      `skipped ${skippedInactiveOrUnknown} inactive/unknown-status rows, detail failures skipped: ${detailFailures}.`,
  };
}

// --- CBRE Deal Flow: public Real Capital Markets ListingEngine API ---

const CBRE_DEALFLOW_BASE = "https://www.cbredealflow.com";
const CBRE_DEALFLOW_SOURCE_URL = `${CBRE_DEALFLOW_BASE}/`;
const CBRE_DEALFLOW_FALLBACK_ENGINE_KEY = "oi5qxFqUeAwpuWTlIxfX2WDpoZa3NjIo51F63rmSsEI";
const CBRE_DEALFLOW_PAGE_SIZE = 200;
const CBRE_DEALFLOW_DETAIL_CONCURRENCY = Math.min(CONCURRENCY, 2);
const CBRE_DEALFLOW_PROJECT_TYPE_BY_TX: Record<Tx, string> = {
  sale: "Investment Sale",
  lease: "Leasing",
};

type CbreDealflowCard = {
  id: string | null;
  url: string;
  urlKind: "detail" | "brochure";
  listingPv: string | null;
  name: string | null;
  transactionType: string;
  assetType: string | null;
  description: string | null;
  city: string | null;
  state: string | null;
  country: string | null;
  sizeText: string | null;
  status: string | null;
  brokerIds: number[];
  contactsDetailed?: any[];
  brochures?: any[];
  photos: string[];
  cbreDealflowCard: Record<string, any>;
};

function cbreDealflowHeaders(accept = "application/json, text/javascript, */*; q=0.01"): Record<string, string> {
  return {
    accept,
    origin: CBRE_DEALFLOW_BASE,
    referer: CBRE_DEALFLOW_SOURCE_URL,
    "user-agent": "Mozilla/5.0 CRE collector",
    "x-requested-with": "XMLHttpRequest",
  };
}

function cbreDealflowUrl(href: string | null | undefined): string | null {
  const h = clean(href ?? null);
  if (!h || /^javascript:/i.test(h) || /^mailto:/i.test(h) || /^tel:/i.test(h)) return null;
  try {
    return new URL(h, CBRE_DEALFLOW_BASE).toString();
  } catch {
    return null;
  }
}

async function cbreDealflowGetText(url: string): Promise<string> {
  const res = await fetch(url, {
    headers: cbreDealflowHeaders("text/html,application/json,*/*"),
    signal: AbortSignal.timeout(30000),
  });
  if (!res.ok) throw new Error(`CBRE Deal Flow GET ${url} HTTP ${res.status}`);
  return res.text();
}

async function cbreDealflowPostJson(path: string, body: URLSearchParams): Promise<any> {
  const url = `${CBRE_DEALFLOW_BASE}${path}`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      ...cbreDealflowHeaders(),
      "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    },
    body,
    signal: AbortSignal.timeout(30000),
  });
  const text = await res.text();
  let parsed: any = null;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error(`CBRE Deal Flow ${path} returned non-JSON HTTP ${res.status}`);
  }
  if (!res.ok || parsed?.success === false) {
    throw new Error(`CBRE Deal Flow ${path} HTTP ${res.status}`);
  }
  return parsed;
}

function extractCbreDealflowEngineKey(html: string): string {
  return (
    html.match(/new\s+ListingEngine\s*\(\s*\{[\s\S]*?key\s*:\s*["']([^"']+)/i)?.[1] ??
    html.match(/pv=([A-Za-z0-9_-]{30,})/)?.[1] ??
    CBRE_DEALFLOW_FALLBACK_ENGINE_KEY
  );
}

function parseCbreDealflowFilters(filters: any): Record<string, any> {
  return prune({
    projectTypes: Array.isArray(filters?.ProjectType) ? filters.ProjectType : undefined,
    countries: Array.isArray(filters?.Country) ? filters.Country : undefined,
    states: Array.isArray(filters?.State) ? filters.State : undefined,
    statuses: Array.isArray(filters?.Status) ? filters.Status : undefined,
    assetTypes: Array.isArray(filters?.AssetType) ? filters.AssetType : undefined,
  }) ?? {};
}

function parseCbreDealflowLocation(text: string | null): { city: string | null; state: string | null } {
  const normalized = clean((text ?? "").replace(/\u201A/g, ","));
  const match = normalized?.match(/^(.+?),\s*([A-Z]{2})\b/);
  return {
    city: match ? clean(match[1]) : null,
    state: match?.[2] ?? null,
  };
}

function listingPvFromCbreDealflowUrl(url: string | null): string | null {
  if (!url) return null;
  try {
    return new URL(url).searchParams.get("pv");
  } catch {
    return null;
  }
}

function cbreDealflowCardContacts($: cheerio.CheerioAPI, card: cheerio.Cheerio<any>): any[] {
  const contacts: any[] = [];
  card.find(".contacts .tab-text").each((_, el) => {
    const row = $(el);
    const name = clean(row.find(".name").first().text()) ?? clean(row.text().match(/[A-Za-z][A-Za-z .'-]+/)?.[0] ?? null);
    const email = clean(row.find('a[href^="mailto:"]').first().attr("href")?.replace(/^mailto:/i, ""));
    const phone =
      clean(row.find('a[href^="tel:"]').first().text()) ??
      clean(row.find('a[href^="tel:"]').first().attr("href")?.replace(/^tel:/i, ""));
    if (!name && !email && !phone) return;
    contacts.push(
      prune({
        name,
        email,
        phone,
        company: "CBRE",
      }) ?? {}
    );
  });
  return contacts;
}

function parseCbreDealflowCards(html: string, tx: Tx): CbreDealflowCard[] {
  const $ = cheerio.load(html);
  const cards: CbreDealflowCard[] = [];
  $("li.item, ul.gridview > li").each((_, el) => {
    const card = $(el);
    const detailUrl = cbreDealflowUrl(
      card.find('a[href*="landing.aspx"], a[href*="modern.aspx"], a[href*="/buyer/brochure"]').first().attr("href")
    );
    if (!detailUrl) return;
    const urlKind = /\/buyer\/brochure/i.test(detailUrl) ? "brochure" : "detail";
    const detailText = clean(card.find(".details").first().text());
    const projectType =
      clean(detailText?.match(/\b(Investment Sale|Leasing)\b/i)?.[1] ?? null) ??
      CBRE_DEALFLOW_PROJECT_TYPE_BY_TX[tx];
    const wanted = CBRE_DEALFLOW_PROJECT_TYPE_BY_TX[tx];
    if (projectType.toLowerCase() !== wanted.toLowerCase()) return;
    const location = parseCbreDealflowLocation(card.find(".location .city, .location").first().text());
    const country = clean(card.find(".country").first().text()?.replace(/\|/g, ""));
    const img = cbreDealflowUrl(card.find("img").first().attr("src"));
    const sizeText =
      clean(detailText?.match(/\b(?:Investment Sale|Leasing)\s*\|\s*([^|]+)$/i)?.[1] ?? null) ??
      clean(detailText?.match(/([0-9][0-9,.]*\s*(?:sq ft|sf|units?|acres?|ac)\b)/i)?.[1] ?? null);
    const listingPv = listingPvFromCbreDealflowUrl(detailUrl);
    const contactsDetailed = cbreDealflowCardContacts($, card);
    const brokerIds = contactsDetailed
      .map((c) =>
        brokerRef({
          name: clean(c.name),
          email: clean(c.email),
          phone: clean(c.phone),
          company: "CBRE",
        })
      )
      .filter((id: number | null): id is number => id !== null);
    cards.push({
      id: listingPv,
      url: detailUrl,
      urlKind,
      listingPv,
      name: clean(card.find(".headline").first().text()) ?? clean(card.find("a.summary p").attr("title")),
      transactionType: tx === "sale" ? "Investment Sale" : "Lease",
      assetType: clean(card.find(".asset").first().text()?.replace(/^--$/, "")),
      description: clean(card.find("a.summary p").first().text()),
      city: location.city,
      state: location.state,
      country,
      sizeText,
      status: clean(card.find(".status").first().text()),
      brokerIds,
      contactsDetailed,
      brochures:
        urlKind === "brochure"
          ? [
              {
                name: "Public brochure",
                url: detailUrl,
              },
            ]
          : [],
      photos: img ? [img] : [],
      cbreDealflowCard: prune({
        listingPv,
        urlKind,
        projectType,
        status: clean(card.find(".status").first().text()),
        contactsText: clean(card.find(".contacts, .contact").text()),
        detailsText: detailText,
      }) ?? {},
    });
  });
  return cards;
}

function parseCbreDealflowDetailData(html: string): any | null {
  const match = html.match(/var\s+data\s*=\s*(\{[\s\S]*?\})\s*<\/script>/i);
  if (!match) return null;
  try {
    return JSON.parse(match[1]);
  } catch {
    return null;
  }
}

function cbreDealflowTextFromHtml(html: string | null | undefined): string | null {
  if (!html) return null;
  return clean(cheerio.load(html).text());
}

function cbreDealflowDescription(data: any, fallback: string | null): string | null {
  const summary = clean(data?.projectfields?.summary);
  if (summary) return summary;
  for (const section of data?.sections ?? []) {
    for (const content of section?.contents ?? []) {
      const text = cbreDealflowTextFromHtml(content?.content) ?? clean(content?.subtitle);
      if (text && text.length > 40) return text.slice(0, 2000);
    }
  }
  return fallback;
}

function cbreDealflowImageUrls(data: any, cardPhotos: string[]): string[] {
  const candidates: Array<string | null> = [...cardPhotos];
  const pushImage = (img: any) => {
    candidates.push(cbreDealflowUrl(img?.imageUrl));
    candidates.push(cbreDealflowUrl(img?.thumburl));
  };
  for (const photo of data?.photos ?? []) pushImage(photo);
  for (const section of data?.sections ?? []) {
    for (const img of section?.images ?? []) pushImage(img);
  }
  return dedupeStrings(candidates).filter((url) => /\.(?:jpe?g|png|webp|gif)(?:[?#].*)?$/i.test(url));
}

function cbreDealflowDocumentUrls(data: any): any[] {
  const candidates: string[] = [];
  for (const section of data?.sections ?? []) {
    for (const img of section?.images ?? []) {
      const link = cbreDealflowUrl(img?.link);
      if (link && /\.pdf(?:[?#].*)?$/i.test(link)) candidates.push(link);
    }
    for (const content of section?.contents ?? []) {
      const $ = cheerio.load(content?.content ?? "");
      $("a[href]").each((_, a) => {
        const link = cbreDealflowUrl($(a).attr("href"));
        if (link && /\.pdf(?:[?#].*)?$/i.test(link)) candidates.push(link);
      });
    }
  }
  return dedupeStrings(candidates).map((url) => ({ name: titleFromFilename(url), url }));
}

function cbreDealflowContacts(data: any): any[] {
  const contacts: any[] = [];
  for (const section of data?.sections ?? []) {
    for (const c of section?.contacts ?? []) {
      const name = clean(c?.Fullname) ?? clean([c?.Firstname, c?.Lastname].filter(Boolean).join(" "));
      if (!name) continue;
      const avatarUrl = c?.ShowProfileImage ? cbreDealflowUrl(c?.ProfileImageUrl) : null;
      const email = c?.ShowEmail === true ? clean(c?.Email) : null;
      const phone = c?.ShowPhone === true ? clean(c?.Phone) : null;
      contacts.push(
        prune({
          name,
          title: c?.ShowTitle === true ? clean(c?.Title) : null,
          email,
          phone,
          company: clean(c?.CompanyName) ?? "CBRE",
          avatarUrl,
          profileUrl: c?.ShowExpertBio === true ? cbreDealflowUrl(c?.ExpertBioUrl) : null,
          cbreContactId: c?.ProjectContactId ?? null,
        }) ?? { name, company: "CBRE" }
      );
    }
  }
  return contacts;
}

async function enrichCbreDealflowCard(card: CbreDealflowCard, tx: Tx): Promise<any> {
  if (card.urlKind === "brochure") {
    return prune({
      ...card,
      cbreDealflowDetail: {
        pagePvValue: card.listingPv,
        publicBrochureCard: true,
      },
    });
  }
  try {
    const html = await cbreDealflowGetText(card.url);
    const data = parseCbreDealflowDetailData(html);
    if (!data) throw new Error("detail page had no parseable public data object");
    const addr = data.addresses ?? {};
    const fields = data.projectfields ?? {};
    const detailContacts = cbreDealflowContacts(data);
    const contactsDetailed = detailContacts.length ? detailContacts : card.contactsDetailed ?? [];
    const brokerIds = contactsDetailed
      .map((c) =>
        brokerRef({
          name: clean(c.name),
          email: clean(c.email),
          phone: clean(c.phone),
          avatarUrl: clean(c.avatarUrl),
          company: "CBRE",
        })
      )
      .filter((id: number | null): id is number => id !== null);
    const size = num(Number(fields.size));
    const sizeType = clean(fields.sizetype);
    const parcelSize = num(Number(fields.parcelsize));
    const parcelType = clean(fields.parcelType);
    const showPrice = fields.showprice === true && num(Number(fields.value));
    return prune({
      ...card,
      id: data.projectid != null ? String(data.projectid) : card.id,
      name: clean(data.name) ?? card.name,
      description: cbreDealflowDescription(data, card.description),
      assetType: clean(data.assetType?.full) ?? clean(data.assetType?.subType) ?? card.assetType,
      street: clean(addr.street),
      city: clean(addr.city) ?? card.city,
      state: clean(addr.state) ?? card.state,
      postalCode: clean(addr.zip),
      country: clean(addr.country) ?? card.country ?? "United States",
      latitude: num(Number(addr.latitude)),
      longitude: num(Number(addr.longitude)),
      salePriceUsd: tx === "sale" && showPrice ? Number(fields.value) : null,
      salePriceText:
        tx === "sale" && showPrice
          ? `${clean(fields.valuesymbol) ?? "$"}${Number(fields.value).toLocaleString("en-US")}`
          : null,
      sizeText: size && sizeType ? `${size.toLocaleString("en-US")} ${sizeType}` : card.sizeText,
      buildingSizeSqft: sizeType && /sq\s*ft/i.test(sizeType) ? size : null,
      lotSizeAcres: parcelSize && parcelType && /acre|ac\b/i.test(parcelType) ? parcelSize : null,
      brokerIds,
      contactsDetailed,
      brochures: cbreDealflowDocumentUrls(data),
      photos: cbreDealflowImageUrls(data, card.photos),
      cbreDealflowDetail: {
        projectId: data.projectid ?? null,
        pagePvValue: clean(data.pagePvValue),
        projectType: clean(data.projectType),
        status: clean(data.status),
        isUserLoggedIn: data.isUserLoggedIn === true,
        gatedLabels: prune({
          agreement: clean(data.loggedinuser?.agreementlabel),
          brochure: clean(data.loggedinuser?.brochurelabel),
        }),
        photoCount: Array.isArray(data.photos) ? data.photos.length : 0,
        sectionCount: Array.isArray(data.sections) ? data.sections.length : 0,
      },
    });
  } catch (err) {
    console.error(`  cbre-dealflow/${tx}: detail failed for ${card.url}: ${err}`);
    return prune({
      ...card,
      detailError: String(err),
    });
  }
}

async function srcCbreDealflow(tx: Tx, max: number): Promise<SourceResult> {
  const projectType = CBRE_DEALFLOW_PROJECT_TYPE_BY_TX[tx];
  const home = await cbreDealflowGetText(CBRE_DEALFLOW_SOURCE_URL);
  const engineKey = extractCbreDealflowEngineKey(home);
  const filters = await cbreDealflowPostJson(
    `/api/Handler/ListingEngine/GetFilters?pv=${encodeURIComponent(engineKey)}`,
    new URLSearchParams({ Start: "1", PageSize: "1" })
  );
  const filterSummary = parseCbreDealflowFilters(filters);
  const want = Math.min(max, Number.MAX_SAFE_INTEGER);
  const listingsByUrl = new Map<string, CbreDealflowCard>();
  let total: number | null = null;
  let totalAvail: number | null = null;
  let start = 1;
  for (let page = 1; page <= PAGE_CAP && listingsByUrl.size < want; page++) {
    const pageSize = Math.min(CBRE_DEALFLOW_PAGE_SIZE, want - listingsByUrl.size);
    const data = await cbreDealflowPostJson(
      `/api/AjaxEngine/GetListingsHtml?&pv=${encodeURIComponent(engineKey)}`,
      new URLSearchParams({
        Start: String(start),
        PageSize: String(pageSize),
        FilterProjectType: projectType,
      })
    );
    total = total ?? (Number.isFinite(Number(data.total)) ? Number(data.total) : null);
    totalAvail = totalAvail ?? (Number.isFinite(Number(data.totalAvail)) ? Number(data.totalAvail) : null);
    const cards = parseCbreDealflowCards(String(data.html ?? ""), tx);
    for (const card of cards) {
      if (!listingsByUrl.has(card.url) && listingsByUrl.size < want) listingsByUrl.set(card.url, card);
    }
    console.error(
      `  cbre-dealflow/${tx}: page ${page} start ${start}, ${cards.length} ${projectType} cards (${listingsByUrl.size}/${total ?? "?"})`
    );
    const numProjects = Number(data.numProjects ?? cards.length);
    if (!numProjects || cards.length === 0) break;
    start += numProjects;
  }
  const selected = [...listingsByUrl.values()];
  if (!selected.length) throw new Error(`no public ${projectType} cards found on CBRE Deal Flow`);
  let done = 0;
  const listings = await pmap(selected, CBRE_DEALFLOW_DETAIL_CONCURRENCY, async (card) => {
    const listing = await enrichCbreDealflowCard(card, tx);
    done++;
    if (done % 25 === 0 || done === selected.length) {
      console.error(`  cbre-dealflow/${tx}: detail enriched ${done}/${selected.length}`);
    }
    return listing;
  });
  return {
    company: "CBRE Deal Flow",
    sourceUrl: CBRE_DEALFLOW_SOURCE_URL,
    method:
      "Public RCM ListingEngine API filtered by FilterProjectType, paginated cards plus anonymous detail data object enrichment",
    totalAvailable: total,
    listings,
    note: `Public filter totalAvail was ${totalAvail ?? "unknown"} across all project types; ${projectType} filtered total was ${total ?? "unknown"}. Filter facets sampled: ${Object.entries(filterSummary)
      .map(([k, v]) => `${k}=${Array.isArray(v) ? v.length : "?"}`)
      .join(", ")}. Gated agreement, brochure, executive-summary, and deal-room links are retained only in raw metadata labels, not document rows.`,
  };
}

// --- Colliers: public SalesTracker RCM ListingEngine GET path ---

const COLLIERS_SALESTRACKER_BASE = "https://sales.colliers.com";
const COLLIERS_RCM_BASE = "https://my.rcm1.com";
const COLLIERS_SOURCE_URL = `${COLLIERS_SALESTRACKER_BASE}/`;
const COLLIERS_FALLBACK_ENGINE_KEY = "BX0EQVWsJMGzGR6ZiWBDEnJAH-tErDnvHaBoKDFAOy4";
const COLLIERS_PAGE_SIZE = 100;
const COLLIERS_DETAIL_CONCURRENCY = Math.min(CONCURRENCY, 2);

type ColliersCard = {
  id: string | null;
  url: string;
  detailUrl: string | null;
  detailPv: string | null;
  name: string | null;
  transactionType: string;
  assetType: string | null;
  status: string | null;
  city: string | null;
  state: string | null;
  country: string | null;
  salePriceUsd: number | null;
  salePriceText: string | null;
  sizeText: string | null;
  latitude: number | null;
  longitude: number | null;
  brokerIds: number[];
  contactsDetailed: any[];
  photos: string[];
  colliersSalesTrackerCard: Record<string, any>;
};

function colliersHeaders(accept = "application/json, text/javascript, */*; q=0.01"): Record<string, string> {
  return {
    accept,
    origin: COLLIERS_SALESTRACKER_BASE,
    referer: COLLIERS_SOURCE_URL,
    "user-agent": "Mozilla/5.0 CRE collector",
    "x-requested-with": "XMLHttpRequest",
  };
}

function colliersUrl(href: string | null | undefined): string | null {
  const h = clean(href ?? null);
  if (!h || /^javascript:/i.test(h) || /^mailto:/i.test(h) || /^tel:/i.test(h)) return null;
  try {
    return new URL(decodeHtmlEntities(h), COLLIERS_RCM_BASE).toString();
  } catch {
    return null;
  }
}

async function colliersGetText(url: string): Promise<string> {
  const res = await fetch(url, {
    headers: colliersHeaders("text/html,application/json,*/*"),
    signal: AbortSignal.timeout(30000),
  });
  if (!res.ok) throw new Error(`Colliers GET ${url} HTTP ${res.status}`);
  return res.text();
}

async function colliersGetJson(url: string): Promise<any> {
  const text = await colliersGetText(url);
  const parsed = parseJsonBody(text);
  if (parsed === null) throw new Error(`Colliers GET ${url} returned non-JSON`);
  if (parsed?.success === false) throw new Error(`Colliers GET ${url} returned success=false`);
  return parsed;
}

function extractColliersEngineKey(html: string): string {
  return (
    html.match(/new\s+ListingEngine\s*\(\s*\{[\s\S]*?key\s*:\s*["']([^"']+)/i)?.[1] ??
    html.match(/pv=([A-Za-z0-9_-]{30,})/)?.[1] ??
    COLLIERS_FALLBACK_ENGINE_KEY
  );
}

function colliersListUrl(engineKey: string, start: number, pageSize: number): string {
  return `${COLLIERS_RCM_BASE}/api/AjaxEngine/GetListingsHtml?pv=${encodeURIComponent(engineKey)}&Start=${start}&PageSize=${pageSize}`;
}

function colliersMapUrl(engineKey: string, start: number, pageSize: number): string {
  return `${COLLIERS_RCM_BASE}/api/AjaxEngine/GetMapData?pv=${encodeURIComponent(engineKey)}&Start=${start}&PageSize=${pageSize}`;
}

function colliersSlpInitUrl(pv: string): string {
  return `${COLLIERS_RCM_BASE}/api/handler/slp/Init?pv=${encodeURIComponent(pv)}`;
}

function parseColliersLocation(text: string | null): { city: string | null; state: string | null } {
  const normalized = clean((text ?? "").replace(/\u201A/g, ",").replace(/\u00a0/g, " "));
  const match = normalized?.match(/^(.+?),\s*([A-Z]{2})\b/);
  return {
    city: match ? clean(match[1]) : null,
    state: match?.[2] ?? null,
  };
}

function listingPvFromColliersUrl(url: string | null): string | null {
  if (!url) return null;
  try {
    return new URL(url).searchParams.get("pv");
  } catch {
    return null;
  }
}

function colliersContactsFromCard($: cheerio.CheerioAPI, card: cheerio.Cheerio<any>): any[] {
  const contacts: any[] = [];
  card.find(".contacts .contact").each((_, el) => {
    const row = $(el);
    const name = clean(row.find(".name").first().text());
    const email = clean(row.find('a[href^="mailto:"]').first().attr("href")?.replace(/^mailto:/i, ""));
    const phone =
      clean(row.find(".phone").first().text()) ??
      clean(row.find('a[href^="tel:"]').first().attr("href")?.replace(/^tel:/i, ""));
    if (!name && !email && !phone) return;
    contacts.push(
      prune({
        name,
        email,
        phone,
        company: "Colliers",
      }) ?? {}
    );
  });
  return contacts;
}

function parseColliersCards(html: string, mapLocations: any[], start: number): ColliersCard[] {
  const $ = cheerio.load(html);
  const cards: ColliersCard[] = [];
  $("li.item").each((idx, el) => {
    const card = $(el);
    if (!clean(card.text())) return;
    const detailUrl = colliersUrl(
      card.find('a[href*="landing.aspx"], a[href*="modern.aspx"], a[href*="/slp/"]').first().attr("href")
    );
    const detailPv = listingPvFromColliersUrl(detailUrl);
    const mapRow = mapLocations[idx] ?? {};
    const projectId = mapRow.ProjectId ?? mapRow.projectId ?? null;
    const id = projectId != null ? String(projectId) : detailPv;
    const location = parseColliersLocation(card.find(".city").first().text());
    const photo = colliersUrl(card.find("img").first().attr("src"));
    const contactsDetailed = colliersContactsFromCard($, card);
    const brokerIds = contactsDetailed
      .map((c) =>
        brokerRef({
          name: clean(c.name),
          email: clean(c.email),
          phone: clean(c.phone),
          company: "Colliers",
        })
      )
      .filter((brokerId: number | null): brokerId is number => brokerId !== null);
    cards.push({
      id,
      url: detailUrl ?? `${COLLIERS_SOURCE_URL}#project-${id ?? start + idx}`,
      detailUrl,
      detailPv,
      name: clean(card.find(".headline").first().text()),
      transactionType: "Investment Sale",
      assetType: clean(card.find(".asset").first().text()),
      status: clean(card.find(".status").first().text()),
      city: location.city,
      state: location.state,
      country: "US",
      salePriceUsd: moneyToNumber(clean(card.find(".price").first().text())),
      salePriceText: clean(card.find(".price").first().text()),
      sizeText: clean(card.find(".sq-ft").first().text()),
      latitude: num(Number(mapRow.Latitude ?? mapRow.latitude)),
      longitude: num(Number(mapRow.Longitude ?? mapRow.longitude)),
      brokerIds,
      contactsDetailed,
      photos: photo ? [photo] : [],
      colliersSalesTrackerCard: prune({
        detailPv,
        projectId,
        hasDetailUrl: Boolean(detailUrl),
        cardIndex: start + idx,
      }) ?? {},
    });
  });
  return cards;
}

function colliersProjectField(details: any, name: string): string | null {
  const fields = Array.isArray(details?.ProjectFields) ? details.ProjectFields : [];
  const row = fields.find((f: any) => clean(f?.Name)?.toLowerCase() === name.toLowerCase());
  return clean(row?.Value);
}

function colliersSqftToNumber(value: string | null): number | null {
  const text = clean(value);
  if (!text) return null;
  const match = text.match(/([0-9][0-9,.]*)\s*(?:sq\.?\s*ft\.?|sf)\b/i);
  return match ? Number(match[1].replace(/,/g, "")) : null;
}

function colliersAcresToNumber(value: string | null): number | null {
  const text = clean(value);
  if (!text) return null;
  const match = text.match(/([0-9][0-9,.]*)\s*(?:acres?|ac)\b/i);
  return match ? Number(match[1].replace(/,/g, "")) : null;
}

function colliersDetailContacts(detail: any): any[] {
  const contacts = Array.isArray(detail?.ProjectContacts) ? detail.ProjectContacts : [];
  return contacts
    .map((c: any) =>
      prune({
        name: clean(c?.Name),
        title: clean(c?.Title),
        email: c?.ShowEmail === false ? null : clean(c?.Email),
        phone: clean(c?.Phone),
        company: clean(c?.Company) ?? "Colliers",
        avatarUrl: colliersUrl(c?.ProfileImageUrl),
        profileUrl: c?.ShowExpertBio === true ? colliersUrl(c?.ExpertBioUrl) : null,
        license: clean(c?.License),
        colliersProjectContactId: c?.ProjectContactId ?? null,
      })
    )
    .filter(Boolean);
}

function colliersDetailImages(detail: any, fallback: string[]): string[] {
  const candidates: Array<string | null> = [...fallback];
  for (const img of detail?.GalleryImages ?? []) candidates.push(colliersUrl(img?.ImageUrl));
  return dedupeStrings(candidates).filter((url) => /\.(?:jpe?g|png|webp|gif)(?:[?#].*)?$/i.test(url));
}

async function enrichColliersCard(card: ColliersCard): Promise<any> {
  if (!card.detailPv) {
    return prune({
      ...card,
      colliersSalesTrackerDetail: {
        skipped: "card did not expose a public SLP detail link",
      },
    });
  }
  try {
    const detail = await colliersGetJson(colliersSlpInitUrl(card.detailPv));
    const summary = detail?.ProjectSummary ?? {};
    const address = summary?.Address ?? {};
    const details = detail?.ProjectDetails ?? {};
    const contactsDetailed = colliersDetailContacts(detail);
    const brokerIds = contactsDetailed
      .map((c) =>
        brokerRef({
          name: clean(c.name),
          email: clean(c.email),
          phone: clean(c.phone),
          avatarUrl: clean(c.avatarUrl),
          company: "Colliers",
        })
      )
      .filter((brokerId: number | null): brokerId is number => brokerId !== null);
    const description =
      stripHtmlText(detail?.SimpleLandingPageValues?.Description) ??
      stripHtmlText(detail?.SimpleLandingPageValues?.InvestmentHighlights) ??
      clean(detail?.Seo?.MetaDescription);
    const projectId = summary?.AttributeVisibility?.ProjectId ?? summary?.ProjectId ?? card.id;
    return prune({
      ...card,
      id: projectId != null ? String(projectId) : card.id,
      name: clean(summary?.ProjectName) ?? card.name,
      description,
      assetType: clean(details?.AssetType?.Value) ?? card.assetType,
      status: clean(summary?.Status) ?? colliersProjectField(details, "Status") ?? card.status,
      street: clean(address?.Street),
      city: clean(address?.City) ?? card.city,
      state: clean(address?.State) ?? card.state,
      postalCode: clean(address?.Zip),
      country: clean(address?.CountryCode) ?? card.country,
      latitude: num(Number(address?.Latitude)) ?? card.latitude,
      longitude: num(Number(address?.Longitude)) ?? card.longitude,
      salePriceUsd: moneyToNumber(clean(summary?.AskingPrice)) ?? moneyToNumber(colliersProjectField(details, "Asking Price")) ?? card.salePriceUsd,
      salePriceText: clean(summary?.AskingPrice) ?? colliersProjectField(details, "Asking Price") ?? card.salePriceText,
      sizeText: colliersProjectField(details, "Size") ?? card.sizeText,
      buildingSizeSqft: colliersSqftToNumber(colliersProjectField(details, "Size")),
      lotSizeAcres: colliersAcresToNumber(colliersProjectField(details, "Parcel")),
      yearBuilt: num(Number(colliersProjectField(details, "Year Built"))),
      brokerIds: brokerIds.length ? brokerIds : card.brokerIds,
      contactsDetailed: contactsDetailed.length ? contactsDetailed : card.contactsDetailed,
      brochures: [],
      photos: colliersDetailImages(detail, card.photos),
      colliersSalesTrackerDetail: {
        projectId,
        projectType: clean(details?.ProjectType?.Value),
        assetType: clean(details?.AssetType?.Value),
        pageTitle: clean(detail?.Seo?.PageTitle),
        photoCount: Array.isArray(detail?.GalleryImages) ? detail.GalleryImages.length : 0,
        contactCount: contactsDetailed.length,
        brochureUrl: colliersUrl(detail?.ProjectHeader?.BrochureUrl),
        agreementUrl: colliersUrl(detail?.ProjectHeader?.AgreementButton?.buttonUrl),
        brochureAndAgreementNote:
          "Stored in raw metadata only; collector does not download or classify gated Colliers SalesTracker documents as public document rows.",
      },
    });
  } catch (err) {
    console.error(`  colliers/sale: detail failed for ${card.url}: ${err}`);
    return prune({
      ...card,
      detailError: String(err),
    });
  }
}

async function srcColliers(tx: Tx, max: number): Promise<SourceResult> {
  if (tx === "lease") {
    return {
      company: "Colliers",
      sourceUrl: COLLIERS_SOURCE_URL,
      method: "skipped",
      totalAvailable: 0,
      listings: [],
      note:
        "Colliers SalesTracker is investment-sale oriented. The main Colliers lease search remains blocked behind the Coveo POST path; no lease GET feed has been proven.",
    };
  }
  const home = await colliersGetText(COLLIERS_SOURCE_URL);
  const engineKey = extractColliersEngineKey(home);
  const want = Math.min(max, Number.MAX_SAFE_INTEGER);
  const listingsById = new Map<string, ColliersCard>();
  let total: number | null = null;
  let totalAvail: number | null = null;
  let start = 1;
  for (let page = 1; page <= PAGE_CAP && listingsById.size < want; page++) {
    const pageSize = Math.min(COLLIERS_PAGE_SIZE, want - listingsById.size);
    const [listData, mapData] = await Promise.all([
      colliersGetJson(colliersListUrl(engineKey, start, pageSize)),
      colliersGetJson(colliersMapUrl(engineKey, start, pageSize)),
    ]);
    total = total ?? (Number.isFinite(Number(listData.total)) ? Number(listData.total) : null);
    totalAvail =
      totalAvail ?? (Number.isFinite(Number(listData.totalAvail)) ? Number(listData.totalAvail) : null);
    const mapLocations = Array.isArray(mapData?.projectLocations) ? mapData.projectLocations : [];
    const cards = parseColliersCards(String(listData.html ?? ""), mapLocations, start);
    for (const card of cards) {
      const key = card.id ?? card.detailPv ?? card.url;
      if (!listingsById.has(key) && listingsById.size < want) listingsById.set(key, card);
    }
    console.error(
      `  colliers/sale: page ${page} start ${start}, ${cards.length} cards (${listingsById.size}/${total ?? "?"})`
    );
    const numProjects = Number(listData.numProjects ?? cards.length);
    if (!numProjects || cards.length === 0) break;
    start += numProjects;
  }
  const selected = [...listingsById.values()];
  if (!selected.length) throw new Error("no public Colliers SalesTracker cards found");
  let done = 0;
  const listings = await pmap(selected, COLLIERS_DETAIL_CONCURRENCY, async (card) => {
    const listing = await enrichColliersCard(card);
    done++;
    if (done % 25 === 0 || done === selected.length) {
      console.error(`  colliers/sale: detail enriched ${done}/${selected.length}`);
    }
    return listing;
  });
  const missingDetails = selected.filter((card) => !card.detailPv).length;
  return {
    company: "Colliers",
    sourceUrl: COLLIERS_SOURCE_URL,
    method:
      "Public Colliers SalesTracker RCM ListingEngine GET list/map endpoints plus anonymous SLP Init detail enrichment",
    totalAvailable: total,
    listings,
    note:
      `SalesTracker public list totalAvail was ${totalAvail ?? "unknown"} and filtered total was ${total ?? "unknown"}. ` +
      `${missingDetails} collected card(s) in this run did not expose a public SLP detail link and were kept as card/map rows. ` +
      "Main colliers.com Coveo sale/lease coverage remains blocked; no POST, agreement, or gated document path is used.",
  };
}

// --- Transwestern: public properties GET feed plus detail enrichment ---

const TRANSWESTERN_HOST = "https://transwestern.com";
const TRANSWESTERN_BUCKETS: Record<Tx, string[]> = {
  sale: ["Sale", "Sale or Lease"],
  lease: ["Lease", "Sublease", "Sale or Lease"],
};

function canonicalTranswesternUrl(href: string | null): string | null {
  const h = clean(href);
  if (!h || /^javascript:/i.test(h) || h === "-") return null;
  try {
    return new URL(h, TRANSWESTERN_HOST).toString();
  } catch {
    return null;
  }
}

function transwesternFeedUrl(bucket: string): string {
  const params = new URLSearchParams({
    call: "ajax",
    search: "",
    Latitude: "",
    Longitude: "",
    DealsType: bucket,
    PropertyType: "0",
    MetroName: "",
    SubTypeIDs: "",
    TenancyTypes: "",
    CheckLeed: "false",
    IsEnergyStar: "false",
    MinPrice: "",
    MaxPrice: "",
    MinSize: "",
    MaxSize: "",
    SortType: "asc",
    SortColumn: "",
    class: "",
    TotalLotSizeMin: "",
    TotalLotSizeMax: "",
    NoOfUnitsMin: "",
    NoOfUnitsMax: "",
  });
  return `${TRANSWESTERN_HOST}/properties?${params.toString()}`;
}

function transwesternDetailUrl(pageUrl: any): string | null {
  const slug = clean(String(pageUrl ?? ""));
  if (!slug || slug === "-") return null;
  return `${TRANSWESTERN_HOST}/property/${encodeURIComponent(slug).replace(/%2F/g, "/")}`;
}

function transwesternTransactionType(bucket: string): string {
  if (/sale or lease/i.test(bucket)) return "Sale/Lease";
  if (/sublease/i.test(bucket)) return "Sublease";
  if (/lease/i.test(bucket)) return "Lease";
  return "Sale";
}

function transwesternSizeText(row: any): string | null {
  const size = num(Number(row.PropertySize));
  return size ? `${size.toLocaleString("en-US")} SF` : null;
}

function transwesternPriceText(row: any, tx: Tx): string | null {
  const price = num(Number(row.Price));
  if (!price) return tx === "sale" ? "Contact broker for pricing" : null;
  return `$${price.toLocaleString("en-US")}`;
}

function dedupeStrings(values: Array<string | null | undefined>): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const value of values) {
    const v = clean(value);
    if (!v || seen.has(v)) continue;
    seen.add(v);
    out.push(v);
  }
  return out;
}

function parseTranswesternFacts($: cheerio.CheerioAPI): Record<string, string> {
  const facts: Record<string, string> = {};
  $("li, .property-detail li, .property-facts li").each((_, el) => {
    const label = clean($(el).find("b,strong").first().text()?.replace(/:$/, ""));
    if (!label) return;
    const value = clean($(el).text().replace($(el).find("b,strong").first().text(), ""));
    if (value) facts[label] = value.replace(/^:\s*/, "");
  });
  return facts;
}

function parseTranswesternAvailability($: cheerio.CheerioAPI): any[] {
  const rows: any[] = [];
  $("#tblAvailability tr").each((_, tr) => {
    const cells = $(tr)
      .find("th,td")
      .map((__, td) => clean($(td).text()))
      .get()
      .filter(Boolean);
    if (cells.length < 2 || /suite/i.test(cells.join(" ")) && $(tr).find("th").length) return;
    rows.push({
      suite: cells[0] ?? null,
      size: cells[1] ?? null,
      rate: cells[2] ?? null,
      type: cells[3] ?? null,
      raw: cells,
    });
  });
  return rows;
}

function extractTranswesternContacts(doc: ScrapedDoc): any[] {
  const $ = cheerio.load(doc.rawHtml);
  const contactsByKey = new Map<string, any>();
  $(".PropertyVcard .v-card, .v-card").each((_, el) => {
    const card = $(el);
    const profileUrl = canonicalTranswesternUrl(
      card.find('a[href^="/"]:not([href*="vcard-generator"])').first().attr("href") ?? null
    );
    const vcardUrl = canonicalTranswesternUrl(
      card.find('a[href*="vcard-generator"]').first().attr("href") ?? null
    );
    const avatarUrl = canonicalTranswesternUrl(card.find("img").first().attr("src") ?? null);
    const phone =
      clean(card.find('a[href^="tel:"]').first().text()) ??
      clean(card.text().match(/(\+?1?\s*\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4})/)?.[1] ?? null);
    const email = clean(card.find('a[href^="mailto:"]').first().attr("href")?.replace(/^mailto:/i, ""));
    const linkText = clean(
      card.find('a[href^="/"]:not([href*="vcard-generator"])').first().text()
    );
    const name =
      clean(card.find(".name, .broker-name, h3, h4").first().text()) ??
      linkText ??
      clean(card.find("strong").first().text());
    const title =
      clean(card.find(".title, .job-title").first().text()) ??
      clean(
        card
          .text()
          .split("\n")
          .map((s) => s.trim())
          .find((s) => /associate|director|broker|principal|vice president|managing/i.test(s)) ??
          null
      );
    const key = profileUrl ?? vcardUrl ?? email ?? name;
    if (!key) return;
    contactsByKey.set(key, {
      name,
      title,
      email,
      phone,
      company: "Transwestern",
      profileUrl,
      avatarUrl,
      vcardUrl,
    });
  });
  return [...contactsByKey.values()].filter(
    (c) => c.name || c.email || c.phone || c.profileUrl || c.avatarUrl || c.vcardUrl
  );
}

function extractTranswesternDocuments(doc: ScrapedDoc): any[] {
  const $ = cheerio.load(doc.rawHtml);
  const candidates: string[] = [];
  $('#tblAttachments a[href], a.download-att-btn[href], a.download-flyer-btn[href], a[href$=".pdf"], a[href*=".pdf"], a[href*="twurls.com"]').each(
    (_, el) => {
      const u = canonicalTranswesternUrl($(el).attr("href") ?? null);
      if (u) candidates.push(u);
    }
  );
  for (const link of doc.links ?? []) {
    if (/\.pdf(?:\?|$)|twurls\.com/i.test(link)) {
      const u = canonicalTranswesternUrl(link);
      if (u) candidates.push(u);
    }
  }
  return dedupeStrings(candidates)
    .filter((url) => !/\/Upload\/TREC\/|\/privacy-policy(?:\?|$)|health1\.aetna\.com/i.test(url))
    .map((url) => ({ name: titleFromFilename(url), url }));
}

function extractTranswesternPhotos(doc: ScrapedDoc, feedImage: string | null): string[] {
  const $ = cheerio.load(doc.rawHtml);
  const candidates: Array<string | null> = [feedImage];
  $('.photos-list a.chocolat-image[href], a.chocolat-image[href], a[href*="/images/"], img[src*="/images/"]').each(
    (_, el) => {
      candidates.push(canonicalTranswesternUrl($(el).attr("href") ?? $(el).attr("src") ?? null));
    }
  );
  return dedupeStrings(candidates).filter(
    (url) =>
      !/\.pdf(?:\?|$)/i.test(url) &&
      !/\/assets\/images\/(?:mail|comment|connect-image|tw-logo|Transwestern_2023|tw_gl|transwestern-mapmarker)/i.test(url)
  );
}

function transwesternDescription($: cheerio.CheerioAPI, doc: ScrapedDoc): string | null {
  const candidate =
    clean($(".property-description, .PropertyDescription, #overview").first().text()) ??
    clean(doc.markdown.match(/Overview\s*([\s\S]{1,1800}?)(?:\n[A-Z][A-Za-z ]+\n|\n#{1,6}\s|$)/i)?.[1]);
  if (
    !candidate ||
    /TREC Information About Brokerage Services|Privacy Policy|Copyright\s+Transwestern|Sitemap|Working-at-Transwestern/i.test(
      candidate
    )
  ) {
    return null;
  }
  return candidate;
}

async function enrichTranswesternListing(row: any, bucket: string, tx: Tx): Promise<any> {
  const detailUrl = transwesternDetailUrl(row.PageUrl);
  const feedImage = canonicalTranswesternUrl(clean(row.PropertyImage));
  const base = {
    id: clean(String(row.PageUrl ?? "")),
    name: clean(row.BuildingName),
    transactionType: transwesternTransactionType(bucket),
    assetType: clean(row.PropertyTypeName),
    street: clean(row.FullAddress),
    city: clean(row.City),
    state: clean(row.State)?.toUpperCase() ?? null,
    postalCode: clean(row.ZipCode),
    country: "US",
    latitude: row.Latitude != null ? Number(row.Latitude) : null,
    longitude: row.Longitude != null ? Number(row.Longitude) : null,
    salePriceUsd: tx === "sale" ? num(Number(row.Price)) : null,
    salePriceText: tx === "sale" ? transwesternPriceText(row, tx) : null,
    sizeText: transwesternSizeText(row),
    buildingSizeSqft: num(Number(row.PropertySize)),
    brokerIds: [],
    photos: feedImage ? [feedImage] : [],
    url: detailUrl,
    rawTranswesternFeed: row,
    transwesternBucket: bucket,
  };
  if (!detailUrl) return prune({ ...base, detailError: "missing or invalid PageUrl" });
  try {
    const doc = await scrapeDoc(detailUrl, { waitFor: 1500, timeout: 60000 });
    const $ = cheerio.load(doc.rawHtml);
    const facts = parseTranswesternFacts($);
    const availability = parseTranswesternAvailability($);
    const contactsDetailed = extractTranswesternContacts(doc);
    const brokerIds = contactsDetailed
      .map((c) =>
        brokerRef({
          name: clean(c.name),
          email: clean(c.email),
          phone: clean(c.phone),
          office: clean(c.office),
          avatarUrl: clean(c.avatarUrl),
          company: "Transwestern",
        })
      )
      .filter((id: number | null): id is number => id !== null);
    const coordMatch = doc.rawHtml.match(/myLatLng\s*=\s*\{\s*lat:\s*(-?[0-9.]+),\s*lng:\s*(-?[0-9.]+)/i);
    const description = transwesternDescription($, doc);
    const leaseRateText =
      availability.map((a) => clean(a.rate)).find((rate) => rate && /\$|psf|sf|negotiable/i.test(rate)) ??
      null;
    return prune({
      ...base,
      name: clean($("h1").first().text()) ?? base.name,
      description,
      latitude: base.latitude ?? (coordMatch ? Number(coordMatch[1]) : null),
      longitude: base.longitude ?? (coordMatch ? Number(coordMatch[2]) : null),
      leaseRateText: tx === "lease" ? leaseRateText : null,
      brokerIds,
      contactsDetailed,
      brochures: extractTranswesternDocuments(doc),
      photos: extractTranswesternPhotos(doc, feedImage),
      transwesternFacts: facts,
      availability,
      detailScrape: {
        url: detailUrl,
        markdownLength: doc.markdown.length,
        rawHtmlLength: doc.rawHtml.length,
        linkCount: doc.links.length,
      },
    });
  } catch (err) {
    console.error(`  transwestern/${tx}: detail failed for ${detailUrl}: ${err}`);
    return prune({
      ...base,
      detailError: String(err),
    });
  }
}

async function srcTranswestern(tx: Tx, max: number): Promise<SourceResult> {
  const buckets = TRANSWESTERN_BUCKETS[tx];
  const rowsBySlug = new Map<string, { row: any; bucket: string }>();
  const bucketCounts: Record<string, number> = {};
  for (const bucket of buckets) {
    const data = await scrapeJson(transwesternFeedUrl(bucket), { timeout: 60000 });
    const rows = Array.isArray(data) ? data : [];
    bucketCounts[bucket] = rows.length;
    console.error(`  transwestern/${tx}/${bucket}: ${rows.length} feed rows`);
    for (const row of rows) {
      const slug = clean(String(row.PageUrl ?? ""));
      if (!slug || slug === "-") continue;
      if (!rowsBySlug.has(slug)) rowsBySlug.set(slug, { row, bucket });
    }
  }
  const selected = [...rowsBySlug.values()].slice(0, Math.min(max, Number.MAX_SAFE_INTEGER));
  let done = 0;
  const listings = await pmap(selected, CONCURRENCY, async ({ row, bucket }) => {
    const listing = await enrichTranswesternListing(row, bucket, tx);
    done++;
    if (done % 25 === 0 || done === selected.length) {
      console.error(`  transwestern/${tx}: detail enriched ${done}/${selected.length}`);
    }
    return listing;
  });
  const total = [...new Set([...rowsBySlug.keys()])].length;
  return {
    company: "Transwestern",
    sourceUrl: "https://transwestern.com/properties",
    method: "Public /properties?call=ajax GET feed by DealsType plus detail-page raw HTML enrichment",
    totalAvailable: total,
    listings,
    note: `Bucket counts before slug de-dupe: ${Object.entries(bucketCounts)
      .map(([bucket, count]) => `${bucket}=${count}`)
      .join(", ")}. Rows with invalid PageUrl are skipped.`,
  };
}

const UNSUPPORTED: Record<string, string> = {};

// ---------- main ----------

async function runSource(key: SourceKey, tx: Tx, max: number): Promise<SourceResult> {
  switch (key) {
    case "cbre":
      return srcCbre(tx, max);
    case "cbre-dealflow":
      return srcCbreDealflow(tx, max);
    case "jll":
      return srcJll(tx, max);
    case "jll-investor":
      return srcJllInvestor(tx, max);
    case "cushman-wakefield":
      return srcCushman(tx, max);
    case "colliers":
      return srcColliers(tx, max);
    case "newmark":
      return srcNewmark(tx, max);
    case "marcus-millichap":
      return srcMarcusMillichap(tx, max);
    case "avison-young":
      return srcAvisonYoung(tx, max);
    case "savills":
      return srcSavills(tx, max);
    case "svn":
      return srcBuildout(
        "SVN",
        "b933480474026c41d248b77156c84aef37dcac68",
        "https://svn.com/properties/",
        tx,
        max,
        {
          preferDirectJson: true,
          directReferer: "https://svn.com/properties/",
          pageConcurrency: 1,
          requireCompletePages: true,
          cacheSlug: "svn",
          usePageCache: true,
          recoveryPasses: 1,
          recoveryCooldownMs: 15000,
          maxRecoveryPages: 60,
        }
      );
    case "lee-associates":
      return srcBuildout(
        "Lee & Associates",
        "9a64a93980aeae8db347e72cdfa8ca61017acc9a",
        "https://www.lee-associates.com/properties/",
        tx,
        max,
        {
          preferDirectJson: true,
          directReferer: "https://www.lee-associates.com/properties/",
          pageConcurrency: 1,
          requireCompletePages: true,
          cacheSlug: "lee-associates",
          usePageCache: true,
          recoveryPasses: 1,
          recoveryCooldownMs: 15000,
          maxRecoveryPages: 60,
        }
      );
    case "nai-global":
      return srcNaiGlobal(tx, max);
    case "transwestern":
      return srcTranswestern(tx, max);
    default:
      throw new Error(`unhandled source ${key}`);
  }
}

async function main() {
  const startedAt = new Date().toISOString();
  const sources: any[] = [];
  const listings: any[] = [];

  for (const key of requestedSources) {
    if (UNSUPPORTED[key]) {
      console.error(`skipping unsupported source ${key}`);
      sources.push({ sourceKey: key, supported: false, note: UNSUPPORTED[key] });
      continue;
    }
    for (const tx of TRANSACTIONS) {
      console.error(
        `collecting ${key}/${tx} (max ${Number.isFinite(MAX_ITEMS) ? MAX_ITEMS : "unlimited"})...`
      );
      try {
        const res = await runSource(key, tx, MAX_ITEMS);
        sources.push({
          sourceKey: key,
          transaction: tx,
          supported: true,
          company: res.company,
          sourceUrl: res.sourceUrl,
          method: res.method,
          totalAvailableOnSource: res.totalAvailable,
          listingsCollected: res.listings.length,
          note: res.note ?? null,
        });
        for (const l of res.listings) {
          listings.push(
            prune({ sourceKey: key, sourceCompany: res.company, transactionMode: tx, ...l })
          );
        }
        console.error(
          `  ${key}/${tx}: ${res.listings.length} listings (source total: ${res.totalAvailable ?? "unknown"})`
        );
      } catch (err) {
        console.error(`  ${key}/${tx} FAILED: ${err}`);
        sources.push({
          sourceKey: key,
          transaction: tx,
          supported: true,
          error: String(err).slice(0, 300),
        });
      }
    }
  }

  const succeeded = new Set(
    sources.filter((s) => s.listingsCollected > 0).map((s) => s.sourceKey)
  ).size;
  if (listings.length === 0) {
    throw new Error("no listings collected from any source");
  }
  console.error(
    `done: ${listings.length} listings from ${succeeded} sources, ${brokers.length} unique brokers`
  );

  const out = {
    description:
      "Commercial real estate listings (for sale and for lease) collected from major brokerage websites via local self-hosted Firecrawl, normalized to a common structure.",
    runMeta: {
      apiUrl: API_URL,
      transactions: TRANSACTIONS,
      maxItemsPerSource: Number.isFinite(MAX_ITEMS) ? MAX_ITEMS : null,
      pageCap: PAGE_CAP,
      startedAt,
      finishedAt: new Date().toISOString(),
    },
    sources,
    listings,
    brokers: brokers.map((b) => prune(b) ?? {}),
    totalListings: listings.length,
  };
  const json = JSON.stringify(out);
  if (OUT_PATH) {
    mkdirSync(dirname(OUT_PATH), { recursive: true });
    writeFileSync(OUT_PATH, json);
    console.error(`wrote ${OUT_PATH} (${(json.length / 1e6).toFixed(1)} MB)`);
  } else {
    process.stdout.write(json);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
