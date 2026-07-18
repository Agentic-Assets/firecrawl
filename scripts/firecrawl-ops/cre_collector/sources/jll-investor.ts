// sources/jll-investor.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { brokerRef, brokers } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { harvestDetail } from "../lib/harvest.js";
import { decodeHtmlEntities, dedupeStrings, extractSitemapUrlEntries, stripHtmlText, titleFromFilename } from "../lib/html.js";
import { scrapeDoc, scrapeRaw } from "../lib/scrape.js";
import { DocItem, MediaItem, ScrapedDoc, SourceResult, Tx } from "../types.js";
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
// A single unresponsive public page must not hold the all-source collector for
// multiple retry windows. The worker records a detailError and retains the
// sitemap row, so a bounded timeout favors a complete, additive inventory run
// over indefinitely waiting for optional detail enrichment.
export const JLL_INVESTOR_DETAIL_TIMEOUT_MS = boundedInt(
  process.env.JLL_INVESTOR_DETAIL_TIMEOUT_MS,
  45000,
  10000,
  120000
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

/**
 * Extract a single normalized license string from a JLL Investor broker licenses array.
 * Investor shape: [{ number, location, type }].
 * Returns the first entry formatted as "location: number", or null.
 */
export function jllInvestorExtractLicense(licenses: any): string | null {
  const arr = Array.isArray(licenses) ? licenses : [];
  const first = arr[0];
  if (!first) return null;
  const location = clean(first?.location ?? first?.state ?? first?.type);
  const number = clean(first?.number ?? first?.licenseNumber);
  if (number && location) return `${location}: ${number}`;
  if (number) return number;
  return null;
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
        license: jllInvestorExtractLicense(broker?.licenses),
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

// Promote the stranded JLL Investor multimedia video / virtual-tour fields for
// harvestDetail. `multimedia.videos` / `videoUrls` and `multimedia.virtualTours`
// / `tourUrls` are string-url arrays the adapter previously dropped. Video urls
// are emitted as BARE STRINGS so the harvester derives provider + embedUrl; tour
// / 360 urls are emitted as TYPED virtual_tour items so they keep that
// classification on unrecognized hosts. harvestDetail dedups by url. Never throws.
export function jllInvestorStrandedMedia(listing: any): (MediaItem | string)[] {
  const mm = listing?.multimedia ?? {};
  const out: (MediaItem | string)[] = [];
  const urlsOf = (value: any): string[] => {
    const arr = Array.isArray(value) ? value : value != null ? [value] : [];
    return arr
      .map((raw: any) => clean(typeof raw === "string" ? raw : raw?.url ?? raw?.src))
      .filter((u: string | null): u is string => Boolean(u) && /^https?:\/\//i.test(u as string));
  };
  for (const url of urlsOf(mm.videos ?? mm.videoUrls ?? listing?.videos)) out.push(url);
  for (const url of urlsOf(mm.virtualTours ?? mm.tourUrls ?? listing?.virtualTours)) {
    out.push({ mediaType: "virtual_tour", provider: null, url, embedUrl: null, title: null });
  }
  for (const url of urlsOf(mm.view360URLs ?? listing?.view360URLs)) {
    out.push({ mediaType: "virtual_tour", provider: null, url, embedUrl: null, title: null });
  }
  return out;
}

// Promote gated / CA document urls (documentsCA) as DocItems. The public teaser
// documents already flow through jllInvestorDocumentUrls -> brochures; the CA
// set was previously kept only in raw metadata. harvestDetail classifies each by
// filename/keyword (om/financials/rent_roll/...) and dedups by url.
export function jllInvestorStrandedDocs(listing: any): DocItem[] {
  const urls: string[] = [];
  const visit = (value: any) => {
    if (!value) return;
    if (typeof value === "string") {
      if (/^https?:\/\//i.test(value)) urls.push(value);
      return;
    }
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (typeof value === "object") {
      visit(value.url ?? value.href);
      for (const nested of Object.values(value)) visit(nested);
    }
  };
  visit(listing?.documentsCA);
  return dedupeStrings(urls).map((url) => ({ url, title: titleFromFilename(url), docType: "other" as const }));
}

// Lift stranded structured fields the JLL Investor detail payload exposes onto
// the existing listing keys cre_ingest.to_row maps. Only clearly-present values
// are lifted; absent fields stay undefined (prune removes them) so this never
// clobbers good data.
//
// Phase-2 additions (additive only):
//   highlights  <- jllInvestorDetail.highlights may be an HTML string (strip) or
//                  an array of objects/strings
//   statusBadge <- jllInvestorDetail.stageName / isUnderContract / top-level status
//   canonicalUrl <- base.url (the invest.jll.com detail URL)
//   extraFacts  <- { deal_type } from jllInvestorDetail.dealType
export function jllInvestorStrandedStructured(listing: any): Record<string, any> {
  // highlights: the investor payload stores these as an HTML string (rich text editor output)
  // OR as an array of objects/strings. Handle both.
  let highlights: string[] = [];
  if (typeof listing?.highlights === "string") {
    // HTML string: strip tags, split on list-item boundaries, clean each line.
    const stripped = stripHtmlText(listing.highlights) ?? "";
    highlights = dedupeStrings(
      stripped
        .split(/\n|(?<=\.)(?=\s*[A-Z])/)
        .map((s) => clean(s))
        .filter(Boolean) as string[]
    );
  } else if (Array.isArray(listing?.highlights)) {
    highlights = dedupeStrings(
      listing.highlights
        .map((h: any) =>
          typeof h === "string"
            ? clean(h)
            : clean(h?.text ?? h?.value ?? h?.title)
        )
        .filter(Boolean)
    );
  }

  // statusBadge: derive from the investor detail shape (stageName / isUnderContract).
  // Routes to the existing OPT-IN activation gate in cre_ingest; never auto-activates.
  let statusBadge: string | undefined;
  if (listing?.isUnderContract) {
    statusBadge = "Under Contract";
  } else if (clean(listing?.stageName)) {
    statusBadge = clean(listing.stageName) ?? undefined;
  }

  // extraFacts: long-tail facts with no discrete column.
  const extraFacts: Record<string, unknown> = {};
  const dealType = clean(listing?.dealType);
  if (dealType) extraFacts.deal_type = dealType;

  return (
    prune({
      units: num(listing?.numberOfUnits) ?? num(Number(listing?.numberOfUnits)),
      yearBuilt: num(listing?.yearBuilt) ?? num(Number(listing?.yearBuilt)),
      occupancyRate: num(listing?.occupancyRate) ?? num(Number(listing?.occupancy)),
      capRatePct: num(listing?.capRate) ?? num(Number(listing?.capRate)),
      market: clean(listing?.market),
      submarket: clean(listing?.submarket),
      highlights: highlights.length ? highlights : undefined,
      statusBadge,
      extraFacts: Object.keys(extraFacts).length ? extraFacts : undefined,
    }) ?? {}
  );
}

// Pure transform (no network): given the base row and an already-scraped detail
// doc, return the enriched listing row (or a `detailError` row when the page has
// no pdp listing in __NEXT_DATA__). Factored out of enrichJllInvestorListing so
// the enrichment worker and the unit test can reuse it against a saved fixture.
// `base.url` flows through unchanged, satisfying the worker's URL-keyed
// completion match.
export function parseJllInvestorDetail(base: any, doc: ScrapedDoc): any {
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
  const teaserDocs = documentUrls.map((url) => ({ name: titleFromFilename(url), url }));
  const photos = jllInvestorImageUrls(listing, base.photos ?? []);
  // Capture-everything harvest: unify the full detail page (markdown / links /
  // images / video+iframe attributes) with the stranded native fields promoted
  // via ctx.extra* (multimedia videos/tours, CA/gated documents, image gallery).
  // Public teaser documents stay on the existing `brochures` channel; they are
  // NOT promoted into extraDocs (cre_listing_documents has no (listing_id,url)
  // unique key, so a url in BOTH channels would double-insert). harvested.documents
  // is filtered to exclude any url already on the brochures channel. When the doc
  // carries no structured `images` (e.g. a saved fixture), fall back to the native
  // gallery for the image channel rather than the rawHtml <img> regex.
  const harvestDoc: ScrapedDoc = Array.isArray(doc.images) ? doc : { ...doc, images: photos };
  const harvested = harvestDetail(harvestDoc, {
    baseUrl: base.url,
    extraMedia: jllInvestorStrandedMedia(listing),
    extraDocs: jllInvestorStrandedDocs(listing),
    extraImages: photos,
  });
  const teaserUrlSet = new Set(documentUrls.map((u) => u.toLowerCase()));
  const documents = harvested.documents.filter((d) => !teaserUrlSet.has(d.url.toLowerCase()));
  const lifted = jllInvestorStrandedStructured(listing);
  // canonicalUrl: the invest.jll.com detail page URL is the stable canonical
  // for investor listings. Use base.url (already normalized by srcJllInvestor).
  const canonicalUrl = clean(base.url) ?? undefined;
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
    ...lifted,
    canonicalUrl,
    brokerIds,
    contactsDetailed,
    brochures: teaserDocs,
    documents,
    media: harvested.media,
    links: harvested.links,
    photos: dedupeStrings([...photos, ...harvested.images]),
    markdown: doc.markdown || base.markdown,
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
}

export async function enrichJllInvestorListing(base: any): Promise<any> {
  if (!base.url) return base;
  try {
    let doc = await scrapeDoc(base.url, {
      waitFor: JLL_INVESTOR_DETAIL_WAIT_MS,
      timeout: JLL_INVESTOR_DETAIL_TIMEOUT_MS,
    });
    let next = jllInvestorNextData(doc.rawHtml);
    let listing = next?.props?.pageProps?.initialState?.pdp?.listing;
    if (!listing && JLL_INVESTOR_DETAIL_FALLBACK_WAIT_MS > JLL_INVESTOR_DETAIL_WAIT_MS) {
      doc = await scrapeDoc(base.url, {
        waitFor: JLL_INVESTOR_DETAIL_FALLBACK_WAIT_MS,
        timeout: JLL_INVESTOR_DETAIL_TIMEOUT_MS,
      });
    }
    return parseJllInvestorDetail(base, doc);
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
