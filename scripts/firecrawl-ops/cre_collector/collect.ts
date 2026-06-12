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
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

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

async function buildoutInventory(
  company: string,
  pluginKey: string
): Promise<{ items: any[]; total: number | null }> {
  const cached = buildoutCache.get(pluginKey);
  if (cached) return cached;
  const cachedFailure = buildoutFailureCache.get(pluginKey);
  if (cachedFailure) throw cachedFailure;
  const first = await scrapeJson(
    `https://buildout.com/plugins/${pluginKey}/inventory.json?page=0`,
    { timeout: 60000 }
  );
  const total: number | null = first.meta?.total ?? null;
  const limit: number = first.meta?.limit ?? 30;
  const items: any[] = [...(first.inventory ?? [])];
  let failedPages = 0;
  if (total && total > limit) {
    const pages = Math.min(Math.ceil(total / limit), 1200);
    const failureLimit = Math.max(3, Math.floor(pages * 0.03));
    const pageNums = Array.from({ length: pages - 1 }, (_, i) => i + 1);
    let done = 1;
    let aborting = false;
    const chunks = await pmap(pageNums, CONCURRENCY, async (p) => {
      if (aborting) return [];
      try {
        const d = await scrapeJson(
          `https://buildout.com/plugins/${pluginKey}/inventory.json?page=${p}`,
          { timeout: 60000 }
        );
        done++;
        if (done % 25 === 0) console.error(`  ${company}: inventory page ${done}/${pages}`);
        return d.inventory ?? [];
      } catch (err) {
        failedPages++;
        console.error(`  ${company}: inventory page ${p} FAILED after retries: ${err}`);
        if (failedPages > failureLimit) {
          aborting = true;
        }
        return [];
      }
    });
    for (const c of chunks) items.push(...c);
    // A few rate-limited pages are tolerable (gap fills on the next run);
    // a large gap means the feed is unusable and must not be cached or
    // ingested (mark-missing on a gappy run would soft-delete live rows).
    if (failedPages > failureLimit) {
      const abortError = new Error(
        `${company}: ${failedPages}/${pages} inventory pages failed; aborting this source`
      );
      buildoutFailureCache.set(pluginKey, abortError);
      throw abortError;
    }
  }
  const result = { items, total };
  buildoutCache.set(pluginKey, result);
  console.error(
    `  ${company}: full inventory cached (${items.length} items, total ${total ?? "?"}${failedPages ? `, ${failedPages} pages skipped` : ""})`
  );
  return result;
}

async function srcBuildout(
  company: string,
  pluginKey: string,
  listingsPage: string,
  tx: Tx,
  max: number
): Promise<SourceResult> {
  const { items, total } = await buildoutInventory(company, pluginKey);
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

async function srcNewmark(tx: Tx, max: number): Promise<SourceResult> {
  const sourceUrl = "https://www.nmrk.com/properties";
  if (!newmarkCreds) {
    const html = await scrapeRaw(sourceUrl, { waitFor: 3000 });
    const appId = html.match(/algoliaAppId='([^']+)'/)?.[1];
    const searchKey = html.match(/algoliaSearchApiKey='([^']+)'/)?.[1];
    const indexName = html.match(/algoliaIndexName='([^']+)'/)?.[1] ?? "prod_entries";
    if (!appId || !searchKey) {
      throw new Error("could not extract Algolia credentials from nmrk.com/properties");
    }
    newmarkCreds = { appId, searchKey, indexName };
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
    return scrapeJson(
      `https://${appId}-dsn.algolia.net/1/indexes/${indexName}?x-algolia-application-id=${appId}&x-algolia-api-key=${searchKey}&query=&hitsPerPage=${hitsPerPage}&page=${page}&facets=${encodeURIComponent(facetsField)}&facetFilters=${facetFilters}`,
      { timeout: 60000 }
    );
  };
  const queryFilters = async (filters: string, hitsPerPage: number, page = 0) =>
    scrapeJson(
      `https://${appId}-dsn.algolia.net/1/indexes/${indexName}?x-algolia-application-id=${appId}&x-algolia-api-key=${searchKey}&query=&hitsPerPage=${hitsPerPage}&page=${page}&filters=${encodeURIComponent(filters)}`,
      { timeout: 60000 }
    );

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
  const listings = hits.map((h: any) => ({
    id: clean(h.slug),
    name: clean(h.title),
    headline: clean(h.content),
    transactionType: facetVal,
    assetType: Array.isArray(h.property_types)
      ? h.property_types.join(", ")
      : clean(h.property_type),
    street: clean(h.address),
    city: clean(h.city),
    state: clean(h.state),
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
    photos: (h.thumbnails ?? []).slice(-1).map((t: any) => t.url),
    url: h.url ? `https://www.nmrk.com${h.url}` : null,
    lastUpdated: clean(h.updateDate)?.slice(0, 10) ?? null,
  }));
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
      const searchUrl = jllFilteredSearchUrl(tenure, propertyType, page);
      const html = await scrapeRaw(searchUrl, { waitFor: 8000 });
      const parsed = parseJllSearchPage(html, tx, propertyType, page);
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

  const knownTotals = Object.values(filterTotals).filter((n): n is number => typeof n === "number");
  const total = knownTotals.length ? knownTotals.reduce((sum, n) => sum + n, 0) : null;
  const totalEvidence = JLL_PROPERTY_TYPES.map(
    (propertyType) => `${propertyType}=${filterTotals[propertyType] ?? "?"}`
  ).join(", ");
  return {
    company: "JLL",
    sourceUrl,
    method:
      "Rendered search pages parsed (cards), paginated across public propertyTypes filters with URL de-dupe",
    totalAvailable: total,
    listings,
    note: `Per-filter source totals before cross-filter de-dupe: ${totalEvidence}. Detail-page enrichment remains deferred in this cautious pass.`,
  };
}

// --- JLL Investor Center: rendered page (sale-only by nature) ---

async function srcJllInvestor(tx: Tx, max: number): Promise<SourceResult> {
  if (tx === "lease") {
    return {
      company: "JLL Investor Center",
      sourceUrl: "https://invest.jll.com",
      method: "skipped",
      totalAvailable: 0,
      listings: [],
      note: "Investment-sale platform; no lease inventory.",
    };
  }
  const sourceUrl =
    "https://invest.jll.com/us/en/property-search?filter=%7B%22location%22%3A%5B%22United%20States%22%5D%7D";
  const html = await scrapeRaw(sourceUrl, { waitFor: 8000 });
  const $ = cheerio.load(html);
  const total =
    Number((html.match(/([0-9][0-9,]*)\s+results/i) ?? [])[1]?.replace(/,/g, "")) || null;
  const seen = new Set<string>();
  const listings: any[] = [];
  $('a[href*="/us/en/listings/"]').each((_, el) => {
    if (listings.length >= max) return;
    const href = $(el).attr("href")!;
    const abs = href.startsWith("http") ? href : `https://invest.jll.com${href}`;
    if (seen.has(abs)) return;
    seen.add(abs);
    const card = $(el).closest("li,article,div[class]");
    const txt = clean(card.text()) ?? "";
    const img = card.find("img").attr("src") ?? null;
    const slugParts = abs.split("/listings/")[1]?.split("/") ?? [];
    listings.push({
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
    });
  });
  if (!listings.length) throw new Error("no listing cards found on JLL Investor Center search page");
  return {
    company: "JLL Investor Center",
    sourceUrl,
    method: "Rendered search page parsed (cards)",
    totalAvailable: total,
    listings,
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
const MARCUS_PUBLIC_SEARCH_CAP = 100;

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
  const want = Math.min(max, MARCUS_PUBLIC_SEARCH_CAP);
  const pageSize = Math.max(1, Number.isFinite(want) ? want : MARCUS_PUBLIC_SEARCH_CAP);
  const search = await marcusPost("/api/contentsearch/properties", marcusSearchBody(pageSize));
  const results = search.Results ?? search;
  const rows = Array.isArray(results.Properties) ? results.Properties : [];
  const total = typeof results.TotalCount === "number" ? results.TotalCount : null;
  if (!rows.length) throw new Error("Marcus & Millichap public properties API returned no rows");
  console.error(
    `  marcus-millichap/sale: public properties API returned ${rows.length} row(s), total ${total ?? "?"}`
  );
  const baseListings = rows.map((row: any) => parseMarcusTileHtml(row.Tile, row)).filter((l: any) => l.url);
  let done = 0;
  const listings = await pmap(baseListings, CONCURRENCY, async (row) => {
    const enriched = await enrichMarcusListing(row);
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
      "Public POST /api/contentsearch/properties JSON, newest-100 public cap, plus direct public detail HTML enrichment",
    totalAvailable: total,
    listings,
    note:
      "Public sale inventory only. The listing endpoint reports the full matching total but the public UI/API caps unfiltered search rows at the newest 100; broader public map ActivityIds require separate mappropertydetail expansion and remain deferred for load control.",
  };
}

// --- Avison Young: SharpLaunch search app ---

const AVISON_YOUNG_PAGE_URL =
  "https://www.avisonyoung.us/properties/#/?transaction=sale&view=sidebar&status=active";
const AVISON_YOUNG_API_BASE = "https://pse-api.sharplaunch.com/data";
const AVISON_YOUNG_FALLBACK_API_KEY = "b9fda00f3d4d7f623665270841e32176";
const AVISON_YOUNG_CDN_BASE = "https://cdn.sharplaunch.com";

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

async function srcAvisonYoung(tx: Tx, max: number): Promise<SourceResult> {
  const sourceUrl = `https://www.avisonyoung.us/properties/#/?transaction=${tx}&view=sidebar&status=active`;
  const { websiteRows, teamMembers } = await getAvisonYoungFeed();
  const rows = websiteRows
    .filter((row) => row?.status === "active")
    .filter(isAvisonYoungUsCompatible)
    .filter((row) => avisonYoungMatchesTx(row, tx))
    .sort((a, b) => Number(a.order_id ?? a.id ?? 0) - Number(b.order_id ?? b.id ?? 0));
  const want = Math.min(max, rows.length);
  const listings = rows.slice(0, want).map((row) => {
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
    return {
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
    };
  });
  if (!listings.length) throw new Error(`no ${tx} listings found in Avison Young SharpLaunch feed`);
  return {
    company: "Avison Young (US)",
    sourceUrl,
    method: "SharpLaunch public website/team_member API (full active feed; client-side US and transaction partition)",
    totalAvailable: rows.length,
    listings,
    note: "Detail pages are not scraped in this adapter pass; listing and contact fields come from the public SharpLaunch API. Image and avatar URLs are stored only as CDN URLs.",
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
    }) ?? null;
  return { city: clean(city), state, postalCode };
}

async function srcSavills(tx: Tx, max: number): Promise<SourceResult> {
  const base =
    tx === "sale"
      ? "https://search.savills.com/com/en/list/property-for-sale/united-states-of-america"
      : "https://search.savills.com/com/en/list/property-to-rent/united-states-of-america";
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
        transactionType: tx === "sale" ? "Sale" : "Lease",
        city: location.city,
        state: location.state,
        postalCode: location.postalCode,
        country: "US",
        salePriceUsd: tx === "sale" && /\$/.test(priceText ?? "") ? moneyToNumber(priceText) : null,
        salePriceText: tx === "sale" ? priceText : null,
        leaseRateText: tx === "lease" ? priceText : null,
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
    console.error(`  savills/${tx}: page ${page}, ${listings.length} collected (total ${total ?? "?"})`);
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
    listingStatus: clean(detail?.listingStatus),
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
  return {
    company: "NAI Global",
    sourceUrl: NAI_WIDGET_URL,
    method: "Infabode public GraphQL feed plus publicPost detail enrichment, offset paginated",
    totalAvailable: stoppedOnShortPage ? rows.length : null,
    listings,
    note:
      `${NAI_SOURCE_IDS.length} documented NAI source organization ids; stable Infabode IDs and detail URLs captured. ` +
      `Documents and contacts remain URL-only when public fields exist; detail failures retained per listing: ${detailFailures}.`,
  };
}

// --- CBRE Deal Flow: rendered public homepage grid (sale-only platform) ---

async function srcCbreDealflow(tx: Tx, max: number): Promise<SourceResult> {
  if (tx === "lease") {
    return {
      company: "CBRE Deal Flow",
      sourceUrl: "https://www.cbredealflow.com/",
      method: "skipped",
      totalAvailable: 0,
      listings: [],
      note: "Investment-sale platform; no lease inventory.",
    };
  }
  const sourceUrl = "https://www.cbredealflow.com/";
  const html = await scrapeRaw(sourceUrl, { waitFor: 8000, proxy: "stealth", timeout: 120000 });
  const $ = cheerio.load(html);
  const total =
    Number((html.match(/([0-9][0-9,]*)\s*ASSETS LISTED/i) ?? [])[1]?.replace(/,/g, "")) || null;
  const byHref = new Map<string, { texts: string[]; img: string | null; ctx: string }>();
  $('a[href*="landing.aspx"]').each((_, el) => {
    const href = $(el).attr("href")!;
    const abs = href.startsWith("http") ? href : `https://www.cbredealflow.com${href}`;
    const rec = byHref.get(abs) ?? { texts: [], img: null, ctx: "" };
    const t = clean($(el).text());
    if (t) rec.texts.push(t);
    rec.img = rec.img ?? $(el).find("img").attr("src") ?? null;
    if (!rec.ctx) rec.ctx = clean($(el).closest("td,li,div[class]").parent().text()) ?? "";
    byHref.set(abs, rec);
  });
  const listings: any[] = [];
  for (const [abs, rec] of byHref) {
    if (listings.length >= max) break;
    const texts = rec.texts.sort((a, b) => a.length - b.length);
    const name = texts[0] ?? null;
    if (!name || name.length < 4) continue;
    const description = texts.length > 1 ? texts[texts.length - 1] : null;
    const typeCountry = (rec.ctx.match(
      /(Office|Industrial|Retail|Multifamily|Land|Hotel|Mixed[- ]Use|Healthcare|Self Storage|Data Cent[a-z]+|Senior Housing|Debt|Other)\s*\|?\s*(United States|[A-Za-z ]{4,30})/
    ) ?? []) as any[];
    const ctxClean = rec.ctx
      .split(name)
      .join(" ")
      .replace(/\b(Details|Contacts|Available|New Listing|Featured)\b/g, " ");
    const cityMatches = [...ctxClean.matchAll(/([A-Z][A-Za-z .'-]{1,30}?)[‚,]\s*([A-Z]{2})\b/g)];
    const cityState = (cityMatches[cityMatches.length - 1] ?? []) as any[];
    listings.push({
      name,
      transactionType: "Sale (investment)",
      assetType: typeCountry[1] ?? null,
      description: description && description !== name ? description : null,
      city: cityState[1] ? clean(String(cityState[1]).split(".").pop() ?? "") : null,
      state: cityState[2] ?? null,
      country: typeCountry[2] ? clean(typeCountry[2]) : null,
      brokerIds: [],
      photos: rec.img ? [rec.img] : [],
      url: abs,
    });
  }
  if (!listings.length) throw new Error("no asset cards found on CBRE Deal Flow homepage");
  return {
    company: "CBRE Deal Flow",
    sourceUrl,
    method: "Rendered public homepage grid parsed (cards)",
    totalAvailable: total,
    listings,
    note: "Deal rooms and full financial detail require registration; public card data only. Coverage limited to the first page of the grid.",
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
  return dedupeStrings(candidates).map((url) => ({ name: titleFromFilename(url), url }));
}

function extractTranswesternPhotos(doc: ScrapedDoc, feedImage: string | null): string[] {
  const $ = cheerio.load(doc.rawHtml);
  const candidates: Array<string | null> = [feedImage];
  $('.photos-list a.chocolat-image[href], a.chocolat-image[href], a[href*="/images/"], img[src*="/images/"]').each(
    (_, el) => {
      candidates.push(canonicalTranswesternUrl($(el).attr("href") ?? $(el).attr("src") ?? null));
    }
  );
  return dedupeStrings(candidates).filter((url) => !/\.pdf(?:\?|$)/i.test(url));
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
    const description =
      clean($(".property-description, .PropertyDescription, #overview").first().text()) ??
      clean(doc.markdown.match(/Overview\s*([\s\S]{1,1800}?)(?:\n[A-Z][A-Za-z ]+\n|\n#{1,6}\s|$)/i)?.[1]);
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

const UNSUPPORTED: Record<string, string> = {
  colliers:
    "Colliers' property search (colliers.com/en/properties) loads results only through Coveo's POST-only search API behind a consent wall, which this collector cannot call. No public GET endpoint or server-rendered listing markup was found.",
};

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
        max
      );
    case "lee-associates":
      return srcBuildout(
        "Lee & Associates",
        "9a64a93980aeae8db347e72cdfa8ca61017acc9a",
        "https://www.lee-associates.com/properties/",
        tx,
        max
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
