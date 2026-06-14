// sources/colliers-main.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { appendFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname } from "node:path";
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { decodeHtmlEntities, dedupeStrings, extractSitemapUrlEntries } from "../lib/html.js";
import { scrapeDoc, scrapeRaw } from "../lib/scrape.js";
import { ScrapeOpts, ScrapedDoc, SourceResult, Tx } from "../types.js";
import { boundedInt, clean, moneyToNumber, num, pmap, prune } from "../lib/util.js";


// --- Colliers main site: public XML sitemap discovery + detail-page render ---
// Unlock 2026-06-12: the bare /sitemap path (not sitemap.xml) is reachable
// through local Firecrawl, and its en ?type=properties child sitemap lists every
// usa####### detail URL with lastmod. Detail pages render with a
// RealEstateListing JSON-LD block plus clean markdown. This folds into the
// `colliers` brokerage as `colliers-main` (main: id prefix), leaving the
// SalesTracker `colliers` source untouched. No Coveo POST, auth, or gated path.
// See cre_scrapers/brokers/colliers/COLLIERS_MAIN_SITEMAP_UNLOCK_2026-06-12.md.

export const COLLIERS_MAIN_HOST = "https://www.colliers.com";
export const COLLIERS_MAIN_SITEMAP_INDEX = `${COLLIERS_MAIN_HOST}/sitemap`;
export const COLLIERS_MAIN_SOURCE_URL = `${COLLIERS_MAIN_HOST}/en/properties`;
export const COLLIERS_MAIN_DETAIL_CONCURRENCY = boundedInt(
  process.env.COLLIERS_MAIN_DETAIL_CONCURRENCY,
  Math.min(CONCURRENCY, 3),
  1,
  6
);
// Colliers detail pages sit behind Cloudflare; under sustained paging the site
// returns 429 "Just a moment..." challenge shells. A waitFor lets the stealth
// browser clear the challenge (same approach as CBRE waitFor 4000 / JLL 8000).
export const COLLIERS_MAIN_DETAIL_WAIT_MS = boundedInt(process.env.COLLIERS_MAIN_DETAIL_WAIT_MS, 4000, 0, 30000);
export const COLLIERS_MAIN_SCRAPE_OPTS: ScrapeOpts = {
  proxy: "stealth",
  timeout: 120000,
  ...(COLLIERS_MAIN_DETAIL_WAIT_MS ? { waitFor: COLLIERS_MAIN_DETAIL_WAIT_MS } : {}),
};

export type ColliersMainEntry = { url: string; lastmod: string | null; id: string };

export let colliersMainSitemapCache: ColliersMainEntry[] | null = null;
export let colliersMainEnrichedMemo: any[] | null = null;

export function colliersMainIsChallenge(doc: ScrapedDoc): boolean {
  const status = doc.metadata?.statusCode;
  if (status === 429 || status === 503) return true;
  const title = (clean(doc.metadata?.title) ?? "").toLowerCase();
  if (/just a moment|attention required|checking your browser|cf-browser-verification/i.test(title)) {
    return true;
  }
  const head = (doc.rawHtml ?? "").slice(0, 4000);
  return /cf-chl-|challenge-platform|_cf_chl_opt|just a moment/i.test(head);
}

// Colliers detail pages are Cloudflare-protected; under sustained paging the
// site returns 429 "Just a moment..." challenge shells. The stealth proxy
// rotates IPs per request, so retrying a challenged URL with backoff usually
// lands on an un-challenged IP. Returns the last doc even if still challenged;
// the parser then tombstones a real 404 or throws for retry on the next pass.
export async function scrapeColliersMainDetailDoc(url: string): Promise<ScrapedDoc> {
  const maxAttempts = boundedInt(process.env.COLLIERS_MAIN_CHALLENGE_RETRIES, 4, 1, 8);
  let doc = await scrapeDoc(url, COLLIERS_MAIN_SCRAPE_OPTS);
  for (let attempt = 2; attempt <= maxAttempts && colliersMainIsChallenge(doc); attempt++) {
    const backoff = 4000 * (attempt - 1) + Math.floor(Math.random() * 3000);
    await new Promise((r) => setTimeout(r, backoff));
    doc = await scrapeDoc(url, COLLIERS_MAIN_SCRAPE_OPTS);
  }
  return doc;
}

export function colliersMainAbs(href: string | null | undefined): string | null {
  const h = clean(href);
  if (!h || /^(javascript:|mailto:|tel:|#)/i.test(h)) return null;
  try {
    return new URL(decodeHtmlEntities(h), COLLIERS_MAIN_HOST).toString();
  } catch {
    return null;
  }
}

export function colliersMainIdFromUrl(url: string): string | null {
  const m = url.match(/\/(usa\d{5,})(?:[/?#]|$)/i);
  return m ? m[1].toLowerCase() : null;
}

export function extractSitemapLocs(xml: string): string[] {
  return [...xml.matchAll(/<loc>\s*([^<]+?)\s*<\/loc>/g)].map((m) => decodeHtmlEntities(m[1]).trim());
}

export async function fetchColliersMainEntries(): Promise<ColliersMainEntry[]> {
  if (colliersMainSitemapCache) return colliersMainSitemapCache;
  const indexXml = await scrapeRaw(COLLIERS_MAIN_SITEMAP_INDEX, COLLIERS_MAIN_SCRAPE_OPTS);
  const childLocs = extractSitemapLocs(indexXml);
  const propsSitemap = childLocs.find((l) => /\/en\/sitemap\?type=properties\b/i.test(l));
  if (!propsSitemap) {
    throw new Error("Colliers main: en ?type=properties sitemap not found in sitemap index");
  }
  const propsXml = await scrapeRaw(propsSitemap, COLLIERS_MAIN_SCRAPE_OPTS);
  const seen = new Set<string>();
  const entries: ColliersMainEntry[] = [];
  for (const e of extractSitemapUrlEntries(propsXml)) {
    const id = colliersMainIdFromUrl(e.loc);
    if (!id || seen.has(id)) continue;
    seen.add(id);
    entries.push({ url: e.loc, lastmod: e.lastmod, id });
  }
  if (!entries.length) {
    throw new Error("Colliers main: ?type=properties sitemap had no usa####### detail URLs");
  }
  console.error(`  colliers-main: sitemap exposed ${entries.length} US property detail URL(s)`);
  colliersMainSitemapCache = entries;
  return entries;
}

// Classify transaction from the JSON-LD name first, then markdown header, then
// the URL slug. Returns a transactionType string plus a sublease flag.
export function colliersMainTransaction(
  ldName: string | null,
  markdown: string,
  url: string
): { type: "Sale" | "Lease" | "Sale/Lease"; sublease: boolean } {
  const name = (ldName ?? "").toLowerCase();
  const head = markdown.slice(0, 4000).toLowerCase();
  const slug = url.toLowerCase();
  const both = /(for sale or lease|for lease or sale|sale\/lease|sale or ground lease|for sale and lease)/;
  const sub = /sublease/;
  const lease = /(for lease|for rent|ground lease)/;
  const sale = /for sale/;
  const isSub = sub.test(name) || sub.test(head) || /sublease/.test(slug);
  if (both.test(name) || both.test(head) || /sale-or-lease|lease-or-sale/.test(slug)) {
    return { type: "Sale/Lease", sublease: isSub };
  }
  // Prefer the JSON-LD name signal (most reliable), then markdown, then slug.
  for (const hay of [name, head, slug]) {
    const hasSale = sale.test(hay) || /for-sale/.test(hay);
    const hasLease = lease.test(hay) || /for-lease|for-rent/.test(hay);
    if (hasSale && !hasLease) return { type: "Sale", sublease: isSub };
    if (hasLease && !hasSale) return { type: isSub ? "Lease" : "Lease", sublease: isSub };
  }
  if (isSub) return { type: "Lease", sublease: true };
  // No clear signal; default to Sale (Colliers leans investment-sale) but keep
  // the raw name in colliersMain for audit.
  return { type: "Sale", sublease: false };
}

export function parseColliersMainAddress(addr: string | null): {
  street: string | null;
  city: string | null;
  state: string | null;
  postalCode: string | null;
  country: string | null;
} {
  const out = {
    street: null as string | null,
    city: null as string | null,
    state: null as string | null,
    postalCode: null as string | null,
    country: null as string | null,
  };
  if (!addr) return out;
  let s = addr.trim();
  if (/,\s*(USA|United States|US)\s*$/i.test(s)) {
    out.country = "US";
    s = s.replace(/,\s*(USA|United States|US)\s*$/i, "").trim();
  } else if (/,\s*Canada\s*$/i.test(s)) {
    out.country = "CA";
    s = s.replace(/,\s*Canada\s*$/i, "").trim();
  }
  const splitHead = (head: string) => {
    const hp = head
      .split(",")
      .map((p) => p.trim())
      .filter(Boolean);
    if (hp.length >= 2) {
      out.city = hp.pop()!;
      out.street = hp.join(", ");
    } else if (hp.length === 1) {
      out.city = hp[0];
    }
  };
  const withZip = s.match(/^(.*?),\s*([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)\s*$/);
  if (withZip) {
    out.state = withZip[2].toUpperCase();
    out.postalCode = withZip[3];
    splitHead(withZip[1].trim());
    return out;
  }
  const noZip = s.match(/^(.*?),\s*([A-Za-z]{2})\s*$/);
  if (noZip) {
    out.state = noZip[2].toUpperCase();
    splitHead(noZip[1].trim());
    return out;
  }
  // Fallback: treat the last comma segment as city, the rest as street.
  splitHead(s);
  return out;
}

export function colliersMainJsonLd(rawHtml: string): any | null {
  for (const m of rawHtml.matchAll(/<script[^>]*application\/ld\+json[^>]*>([\s\S]*?)<\/script>/g)) {
    try {
      const obj = JSON.parse(m[1].trim());
      const arr = Array.isArray(obj) ? obj : [obj];
      const found = arr.find((o: any) => o && o["@type"] === "RealEstateListing");
      if (found) return found;
    } catch {
      // skip malformed block
    }
  }
  return null;
}

export function parseColliersMainDetail(entry: ColliersMainEntry, doc: ScrapedDoc): any {
  const raw = doc.rawHtml ?? "";
  const md = doc.markdown ?? "";
  const ld = colliersMainJsonLd(raw);
  if (!ld) {
    // Live listings always carry a RealEstateListing JSON-LD block. Pages
    // without one fall into three cases, handled distinctly:
    const title = clean(doc.metadata?.title) ?? "";
    const status = doc.metadata?.statusCode;
    const notFound =
      /property not found|page not found|410 gone/i.test(title) ||
      /Property Not Found/i.test(md.slice(0, 3000)) ||
      status === 404 ||
      status === 410;
    if (notFound) {
      // 1) Expired/removed listing (the sitemap lags). Tombstone so we neither
      //    re-fetch it nor emit it.
      return {
        id: entry.id,
        url: entry.url,
        skip: "not_found",
        lastUpdated: entry.lastmod ? entry.lastmod.slice(0, 10) : null,
      };
    }
    if (colliersMainIsChallenge(doc)) {
      // 2) Still a Cloudflare challenge after the retry wrapper exhausted its
      //    attempts. Throw so it is retried (un-cached) on the next pass.
      throw new Error(
        `Colliers main detail still challenged (status ${status ?? "?"}, title "${title.slice(0, 60)}")`
      );
    }
    // 3) A real 200 page that lacks the standard RealEstateListing JSON-LD
    //    (rare alternate template, e.g. "Powered by LightBox"). Permanent, so
    //    tombstone to avoid re-fetching it every run. ~0.1% in sampling.
    return {
      id: entry.id,
      url: entry.url,
      skip: "no_structured_data",
      lastUpdated: entry.lastmod ? entry.lastmod.slice(0, 10) : null,
    };
  }
  const ldName: string | null = clean(ld?.name);

  // ldName: "Office For sale — 11701 I-30, Little Rock, AR 72209, USA | United States | Colliers"
  let typeWord: string | null = null;
  let addrStr: string | null = null;
  if (ldName) {
    const beforePipe = ldName.split("|")[0].trim();
    const dash = beforePipe.split(/\s[—–-]\s/);
    if (dash.length >= 2) {
      typeWord = clean(
        dash[0].replace(/\bfor\s+(sale or lease|lease or sale|sale|lease|sublease|rent).*$/i, "").trim()
      );
      addrStr = dash.slice(1).join(" - ").trim();
    } else {
      typeWord = clean(beforePipe);
    }
  }
  const addr = parseColliersMainAddress(addrStr);
  const tx = colliersMainTransaction(ldName, md, entry.url);

  const priceMatch = md.match(/\$\s?[\d,]+(?:\.\d+)?\s*(?:USD)?/);
  const salePriceText = tx.type === "Lease" ? null : priceMatch ? clean(priceMatch[0]) : null;
  let leaseRateText: string | null = null;
  if (tx.type !== "Sale") {
    const lr = md.match(/\$[\d,.]+\s*(?:\/|per\s*)\s*(?:SF|sq\.?\s*ft)[^\n]{0,24}/i);
    leaseRateText = lr ? clean(lr[0]) : null;
  }

  const bsf = md.match(/Building Size:\s*([\d,]+)\s*SF/i)?.[1];
  const land = md.match(/Land Area:\s*([\d.,]+)\s*ac/i)?.[1];
  const ptype =
    clean(md.match(/\*\*Property Types?\*\*\s*([A-Za-z0-9 ,/&'-]+)/i)?.[1]) ??
    clean(ld?.about?.category) ??
    typeWord;
  const status = clean(md.match(/\*\*Property Status\*\*\s*([A-Za-z ,/-]+)/i)?.[1]);

  const coord =
    raw.match(/[?&]q=(-?\d{1,3}\.\d+),\s*(-?\d{1,3}\.\d+)/) ??
    md.match(/maps\?q=(-?\d{1,3}\.\d+),\s*(-?\d{1,3}\.\d+)/);
  const lat = coord ? num(Number(coord[1])) : null;
  const lng = coord ? num(Number(coord[2])) : null;

  const photos = dedupeStrings(
    [...raw.matchAll(/https:\/\/listingsprod\.blob\.core\.windows\.net\/ourlistings-[a-z]+\/[^\s"'<>)]+/g)].map(
      (m) => m[0]
    )
  ).filter((u) => !/\.pdf(\?|$)/i.test(u));

  const docs: Array<{ name: string | null; url: string }> = [];
  const seenDocs = new Set<string>();
  for (const m of md.matchAll(/\[([^\]]+)\]\((https:\/\/listingsprod\.blob\.core\.windows\.net\/[^\)]+)\)/g)) {
    const name = clean(m[1]);
    const u = clean(m[2]);
    if (!u || seenDocs.has(u)) continue;
    const looksDoc =
      /\.pdf(\?|$)/i.test(u) ||
      /\.(pdf|docx?|xlsx?|zip|pptx?)$/i.test(name ?? "") ||
      /\b(pib|brochure|flyer|om|offering|memorandum|document|package|marketing|deck|teaser)\b/i.test(name ?? "");
    if (looksDoc) {
      seenDocs.add(u);
      docs.push({ name, url: u });
    }
  }

  const $ = cheerio.load(raw);
  const contactsDetailed: any[] = [];
  const brokerIds: number[] = [];
  const seenContacts = new Set<string>();
  $(".expert-card").each((_, el) => {
    const card = $(el);
    const name = clean(card.find(".expert-card__name").first().text());
    const profileUrl = colliersMainAbs(
      card.find(".expert-card__name a, .expert-card__image a").first().attr("href")
    );
    const title = clean(card.find(".expert-card__title").first().text());
    const office = clean(card.find(".expert-card__office").first().text());
    const phone = clean(
      card
        .find('.expert-card__phone a[href^="tel:"]')
        .first()
        .attr("href")
        ?.replace(/^tel:/i, "")
    );
    const avatarUrl = clean(card.find(".expert-card__image img").first().attr("src"));
    const key = profileUrl ?? name ?? phone;
    if (!key || seenContacts.has(key)) return;
    if (!name && !phone && !profileUrl) return;
    seenContacts.add(key);
    contactsDetailed.push({ name, title, office, phone, company: "Colliers", profileUrl, avatarUrl });
    const id = brokerRef({ name, phone, office, avatarUrl, company: "Colliers" });
    if (id !== null) brokerIds.push(id);
  });

  const h1 = clean($("h1").first().text());
  const name =
    (addr.street ? `${addr.street}${addr.city ? ", " + addr.city : ""}` : null) ?? h1 ?? ldName ?? entry.url;
  const description = clean($('meta[name="description"]').attr("content"));

  return prune({
    // Bare usa####### id; cre_ingest folds it into the colliers brokerage with
    // the configured "main:" prefix (mirrors the cbre-dealflow pattern).
    id: entry.id,
    name,
    headline: h1,
    transactionType: tx.type,
    assetType: ptype,
    description,
    street: addr.street,
    city: addr.city,
    state: addr.state,
    postalCode: addr.postalCode,
    country: addr.country ?? "US",
    latitude: lat,
    longitude: lng,
    salePriceUsd: salePriceText ? moneyToNumber(salePriceText) : null,
    salePriceText,
    leaseRateText,
    sizeText: clean(md.match(/Building Size:[^\n|]+/i)?.[0]),
    buildingSizeSqft: bsf ? num(Number(bsf.replace(/,/g, ""))) : null,
    lotSizeAcres: land ? num(Number(land.replace(/,/g, ""))) : null,
    brokerIds: brokerIds.length ? brokerIds : undefined,
    contactsDetailed: contactsDetailed.length ? contactsDetailed : undefined,
    brochures: docs,
    photos,
    url: entry.url,
    lastUpdated: entry.lastmod ? entry.lastmod.slice(0, 10) : null,
    colliersMain: {
      propertyStatus: status,
      sublease: tx.sublease,
      jsonLdName: ldName,
      docCount: docs.length,
      photoCount: photos.length,
      contactCount: contactsDetailed.length,
    },
  });
}

export function colliersMainDetailCachePath(): string {
  // Durable, OUT_PATH-independent so a long run can resume across attempts.
  return "out/cache/colliers-main/detail-cache.jsonl";
}

export function readColliersMainCache(path: string): Map<string, any> {
  const cached = new Map<string, any>();
  if (!existsSync(path)) return cached;
  for (const line of readFileSync(path, "utf8").split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const listing = JSON.parse(line);
      if (listing?.detailError) continue;
      const id = clean(listing?.id);
      if (id) cached.set(id.replace(/^main:/, ""), listing);
    } catch {
      // Ignore a partial final line from an interrupted prior run.
    }
  }
  return cached;
}

export function appendColliersMainCache(path: string, listing: any): void {
  if (listing?.detailError) return;
  mkdirSync(dirname(path), { recursive: true });
  appendFileSync(path, `${JSON.stringify(listing)}\n`);
}

// Enrich the full sitemap once (memoized across the sale and lease passes and
// backed by a durable JSONL cache), then srcColliersMain filters per pass.
export async function colliersMainEnrichAll(max: number): Promise<any[]> {
  if (colliersMainEnrichedMemo) return colliersMainEnrichedMemo;
  const entries = await fetchColliersMainEntries();
  const want = max && max > 0 ? Math.min(max, entries.length) : entries.length;
  const selected = entries.slice(0, want);
  const cachePath = colliersMainDetailCachePath();
  const cached = readColliersMainCache(cachePath);
  if (cached.size) {
    console.error(`  colliers-main: loaded ${cached.size} cached detail row(s) from ${cachePath}`);
  }
  // Per-run cap on NEW detail fetches. Each detail render leaks ~0.8 MB in the
  // fetch/SDK layer, so an unbounded ~15.9k-URL run exhausts the V8 heap. With a
  // cap the process exits (freeing everything) before OOM; the durable cache
  // lets the run_colliers_main_full.sh driver resume until every URL is cached,
  // then a final cache-only pass assembles the artifact with zero fetches.
  // 0 = unlimited. Deferred URLs are not cached, so a later run retries them.
  const fetchCap = boundedInt(process.env.COLLIERS_MAIN_MAX_FETCHES_PER_RUN, 0, 0, 1_000_000);
  let fetchBudget = fetchCap > 0 ? fetchCap : Infinity;
  let done = 0;
  let fromCache = 0;
  let fetched = 0;
  let errors = 0;
  let deferred = 0;
  const listings = await pmap(selected, COLLIERS_MAIN_DETAIL_CONCURRENCY, async (entry) => {
    let listing = cached.get(entry.id);
    if (listing) {
      fromCache++;
    } else if (fetchBudget <= 0) {
      deferred++;
      done++;
      return null; // defer to a later run; not cached, so it is retried then
    } else {
      fetchBudget--;
      try {
        const docDoc = await scrapeColliersMainDetailDoc(entry.url);
        listing = parseColliersMainDetail(entry, docDoc);
        fetched++;
      } catch (err) {
        console.error(`  colliers-main: detail failed for ${entry.url}: ${err}`);
        listing = prune({
          id: entry.id,
          url: entry.url,
          transactionType: null,
          detailError: String(err),
          lastUpdated: entry.lastmod ? entry.lastmod.slice(0, 10) : null,
        });
        errors++;
      }
      appendColliersMainCache(cachePath, listing);
    }
    done++;
    if (done % 100 === 0 || done === selected.length) {
      console.error(
        `  colliers-main: enriched ${done}/${selected.length} (cache ${fromCache}, fetched ${fetched}, errors ${errors}, deferred ${deferred})`
      );
    }
    return listing;
  });
  const result = listings.filter(Boolean);
  if (deferred > 0) {
    console.error(
      `  colliers-main: ${deferred} URL(s) deferred under fetch cap ${fetchCap}; re-run to continue (${result.length} ready, ${fetched} newly fetched this run)`
    );
  }
  colliersMainEnrichedMemo = result;
  return result;
}

export async function srcColliersMain(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  if (monitor) {
    // Monitor mode: cheap sitemap enumeration only (url + lastmod), no detail
    // render. Emit on the sale pass only so a URL is not duplicated across both
    // transactionMode passes; Sale/Lease classification is detail-only and is
    // deferred to the downstream render of new/changed listings.
    if (tx === "lease") {
      return {
        company: "Colliers",
        sourceUrl: COLLIERS_MAIN_SOURCE_URL,
        method:
          "Public colliers.com XML sitemap enumeration (monitor mode; emitted on the sale pass only to avoid duplicate transactionMode rows)",
        totalAvailable: colliersMainSitemapCache ? colliersMainSitemapCache.length : null,
        listings: [],
        note: "Monitor mode: colliers-main sitemap entries are emitted on the sale pass only; lease pass is intentionally empty.",
      };
    }
    const entries = await fetchColliersMainEntries();
    const want = max && max > 0 ? Math.min(max, entries.length) : entries.length;
    const listings = entries.slice(0, want).map((entry) => ({
      id: entry.id,
      url: entry.url,
      lastUpdated: entry.lastmod ? entry.lastmod.slice(0, 10) : null,
    }));
    return {
      company: "Colliers",
      sourceUrl: COLLIERS_MAIN_SOURCE_URL,
      method:
        "Public colliers.com XML sitemap enumeration (/sitemap -> en ?type=properties): url + lastmod only (monitor mode; detail render skipped)",
      totalAvailable: entries.length,
      listings,
      note: "Monitor mode: sitemap url + lastmod only (id matches the full-path main: external id). Status, price, and Sale/Lease classification are detail-only and deferred to the downstream render of new/changed listings.",
    };
  }
  const all = await colliersMainEnrichAll(max);
  const ok = all.filter((l) => l && l.url && !l.detailError && !l.skip);
  const notFound = all.filter((l) => l?.skip === "not_found").length;
  const noData = all.filter((l) => l?.skip === "no_structured_data").length;
  const errored = all.filter((l) => l?.detailError).length;
  const wantSale = (l: any) => l.transactionType === "Sale" || l.transactionType === "Sale/Lease";
  const wantLease = (l: any) => l.transactionType === "Lease" || l.transactionType === "Sale/Lease";
  const listings = ok.filter(tx === "sale" ? wantSale : wantLease);
  return {
    company: "Colliers",
    sourceUrl: COLLIERS_MAIN_SOURCE_URL,
    method:
      "Public colliers.com XML sitemap discovery (/sitemap -> en ?type=properties) plus per-listing detail render through local Firecrawl; RealEstateListing JSON-LD + markdown parse",
    totalAvailable: colliersMainSitemapCache ? colliersMainSitemapCache.length : null,
    listings,
    note:
      `Main colliers.com folded into the colliers brokerage as colliers-main with main: id prefix; SalesTracker rows untouched. ` +
      `${ok.length} live detail-enriched listing(s) of ${all.length} sitemap URL(s) scanned, ${notFound} expired/not-found and ${noData} no-structured-data (tombstoned), ${errored} detail error(s). ` +
      `Sale pass returns Sale + Sale/Lease; lease pass returns Lease + Sale/Lease. ` +
      "Documents and images are URL-only; no Coveo POST, auth, or gated document path is used.",
  };
}
