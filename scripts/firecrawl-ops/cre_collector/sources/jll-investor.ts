// sources/jll-investor.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { brokerRef, brokers } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { decodeHtmlEntities, dedupeStrings, extractSitemapUrlEntries, titleFromFilename } from "../lib/html.js";
import { scrapeDoc, scrapeRaw } from "../lib/scrape.js";
import { SourceResult, Tx } from "../types.js";
import { boundedInt, clean, num, pmap, prune } from "../lib/util.js";


// --- JLL Investor Center: rendered page (sale-only by nature) ---

export const JLL_INVESTOR_HOST = "https://invest.jll.com";
export const JLL_INVESTOR_SEARCH_URL =
  "https://invest.jll.com/us/en/property-search?filter=%7B%22location%22%3A%5B%22United%20States%22%5D%7D";
export const JLL_INVESTOR_SITEMAP_INDEX_URL = `${JLL_INVESTOR_HOST}/sitemap_index.xml`;
export const JLL_INVESTOR_US_SITEMAP_URL = `${JLL_INVESTOR_HOST}/us/sitemap-us.xml`;
export const JLL_INVESTOR_DETAIL_CONCURRENCY = boundedInt(
  process.env.JLL_INVESTOR_DETAIL_CONCURRENCY,
  Math.min(CONCURRENCY, 4),
  1,
  8
);
export const JLL_INVESTOR_DETAIL_WAIT_MS = boundedInt(process.env.JLL_INVESTOR_DETAIL_WAIT_MS, 1000, 0, 30000);
export const JLL_INVESTOR_DETAIL_FALLBACK_WAIT_MS = boundedInt(
  process.env.JLL_INVESTOR_DETAIL_FALLBACK_WAIT_MS,
  8000,
  1000,
  60000
);
export const JLL_INVESTOR_SITEMAP_SCAN_LIMIT = boundedInt(
  process.env.JLL_INVESTOR_SITEMAP_SCAN_LIMIT,
  0,
  0,
  10000
);

export function jllInvestorNextData(rawHtml: string): any | null {
  const $ = cheerio.load(rawHtml);
  const text = $("#__NEXT_DATA__").first().text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export function jllInvestorUrlFromAlias(alias: string | null): string | null {
  const cleaned = clean(alias);
  if (!cleaned) return null;
  if (/^https?:\/\//i.test(cleaned)) return cleaned;
  const path = cleaned.startsWith("/us/en/listings/")
    ? cleaned
    : `/us/en/listings/${cleaned.replace(/^\/+/, "")}`;
  return `${JLL_INVESTOR_HOST}${path}`;
}

export function jllInvestorSitemapUrls(rawHtml: string): string[] {
  const decoded = decodeHtmlEntities(rawHtml);
  const matches = decoded.match(/https:\/\/invest\.jll\.com\/[a-z]{2}\/sitemap-[a-z]{2}\.xml/gi) ?? [];
  return dedupeStrings(matches);
}

export function jllInvestorSitemapCandidateLimit(max: number, total: number): number {
  if (JLL_INVESTOR_SITEMAP_SCAN_LIMIT > 0) return Math.min(total, JLL_INVESTOR_SITEMAP_SCAN_LIMIT);
  if (!Number.isFinite(max)) return total;
  const requested = Math.max(1, Math.trunc(max));
  return Math.min(total, Math.max(requested * 8, requested + 25));
}

export function jllInvestorStatus(row: any): string {
  if (row?.isUnderContract) return "Under Contract";
  const status = clean(row?.stageName ?? row?.status);
  return status ?? "Active";
}

export function jllInvestorSearchListing(row: any): any {
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

export function jllInvestorSearchFallback(rawHtml: string, max: number): any[] {
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

export function jllInvestorDocumentUrls(listing: any): string[] {
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

export function jllInvestorImageUrls(listing: any, fallback: string[] = []): string[] {
  const images = [
    clean(listing?.image),
    ...(Array.isArray(listing?.multimedia?.images) ? listing.multimedia.images.map(clean) : []),
    ...fallback,
  ];
  return dedupeStrings(images).filter((url) => /^https?:\/\//i.test(url));
}

export function jllInvestorContacts(listing: any): any[] {
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

export async function enrichJllInvestorListing(base: any): Promise<any> {
  if (!base.url) return base;
  try {
    let doc = await scrapeDoc(base.url, { waitFor: JLL_INVESTOR_DETAIL_WAIT_MS, timeout: 120000 });
    let next = jllInvestorNextData(doc.rawHtml);
    let listing = next?.props?.pageProps?.initialState?.pdp?.listing;
    if (!listing && JLL_INVESTOR_DETAIL_FALLBACK_WAIT_MS > JLL_INVESTOR_DETAIL_WAIT_MS) {
      doc = await scrapeDoc(base.url, { waitFor: JLL_INVESTOR_DETAIL_FALLBACK_WAIT_MS, timeout: 120000 });
      next = jllInvestorNextData(doc.rawHtml);
      listing = next?.props?.pageProps?.initialState?.pdp?.listing;
    }
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
      lastUpdated:
        clean(listing.dateModified ?? listing.datePublished) ??
        (base.lastmod ? String(base.lastmod).slice(0, 10) : null),
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

export async function srcJllInvestor(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
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
  if (monitor) {
    // Monitor mode is NOT supported for jll-investor: the persisted external id
    // is the detail-page Salesforce listing.id (enrichJllInvestorListing), which
    // cannot be recovered from the sitemap (the cheap key is only the URL slug).
    // Verified 934/934 slug != listing.id against the full sitemap artifact.
    // Emitting slug-keyed rows would make every row read as NEW each run and
    // pollute the change ledger / enrichment queue, so jll-investor stays on the
    // full-sweep cadence and emits no monitor rows. Short-circuit BEFORE the
    // sitemap scrape + detail enumeration: the rows are discarded anyway. A cheap
    // path would need URL-keyed reconciliation in cre_monitor.py (out of scope).
    return {
      company: "JLL Investor Center",
      sourceUrl: JLL_INVESTOR_US_SITEMAP_URL,
      method: "Monitor mode unsupported (detail-derived Salesforce external id); full-sweep cadence only",
      totalAvailable: null,
      listings: [],
      note: "Monitor mode emits no rows for jll-investor: its external id is the detail-page Salesforce listing.id and cannot be derived from the sitemap URL slug. Refresh this source via the full (non-monitor) collection path.",
    };
  }
  const indexHtml = await scrapeRaw(JLL_INVESTOR_SITEMAP_INDEX_URL, { waitFor: 1000, timeout: 60000 });
  const sitemapUrl =
    jllInvestorSitemapUrls(indexHtml).find((url) => url === JLL_INVESTOR_US_SITEMAP_URL) ??
    JLL_INVESTOR_US_SITEMAP_URL;
  const sitemapHtml = await scrapeRaw(sitemapUrl, { waitFor: 1000, timeout: 60000 });
  const seenDetailUrls = new Set<string>();
  const detailEntries = extractSitemapUrlEntries(sitemapHtml)
    .filter((e) => /^https:\/\/invest\.jll\.com\/us\/en\/listings\//i.test(e.loc))
    .map((e) => ({ loc: e.loc.replace(/\/$/, ""), lastmod: e.lastmod }))
    .filter((e) => {
      if (seenDetailUrls.has(e.loc)) return false;
      seenDetailUrls.add(e.loc);
      return true;
    });
  if (!detailEntries.length) throw new Error("no listing URLs found in JLL Investor Center US sitemap");

  const candidateLimit = jllInvestorSitemapCandidateLimit(max, detailEntries.length);
  const candidates = detailEntries.slice(0, candidateLimit);
  console.error(
    `  jll-investor: ${detailEntries.length} sitemap detail URL(s), scanning ${candidates.length}`
  );
  let enrichedCount = 0;
  const enriched = await pmap(candidates, JLL_INVESTOR_DETAIL_CONCURRENCY, async (entry) => {
    const row = await enrichJllInvestorListing({
      id: entry.loc.split("/").filter(Boolean).slice(-1)[0] ?? null,
      transactionType: "Sale (investment)",
      brokerIds: [],
      photos: [],
      url: entry.loc,
      lastmod: entry.lastmod,
    });
    enrichedCount++;
    if (enrichedCount % 10 === 0 || enrichedCount === candidates.length) {
      console.error(`  jll-investor: detail enriched ${enrichedCount}/${candidates.length}`);
    }
    return row;
  });

  const detailErrors = enriched.filter((row) => row?.detailError).length;
  const usRows = enriched.filter((row) => row?.country === "US");
  const listings = usRows.slice(0, Math.min(max, usRows.length));
  const nonUsRows = enriched.length - usRows.length - detailErrors;
  if (!listings.length) throw new Error("no United States listing details found in JLL Investor Center sitemap sample");
  return {
    company: "JLL Investor Center",
    sourceUrl: sitemapUrl,
    method: "Public XML sitemap detail discovery plus detail-page __NEXT_DATA__ enrichment and United States country filtering",
    totalAvailable: detailEntries.length,
    listings,
    note:
      `Sitemap contains global inventory on the US locale path, so rows are retained only when public detail-page country is United States. Scanned ${candidates.length} detail URL(s), kept ${listings.length} U.S. row(s), skipped ${nonUsRows} non-U.S. row(s), and saw ${detailErrors} detail error(s). Detail enrichment stores public teaser document URLs, image URLs, and broker contact fields only; CA/NDA document URLs remain in raw detail metadata.`,
  };
}
