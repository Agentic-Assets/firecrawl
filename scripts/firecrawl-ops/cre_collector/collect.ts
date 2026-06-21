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
  "matthews",
  "franklin-street",
  "lyon-stahl",
  "faris-lee",
  "fortis-net-lease",
  "unique-properties",
  "kiser-group",
  "pinnacle-rea",
  "cawley-chicago",
  "bradford-allen",
  "hudson-peters",
  "gibson-commercial",
  "leibsohn",
  "nai-hiffman",
  "nai-martens",
  "bull-realty",
  "tri-commercial",
  "berger-commercial",
  "nai-bergman",
  "nai-isaac",
  "trinity-partners",
  "metro-commercial",
  "33-realty",
  "nai-hallmark",
  "nai-plotkin",
  "greysteel",
  "nai-talcor",
  "nai-dominion",
  "srs",
  "hanley",
  "kidder-mathews",
  "interra-realty",
  "daum-commercial",
  "foundry-commercial",
  "essex-realty",
  "pyramid-brokerage",
  "shop-companies",
  "velocity-retail",
  "aquila-commercial",
  "finial-group",
  "ackerman",
  "maury-carter",
] as const;

// Buildout-backed firms onboarded via the public plugin inventory API (reuse
// srcBuildout). Token + display name read from each firm's listings page on
// 2026-06-20; sale/lease partitioned client-side by the inventory `sale` flag.
const BUILDOUT_FIRMS: Record<string, { company: string; token: string; page: string }> = {
  "unique-properties": { company: "Unique Properties", token: "43994fa6c8bc167acf6e799d1ecd08173254b362", page: "https://www.uniqueprop.com/" },
  "kiser-group": { company: "Kiser Group", token: "f9624a304f0b834544c60c666a56ca16fcf29a1f", page: "https://www.kisergroup.com/" },
  "pinnacle-rea": { company: "Pinnacle Real Estate Advisors", token: "53aeead9dc03d2337633a409497ff7976f68d56c", page: "https://www.pinnaclerea.com/" },
  "cawley-chicago": { company: "Cawley Chicago", token: "408316c565e1efe74e56779fffe3baa3fdc1f3cf", page: "https://www.cawleychicago.com/" },
  "bradford-allen": { company: "Bradford Allen", token: "f2c7e5eec6ebe7de1f4a0b261bd9a04d715ca1e1", page: "https://www.bradfordallen.com/" },
  "hudson-peters": { company: "Hudson Peters Commercial", token: "fb2068dac489e1dacd436ebe03523aed6df9fe2e", page: "https://www.hudsonpeters.com/" },
  "gibson-commercial": { company: "Gibson Commercial Real Estate", token: "cf76c48a3374831d301742075017a4b5e88642bc", page: "https://www.gibsoncre.com/" },
  "leibsohn": { company: "Leibsohn & Co", token: "9be8516e186ae4deb9ee10eafda9478aca7ffe68", page: "https://www.leibsohn.com/" },
  "nai-hiffman": { company: "NAI Hiffman", token: "783881343a019c17532413fa9b120e61d47c2ae3", page: "https://www.hiffman.com/" },
  "nai-martens": { company: "NAI Martens", token: "6351fc3e892388a1a2dbf1bdc7f65fd1ac144231", page: "https://www.naimartens.com/" },
  "bull-realty": { company: "Bull Realty", token: "6e2064ba71e11d85d50740c87a9372ef9c961a46", page: "https://www.bullrealty.com/" },
  "tri-commercial": { company: "TRI Commercial", token: "4d24ff217c26907aaaa12bb0837e451e568a61e4", page: "https://www.tricommercial.com/" },
  "berger-commercial": { company: "Berger Commercial Real Estate", token: "b1a0682147c41af0dc0ea1af91664ab8ea766aa9", page: "https://www.bergercommercial.com/" },
  "nai-bergman": { company: "NAI Bergman", token: "70e208db445d84be6d7c074ee0108373ccf755a8", page: "https://www.naibergman.com/" },
  "nai-isaac": { company: "NAI Isaac", token: "9ad3babf4f98852f6ed9b0b9db30388bb7e07c5a", page: "https://www.naiisaac.com/" },
  "trinity-partners": { company: "Trinity Partners", token: "1c2d2e5340b1956e6a900d94c4dd3b41b69c2af9", page: "https://www.trinity-partners.com/" },
  "metro-commercial": { company: "Metro Commercial", token: "45a0bd5e3569b2b9d10a3bd88f93fda41ba238f6", page: "https://www.metrocommercial.com/" },
  "33-realty": { company: "33 Realty", token: "5bdefd87a602a896a48f635e07a6724215ed764e", page: "https://33realty.com/" },
  "nai-hallmark": { company: "NAI Hallmark", token: "f883dbd9ac44b7702c0c0bfd4722925868f23ecb", page: "https://www.naihallmark.com/" },
  "nai-plotkin": { company: "NAI Plotkin", token: "f3a493d487cf05648f54bc6264231beb9f4cd176", page: "https://www.naiplotkin.com/" },
  // Found 2026-06-20 via discover_buildout.py. Greysteel was previously assumed
  // Crexi-locked; its Buildout inventory API works.
  "greysteel": { company: "Greysteel", token: "a6dbbaba3cc0ba7d1fbc587e9f06c953cebed964", page: "https://www.greysteel.com/" },
  "nai-talcor": { company: "NAI TALCOR", token: "b9b19d2a3f66dfc3bb532e8c5db7399f4db33349", page: "https://www.naitalcor.com/" },
  "nai-dominion": { company: "NAI Dominion", token: "6a78703278580ac43114429ef6f4a0d484167434", page: "https://www.naidominion.com/" },
};
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
// Ceiling is 4: a full render-heavy run at concurrency 5 OOM-crashed the local
// OrbStack/Firecrawl stack (see TOP30_EXPANSION_PLAN_2026-06-20.md). Default 3.
const CONCURRENCY = Math.max(1, Math.min(4, Number(flags.concurrency ?? "3")));
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
  // Match "$1,250,000", "$1.2M", "$950K", "$1.2 million". The multiplier only
  // applies when a suffix is present, so plain full-dollar strings are unchanged.
  const m = t.replace(/,/g, "").match(/\$\s*([0-9]+(?:\.[0-9]+)?)\s*(k|m|b|thousand|million|billion)?/i);
  if (!m) return null;
  let n = Number(m[1]);
  const suf = (m[2] ?? "").toLowerCase();
  if (suf === "k" || suf === "thousand") n *= 1e3;
  else if (suf === "m" || suf === "million") n *= 1e6;
  else if (suf === "b" || suf === "billion") n *= 1e9;
  return Number.isFinite(n) ? n : null;
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
  title?: string | null;
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
    if (!rec.title && b.title) rec.title = b.title;
    if (!rec.avatarUrl && b.avatarUrl) rec.avatarUrl = b.avatarUrl;
    return existing;
  }
  const idx = brokers.length;
  brokers.push({
    name: b.name ?? null,
    email: b.email ?? null,
    phone: b.phone ?? null,
    office: b.office ?? null,
    title: b.title ?? null,
    avatarUrl: b.avatarUrl ?? null,
    company: b.company,
  });
  brokerIndex.set(key, idx);
  return idx;
}

// ---------- shared JSON-LD helpers (used by JSON-LD-driven sources) ----------
// Node extraction uses jsonLdObjects() (defined above), which recursively walks
// nested @graph arrays. These add type/meta convenience on top.

function ldType(n: any): string {
  const t = n?.["@type"];
  return Array.isArray(t) ? t.join(",") : String(t ?? "");
}

function metaContent(html: string, prop: string): string | null {
  const p = prop.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const m =
    html.match(new RegExp(`<meta[^>]+(?:property|name)=["']${p}["'][^>]+content=["']([^"']*)["']`, "i")) ??
    html.match(new RegExp(`<meta[^>]+content=["']([^"']*)["'][^>]+(?:property|name)=["']${p}["']`, "i"));
  return m ? clean(m[1]) : null;
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
  // True when the run did NOT enumerate the full catalog (detail-fetch failures,
  // skipped Buildout pages, etc.). Downstream ingest must treat an incomplete
  // source as mark-missing INELIGIBLE so a flaky run cannot soft-delete live rows.
  incomplete?: boolean;
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

const buildoutCache = new Map<string, { items: any[]; total: number | null; failedPages: number }>();
const buildoutFailureCache = new Map<string, Error>();

async function buildoutInventory(
  company: string,
  pluginKey: string
): Promise<{ items: any[]; total: number | null; failedPages: number }> {
  const cached = buildoutCache.get(pluginKey);
  if (cached) return cached;
  const cachedFailure = buildoutFailureCache.get(pluginKey);
  if (cachedFailure) throw cachedFailure;
  const first = await scrapeJson(
    `https://buildout.com/plugins/${pluginKey}/inventory.json?page=0`,
    { timeout: 60000 }
  );
  // Validate the shape: a rate-limit/interstitial page can parse as some JSON
  // object that lacks inventory[]/meta. Accepting it would cache an empty feed
  // and (with --mark-missing) soft-delete live rows. Cache the failure so the
  // second transaction pass does not retry a known-bad token.
  if (!Array.isArray(first?.inventory)) {
    const err = new Error(
      `${company}: Buildout inventory response had no inventory[] (likely an interstitial/rate-limit page)`
    );
    buildoutFailureCache.set(pluginKey, err);
    throw err;
  }
  if (first.meta != null && typeof first.meta.total !== "number") {
    const err = new Error(`${company}: Buildout inventory meta.total is not numeric`);
    buildoutFailureCache.set(pluginKey, err);
    throw err;
  }
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
        // Validate EVERY page like page 0: a parseable interstitial without an
        // inventory[] would otherwise return [] and count as a success, leaving a
        // silent gap that mark-missing could act on. Treat it as a failed page.
        if (!Array.isArray(d.inventory)) {
          throw new Error(`inventory page ${p} response missing inventory[] (interstitial?)`);
        }
        done++;
        if (done % 25 === 0) console.error(`  ${company}: inventory page ${done}/${pages}`);
        return d.inventory;
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
  const result = { items, total, failedPages };
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
  const { items, total, failedPages } = await buildoutInventory(company, pluginKey);
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
    incomplete: failedPages > 0,
    note: failedPages > 0 ? `${failedPages} inventory page(s) skipped` : undefined,
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

// --- Matthews REIS: public sitemap enumeration + server-rendered detail pages ---
// Matthews (matthews.com) is a Next.js site whose property DETAIL pages
// (/properties/{slug}) are fully server-rendered and fetch without a token or JS
// render. The complete catalog is enumerable from the public sitemap.xml (~8k
// URLs, ~3.5k /properties/{slug}). Tenure is encoded in the slug: `leasing-...`
// are lease, the rest are investment-sale listings — partitioned client-side so
// each transaction pass only fetches its own slice. DOM hooks and the dual
// (encoded/unencoded) image strategy are ported from the sibling display repo's
// lib/live/parsers/matthews.ts.

/** cms.matthews.com assets that are not property photos. */
const MATTHEWS_NON_PHOTO = /headshot|web-use|brand-logo|logo|og-default|placeholder|favicon|sprite/i;

function matthewsImages(html: string): string[] {
  const urls: string[] = [];
  const seen = new Set<string>();
  const add = (raw: string | null) => {
    if (!raw) return;
    let url = raw.trim();
    if (url.startsWith("//")) url = "https:" + url;
    if (!/^https:\/\/cms\.matthews\.com\/wp-content\/uploads\//i.test(url)) return;
    if (MATTHEWS_NON_PHOTO.test(url) || seen.has(url)) return;
    seen.add(url);
    urls.push(url);
  };
  // 1) Next.js image proxy srcsets carry the encoded original as ?url=.
  const nextRe = /\/_next\/image\?url=([^&"'\\ ]+)/gi;
  let m: RegExpExecArray | null;
  while ((m = nextRe.exec(html))) {
    try {
      add(decodeURIComponent(m[1]));
    } catch {
      /* malformed encoding — skip */
    }
  }
  // 2) Unencoded URLs (live RSC payload / plain <img src>).
  const directRe =
    /https?:\/\/cms\.matthews\.com\/wp-content\/uploads\/[^"'\\ )]+?\.(?:jpe?g|png|webp)/gi;
  while ((m = directRe.exec(html))) add(m[0]);
  return urls;
}

function matthewsBrokers($: cheerio.CheerioAPI): {
  name: string | null;
  email: string | null;
  phone: string | null;
  avatarUrl: string | null;
}[] {
  const out: { name: string | null; email: string | null; phone: string | null; avatarUrl: string | null }[] = [];
  $('a[id="agentName"]').each((_, el) => {
    const name = clean($(el).text());
    if (!name || out.some((b) => b.name === name)) return;
    // Walk up to the nearest ancestor holding this broker's tel:/mailto:.
    let card = $(el);
    for (let i = 0; i < 6; i++) {
      const parent = card.parent();
      if (parent.length === 0) break;
      card = parent;
      if (card.find('a[href^="tel:"], a[href^="mailto:"]').length > 0) break;
    }
    const mailHref = card.find('a[href^="mailto:"]').first().attr("href") ?? "";
    const telText = clean(card.find('a[href^="tel:"]').first().text());
    const telHref = card.find('a[href^="tel:"]').first().attr("href") ?? "";
    let avatar = card.find('img[src*="cms.matthews.com"]').first().attr("src") ?? null;
    if (avatar?.startsWith("//")) avatar = "https:" + avatar;
    out.push({
      name,
      email: clean(mailHref.replace(/^mailto:/i, "").split("?")[0]),
      phone: telText || clean(telHref.replace(/^tel:/i, "")),
      avatarUrl: avatar?.startsWith("http") ? avatar : null,
    });
  });
  return out;
}

/** "230 W Main St, Danville, KY, 40422" -> split US address parts. */
function parseMatthewsAddress(line: string | null): {
  street: string | null;
  city: string | null;
  state: string | null;
  postalCode: string | null;
} {
  const out = { street: null as string | null, city: null as string | null, state: null as string | null, postalCode: null as string | null };
  if (!line) return out;
  const parts = line.split(",").map((p) => p.trim()).filter(Boolean);
  if (parts.length && /^\d{5}(-\d{4})?$/.test(parts[parts.length - 1])) out.postalCode = parts.pop()!;
  if (parts.length && /^[A-Z]{2}$/.test(parts[parts.length - 1])) out.state = parts.pop()!;
  if (parts.length) out.city = parts.pop()!;
  if (parts.length) out.street = parts.join(", ");
  return out;
}

function parseMatthewsDetail(html: string, url: string, tx: Tx): any | null {
  const $ = cheerio.load(html);
  const title = clean($("#propertyTitle").first().text()) || clean($("h1").first().text());
  const photos = matthewsImages(html);
  if (!title && photos.length === 0) return null; // not a rendered detail page

  const addr = parseMatthewsAddress(clean($("#propertyAddress").first().text()));

  const priceText = clean($("#propertyPrice").first().text());
  const realPrice =
    priceText && !/call|inquire|contact|request|tbd|offer/i.test(priceText) ? priceText : null;

  // Key-info pairs: zip the title/value spans by document order.
  const labels: string[] = [];
  const values: string[] = [];
  $(".key-info-title").each((_, el) => {
    const t = clean($(el).text());
    if (t) labels.push(t.replace(/:$/, ""));
  });
  $(".key-info-value").each((_, el) => {
    values.push(clean($(el).text()) ?? "");
  });
  const facts: Record<string, string> = {};
  for (let i = 0; i < Math.min(labels.length, values.length); i++) {
    if (labels[i] && values[i] && !(labels[i] in facts)) facts[labels[i]] = values[i];
  }
  const factGet = (re: RegExp): string | null => {
    const k = Object.keys(facts).find((key) => re.test(key));
    return k ? facts[k] : null;
  };
  const capText = factGet(/cap\s*rate|^cap\b/i);
  const capRatePct = capText ? Number((capText.match(/([0-9]+(?:\.[0-9]+)?)/) ?? [])[1]) || null : null;
  const assetType = factGet(/^type$|property type/i);
  const leasableText = factGet(/leasable area|building (?:size|sf)|gla|rentable/i);
  const buildingSizeSqft = leasableText ? Number(leasableText.replace(/[^0-9.]/g, "")) || null : null;
  const lotText = factGet(/lot size/i);
  const lotSizeAcres = lotText ? Number((lotText.match(/([0-9.]+)\s*acre/i) ?? [])[1]) || null : null;
  const yearText = factGet(/year built/i);
  const yearBuilt = yearText ? Number((yearText.match(/(\d{4})/) ?? [])[1]) || null : null;

  // Highlights -> description (Matthews has no narrative description section).
  const highlights: string[] = [];
  $("h3").each((_, el) => {
    if (!/^highlights$/i.test(clean($(el).text()) ?? "")) return;
    const prose = $(el).nextAll(".prose").first();
    const text = prose.length ? prose.text() : "";
    for (const part of text.split("•")) {
      const item = clean(part);
      if (item && !highlights.includes(item)) highlights.push(item);
    }
  });

  // Offering Memorandum PDF.
  const docHref = $("#propertyDocumentLink").first().attr("href") ?? null;
  const brochures = docHref
    ? [{ name: "Offering Memorandum", url: docHref.startsWith("http") ? docHref : `https://www.matthews.com${docHref}` }]
    : [];

  const brokerIds = matthewsBrokers($)
    .map((b) =>
      brokerRef({ name: b.name, email: b.email, phone: b.phone, avatarUrl: b.avatarUrl, office: null, company: "Matthews" })
    )
    .filter((x): x is number => x !== null);

  const slug = (url.split("/properties/")[1] ?? url).replace(/[/?#].*$/, "");

  return {
    id: slug,
    name: title,
    transactionType: tx === "sale" ? "Sale" : "Lease",
    assetType,
    description: highlights.length ? highlights.join(" • ") : null,
    street: addr.street,
    city: addr.city,
    state: addr.state,
    postalCode: addr.postalCode,
    country: "US",
    salePriceUsd: tx === "sale" && realPrice ? moneyToNumber(realPrice) : null,
    salePriceText: tx === "sale" ? realPrice : null,
    capRatePct,
    leaseRateText: tx === "lease" ? realPrice ?? priceText ?? null : null,
    sizeText: leasableText ? `${leasableText} SF` : null,
    buildingSizeSqft,
    lotSizeAcres,
    yearBuilt,
    brokerIds,
    brochures,
    photos,
    url,
    highlights,
  };
}

// Matthews rate-limits Firecrawl rendering (it cut us off after ~58 sustained renders),
// but its /properties/{slug} detail pages are fully server-rendered, so plain HTTP fetches
// carry the same DOM parseMatthewsDetail needs. We throttle globally and back off on
// 429/403 to stay under the rate limit — and avoid Firecrawl renders entirely (no crash
// risk). The global gate serializes requests regardless of CONCURRENCY.
let matthewsNextSlot = 0;
let matthewsInterval = 1800; // ms between requests; grows on throttle
const matthewsSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
async function matthewsGate(): Promise<void> {
  const now = Date.now();
  const slot = Math.max(now, matthewsNextSlot);
  matthewsNextSlot = slot + matthewsInterval;
  const wait = slot - now;
  if (wait > 0) await matthewsSleep(wait);
}
async function matthewsFetch(url: string): Promise<string> {
  for (let attempt = 0; attempt < 6; attempt++) {
    await matthewsGate();
    let status = 0;
    try {
      const res = await fetch(url, {
        headers: {
          "User-Agent":
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
          Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
      });
      status = res.status;
      if (res.ok) return res.text();
    } catch {
      /* network blip -> retry */
    }
    if (status === 0 || status === 429 || status === 403 || status === 503) {
      matthewsInterval = Math.min(matthewsInterval + 700, 7000); // slow everyone down
      await matthewsSleep(20000 + attempt * 15000 + Math.random() * 5000);
      continue;
    }
    throw new Error(`Matthews HTTP ${status}`);
  }
  throw new Error("Matthews: throttled after retries");
}

async function srcMatthews(tx: Tx, max: number): Promise<SourceResult> {
  const sourceUrl = "https://www.matthews.com/listings";
  const xml = await matthewsFetch("https://www.matthews.com/sitemap.xml");
  const detail = Array.from(
    new Set(xml.match(/https:\/\/www\.matthews\.com\/properties\/[^<\s"')]+/gi) ?? [])
  );
  if (!detail.length) {
    throw new Error(
      "Matthews: no /properties/ URLs found in sitemap.xml (fetch may have been blocked or transformed)"
    );
  }
  const isLease = (u: string) => /\/properties\/leasing-/i.test(u);
  const urls = detail.filter((u) => (tx === "lease" ? isLease(u) : !isLease(u)));
  const take = Number.isFinite(max) ? urls.slice(0, max) : urls;

  let failures = 0;
  const parsed = await pmap(take, CONCURRENCY, async (u) => {
    try {
      const html = await matthewsFetch(u);
      return parseMatthewsDetail(html, u, tx);
    } catch (err) {
      failures++;
      console.error(`  matthews/${tx}: ${u} failed: ${err}`);
      return null;
    }
  });
  const listings = parsed.filter((l): l is any => l !== null);
  if (!listings.length) {
    throw new Error("Matthews: sitemap enumerated detail pages but none parsed");
  }
  return {
    company: "Matthews",
    sourceUrl,
    method:
      "Public sitemap.xml enumeration -> server-rendered /properties/{slug} detail pages, DOM-parsed (curl-able, no token)",
    totalAvailable: urls.length,
    listings,
    incomplete: failures > 0,
    note: failures > 0 ? `${failures} detail page(s) failed to fetch` : undefined,
  };
}

// --- Lyon Stahl: own WordPress property sitemap + JSON-LD detail pages ---
// Lyon Stahl (lyonstahl.com) is an LA multifamily INVESTMENT-SALES specialist.
// Its own site exposes a clean property sitemap (/properties-sitemapN.xml) and
// server-rendered detail pages whose JSON-LD @graph carries Product{offers.price},
// an ApartmentComplex/Place node {address, numberOfRooms, floorSize,
// additionalProperty}, and Person nodes for brokers. Listed on Crexi too, but the
// own site is fully enumerable with plain GET (no Crexi API needed). Sale-only.

function parseLyonStahlDetail(html: string, url: string, tx: Tx): any | null {
  if (tx !== "sale") return null; // investment-sales only; no lease inventory
  const nodes = jsonLdObjects(html); // recursive @graph walker (handles nested graphs)
  const product = nodes.find((n) => ldType(n).includes("Product"));
  const place =
    nodes.find(
      (n) => /Apartment|Residence|House|SingleFamily|Place/i.test(ldType(n)) && n.address && typeof n.address === "object"
    ) ?? nodes.find((n) => n.address && typeof n.address === "object");
  let offer: any = product?.offers ?? null;
  if (Array.isArray(offer)) offer = offer[0];
  const avail = String(offer?.availability ?? "");
  if (/SoldOut|Discontinued|OutOfStock/i.test(avail)) return null; // drop sold/off-market

  const addr = place && typeof place.address === "object" ? place.address : {};
  const ogTitle = metaContent(html, "og:title");
  const ogAddr = ogTitle ? ogTitle.replace(/\s*[-|–]\s*Lyon Stahl.*$/i, "").trim() : "";
  // og:title fallback for address parts the JSON-LD place node omits.
  const ogStreet = ogAddr.split(",")[0]?.trim() || null;
  const ogState = (ogAddr.match(/,\s*([A-Z]{2})\b/) ?? [])[1] ?? null;
  const ogZip = (ogAddr.match(/\b(\d{5})(?:-\d{4})?\b/) ?? [])[1] ?? null;
  const street = clean(addr.streetAddress) || ogStreet;
  const city = clean(addr.addressLocality);
  const state = clean(addr.addressRegion) || ogState;
  const postalCode = clean(addr.postalCode) || ogZip;
  const name = clean(place?.name) || clean(product?.name) || (ogAddr ? clean(ogAddr) : null) || street;
  if (!name && !street) return null; // not a parseable listing page

  const priceNum =
    offer?.price != null ? Number(String(offer.price).replace(/[^0-9.]/g, "")) || null : null;
  const sqft =
    place?.floorSize?.value != null ? Number(String(place.floorSize.value).replace(/[^0-9.]/g, "")) || null : null;
  const units =
    place?.numberOfRooms != null ? Number(String(place.numberOfRooms).replace(/[^0-9.]/g, "")) || null : null;

  // additionalProperty facts (cap rate, year built, GRM, ...)
  const ap: any[] = [];
  for (const n of [place, product]) for (const p of n?.additionalProperty ?? []) if (p?.name) ap.push(p);
  const apGet = (re: RegExp): string | null => {
    const f = ap.find((p) => re.test(String(p.name)));
    return f ? String(f.value) : null;
  };
  const capText = apGet(/cap\s*rate/i);
  const capRaw = capText ? Number((capText.match(/([0-9.]+)/) ?? [])[1]) || null : null;
  const capRatePct = capRaw && capRaw > 0 && capRaw <= 20 ? capRaw : null; // drop implausible / pro-forma outliers
  const yearText = apGet(/year built/i);
  const yearBuilt = yearText ? Number((yearText.match(/(\d{4})/) ?? [])[1]) || null : null;

  // photos: Product/Place LD image(s) + og:image, restricted to lyonstahl.com,
  // excluding headshots/logos/favicons/thumbnails.
  const photos: string[] = [];
  const seen = new Set<string>();
  const addImg = (v: any) => {
    const s = typeof v === "string" ? v : v?.url;
    if (typeof s !== "string") return;
    if (!/^https:\/\/(www\.)?lyonstahl\.com\//i.test(s)) return;
    if (/headshot|logo|favicon|placeholder|cropped-/i.test(s) || seen.has(s)) return;
    seen.add(s);
    photos.push(s);
  };
  for (const n of [product, place]) {
    const im = n?.image;
    if (Array.isArray(im)) im.forEach(addImg);
    else if (im) addImg(im);
  }
  addImg(metaContent(html, "og:image"));

  // brokers from Person LD nodes (name + jobTitle + headshot; no per-agent email/phone exposed)
  const brokerIds = nodes
    .filter((n) => ldType(n).includes("Person"))
    .map((p) =>
      brokerRef({
        name: clean(p.name),
        title: clean(p.jobTitle),
        avatarUrl: clean(typeof p.image === "string" ? p.image : p.image?.url),
        email: null,
        phone: null,
        company: "Lyon Stahl",
      })
    )
    .filter((v): v is number => v !== null);

  const slug = (url.split("/properties/")[1] ?? url).replace(/[/?#].*$/, "");
  return {
    id: slug || null,
    name,
    transactionType: "Sale",
    assetType: /Apartment|Residence/i.test(ldType(place)) ? "Multifamily" : null,
    description: metaContent(html, "og:description"),
    street,
    city,
    state,
    postalCode,
    country: clean(addr.addressCountry) || "US",
    salePriceUsd: priceNum,
    salePriceText: priceNum ? `$${priceNum.toLocaleString("en-US")}` : null,
    capRatePct,
    buildingSizeSqft: sqft,
    yearBuilt,
    units,
    brokerIds,
    photos,
    url,
  };
}

async function srcLyonStahl(tx: Tx, max: number): Promise<SourceResult> {
  const sourceUrl = "https://www.lyonstahl.com/properties/";
  if (tx === "lease") {
    return {
      company: "Lyon Stahl",
      sourceUrl,
      method: "Investment-sales only; no public lease inventory",
      totalAvailable: 0,
      listings: [],
      note: "Lyon Stahl markets multifamily investment sales; no lease feed.",
    };
  }
  const indexXml = await scrapeRaw("https://www.lyonstahl.com/sitemap.xml", { timeout: 60000 });
  const subSitemaps = Array.from(
    new Set(indexXml.match(/https?:\/\/[^<\s"']*properties-sitemap[0-9]+\.xml/gi) ?? [])
  );
  if (!subSitemaps.length) {
    throw new Error("Lyon Stahl: no properties-sitemap*.xml found in sitemap index");
  }
  const detailSet = new Set<string>();
  for (const sm of subSitemaps) {
    const xml = await scrapeRaw(sm, { timeout: 60000 });
    for (const u of xml.match(/https?:\/\/(?:www\.)?lyonstahl\.com\/properties\/[^<\s"')]+/gi) ?? []) {
      // keep firm detail URLs (/properties/{slug}/), drop the bare landing page
      if (/\/properties\/[^/]+\/?$/.test(u) && !/\/properties\/?$/.test(u)) detailSet.add(u);
    }
  }
  const urls = [...detailSet];
  if (!urls.length) throw new Error("Lyon Stahl: property sub-sitemaps contained no detail URLs");
  const take = Number.isFinite(max) ? urls.slice(0, max) : urls;
  let failures = 0;
  const parsed = await pmap(take, CONCURRENCY, async (u) => {
    try {
      return parseLyonStahlDetail(await scrapeRaw(u, { timeout: 60000 }), u, tx);
    } catch (err) {
      failures++;
      console.error(`  lyon-stahl/${tx}: ${u} failed: ${err}`);
      return null;
    }
  });
  // parsed nulls are intentional sold/off-market skips; only catch-block
  // failures count toward incompleteness.
  const listings = parsed.filter((l): l is any => l !== null);
  if (!listings.length) {
    throw new Error("Lyon Stahl: sitemap enumerated detail pages but none parsed as active listings");
  }
  return {
    company: "Lyon Stahl",
    sourceUrl,
    method:
      "WordPress sitemap-index enumeration -> own server-rendered detail pages, JSON-LD parsed (active sale only)",
    totalAvailable: urls.length,
    listings,
    incomplete: failures > 0,
    note: failures > 0 ? `${failures} detail page(s) failed to fetch` : undefined,
  };
}

// ---------- generic sitemap + LLM structured-extraction source ----------
// For firms with an own property sitemap but heterogeneous DOM and NO consistent
// JSON-LD (Interra, DAUM, Foundry, Essex, Pyramid, ...). Enumerate detail URLs
// from the sitemap, then use Firecrawl's `json` format (local LLM profile) to
// extract a fixed CRE schema per page — one approach across every theme.
// REQUIRES a configured LLM profile (OPENAI_API_KEY + OPENAI_BASE_URL +
// MODEL_NAME; e.g. set_model_profile.sh budget). Per-page LLM calls are slow and
// spend credits, so extraction is CACHED per firm across the sale/lease passes.

type SitemapExtractFirm = {
  company: string;
  host: string; // bare host for URL filtering + external_id slug
  sitemapUrl: string;
  detailPathRe: RegExp; // matches an individual listing URL path
};

const CRE_EXTRACT_SCHEMA = {
  type: "object",
  properties: {
    name: { type: ["string", "null"], description: "property/listing title" },
    streetAddress: { type: ["string", "null"] },
    city: { type: ["string", "null"] },
    state: { type: ["string", "null"], description: "2-letter US state code" },
    postalCode: { type: ["string", "null"] },
    transactionType: { type: "string", enum: ["sale", "lease", "sale_or_lease", "unknown"] },
    salePriceUsd: { type: ["number", "null"], description: "numeric USD sale price; null if not for sale or not disclosed" },
    leaseRateText: { type: ["string", "null"], description: "lease rate exactly as shown, e.g. '$25/SF/yr'; null if not for lease" },
    capRatePct: { type: ["number", "null"], description: "cap rate as a percent number, e.g. 6.5" },
    buildingSizeSqft: { type: ["number", "null"], description: "total building size in square feet as a full integer" },
    propertyType: { type: ["string", "null"], description: "office, retail, industrial, multifamily, land, etc." },
    description: { type: ["string", "null"] },
    brokers: {
      type: "array",
      items: {
        type: "object",
        properties: {
          name: { type: "string" },
          email: { type: ["string", "null"] },
          phone: { type: ["string", "null"] },
        },
      },
    },
  },
  required: ["name"],
} as const;

const CRE_EXTRACT_PROMPT =
  "Extract the single commercial real estate listing on this page. Use null for any field not clearly present; never guess. Set salePriceUsd only when the property is for sale and a numeric asking price is shown. Set leaseRateText only when an actual non-zero lease rate is shown. buildingSizeSqft is the full building square footage as an integer (e.g. 16500, not 16).";

async function scrapeExtract(url: string): Promise<any | null> {
  for (let attempt = 1; attempt <= 2; attempt++) {
    try {
      const doc: any = await firecrawl.scrape(url, {
        onlyMainContent: false,
        formats: [{ type: "json", prompt: CRE_EXTRACT_PROMPT, schema: CRE_EXTRACT_SCHEMA }],
        timeout: 120000,
      } as any);
      const json = doc.json ?? doc.data?.json ?? null;
      if (json && (clean(json.name) || clean(json.streetAddress))) return json;
    } catch (err) {
      console.error(`extract attempt ${attempt} failed for ${url}: ${err}`);
      await new Promise((r) => setTimeout(r, 2000 * attempt));
    }
  }
  return null;
}

// Map + sanitize an LLM extraction into the listing vocabulary. Guardrails drop
// the noise a cheap model produces on ambiguous numerics.
function sanitizeExtracted(j: any, url: string, firm: SitemapExtractFirm): any | null {
  const name = clean(j.name) || clean(j.streetAddress);
  if (!name) return null;
  const tt = String(j.transactionType ?? "").toLowerCase();
  // Validate the lease rate up front and require a real currency/rate signal, so a
  // hallucinated "1 acre"/"2025" string can neither be kept as a rate nor flip the
  // inferred tenure to lease.
  let leaseRateText = clean(j.leaseRateText);
  const looksLikeRate =
    !!leaseRateText &&
    /[1-9]/.test(leaseRateText) &&
    !/^\$?0(\.0+)?(\s|\/|$)/.test(leaseRateText) &&
    /\$|\bpsf\b|\bnnn\b|\/\s*sf|per\s*(?:sf|s\.?f\.?|square\s*f)|\/\s*(?:mo|month|yr|year|annum|ac\b)/i.test(
      leaseRateText
    );
  if (!looksLikeRate) leaseRateText = null;

  let _tt: "sale" | "lease" | "sale_or_lease";
  if (tt.includes("sale") && tt.includes("lease")) _tt = "sale_or_lease";
  else if (tt === "lease") _tt = "lease";
  else if (tt === "sale") _tt = "sale";
  // Unknown/blank tenure: infer ONLY from validated evidence; lease requires a real
  // (sanitized) rate, not a raw model string. Default to sale otherwise.
  else if (typeof j.salePriceUsd === "number" && j.salePriceUsd >= 1000) _tt = "sale";
  else if (leaseRateText) _tt = "lease";
  else _tt = "sale";
  const isSale = _tt === "sale" || _tt === "sale_or_lease";
  const salePriceUsd =
    isSale && typeof j.salePriceUsd === "number" && j.salePriceUsd >= 1000 ? j.salePriceUsd : null;
  const capRatePct =
    typeof j.capRatePct === "number" && j.capRatePct > 0 && j.capRatePct <= 20 ? j.capRatePct : null;
  const buildingSizeSqft =
    typeof j.buildingSizeSqft === "number" && j.buildingSizeSqft >= 100 ? j.buildingSizeSqft : null;
  const stRaw = clean(j.state);
  const state = stRaw ? (/^[A-Za-z]{2}$/.test(stRaw) ? stRaw.toUpperCase() : stRaw) : null;
  const brokerIds = (Array.isArray(j.brokers) ? j.brokers : [])
    .map((b: any) =>
      brokerRef({ name: clean(b?.name), email: clean(b?.email), phone: clean(b?.phone), company: firm.company })
    )
    .filter((x: number | null): x is number => x !== null);
  const slug = (url.split(firm.host)[1] ?? url).replace(/^\/+|\/+$/g, "").replace(/[?#].*$/, "");
  return {
    id: slug || null,
    name,
    transactionType: _tt === "sale_or_lease" ? "Sale/Lease" : _tt === "lease" ? "Lease" : "Sale",
    assetType: clean(j.propertyType),
    description: clean(j.description),
    street: clean(j.streetAddress),
    city: clean(j.city),
    state,
    postalCode: j.postalCode ? String(j.postalCode).slice(0, 12) : null,
    country: "US",
    salePriceUsd,
    salePriceText: salePriceUsd ? `$${salePriceUsd.toLocaleString("en-US")}` : null,
    leaseRateText: _tt === "sale" ? null : leaseRateText,
    capRatePct,
    buildingSizeSqft,
    brokerIds,
    url,
    _tt,
  };
}

async function enumerateSitemap(firm: SitemapExtractFirm): Promise<string[]> {
  const idxXml = await scrapeRaw(firm.sitemapUrl, { timeout: 60000 });
  const out = new Set<string>();
  const collect = (xml: string) => {
    for (const u of xml.match(/https?:\/\/[^<\s"')]+/gi) ?? []) {
      try {
        const url = new URL(u);
        if (url.hostname.replace(/^www\./, "") !== firm.host.replace(/^www\./, "")) continue;
        if (firm.detailPathRe.test(url.pathname)) out.add(u.replace(/[)\]]+$/, ""));
      } catch {
        /* skip malformed */
      }
    }
  };
  collect(idxXml);
  // Follow ALL same-host sub-sitemaps, not just propert/listing-named ones: a firm's
  // listing URLs can live in a generically-named child sitemap, and skipping it would
  // silently undercount while still looking "complete". detailPathRe filters what we
  // keep; property/listing-named children are fetched first so the cap (if ever hit)
  // drops the least-likely ones.
  const allSubs = [
    ...new Set(
      (idxXml.match(/https?:\/\/[^<\s"')]+\.xml/gi) ?? [])
        .map((s) => s.replace(/[)\]]+$/, ""))
        .filter((s) => {
          if (s === firm.sitemapUrl) return false;
          try {
            return new URL(s).hostname.replace(/^www\./, "") === firm.host.replace(/^www\./, "");
          } catch {
            return false;
          }
        })
    ),
  ].sort((a, b) => Number(/propert|listing/i.test(b)) - Number(/propert|listing/i.test(a)));
  const SUB_CAP = 60;
  if (allSubs.length > SUB_CAP) {
    console.error(
      `  ${firm.company}: ${allSubs.length} sub-sitemaps exceed cap ${SUB_CAP}; tail may be skipped (possible undercount)`
    );
  }
  for (const sm of allSubs.slice(0, SUB_CAP)) {
    try {
      collect(await scrapeRaw(sm, { timeout: 60000 }));
    } catch {
      /* tolerate a failed sub-sitemap */
    }
  }
  return [...out];
}

// Cache the extraction per firm so the sale + lease passes share one set of
// (paid) LLM calls. `max` bounds the number of detail pages extracted.
const extractCache = new Map<string, { listings: any[]; failed: number; total: number }>();

async function srcSitemapExtract(
  firm: SitemapExtractFirm,
  key: string,
  tx: Tx,
  max: number
): Promise<SourceResult> {
  let cached = extractCache.get(key);
  if (!cached) {
    const urls = await enumerateSitemap(firm);
    if (!urls.length) throw new Error(`${firm.company}: sitemap had no detail URLs matching ${firm.detailPathRe}`);
    const take = Number.isFinite(max) ? urls.slice(0, max) : urls;
    let failed = 0;
    const parsed = await pmap(take, CONCURRENCY, async (u) => {
      const j = await scrapeExtract(u);
      if (!j) {
        failed++;
        return null;
      }
      return sanitizeExtracted(j, u, firm);
    });
    cached = { listings: parsed.filter((l): l is any => l !== null), failed, total: urls.length };
    extractCache.set(key, cached);
  }
  const listings = cached.listings
    .filter((l) => (tx === "sale" ? l._tt !== "lease" : l._tt !== "sale"))
    .map((l) => {
      const { _tt, ...rest } = l;
      return rest;
    });
  return {
    company: firm.company,
    sourceUrl: firm.sitemapUrl,
    method: "Own sitemap enumeration + Firecrawl LLM structured extraction (json format) per detail page",
    totalAvailable: cached.total,
    listings,
    incomplete: cached.failed > 0,
    note: cached.failed > 0 ? `${cached.failed} page(s) failed extraction` : undefined,
  };
}

const SITEMAP_EXTRACT_FIRMS: Record<string, SitemapExtractFirm> = {
  "interra-realty": {
    company: "Interra Realty",
    host: "interrarealty.com",
    sitemapUrl: "https://www.interrarealty.com/sitemap.xml",
    detailPathRe: /^\/listing\/[^/]+\/?$/i,
  },
  "daum-commercial": {
    company: "DAUM Commercial",
    host: "daumcommercial.com",
    sitemapUrl: "https://www.daumcommercial.com/sitemap.xml",
    detailPathRe: /^\/property\/[^/]+\/?$/i,
  },
  "foundry-commercial": {
    company: "Foundry Commercial",
    host: "foundrycommercial.com",
    sitemapUrl: "https://www.foundrycommercial.com/sitemap.xml",
    detailPathRe: /^\/property\/[^/]+\/?$/i,
  },
  "essex-realty": {
    company: "Essex Realty Group",
    host: "essexrealtygroup.com",
    sitemapUrl: "https://www.essexrealtygroup.com/sitemap_index.xml",
    detailPathRe: /^\/properties\/[^/]+\/?$/i,
  },
  "pyramid-brokerage": {
    company: "Pyramid Brokerage Company",
    host: "pyramidbrokerage.com",
    sitemapUrl: "https://www.pyramidbrokerage.com/sitemap.xml",
    detailPathRe: /^\/listings\/[^/]+\/?$/i,
  },
  "shop-companies": {
    company: "SHOP Companies",
    host: "shopcompanies.com",
    sitemapUrl: "https://www.shopcompanies.com/sitemap.xml",
    detailPathRe: /^\/properties\/[^/]+\/?$/i,
  },
  "velocity-retail": {
    company: "Velocity Retail Group",
    host: "velocityretail.com",
    sitemapUrl: "https://www.velocityretail.com/sitemap_index.xml",
    detailPathRe: /^\/property\/[^/]+\/?$/i,
  },
  "aquila-commercial": {
    company: "AQUILA Commercial",
    host: "aquilacommercial.com",
    sitemapUrl: "https://www.aquilacommercial.com/sitemap.xml",
    detailPathRe: /^\/property\/[^/]+\/?$/i,
  },
  "finial-group": {
    company: "Finial Group",
    host: "finialgroup.com",
    sitemapUrl: "https://www.finialgroup.com/sitemap.xml",
    detailPathRe: /^\/properties\/[^/]+\/?$/i,
  },
  "ackerman": {
    company: "Ackerman & Co",
    host: "ackermanco.com",
    sitemapUrl: "https://www.ackermanco.com/sitemap_index.xml",
    detailPathRe: /^\/properties\/[^/]+\/?$/i,
  },
  "maury-carter": {
    company: "Maury L. Carter & Associates",
    host: "maurycarter.com",
    sitemapUrl: "https://www.maurycarter.com/sitemap.xml",
    detailPathRe: /^\/property\/[^/]+\/?$/i,
  },
};

// --- SRS Real Estate Partners: Salesforce-backed Cloud Run search API ---
// srsre.com is a Cloudflare-protected Next.js app, but its listings come from a
// PUBLIC Google Cloud Run backend (NOT behind Cloudflare), reverse-engineered
// 2026-06-20 from the page's JS bundle:
//   POST https://srsre-next-...run.app/api/property-search
//   body: { query: { offset: 12*page, pageSize: 12, ...UI_FILTERS }, client_ip: "" }
//   resp: { total, properties: [ { id, location, square_feet_data, permalink,
//           apto_data: <full Salesforce SRS_Listings__c record> } ] }
// The API is open (no auth), so this source calls it directly (global fetch), not
// through Firecrawl. UI_FILTERS below is the site's default "all options" filter
// (so the search is unfiltered = the whole catalog, ~2,122). Cached across the
// sale/lease passes and partitioned by each listing's Availability__c.
const SRS_API = "https://srsre-next-412955565034.us-central1.run.app/api/property-search";
const SRS_FILTERS = {
  availabilityType: ["sale", "lease", "investment-sale"],
  propertyType: ["retail", "industrial", "office", "land", "multifamily", "hospitality", "healthcare", "special_purpose"],
  address: null,
  tenant: "",
  ownershipType: ["fee-simple-land-building", "ground-lease-land-only", "leasehold-lease-only", "other"],
  portfolio: [] as string[],
  tenancyType: ["single-tenant", "multi-tenant", "land"],
  subType: null,
  orderDirection: "DESC",
  orderBy: "date",
  office: "",
  broker: "",
  sizeRange: { required: false },
  lotSizeRange: { required: false },
  priceRange: { required: true },
  capRateRange: { required: true },
  latLong: null,
  searchTerms: "",
};
const SRS_PAGE_SIZE = 12;
let srsCache: any[] | null = null;

async function srsPost(page: number): Promise<any> {
  const body = JSON.stringify({ query: { offset: SRS_PAGE_SIZE * page, pageSize: SRS_PAGE_SIZE, ...SRS_FILTERS }, client_ip: "" });
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const res = await fetch(SRS_API, {
        method: "POST",
        headers: { "Content-Type": "application/json", "User-Agent": "Mozilla/5.0", Origin: "https://www.srsre.com" },
        body,
      });
      if (!res.ok) throw new Error(`SRS API HTTP ${res.status}`);
      const j: any = await res.json();
      if (j && Array.isArray(j.properties)) return j;
      throw new Error("SRS API response missing properties[]");
    } catch (err) {
      if (attempt === 3) throw err;
      await new Promise((r) => setTimeout(r, 2000 * attempt));
    }
  }
}

async function srsFetchAll(max: number): Promise<{ items: any[]; total: number; incomplete: boolean }> {
  if (srsCache) return { items: srsCache, total: srsCache.length, incomplete: false };
  const first = await srsPost(0);
  const total: number = first.total ?? 0;
  const items: any[] = [...(first.properties ?? [])];
  const allPages = Math.ceil(total / SRS_PAGE_SIZE);
  // Bound fetches by max (shared across both tenure passes): a small probe pulls a
  // few pages; max=0 pulls the whole catalog.
  const wantPages = Number.isFinite(max) ? Math.min(allPages, Math.ceil((max * 4) / SRS_PAGE_SIZE) + 1) : allPages;
  let failed = 0;
  const pageNums = Array.from({ length: Math.max(0, wantPages - 1) }, (_, i) => i + 1);
  const chunks = await pmap(pageNums, CONCURRENCY, async (p) => {
    try {
      return (await srsPost(p)).properties ?? [];
    } catch (err) {
      failed++;
      console.error(`  srs: page ${p} failed: ${err}`);
      return [];
    }
  });
  for (const c of chunks) items.push(...c);
  const complete = wantPages >= allPages;
  if (complete) srsCache = items; // only cache a full pull
  return { items, total, incomplete: failed > 0 };
}

function srsTenure(x: any): { isSale: boolean; isLease: boolean } {
  const av = String(x?.apto_data?.Availability__c ?? "").toLowerCase();
  const pl = (x?.permalink ?? "").toLowerCase();
  const isLease = av.includes("lease") || /\/lease\//.test(pl);
  const isSale = av.includes("sale") || /\/sale\//.test(pl) || (!isLease); // default investment-sales
  return { isSale, isLease };
}

function mapSrs(x: any, tx: Tx): any {
  const a = x.apto_data ?? {};
  const num = (v: any) => (v != null && v !== "" && isFinite(Number(v)) ? Number(v) : null);
  const { isSale } = srsTenure(x);
  const salePrice = !a.Hide_Sale_Price__c ? num(a.Sale_Price__c) : null;
  const imgs: string[] = [];
  const li = a.listing_images;
  if (Array.isArray(li)) for (const im of li) {
    const u = typeof im === "string" ? im : im?.url ?? im?.src;
    if (typeof u === "string" && u.startsWith("http")) imgs.push(u);
  }
  if (!imgs.length && typeof x.thumbnail === "string" && x.thumbnail.startsWith("http")) imgs.push(x.thumbnail);
  const brokerIds = (Array.isArray(a.related_brokers) ? a.related_brokers : [])
    .map((b: any) =>
      brokerRef({
        name: clean(b?.name ?? b?.Name ?? [b?.FirstName, b?.LastName].filter(Boolean).join(" ")),
        email: clean(b?.email ?? b?.Email),
        phone: clean(b?.phone ?? b?.Phone),
        company: "SRS Real Estate Partners",
      })
    )
    .filter((v: number | null): v is number => v !== null);
  return {
    id: clean(a.SRS_Listings_ID__c) ?? clean(x.id) ?? clean(a.Id),
    name: clean(a.Name) || clean(a.Property_Address__c),
    transactionType: tx === "sale" ? "Sale" : "Lease",
    assetType: clean(a.Primary_Property_Type__c),
    description: clean(a.Description__c),
    street: clean(a.Property_Address__c) ?? clean(a.Postal_Address__Street__s),
    city: clean(a.Property_City__c) ?? clean(a["Postal_Address__City__s"]),
    state: clean(a.Property_State__c) ?? clean(a["Postal_Address__StateCode__s"]),
    postalCode: a.Property_Zip__c ? String(a.Property_Zip__c).slice(0, 12) : clean(a["Postal_Address__PostalCode__s"]),
    country: "US",
    latitude: num(x.location?.lat) ?? num(a.Property_Latitude__c) ?? num(a.Latitude__c),
    longitude: num(x.location?.lon) ?? num(a.Property_Longitude__c) ?? num(a.Longitude__c),
    salePriceUsd: isSale ? salePrice : null,
    salePriceText: isSale && salePrice ? `$${salePrice.toLocaleString("en-US")}` : null,
    capRatePct: num(a.Cap_Rate__c),
    buildingSizeSqft: num(a.Total_Property_SF_GLA__c) ?? num(a.square_footage),
    lotSizeAcres: num(a.Total_Property_Land_Acres__c) ?? num(a.lot_size_acres),
    yearBuilt: num(a.Year_Built__c),
    sizeText: clean(x.square_feet_data),
    brokerIds,
    photos: imgs.slice(0, 12),
    url: x.permalink ? `https://www.srsre.com${x.permalink}` : null,
  };
}

async function srcSrs(tx: Tx, max: number): Promise<SourceResult> {
  const { items, total, incomplete } = await srsFetchAll(max);
  const listings: any[] = [];
  for (const x of items) {
    if (listings.length >= max) break;
    const { isSale, isLease } = srsTenure(x);
    if (tx === "sale" && !isSale) continue;
    if (tx === "lease" && !isLease) continue;
    listings.push(mapSrs(x, tx));
  }
  return {
    company: "SRS Real Estate Partners",
    sourceUrl: "https://www.srsre.com/properties",
    method: "Salesforce-backed Cloud Run search API (POST /api/property-search, paginated, direct)",
    totalAvailable: total,
    listings,
    incomplete,
  };
}

// --- Hanley Investment Group: embedded rethink_properties JSON on /listings/ ---
// Retail net-lease investment-sales firm on the Rethink (Salesforce) CRE platform.
// The /listings/ page is directly fetchable (Cloudflare in monitor-mode, not
// blocking) and server-embeds the WHOLE catalog in `var rethink_properties = [...]`.
// No render, no API reverse-engineering — fetch the page, parse the array, partition
// by deal record type (Seller/Buyer_Rep = sale; Landlord/Tenant_Rep = lease).
const HANLEY_URL = "https://hanleyinvestmentgroup.com/listings/";

function extractRethinkProperties(html: string): any[] {
  const i = html.indexOf("rethink_properties");
  if (i < 0) return [];
  const start = html.indexOf("[", i);
  if (start < 0) return [];
  let depth = 0,
    end = -1,
    inStr = false,
    esc = false;
  for (let j = start; j < html.length; j++) {
    const c = html[j];
    if (inStr) {
      if (esc) esc = false;
      else if (c === "\\") esc = true;
      else if (c === '"') inStr = false;
    } else if (c === '"') inStr = true;
    else if (c === "[") depth++;
    else if (c === "]") {
      depth--;
      if (depth === 0) {
        end = j + 1;
        break;
      }
    }
  }
  if (end < 0) return [];
  try {
    return JSON.parse(html.slice(start, end));
  } catch {
    return [];
  }
}

async function fetchHanleyHtml(): Promise<string> {
  try {
    const res = await fetch(HANLEY_URL, {
      headers: { "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36", Accept: "text/html" },
    });
    if (res.ok) {
      const html = await res.text();
      if (html.includes("rethink_properties")) return html;
    }
  } catch {
    /* fall back to Firecrawl */
  }
  return scrapeRaw(HANLEY_URL, { proxy: "stealth", waitFor: 3000 });
}

function hanleyIsLease(x: any): boolean {
  const tags = [String(x.dealRecordType ?? ""), ...(Array.isArray(x.dealPipelineTypes) ? x.dealPipelineTypes : [])].join(",").toLowerCase();
  return /landlord|tenant|lease/.test(tags);
}

function mapHanley(x: any, tx: Tx): any {
  const num = (v: any) => (v != null && v !== "" && isFinite(Number(v)) ? Number(v) : null);
  const isLease = hanleyIsLease(x);
  const sf = num(x.propertySquareFootage) ?? num(x.spaceSquareFootage);
  return {
    id: clean(x.id),
    name: clean(x.name) || clean(x.address),
    transactionType: tx === "sale" ? "Sale" : "Lease",
    assetType: clean(x.propertyType) ?? clean(x.propertyRecordType),
    street: clean(x.address),
    city: clean(x.city),
    state: clean(x.state),
    postalCode: x.zipCode ? String(x.zipCode).slice(0, 12) : null,
    country: "US",
    latitude: num(x.latitude),
    longitude: num(x.longitude),
    salePriceUsd: !isLease ? num(x.salesPrice) : null,
    salePriceText: !isLease && num(x.salesPrice) ? `$${Number(x.salesPrice).toLocaleString("en-US")}` : null,
    leaseRateText: isLease ? clean(String(x.leaseRate ?? "")) || null : null,
    capRatePct: num(x.capRate),
    buildingSizeSqft: sf,
    sizeText: sf ? `${sf.toLocaleString("en-US")} SF` : null,
    brokerIds: [], // array exposes only leadBrokerUserId (an id, no name)
    photos: typeof x.image === "string" && x.image.startsWith("http") ? [x.image] : [],
    url: x.id ? `${HANLEY_URL}?id=${x.id}` : HANLEY_URL,
    status: clean(x.status),
    numberOfUnits: num(x.numberOfUnits),
  };
}

let hanleyCache: any[] | null = null;
async function srcHanley(tx: Tx, max: number): Promise<SourceResult> {
  if (!hanleyCache) {
    const arr = extractRethinkProperties(await fetchHanleyHtml());
    if (!arr.length) throw new Error("Hanley: rethink_properties array not found / empty");
    hanleyCache = arr.filter((x) => String(x.visibility ?? "").toLowerCase().startsWith("public"));
  }
  const listings: any[] = [];
  for (const x of hanleyCache) {
    if (listings.length >= max) break;
    if (tx === "lease" ? !hanleyIsLease(x) : hanleyIsLease(x)) continue;
    listings.push(mapHanley(x, tx));
  }
  return {
    company: "Hanley Investment Group",
    sourceUrl: HANLEY_URL,
    method: "Direct fetch of /listings/ + embedded rethink_properties JSON (Rethink/Salesforce platform)",
    totalAvailable: hanleyCache.length,
    listings,
  };
}

// --- Kidder Mathews: open "KM backend" search API ---
// kidder.com/properties/ is a jQuery app whose listings come from a PUBLIC backend
// (services.kidder.com), reverse-engineered 2026-06-20 from /properties/assets/js/
// app.min.js: POST https://services.kidder.com/search/public/listing with
//   { startIndex, numResults, includeAggregations:false }  (the SearchRequest shape)
//   -> { totalResultCount, results:[ {listing_key, property_*, list_price,
//        asking_rent_max, sf_avail, use_type, brokers:[names], lat/lon, photos} ] }
// Open API (no auth). Direct paginated fetch (not Firecrawl). ~3,108 listings.
const KIDDER_API = "https://services.kidder.com/search/public/listing";
const KIDDER_PAGE = 50;
let kidderCache: any[] | null = null;

async function kidderPost(startIndex: number): Promise<any> {
  const body = JSON.stringify({ startIndex, numResults: KIDDER_PAGE, includeAggregations: false });
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const res = await fetch(KIDDER_API, {
        method: "POST",
        headers: { "Content-Type": "application/json;charset=UTF-8", "User-Agent": "Mozilla/5.0", Origin: "https://www.kidder.com" },
        body,
      });
      if (!res.ok) throw new Error(`Kidder API HTTP ${res.status}`);
      const j: any = await res.json();
      if (j && Array.isArray(j.results)) return j;
      throw new Error("Kidder API response missing results[]");
    } catch (err) {
      if (attempt === 3) throw err;
      await new Promise((r) => setTimeout(r, 2000 * attempt));
    }
  }
}

async function kidderFetchAll(max: number): Promise<{ items: any[]; total: number; incomplete: boolean }> {
  if (kidderCache) return { items: kidderCache, total: kidderCache.length, incomplete: false };
  const first = await kidderPost(0);
  const total: number = first.totalResultCount ?? 0;
  const items: any[] = [...(first.results ?? [])];
  const allPages = Math.ceil(total / KIDDER_PAGE);
  const wantPages = Number.isFinite(max) ? Math.min(allPages, Math.ceil((max * 4) / KIDDER_PAGE) + 1) : allPages;
  let failed = 0;
  const pageNums = Array.from({ length: Math.max(0, wantPages - 1) }, (_, i) => i + 1);
  const chunks = await pmap(pageNums, CONCURRENCY, async (p) => {
    try {
      return (await kidderPost(p * KIDDER_PAGE)).results ?? [];
    } catch (err) {
      failed++;
      console.error(`  kidder: page ${p} failed: ${err}`);
      return [];
    }
  });
  for (const c of chunks) items.push(...c);
  if (wantPages >= allPages) kidderCache = items;
  return { items, total, incomplete: failed > 0 };
}

function kidderTenure(x: any): { isSale: boolean; isLease: boolean } {
  const isLease = x.asking_rent_max != null || x.sublease_flg === true;
  const isSale = x.list_price != null || x.retail_investment_nnn_flg === true || !isLease;
  return { isSale, isLease };
}

function mapKidder(x: any, tx: Tx): any {
  const num = (v: any) => (v != null && v !== "" && isFinite(Number(v)) ? Number(v) : null);
  const { isSale } = kidderTenure(x);
  const sale = isSale ? num(x.list_price) : null;
  const rent = num(x.asking_rent_max);
  const brokerIds = (Array.isArray(x.brokers) ? x.brokers : [])
    .map((b: any) => brokerRef({ name: clean(typeof b === "string" ? b : b?.name), company: "Kidder Mathews" }))
    .filter((v: number | null): v is number => v !== null);
  const photos = [x.listing_photo, x.property_photo].filter((u: any) => typeof u === "string" && u.startsWith("http")).slice(0, 2);
  const key = x.listing_key ?? x.property_key;
  return {
    id: key != null ? String(key) : null,
    name: clean(x.property_name) || clean(x.building_name) || clean(x.property_address),
    transactionType: tx === "sale" ? "Sale" : "Lease",
    assetType: clean(x.use_type),
    street: clean(x.property_address),
    city: clean(x.city),
    state: clean(x.state_code),
    postalCode: x.zip_postal_code ? String(x.zip_postal_code).slice(0, 12) : null,
    country: "US",
    latitude: num(x.latitude),
    longitude: num(x.longitude),
    salePriceUsd: sale,
    salePriceText: sale ? `$${sale.toLocaleString("en-US")}` : null,
    leaseRateText: !isSale && rent != null ? `$${rent}/SF` : null,
    buildingSizeSqft: num(x.sf_avail),
    sizeText: num(x.sf_avail) ? `${num(x.sf_avail)!.toLocaleString("en-US")} SF` : null,
    brokerIds,
    photos,
    url: key != null ? `https://www.kidder.com/listings/${key}` : "https://www.kidder.com/properties/",
  };
}

async function srcKidder(tx: Tx, max: number): Promise<SourceResult> {
  const { items, total, incomplete } = await kidderFetchAll(max);
  const listings: any[] = [];
  for (const x of items) {
    if (listings.length >= max) break;
    const { isSale, isLease } = kidderTenure(x);
    if (tx === "sale" && !isSale) continue;
    if (tx === "lease" && !isLease) continue;
    listings.push(mapKidder(x, tx));
  }
  return {
    company: "Kidder Mathews",
    sourceUrl: "https://www.kidder.com/properties/",
    method: "Open KM backend search API (POST services.kidder.com/search/public/listing, paginated, direct)",
    totalAvailable: total,
    listings,
    incomplete,
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
    case "matthews":
      return srcMatthews(tx, max);
    case "franklin-street": {
      // Client-rendered Buildout (buildout.com/api.js) with SEPARATE for-sale
      // and for-lease plugin feeds (each internally single-tenure). Pick the
      // feed by transaction; tokens read from /properties/for-{sale,lease}/ on
      // 2026-06-20 (sale feed 227, lease feed 195). srcBuildout's sale-boolean
      // filter still applies as a safety net.
      const fsToken =
        tx === "lease"
          ? "2f82fcd26667c4b0126d0084938ffa265f05fa4a"
          : "a234450b432b2b2bebc1ace7e6f692e4489bde70";
      return srcBuildout("Franklin Street", fsToken, "https://www.franklinst.com/properties/", tx, max);
    }
    case "lyon-stahl":
      return srcLyonStahl(tx, max);
    case "srs":
      return srcSrs(tx, max);
    case "hanley":
      return srcHanley(tx, max);
    case "kidder-mathews":
      return srcKidder(tx, max);
    case "faris-lee":
      // Retail net-lease investment-sales firm on Buildout. Public plugin
      // inventory API (token read from /listings on 2026-06-20); all sale.
      return srcBuildout(
        "Faris Lee Investments",
        "de89d4f043da3999d293e1adcfd541bf2530acca",
        "https://www.farislee.com/listings/",
        tx,
        max
      );
    case "fortis-net-lease":
      // Net-lease investment-sales firm on Buildout. Public plugin inventory
      // API (token read from /net-lease-properties on 2026-06-20); all sale.
      return srcBuildout(
        "Fortis Net Lease",
        "8c286e4a49fdc706359ab9c041e0db1465de1fcf",
        "https://www.fortisnetlease.com/net-lease-properties/",
        tx,
        max
      );
    default: {
      const bf = BUILDOUT_FIRMS[key];
      if (bf) return srcBuildout(bf.company, bf.token, bf.page, tx, max);
      const ef = SITEMAP_EXTRACT_FIRMS[key];
      if (ef) return srcSitemapExtract(ef, key, tx, max);
      throw new Error(`unhandled source ${key}`);
    }
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
          incomplete: res.incomplete ?? false,
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
