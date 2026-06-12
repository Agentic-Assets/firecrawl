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

type ScrapeOpts = { waitFor?: number; proxy?: "stealth" | "basic" | "auto"; timeout?: number };
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
    for (const candidate of [body, unescaped]) {
      const start = candidate.indexOf("{");
      const end = candidate.lastIndexOf("}");
      if (start !== -1 && end > start) {
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

async function scrapeJson(url: string, opts: ScrapeOpts = {}): Promise<any> {
  // A successful scrape can still return a non-JSON body (rate-limit or
  // challenge interstitial, e.g. Buildout under sustained paging). Retry the
  // whole scrape with growing backoff before giving up.
  for (let attempt = 1; attempt <= 3; attempt++) {
    const body = await scrapeRaw(url, opts);
    const parsed = parseJsonBody(body);
    if (parsed !== null) return parsed;
    console.error(`non-JSON body from ${url} (attempt ${attempt}); backing off`);
    await new Promise((r) => setTimeout(r, 8000 * attempt));
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
    method: "Newmark Algolia search API (JSON; credentials read from the page; state-faceted pagination)",
    totalAvailable: total,
    listings,
  };
}

// --- JLL: rendered search pages ---

async function srcJll(tx: Tx, max: number): Promise<SourceResult> {
  const tenure = tx === "sale" ? "sale" : "rent";
  const sourceUrl = `https://property.jll.com/search?tenureTypes=${tenure}`;
  const listings: any[] = [];
  let total: number | null = null;
  for (let page = 1; listings.length < max && page <= PAGE_CAP; page++) {
    const html = await scrapeRaw(`${sourceUrl}&page=${page}`, { waitFor: 8000 });
    const $ = cheerio.load(html);
    total =
      total ??
      (Number(
        (($("h2").text() || html).match(/([0-9][0-9,]*)\s+properties/i) ?? [])[1]?.replace(/,/g, "")
      ) || null);
    const seenHere = new Set<string>();
    $('a.text-base[href*="/listings/"]').each((_, el) => {
      if (listings.length >= max) return;
      const href = $(el).attr("href")!;
      if (seenHere.has(href)) return;
      seenHere.add(href);
      if (listings.some((l) => l.url?.endsWith(href))) return;
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
        id: href.split("/listings/")[1] ?? null,
        name: lines[0] ?? null,
        transactionType: tx === "sale" ? "Sale" : "Lease",
        city: m ? clean(m[1]) : null,
        state: m ? m[2] : null,
        postalCode: m?.[3] ?? null,
        country: "US",
        salePriceUsd: tx === "sale" ? moneyToNumber(priceText) : null,
        salePriceText: tx === "sale" ? priceText : null,
        leaseRateText: tx === "lease" ? priceText : null,
        sizeText,
        brokerIds: [],
        url: href.startsWith("http") ? href : `https://property.jll.com${href}`,
      });
    });
    if (seenHere.size === 0) break;
    console.error(`  jll/${tx}: page ${page}, ${listings.length} collected (total ${total ?? "?"})`);
  }
  if (!listings.length) throw new Error("no listing cards found on JLL search page");
  return {
    company: "JLL",
    sourceUrl,
    method: "Rendered search pages parsed (cards), paginated",
    totalAvailable: total,
    listings,
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
    const doc = await scrapeDoc(base.url, { waitFor: 6000, timeout: 120000 });
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
  const first = await scrapeJson(cushmanSearchApiUrl(tx, 0), { timeout: 90000 });
  const content = Array.isArray(first.content) ? first.content : [];
  const total: number = Number(first.total_item ?? content.length);
  if (!content.length) throw new Error(`Cushman & Wakefield API returned no ${tx} content`);
  const want = Math.min(max, total);
  const pages = Math.ceil(want / CUSHMAN_PAGE_SIZE);
  console.error(`  cushman-wakefield/${tx}: ${total} total, fetching ${pages} API page(s)`);
  const chunks: any[][] = [content];
  if (pages > 1) {
    const offsets = Array.from({ length: pages - 1 }, (_, i) => (i + 1) * CUSHMAN_PAGE_SIZE);
    const rest = await pmap(offsets, CONCURRENCY, async (offset) => {
      const d = await scrapeJson(cushmanSearchApiUrl(tx, offset), { timeout: 90000 });
      const rows = Array.isArray(d.content) ? d.content : [];
      console.error(`  cushman-wakefield/${tx}: API offset ${offset}, ${rows.length} rows`);
      return rows;
    });
    chunks.push(...rest);
  }
  const rows = chunks.flat().slice(0, want);
  let done = 0;
  const listings = await pmap(rows, Math.min(CONCURRENCY, 3), async (row) => {
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

// --- Marcus & Millichap: rendered properties page (sale-only platform) ---

async function srcMarcusMillichap(tx: Tx, max: number): Promise<SourceResult> {
  if (tx === "lease") {
    return {
      company: "Marcus & Millichap",
      sourceUrl: "https://www.marcusmillichap.com/properties",
      method: "skipped",
      totalAvailable: 0,
      listings: [],
      note: "Investment-sales platform; lease inventory not listed publicly.",
    };
  }
  const sourceUrl = "https://www.marcusmillichap.com/properties";
  const html = await scrapeRaw(sourceUrl, { waitFor: 9000, proxy: "stealth", timeout: 120000 });
  const $ = cheerio.load(html);
  const seen = new Set<string>();
  const listings: any[] = [];
  $('a[href*="marcusmillichap.com/properties/"], a[href^="/properties/"]').each((_, el) => {
    if (listings.length >= max) return;
    const href = $(el).attr("href")!;
    if (!/properties\/\d+/.test(href)) return;
    const abs = href.startsWith("http") ? href : `https://www.marcusmillichap.com${href}`;
    if (seen.has(abs)) return;
    seen.add(abs);
    let card = $(el);
    if (!card.find("h2").length) {
      const parent = card
        .parents()
        .filter((__, p) => $(p).find("h2").length > 0)
        .first();
      if (parent.length) card = parent;
    }
    const heading = clean(card.find("h2").first().text());
    const nameOnly = heading?.split("|")[0]?.trim() ?? null;
    const location = clean(card.find(".mm-location").first().text());
    const m = (location ?? "").match(/^(.*?),\s*([A-Z]{2})$/);
    const priceText =
      clean(card.find(".mm-listing-price").first().text())?.replace(/^Listing Price:\s*/i, "") ??
      null;
    const capRate = (clean(card.find(".mm-cap-rate").first().text()) ?? "").match(
      /([0-9.]+)%/
    )?.[1];
    const sizeLine = clean(card.find(".mm-size").first().text());
    const idMatch = abs.match(/properties\/(\d+)/);
    listings.push({
      id: idMatch ? idMatch[1] : null,
      name: nameOnly,
      transactionType: "Sale",
      assetType: clean(card.find("h3").first().text()),
      city: m ? clean(m[1]) : location,
      state: m ? m[2] : null,
      country: "US",
      salePriceUsd: moneyToNumber(priceText),
      salePriceText: priceText,
      capRatePct: capRate ? Number(capRate) : null,
      sizeText: sizeLine,
      brokerIds: [],
      photos: [card.find("img").attr("src")].filter(Boolean),
      url: abs,
    });
  });
  if (!listings.length)
    throw new Error("no listing cards found on Marcus & Millichap properties page");
  return {
    company: "Marcus & Millichap",
    sourceUrl,
    method: "Rendered properties page parsed (cards); full result API is POST-only",
    totalAvailable: null,
    listings,
    note: "Coverage limited to listings rendered on the first page (sorted by default).",
  };
}

// --- Avison Young: SharpLaunch search app ---

async function srcAvisonYoung(tx: Tx, max: number): Promise<SourceResult> {
  const sourceUrl = `https://www.avisonyoung.us/properties/#/?transaction=${tx}&view=sidebar&status=active`;
  const html = await scrapeRaw(sourceUrl, { waitFor: 14000, timeout: 120000 });
  const $ = cheerio.load(html);
  const listings: any[] = [];
  $('a[id^="sidebar_item_"]').each((_, el) => {
    if (listings.length >= max) return;
    const href = $(el).attr("href");
    const card = $(el);
    const badge = clean(card.find('[class*="figure__badge"]').text())?.toLowerCase() ?? "";
    if (badge && !badge.includes(tx)) return;
    const name = clean(card.find('[class*="item__heading"]').first().text());
    const below = clean(card.find('[class*="heading_below"]').first().text());
    const pairs: Record<string, string> = {};
    card.find('[class*="details__item"]').each((_, d) => {
      const label = clean($(d).find('[class*="item_label"]').text());
      const value = clean($(d).find('[class*="item_value"]').text());
      if (label && value) pairs[label] = value;
    });
    const m = (below ?? "").match(/^(.*?),\s*([A-Z]{2})$/);
    const sizeLabel = Object.keys(pairs).find((k) => /square feet|acre/i.test(k));
    listings.push({
      id: $(el).attr("id")?.replace("sidebar_item_", "") ?? null,
      name,
      transactionType:
        badge.includes("sale") && badge.includes("lease")
          ? "Sale/Lease"
          : tx === "lease"
            ? "Lease"
            : "Sale",
      city: m ? clean(m[1]) : below,
      state: m ? m[2] : null,
      country: "US",
      sizeText: sizeLabel ? `${pairs[sizeLabel]} ${sizeLabel}` : null,
      details: Object.keys(pairs).length ? pairs : null,
      brokerIds: [],
      url: href ? (href.startsWith("http") ? href : `https://www.avisonyoung.us${href}`) : null,
    });
  });
  if (!listings.length) throw new Error("no sidebar listing items found on Avison Young properties app");
  return {
    company: "Avison Young (US)",
    sourceUrl,
    method: "Rendered SharpLaunch search app parsed (sidebar items)",
    totalAvailable: $('a[id^="sidebar_item_"]').length || null,
    listings,
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

// --- NAI Global: Infabode listings widget ---

async function srcNaiGlobal(tx: Tx, max: number): Promise<SourceResult> {
  const sourceUrl = "https://ab.infabode.com/nai-global/listings3";
  const html = await scrapeRaw(sourceUrl, { waitFor: 8000 });
  const $ = cheerio.load(html);
  const listings: any[] = [];
  const want = tx === "sale" ? /sale/i : /lease|rent/i;
  $("div.listing-card").each((_, el) => {
    if (listings.length >= max) return;
    const card = $(el);
    const type = clean(card.find(".listing-card-header").first().text());
    if (type && !want.test(type)) return;
    const title = clean(card.find(".listing-card-title").first().text());
    const summary = clean(card.find(".listing-card-summary").first().text());
    const contentType = clean(card.find(".listing-card-content-type").first().text());
    const leafTexts: string[] = [];
    card
      .find("*")
      .addBack()
      .contents()
      .each((__, n) => {
        if (n.type === "text") {
          const t = clean((n as any).data);
          if (t) leafTexts.push(t);
        }
      });
    const locLine = leafTexts.find((t) => /^.{2,60}, [A-Z]{2}, United States$/.test(t)) ?? null;
    const m = (locLine ?? "").match(/^(.*?),\s*([A-Z]{2}),/);
    const publisher =
      (card
        .text()
        .match(/Published by\s*([A-Za-z0-9 .,&'-]+?)(?:\d+ (?:day|hour|week|month)|$)/) ?? [])[1] ??
      null;
    const img = card.find("img").first().attr("src") ?? null;
    // The Infabode widget exposes no per-card id or link; a hash of the
    // stable display fields is the only dedup key available.
    const cardKey = [title ?? "", m ? m[1] : "", m ? m[2] : ""].join("|");
    listings.push({
      id: title ? "card:" + createHash("sha1").update(cardKey).digest("hex").slice(0, 16) : null,
      name: title,
      transactionType: tx === "sale" ? "Sale" : "Lease",
      assetType: contentType,
      description: summary,
      city: m ? clean(m[1]) : null,
      state: m ? m[2] : null,
      country: "US",
      listingOffice: clean(publisher ?? ""),
      brokerIds: [],
      photos: img ? [img] : [],
      url: sourceUrl,
    });
  });
  if (!listings.length) throw new Error(`no ${tx} listing cards found on NAI Global listings page`);
  return {
    company: "NAI Global",
    sourceUrl,
    method: "Rendered Infabode listings widget parsed (cards, infinite scroll, first batch)",
    totalAvailable: null,
    listings,
    note: "Cards are not individually linked; the listings page URL is provided. Coverage limited to the first rendered batch.",
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

const UNSUPPORTED: Record<string, string> = {
  colliers:
    "Colliers' property search (colliers.com/en/properties) loads results only through Coveo's POST-only search API behind a consent wall, which this collector cannot call. No public GET endpoint or server-rendered listing markup was found.",
  transwestern:
    "Transwestern's property search (transwestern.com/properties) is a map-driven app whose data loads via POST requests only, with no public GET endpoint or server-rendered listing markup.",
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
