// sources/cushman-wakefield.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { decodeHtmlEntities, firstJsonLd, titleFromFilename } from "../lib/html.js";
import { harvestDetail } from "../lib/harvest.js";
import { parseLeaseRate } from "../lib/parse.js";
import { scrapeDoc, scrapeJson } from "../lib/scrape.js";
import { ScrapeOpts, ScrapedDoc, SourceResult, Tx } from "../types.js";
import { boundedInt, clean, moneyToNumber, pmap, prune } from "../lib/util.js";
import {
  detailObservation,
  refreshGenerationId,
  requireFreshDetails,
} from "../lib/freshness.js";


// --- Cushman & Wakefield: public JSON search API plus detail enrichment ---

export const CUSHMAN_HOST = "https://www.cushmanwakefield.com";
export const CUSHMAN_API_BASE = `${CUSHMAN_HOST}/api/properties/search`;
export const CUSHMAN_PAGE_SIZE = 100;
export const CUSHMAN_QUERY = clean(process.env.CUSHMAN_QUERY ?? null);
export const CUSHMAN_API_CONCURRENCY = boundedInt(process.env.CUSHMAN_API_CONCURRENCY, 1, 1, CONCURRENCY);
export const CUSHMAN_DETAIL_CONCURRENCY = boundedInt(
  process.env.CUSHMAN_DETAIL_CONCURRENCY,
  CONCURRENCY,
  1,
  CONCURRENCY
);
// The public search API carries the canonical external id and the primary
// inventory fields. `base` is an explicit recovery mode for a time-bounded
// additive refresh: it avoids rendering every detail page while preserving
// existing detail fields through the ingest merge. Normal collection remains
// `full` so it continues to harvest optional page-only facts.
export function cushmanUseBaseRows(monitor: boolean): boolean {
  return monitor || process.env.CUSHMAN_DETAIL_MODE === "base";
}

export function assertCushmanFreshnessMode(monitor: boolean): void {
  if (!monitor && requireFreshDetails() && cushmanUseBaseRows(monitor)) {
    throw new Error(
      "strict CRE refresh requires CUSHMAN_DETAIL_MODE=full; API-base rows cannot prove detail freshness"
    );
  }
}

export function canonicalCushmanUrl(url: string | null): string | null {
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

export function canonicalCushmanAssetUrl(url: string): string | null {
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

export function dedupeAssetsBestWidth(urls: string[]): string[] {
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

export function extractCushmanAssetUrls(doc: ScrapedDoc): string[] {
  const text = [doc.rawHtml, doc.markdown, ...(doc.links ?? [])].join("\n");
  const candidates = text.match(/https?:\/\/assets\.cushmanwakefield\.com\/[^"'<>\s\])]+/gi) ?? [];
  return dedupeAssetsBestWidth(
    candidates
      .map((u) => canonicalCushmanAssetUrl(u))
      .filter((u: string | null): u is string => Boolean(u))
  );
}

export function pmediaId(url: string): string | null {
  return url.match(/\/pmedia\/([^/]+)\//i)?.[1] ?? null;
}

export function extractCushmanDocuments(assetUrls: string[]): any[] {
  const seen = new Set<string>();
  const docs: any[] = [];
  for (const url of assetUrls) {
    if (!/\.pdf(?:\?|$)/i.test(url) || seen.has(url)) continue;
    seen.add(url);
    docs.push({ name: titleFromFilename(url), url });
  }
  return docs;
}

export function extractCushmanPhotos(assetUrls: string[]): string[] {
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

export function markdownLabel(markdown: string, label: string): string | null {
  const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const m = markdown.match(new RegExp(`${escaped}:?\\s*\\n+\\s*([^\\n]+)`, "i"));
  return clean(m?.[1] ?? null);
}

export function firstNumberText(text: string | null): number | null {
  if (!text) return null;
  const m = text.replace(/,/g, "").match(/([0-9]+(?:\.[0-9]+)?)/);
  return m ? Number(m[1]) : null;
}

export function sqftFromText(text: string | null): number | null {
  if (!text) return null;
  const m = text.replace(/,/g, "").match(/([0-9]+(?:\.[0-9]+)?)\s*(?:SF|sq\.?\s*ft\.?)/i);
  return m ? Number(m[1]) : null;
}

export function acresFromText(text: string | null): number | null {
  if (!text) return null;
  const acres = text.replace(/,/g, "").match(/([0-9]*\.?[0-9]+)\s*Acres?/i);
  if (acres) return Number(acres[1]);
  const sqft = sqftFromText(text);
  return sqft ? sqft / 43560 : null;
}

export function extractCushmanContacts(doc: ScrapedDoc): any[] {
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

export function assertCushmanDetailDoc(doc: ScrapedDoc, expected: any): any {
  const status = Number(doc.metadata?.statusCode);
  if (Number.isFinite(status) && status >= 400) {
    throw new Error(`Cushman detail returned HTTP ${status}`);
  }
  const combined = `${doc.rawHtml ?? ""}\n${doc.markdown ?? ""}`;
  if (
    !combined.trim()
    || /just a moment|checking your browser|verify you are human|captcha|access denied|cf-chl-|page not found|404\s*[-:]\s*page not found|internal server error|service unavailable/i.test(
      combined
    )
  ) {
    throw new Error("Cushman detail returned a challenge or error shell");
  }

  const listing = firstJsonLd(doc.rawHtml, "RealEstateListing");
  const expectedUrl = canonicalCushmanUrl(clean(expected?.url));
  const observedUrl = canonicalCushmanUrl(
    clean(
      listing?.url
      ?? doc.metadata?.sourceURL
      ?? doc.metadata?.url
    )
  );
  let urlMatches = false;
  if (expectedUrl && observedUrl) {
    try {
      urlMatches =
        new URL(expectedUrl).pathname.replace(/\/+$/, "").toLowerCase()
        === new URL(observedUrl).pathname.replace(/\/+$/, "").toLowerCase();
    } catch {
      urlMatches = expectedUrl === observedUrl;
    }
    if (!urlMatches) {
      throw new Error(
        `Cushman detail identity does not match expected URL ${expectedUrl}`
      );
    }
  }

  const normalizedText = clean(
    cheerio.load(doc.rawHtml)("body").text()
    || doc.markdown
  )?.toLowerCase() ?? "";
  const identityValues = [
    clean(expected?.name),
    clean(expected?.street),
    clean(expected?.id),
  ]
    .filter((value): value is string => Boolean(value && value.length >= 4))
    .map((value) => value.toLowerCase());
  const contentMatches = identityValues.some((value) =>
    normalizedText.includes(value)
  );
  const $ = cheerio.load(doc.rawHtml);
  const hasListingStructure = Boolean(
    listing
    || clean($("h1").first().text())
    || /(?:property details|property overview|available space|building size|sale price|lease rate)/i.test(
      doc.markdown ?? ""
    )
  );
  if (!hasListingStructure || (!urlMatches && !contentMatches)) {
    throw new Error("Cushman detail identity does not match the requested property");
  }
  return listing;
}

export type CushmanInventoryPageEvidence = {
  total: number;
};

function cushmanProviderIdentity(row: any): string | null {
  const value = row?.id;
  if (typeof value === "string") return clean(value);
  return typeof value === "number" && Number.isFinite(value)
    ? String(value)
    : null;
}

export function assertCushmanInventoryPage(
  data: any,
  offset: number,
  expected: CushmanInventoryPageEvidence | null = null,
  strict = requireFreshDetails()
): CushmanInventoryPageEvidence {
  if (!strict) {
    const rows = Array.isArray(data?.content) ? data.content : [];
    const parsedTotal = Number(data?.total_item);
    return {
      total: Number.isFinite(parsedTotal) ? parsedTotal : rows.length,
    };
  }
  if (
    !data
    || typeof data !== "object"
    || Array.isArray(data)
    || !Array.isArray(data.content)
  ) {
    throw new Error(
      `Cushman strict inventory offset ${offset} requires a content array`
    );
  }
  const total = Number(data.total_item);
  if (!Number.isFinite(total) || !Number.isInteger(total) || total < 0) {
    throw new Error(
      `Cushman strict inventory offset ${offset} requires a valid integer total`
    );
  }
  if (expected && total !== expected.total) {
    throw new Error(
      `Cushman strict inventory total changed from ${expected.total} to ${total} at offset ${offset}`
    );
  }
  const validOffset =
    Number.isInteger(offset)
    && offset >= 0
    && offset % CUSHMAN_PAGE_SIZE === 0
    && (total === 0 ? offset === 0 : offset < total);
  if (!validOffset) {
    throw new Error(
      `Cushman strict inventory requested invalid offset ${offset} for total ${total}`
    );
  }
  const expectedRows = Math.max(
    0,
    Math.min(CUSHMAN_PAGE_SIZE, total - offset)
  );
  if (data.content.length !== expectedRows) {
    throw new Error(
      `Cushman strict inventory offset ${offset} expected ${expectedRows} rows, received ${data.content.length}`
    );
  }
  const identities = new Set<string>();
  for (const [index, row] of data.content.entries()) {
    const identity = cushmanProviderIdentity(row);
    if (!identity) {
      throw new Error(
        `Cushman strict inventory offset ${offset} row ${index} requires a nonempty provider id`
      );
    }
    if (identities.has(identity)) {
      throw new Error(
        `Cushman strict inventory offset ${offset} has duplicate provider identity ${identity}`
      );
    }
    identities.add(identity);
  }
  return { total };
}

export function assertCushmanInventoryReconciled(
  rows: any[],
  total: number,
  strict = requireFreshDetails()
): void {
  if (!strict) return;
  const identities = new Set<string>();
  for (const [index, row] of rows.entries()) {
    const identity = cushmanProviderIdentity(row);
    if (!identity) {
      throw new Error(
        `Cushman strict inventory aggregate row ${index} requires a nonempty provider id`
      );
    }
    if (identities.has(identity)) {
      throw new Error(
        `Cushman strict inventory aggregate has duplicate provider identity ${identity}`
      );
    }
    identities.add(identity);
  }
  if (rows.length !== total || identities.size !== total) {
    throw new Error(
      `Cushman strict inventory expected ${total} unique rows, reconciled ${identities.size}`
    );
  }
}

export function cushmanSearchApiUrl(tx: Tx, offset: number): string {
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

export function baseCushmanExtraFacts(row: any): Record<string, unknown> | undefined {
  const headlineText = clean(row.attribute1);
  const isSublease = headlineText != null && /sublease/i.test(headlineText);
  const isInvestment: boolean | null =
    row.is_investment_property === true ? true : null;
  const ef: Record<string, unknown> = {};
  if (isSublease) ef["sublease"] = true;
  if (isInvestment != null) ef["is_investment_property"] = isInvestment;
  return Object.keys(ef).length > 0 ? ef : undefined;
}

export function baseCushmanListing(row: any, tx: Tx): any {
  const url = canonicalCushmanUrl(row.url ?? row.relative_url);
  const street = clean(row.property_street);
  const city = clean(row.property_city);
  const state = clean(row.state_or_province)?.toUpperCase() ?? null;
  const zip = clean(row.property_postal_code);
  const listingStatus = clean(row.listing_status);
  const extraFacts = baseCushmanExtraFacts(row);
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
    // Phase-2 scalar lifts (WS1):
    canonicalUrl: url,
    listingStatus,
    statusBadge: listingStatus,
    extraFacts,
    rawCushmanApi: row,
  };
}

export function cushmanBaseRefreshListing(
  row: any,
  tx: Tx,
  inventoryObservedAt = new Date().toISOString()
): any {
  return {
    ...baseCushmanListing(row, tx),
    // The API-only path is intentionally incomplete. Tell ingestion to
    // preserve previously harvested contacts/documents/images/media/links
    // instead of treating omitted arrays as authoritative empty arrays.
    preserveChildCollections: true,
    inventoryObservedAt,
    freshnessProvenance: {
      detailScope: "inventory_only",
      generationId: refreshGenerationId(),
      method: "cushman_search_api",
      cacheDisposition: "live",
    },
  };
}

export async function enrichCushmanListing(
  row: any,
  tx: Tx,
  inventoryObservedAt = new Date().toISOString()
): Promise<any> {
  const base = baseCushmanListing(row, tx);
  if (!base.url) {
    return {
      ...base,
      inventoryObservedAt,
      detailError: "missing canonical detail URL",
    };
  }
  try {
    const doc = await scrapeDoc(base.url, {
      waitFor: 1000,
      timeout: 60000,
      ...(requireFreshDetails() ? { maxAge: 0 } : {}),
    });
    const listingLd = assertCushmanDetailDoc(doc, base);
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
    // Stranded labeled structured fields lifted onto existing cre_listings cols
    // (only when clearly labeled on the detail markdown).
    const occupancyText = markdownLabel(doc.markdown, "Occupancy") ?? markdownLabel(doc.markdown, "Percent Leased");
    const occupancyPct = occupancyText && /%/.test(occupancyText) ? firstNumberText(occupancyText) : null;
    const zoning = markdownLabel(doc.markdown, "Zoning");
    // Capture-everything: harvest video/tour media, outbound links, and any
    // ADDITIONAL classified documents from the rendered detail page. The asset-
    // derived documents/photos keep their existing channels (with titles); only
    // the cushman assets.cushmanwakefield.com PDFs are re-passed so harvest can
    // classify them (om/financials/etc.) into the documents superset.
    const harvested = harvestDetail(doc, {
      extraDocs: documents.map((d) => d.url as string),
      baseUrl: base.url,
    });
    // Phase-2 scalar lifts (WS1): parse lease rate from the scraped label.
    const parsedRate = tx === "lease" && leaseRateText ? parseLeaseRate(leaseRateText) : null;
    const leaseRateMin = parsedRate?.min ?? null;
    const leaseRateMax = parsedRate?.max ?? null;
    const leaseRateType = parsedRate?.type ?? null;

    // extraFacts inherits from base (sublease + is_investment_property), already computed there.

    const observed = detailObservation("cushman_detail", "live");
    return prune({
      ...base,
      inventoryObservedAt,
      detailObservedAt: observed.observedAt,
      freshnessProvenance: {
        detailScope: "detail_page",
        generationId: observed.generationId,
        method: observed.method,
        cacheDisposition: observed.cacheDisposition,
      },
      name: clean(listingLd?.name) ?? base.name,
      description: clean(listingLd?.description) ?? clean(doc.markdown.match(/Overview\s*-+\s*([\s\S]{1,2500}?)(?:\n[A-Z][A-Za-z ]+\n-+|\n#{1,6}\s|\nCONTACT|\nLOCATION|$)/i)?.[1]),
      salePriceUsd: tx === "sale" ? moneyToNumber(salePriceText) : null,
      salePriceText: tx === "sale" ? salePriceText ?? base.salePriceText : null,
      leaseRateText: tx === "lease" ? leaseRateText : null,
      leaseRateMin: leaseRateMin ?? undefined,
      leaseRateMax: leaseRateMax ?? undefined,
      leaseRateType: leaseRateType ?? undefined,
      sizeText: buildingSizeText ?? lotSizeText,
      buildingSizeSqft: sqftFromText(buildingSizeText),
      lotSizeAcres: acresFromText(lotSizeText),
      yearBuilt: firstNumberText(yearText),
      occupancyRate: occupancyPct != null ? occupancyPct : null,
      zoning: clean(zoning),
      brokerIds,
      brochures: documents,
      photos: photos.length ? photos : base.photos,
      documents: harvested.documents.length ? harvested.documents : undefined,
      media: harvested.media.length ? harvested.media : undefined,
      links: harvested.links.length ? harvested.links : undefined,
      markdown: doc.markdown || undefined,
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
        mediaCount: harvested.media.length,
        harvestLinkCount: harvested.links.length,
      },
    });
  } catch (err) {
    console.error(`  cushman-wakefield/${tx}: detail failed for ${base.url}: ${err}`);
    return prune({
      ...base,
      inventoryObservedAt,
      detailError: String(err),
    });
  }
}

export async function srcCushman(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  assertCushmanFreshnessMode(monitor);
  const strict = requireFreshDetails();
  const sourceUrl =
    tx === "sale"
      ? "https://www.cushmanwakefield.com/en/united-states/properties/invest/search"
      : "https://www.cushmanwakefield.com/en/united-states/properties/lease/search";
  const apiOpts: ScrapeOpts = { timeout: 90000, jsonAttempts: 8, jsonBackoffMs: 12000 };
  const strictApiOpts = {
    ...apiOpts,
    ...(strict ? { maxAge: 0 } : {}),
  };
  const first = await scrapeJson(cushmanSearchApiUrl(tx, 0), strictApiOpts);
  const firstPage = assertCushmanInventoryPage(first, 0, null, strict);
  const content = Array.isArray(first.content) ? first.content : [];
  const total: number = strict
    ? firstPage.total
    : Number(first.total_item ?? content.length);
  if (!content.length) throw new Error(`Cushman & Wakefield API returned no ${tx} content`);
  const want = Math.min(max, total);
  const fetchCount = strict ? total : want;
  const pages = Math.ceil(fetchCount / CUSHMAN_PAGE_SIZE);
  console.error(`  cushman-wakefield/${tx}: ${total} total, fetching ${pages} API page(s)`);
  const chunks: any[][] = [content];
  if (pages > 1) {
    const offsets = Array.from({ length: pages - 1 }, (_, i) => (i + 1) * CUSHMAN_PAGE_SIZE);
    const rest = await pmap(offsets, CUSHMAN_API_CONCURRENCY, async (offset) => {
      const d = await scrapeJson(cushmanSearchApiUrl(tx, offset), strictApiOpts);
      const rows = Array.isArray(d.content) ? d.content : [];
      assertCushmanInventoryPage(d, offset, firstPage, strict);
      console.error(`  cushman-wakefield/${tx}: API offset ${offset}, ${rows.length} rows`);
      return rows;
    });
    chunks.push(...rest);
  }
  // Strict refreshes validate every page, fetch the full provider inventory,
  // and reconcile unique provider ids before a finite output cap is applied.
  // Non-strict monitoring retains the prior tolerant behavior and reports a
  // short later page as truncation rather than failing the whole collection.
  // Both paths report a finite output cap below the provider total.
  const collectedRows = chunks.flat();
  assertCushmanInventoryReconciled(collectedRows, total, strict);
  const truncated =
    (strict ? collectedRows.length < total : collectedRows.length < want)
    || want < total;
  const rows = collectedRows.slice(0, want);
  const inventoryObservedAt = new Date().toISOString();
  let done = 0;
  // Monitor mode and explicit API-base recovery both skip per-listing rendering.
  // The base API mapping preserves the canonical identity and core inventory
  // fields; additive ingestion retains previously captured detail-only values.
  const useBaseRows = cushmanUseBaseRows(monitor);
  const listings = useBaseRows
    ? rows.map((row) => cushmanBaseRefreshListing(row, tx, inventoryObservedAt))
    : await pmap(rows, CUSHMAN_DETAIL_CONCURRENCY, async (row) => {
        const enriched = await enrichCushmanListing(row, tx, inventoryObservedAt);
        done++;
        if (done % 25 === 0 || done === rows.length) {
          console.error(`  cushman-wakefield/${tx}: detail enriched ${done}/${rows.length}`);
        }
        return enriched;
      });
  return {
    company: "Cushman & Wakefield",
    sourceUrl,
    method: useBaseRows
      ? "Cushman public /api/properties/search JSON pagination (API-base recovery mode)"
      : "Cushman public /api/properties/search JSON pagination plus detail-page raw HTML enrichment",
    totalAvailable: total,
    listings,
    truncated,
  };
}
