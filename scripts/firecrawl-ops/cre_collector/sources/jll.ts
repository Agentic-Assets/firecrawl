// sources/jll.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { createHash } from "node:crypto";
import { brokerRef, brokers } from "../lib/broker.js";
import { CONCURRENCY, PAGE_CAP } from "../lib/config.js";
import { harvestDetail } from "../lib/harvest.js";
import { dedupeStrings, stripHtmlText, titleFromFilename } from "../lib/html.js";
import { normBuildingClass } from "../lib/parse.js";
import { scrapeDoc, scrapeRaw } from "../lib/scrape.js";
import { DocItem, MediaItem, ScrapedDoc, SourceResult, Tx } from "../types.js";
import { boundedInt, clean, moneyToNumber, num, pmap, prune } from "../lib/util.js";
import {
  detailObservation,
  generationMatches,
  refreshGenerationId,
  requireFreshDetails,
} from "../lib/freshness.js";


// --- JLL: rendered search pages ---

export const JLL_PROPERTY_TYPES = [
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
export const JLL_SEARCH_PAGE_SIZE = 50;
export const JLL_DETAIL_CONCURRENCY = boundedInt(
  process.env.JLL_DETAIL_CONCURRENCY,
  Math.min(CONCURRENCY, 3),
  1,
  10
);
export const JLL_DETAIL_WAIT_MS = boundedInt(process.env.JLL_DETAIL_WAIT_MS, 1000, 0, 30000);
export const JLL_DETAIL_FALLBACK_WAIT_MS = boundedInt(
  process.env.JLL_DETAIL_FALLBACK_WAIT_MS,
  8000,
  1000,
  60000
);

export function jllPropertyTypeLabel(propertyType: string): string {
  return propertyType
    .split("-")
    .map((part) => part[0].toUpperCase() + part.slice(1))
    .join(" ");
}

export function normalizedJllListingUrl(href: string): string {
  const abs = href.startsWith("http") ? href : `https://property.jll.com${href}`;
  const url = new URL(abs);
  url.hash = "";
  url.search = "";
  return url.toString().replace(/\/$/, "");
}

export function jllFilteredSearchUrl(tenure: "sale" | "rent", propertyType: string, page: number): string {
  const url = new URL("https://property.jll.com/search");
  url.searchParams.set("tenureTypes", tenure);
  url.searchParams.set("propertyTypes", propertyType);
  url.searchParams.set("page", String(page));
  return url.toString();
}

export function parseJllSearchPage(html: string, tx: Tx, propertyType: string, page: number): {
  total: number | null;
  listings: any[];
} {
  const $ = cheerio.load(html);
  const totalMatch = ($("h2").text() || html).match(
    /([0-9][0-9,]*)\s+propert(?:y|ies)/i
  );
  const total = totalMatch ? Number(totalMatch[1].replace(/,/g, "")) : null;
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

export function assertJllSearchPageCompleteness(
  parsed: { total: number | null; listings: any[] },
  page: number,
  expectedTotal: number | null = null,
  strict = requireFreshDetails()
): void {
  if (!strict) return;
  const total = parsed.total;
  if (!Number.isInteger(total) || (total as number) < 0) {
    throw new Error(`JLL search page ${page} lacks a finite nonnegative total`);
  }
  if (expectedTotal !== null && total !== expectedTotal) {
    throw new Error(
      `JLL search page ${page} total changed from ${expectedTotal} to ${total}`
    );
  }
  const pages = Math.max(1, Math.ceil((total as number) / JLL_SEARCH_PAGE_SIZE));
  if (!Number.isInteger(page) || page < 1 || page > pages) {
    throw new Error(`JLL search page ${page} falls outside the declared ${pages}-page result`);
  }
  const expectedCards =
    page < pages
      ? JLL_SEARCH_PAGE_SIZE
      : (total as number) - (page - 1) * JLL_SEARCH_PAGE_SIZE;
  const urls = parsed.listings
    .map((listing) => clean(listing?.url))
    .filter((url): url is string => !!url);
  const uniqueUrls = new Set(urls);
  if (urls.length !== parsed.listings.length || uniqueUrls.size !== expectedCards) {
    throw new Error(
      `JLL search page ${page} expected ${expectedCards} unique cards from total=${total}, ` +
        `received ${uniqueUrls.size}`
    );
  }
}

export function assertJllFilterCoverage(
  propertyType: string,
  total: number | null,
  urls: Iterable<string>,
  strict = requireFreshDetails()
): void {
  if (!strict) return;
  if (!Number.isInteger(total) || (total as number) < 0) {
    throw new Error(`JLL ${propertyType} filter lacks a finite nonnegative total`);
  }
  const uniqueUrls = new Set([...urls].map((url) => clean(url)).filter(Boolean));
  if (uniqueUrls.size !== total) {
    throw new Error(
      `JLL ${propertyType} filter reconciled ${uniqueUrls.size} unique cards against reported total ${total}`
    );
  }
}

export async function fetchJllSearchPage(tx: Tx, propertyType: string, page: number): Promise<{
  total: number | null;
  listings: any[];
}> {
  const searchUrl = jllFilteredSearchUrl(tx === "sale" ? "sale" : "rent", propertyType, page);
  const waits = [8000, 12000, 16000];
  let lastParsed: { total: number | null; listings: any[] } | null = null;
  let lastValidationError: unknown = null;
  for (const waitFor of waits) {
    const html = await scrapeRaw(searchUrl, {
      waitFor,
      ...(requireFreshDetails() ? { maxAge: 0 } : {}),
    });
    const parsed = parseJllSearchPage(html, tx, propertyType, page);
    lastParsed = parsed;
    if (requireFreshDetails()) {
      try {
        assertJllSearchPageCompleteness(parsed, page, null, true);
        return parsed;
      } catch (error) {
        lastValidationError = error;
        console.error(
          `  jll/${tx}/${propertyType}: page ${page} failed strict coverage validation ` +
            `(${String(error)}); retrying with waitFor=${waitFor}`
        );
        continue;
      }
    }
    if (parsed.listings.length > 0 || parsed.total === 0) return parsed;
    console.error(
      `  jll/${tx}/${propertyType}: page ${page} rendered 0 cards (total ${parsed.total ?? "?"}); retrying with waitFor=${waitFor}`
    );
  }
  if (lastValidationError) throw lastValidationError;
  return lastParsed ?? { total: null, listings: [] };
}

export function mergeJllListing(existing: any, candidate: any, propertyType: string, page: number) {
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

export function jllNextData(rawHtml: string): any | null {
  const $ = cheerio.load(rawHtml);
  const text = $("#__NEXT_DATA__").first().text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export function jllDetailCacheDir(): string {
  return process.env.JLL_DETAIL_CACHE_DIR ?? "out/cache/jll-detail";
}

export function jllDetailCachePath(url: string): string {
  const key = createHash("sha1").update(normalizedJllListingUrl(url)).digest("hex");
  return `${jllDetailCacheDir()}/${key}.json`;
}

export function jllCachedAtMeetsBoundary(cachedAt: unknown, boundary = process.env.JLL_DETAIL_CACHE_MIN_CACHED_AT): boolean {
  if (!boundary) return true;
  if (typeof cachedAt !== "string") return false;
  const cachedMs = Date.parse(cachedAt);
  const boundaryMs = Date.parse(boundary);
  return Number.isFinite(cachedMs) && Number.isFinite(boundaryMs) && cachedMs >= boundaryMs;
}

export function readJllDetailCache(url: string): ScrapedDoc | null {
  const path = jllDetailCachePath(url);
  if (!existsSync(path)) return null;
  try {
    const cached = JSON.parse(readFileSync(path, "utf8"));
    if (cached.url !== normalizedJllListingUrl(url)) return null;
    if (typeof cached.rawHtml !== "string") return null;
    if (!jllCachedAtMeetsBoundary(cached.cachedAt)) return null;
    if (!generationMatches(cached.generationId)) return null;
    const observedAt =
      typeof cached.detailObservedAt === "string" ? cached.detailObservedAt : cached.cachedAt;
    return {
      rawHtml: cached.rawHtml,
      markdown: typeof cached.markdown === "string" ? cached.markdown : "",
      links: Array.isArray(cached.links) ? cached.links.filter((link: any) => typeof link === "string") : [],
      images: Array.isArray(cached.images) ? cached.images.filter((image: any) => typeof image === "string") : undefined,
      attributes: Array.isArray(cached.attributes) ? cached.attributes : undefined,
      metadata: cached.metadata,
      detailObservation: detailObservation(
        "jll_detail",
        "generation_cache",
        observedAt,
        { generationId: cached.generationId ?? null }
      ),
    };
  } catch {
    return null;
  }
}

export function writeJllDetailCache(url: string, doc: ScrapedDoc): void {
  const path = jllDetailCachePath(url);
  mkdirSync(dirname(path), { recursive: true });
  const tmp = `${path}.${process.pid}.tmp`;
  const observed = doc.detailObservation?.observedAt ?? new Date().toISOString();
  writeFileSync(
    tmp,
    JSON.stringify(
      {
        url: normalizedJllListingUrl(url),
        cachedAt: observed,
        generationId: doc.detailObservation?.generationId ?? refreshGenerationId(),
        detailObservedAt: observed,
        rawHtml: doc.rawHtml,
        markdown: doc.markdown,
        links: doc.links,
        images: doc.images,
        attributes: doc.attributes,
        metadata: doc.metadata,
      },
      null,
      2
    )
  );
  renameSync(tmp, path);
}

export async function scrapeJllDetailDoc(
  url: string,
  opts: { refresh?: boolean; waitFor?: number } = {}
): Promise<ScrapedDoc> {
  const cached = opts.refresh ? null : readJllDetailCache(url);
  if (cached) return cached;
  const scraped = await scrapeDoc(url, {
    waitFor: opts.waitFor ?? JLL_DETAIL_WAIT_MS,
    timeout: 120000,
    ...(requireFreshDetails() ? { maxAge: 0 } : {}),
  });
  const doc: ScrapedDoc = {
    ...scraped,
    detailObservation: detailObservation("jll_detail", "live"),
  };
  writeJllDetailCache(url, doc);
  return doc;
}

export function jllPublicProfileUrl(pageUrl: any): string | null {
  const slug = clean(pageUrl);
  if (!slug) return null;
  if (/^https?:\/\//i.test(slug)) return slug;
  return `https://www.us.jll.com/en/people/${slug.replace(/^\/+/, "")}`;
}

export function jllStringUrls(values: any): string[] {
  if (!Array.isArray(values)) return [];
  return dedupeStrings(values.map((value) => clean(value))).filter((url) => /^https?:\/\//i.test(url));
}

export function jllSurfaceAreaSqft(property: any): number | null {
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

export function jllDescription(property: any): string | null {
  const sections = Array.isArray(property?.descriptionSections) ? property.descriptionSections : [];
  const pieces = sections
    .flatMap((section: any) => [stripHtmlText(section?.title), stripHtmlText(section?.content)])
    .filter(Boolean);
  const highlights = Array.isArray(property?.highlights)
    ? property.highlights.map((item: any) => stripHtmlText(item)).filter(Boolean)
    : [];
  return clean([...pieces, ...highlights].join("\n\n"));
}

/**
 * Extract a single normalized license string from a JLL broker licenses array.
 * Each entry has { location, licenseNumber } (detail-page shape).
 * Returns the first entry formatted as "location: licenseNumber", or null.
 */
export function jllExtractLicense(licenses: any): string | null {
  const arr = Array.isArray(licenses) ? licenses : [];
  const first = arr[0];
  if (!first) return null;
  const location = clean(first?.location ?? first?.state ?? first?.type);
  const number = clean(first?.licenseNumber ?? first?.number);
  if (number && location) return `${location}: ${number}`;
  if (number) return number;
  return null;
}

export function jllContacts(brokersRaw: any[]): any[] {
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
        license: jllExtractLicense(broker?.brokerLicenses ?? broker?.licenses),
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

// Promote the stranded JLL detail video / virtual-tour / 360 fields for
// harvestDetail. `videos`, `virtualTours`, and `view360URLs` are each string-url
// arrays in __NEXT_DATA__ property.* that the adapter previously dropped (only
// kept as raw counts in jllDetail). Video urls are emitted as BARE STRINGS so the
// harvester derives provider + embedUrl (vimeo/youtube); tour/360 urls are emitted
// as TYPED virtual_tour items so they keep that classification even on hosts the
// harvester does not recognize. harvestDetail dedups by url. Never throws.
export function jllStrandedMedia(property: any): (MediaItem | string)[] {
  const out: (MediaItem | string)[] = [];
  for (const url of jllStringUrls(Array.isArray(property?.videos) ? property.videos : [])) {
    out.push(url);
  }
  for (const value of [property?.virtualTours, property?.view360URLs]) {
    for (const url of jllStringUrls(Array.isArray(value) ? value : value != null ? [value] : [])) {
      out.push({ mediaType: "virtual_tour", provider: null, url, embedUrl: null, title: null });
    }
  }
  return out;
}

// Promote JLL floor-plan urls as classified floor_plan DocItems (they otherwise
// fold anonymously into the brochures channel). The brochures list already
// includes floorPlans for backward compatibility; harvest dedups by url so a
// floor plan is not double-counted, but is now correctly typed.
export function jllStrandedDocs(property: any): DocItem[] {
  return jllStringUrls(property?.floorPlans).map((url) => ({
    url,
    title: titleFromFilename(url),
    docType: "floor_plan" as const,
  }));
}

// Lift stranded structured fields the JLL detail payload exposes but the adapter
// previously dropped, onto the existing listing keys cre_ingest.to_row maps
// (camelCase -> column). Only clearly-present values are lifted; absent fields
// stay undefined and prune() removes them, so this never clobbers good data.
//
// Phase-2 additions (additive only):
//   buildingClass  <- normBuildingClass(property.buildingClass) e.g. "Class A" -> "A"
//   highlights     <- property.highlights[].title (objects with .title) or plain strings
//   amenities      <- property.amenities[] (strings or {name} objects)
//   canonicalUrl   <- property.pageUrl (absolute) as the canonical detail URL
//   extraFacts     <- { location_description } from property.locationDescription
export function jllStrandedStructured(property: any): Record<string, any> {
  const amenities = Array.isArray(property?.amenities)
    ? dedupeStrings(
        property.amenities
          .map((a: any) =>
            clean(typeof a === "string" ? a : a?.name ?? a?.title)
          )
          .filter(Boolean)
      )
    : [];

  // jllDetail.highlights is an array of objects with a .title string, not plain strings.
  // Fall back to stripHtmlText on plain strings for forward-compat.
  const highlights = Array.isArray(property?.highlights)
    ? dedupeStrings(
        property.highlights
          .map((h: any) =>
            typeof h === "string"
              ? clean(stripHtmlText(h))
              : clean(h?.title ?? h?.text ?? h?.value)
          )
          .filter(Boolean)
      )
    : [];

  // canonicalUrl: prefer the normalized absolute page URL over the relative slug.
  const pageUrl = clean(property?.pageUrl);
  const canonicalUrl = pageUrl
    ? pageUrl.startsWith("http")
      ? pageUrl
      : `https://property.jll.com${pageUrl.startsWith("/") ? "" : "/"}${pageUrl}`
    : undefined;

  // buildingClass: normalize "Class A"/"A"/"B"/etc. via the frozen lib helper.
  const buildingClass = normBuildingClass(clean(property?.buildingClass)) ?? undefined;

  // extraFacts: long-tail facts with no discrete column.
  const locationDescription = clean(property?.locationDescription);
  const extraFacts: Record<string, unknown> = {};
  if (locationDescription) extraFacts.location_description = locationDescription;

  return (
    prune({
      submarket: clean(property?.submarket),
      yearBuilt: num(property?.yearBuilt) ?? num(Number(property?.yearBuilt)),
      floors: num(property?.numberOfFloors) ?? num(property?.floors),
      units: num(property?.numberOfUnits) ?? num(property?.units),
      capRatePct: num(property?.capRate),
      amenities: amenities.length ? amenities : undefined,
      highlights: highlights.length ? highlights : undefined,
      buildingClass,
      canonicalUrl,
      extraFacts: Object.keys(extraFacts).length ? extraFacts : undefined,
    }) ?? {}
  );
}

export async function enrichJllListing(base: any): Promise<any> {
  if (!base.url) return base;
  try {
    let doc = await scrapeJllDetailDoc(base.url);
    let next = jllNextData(doc.rawHtml);
    let pageProps = next?.props?.pageProps;
    let property = pageProps?.property;
    if (!property && JLL_DETAIL_FALLBACK_WAIT_MS > JLL_DETAIL_WAIT_MS) {
      doc = await scrapeJllDetailDoc(base.url, { refresh: true, waitFor: JLL_DETAIL_FALLBACK_WAIT_MS });
      next = jllNextData(doc.rawHtml);
      pageProps = next?.props?.pageProps;
      property = pageProps?.property;
    }
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
    // Brochures channel keeps true brochures only; floor plans move to the typed
    // documents channel (floor_plan) via jllStrandedDocs so they are not
    // double-inserted (cre_listing_documents has no (listing_id,url) unique key,
    // so a url present in BOTH brochures and documents would insert twice).
    const brochures = jllStringUrls(property.brochures);
    const images = jllStringUrls(property.images);
    const detailUrl = clean(property.pageUrl) ?? clean(pageProps?.relativeUrl);
    const url = detailUrl ? normalizedJllListingUrl(detailUrl) : base.url;
    const brochureDocs = brochures.map((docUrl) => ({ name: titleFromFilename(docUrl), url: docUrl }));

    // Capture-everything harvest: unify the full detail page (markdown / links /
    // images / video+iframe attributes) with the stranded native fields promoted
    // via ctx.extra* (videos/virtualTours/view360URLs, floorPlans, native image
    // gallery). harvestDetail classifies + dedups by url. When the doc came from
    // the disk cache (no structured `images`), fall back to the native gallery for
    // the image channel rather than the rawHtml <img> regex (which would pull in
    // site-chrome icons); the page links/attributes still harvest from rawHtml.
    const harvestDoc: ScrapedDoc = Array.isArray(doc.images) ? doc : { ...doc, images };
    const harvested = harvestDetail(harvestDoc, {
      baseUrl: url,
      extraMedia: jllStrandedMedia(property),
      extraDocs: jllStrandedDocs(property),
      extraImages: images,
    });
    // Exclude any harvested doc whose url is already on the brochures channel, so
    // the same url is never inserted into cre_listing_documents twice.
    const brochureUrlSet = new Set(brochures.map((u) => u.toLowerCase()));
    const documents = harvested.documents.filter((d) => !brochureUrlSet.has(d.url.toLowerCase()));
    const photos = dedupeStrings([...(images.length ? images : base.photos ?? []), ...harvested.images]);
    const lifted = jllStrandedStructured(property);

    return prune({
      ...base,
      detailObservedAt: doc.detailObservation?.observedAt,
      freshnessProvenance: {
        detailScope: "detail_page",
        generationId: doc.detailObservation?.generationId ?? null,
        method: doc.detailObservation?.method ?? "jll_detail",
        cacheDisposition: doc.detailObservation?.cacheDisposition ?? "live",
      },
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
      ...lifted,
      brokerIds,
      contactsDetailed,
      brochures: brochureDocs,
      documents,
      media: harvested.media,
      links: harvested.links,
      photos,
      markdown: doc.markdown || base.markdown,
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

export async function srcJll(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  const tenure = tx === "sale" ? "sale" : "rent";
  const sourceUrl = `https://property.jll.com/search?tenureTypes=${tenure}`;
  if (monitor) {
    // Monitor mode is NOT supported for jll: the persisted external id is the
    // detail-page numeric property.id (enrichJllListing, id = property.id), which
    // cannot be recovered from the search card (the cheap key is only the URL
    // slug). Verified 11,230/11,230 slug != property.id against a full artifact.
    // Emitting slug-keyed rows would make every row read as NEW each run and
    // pollute the change ledger / enrichment queue, so jll stays on the
    // full-sweep cadence and emits no monitor rows. Short-circuit BEFORE the
    // search-page enumeration: the rows are discarded anyway, so paging every
    // propertyType filter would burn minutes for an empty result. A cheap path
    // would need URL-keyed reconciliation in cre_monitor.py (out of scope here).
    return {
      company: "JLL",
      sourceUrl,
      method: "Monitor mode unsupported (detail-derived numeric external id); full-sweep cadence only",
      totalAvailable: null,
      listings: [],
      note: "Monitor mode emits no rows for jll: its external id is the detail-page numeric property.id and cannot be derived from the search-card URL slug. Refresh this source via the full (non-monitor) collection path.",
    };
  }
  const listings: any[] = [];
  const byUrl = new Map<string, any>();
  const filterTotals: Record<string, number | null> = {};
  const maxByFilterPage: Record<string, number | null> = {};
  const filterUrls = new Map<string, Set<string>>(
    JLL_PROPERTY_TYPES.map((propertyType) => [propertyType, new Set<string>()])
  );
  const strictFreshness = requireFreshDetails();

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
          parsed.total === null
            ? null
            : Math.max(1, Math.ceil(parsed.total / JLL_SEARCH_PAGE_SIZE));
      } else {
        assertJllSearchPageCompleteness(
          parsed,
          page,
          filterTotals[propertyType],
          strictFreshness
        );
      }
      console.error(
        `  jll/${tx}/${propertyType}: page ${page}, ${parsed.listings.length} cards (filter total ${parsed.total ?? "?"})`
      );
      return { propertyType, ...parsed };
    });

    for (const result of pageResults) {
      const urls = filterUrls.get(result.propertyType)!;
      for (const listing of result.listings) {
        const url = clean(listing?.url);
        if (url) urls.add(url);
      }
    }

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
  const inventoryObservedAt = new Date().toISOString();
  for (const listing of listings) {
    listing.inventoryObservedAt = inventoryObservedAt;
  }
  const knownTotals = Object.values(filterTotals).filter((n): n is number => typeof n === "number");
  const total = knownTotals.length ? knownTotals.reduce((sum, n) => sum + n, 0) : null;
  let coverageTruncated = false;
  for (const propertyType of JLL_PROPERTY_TYPES) {
    try {
      assertJllFilterCoverage(
        propertyType,
        filterTotals[propertyType] ?? null,
        filterUrls.get(propertyType) ?? [],
        strictFreshness
      );
    } catch (error) {
      if (Number.isFinite(max) && listings.length >= max) {
        coverageTruncated = true;
        continue;
      }
      throw error;
    }
  }
  if (strictFreshness && Number.isFinite(max) && listings.length >= max) {
    coverageTruncated = true;
  }
  let enrichedCount = 0;
  const enriched = await pmap(listings, JLL_DETAIL_CONCURRENCY, async (listing) => {
    const row = await enrichJllListing(listing);
    enrichedCount++;
    if (enrichedCount % 100 === 0 || enrichedCount === listings.length) {
      console.error(`  jll/${tx}: detail enriched ${enrichedCount}/${listings.length}`);
    }
    return row;
  });
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
    truncated: coverageTruncated,
    note: `Per-filter source totals before cross-filter de-dupe: ${totalEvidence}. Detail enrichment stores public brochure/image/profile URLs only and retains per-row detailError if a detail scrape fails.`,
  };
}
