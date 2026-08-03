// sources/newmark.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import { CONCURRENCY } from "../lib/config.js";
import { detailObservation, requireFreshDetails } from "../lib/freshness.js";
import { stripHtmlText } from "../lib/html.js";
import { harvestDetail } from "../lib/harvest.js";
import { parseMoney } from "../lib/parse.js";
import { scrapeRaw } from "../lib/scrape.js";
import { ScrapedDoc, SourceResult, Tx } from "../types.js";
import { clean, num, pmap } from "../lib/util.js";

/**
 * Parse the Newmark sale_price string, returning null for non-numeric values
 * (e.g. 'Subject to Offer') and $0 placeholders. Uses parseMoney per contract
 * (Section B: salePriceUsd <- parseMoney(sale_price) rejecting 'Subject to Offer').
 */
export function newmarkSalePrice(raw: any): number | null {
  if (typeof raw !== "string") return null;
  const s = raw.trim();
  // Reject known non-numeric strings (case-insensitive guard).
  if (/subject\s+to\s+offer/i.test(s)) return null;
  // parseMoney strips $ and commas; returns null for non-numeric / zero values.
  return parseMoney(s);
}


// --- Newmark: Algolia search API, credentials read from the page ---

export let newmarkCreds: { appId: string; searchKey: string; indexName: string } | null = null;
export const NEWMARK_BOOTSTRAP_MAX_AGE_MS = 24 * 60 * 60 * 1000;
export type NewmarkPeopleLookup =
  | { status: "matched"; person: any }
  | { status: "verified_absent" }
  | { status: "failed"; error: string };
export const newmarkPeopleCache = new Map<string, Promise<NewmarkPeopleLookup>>();

export async function newmarkAlgoliaJson(url: string): Promise<any> {
  const res = await fetch(url, {
    headers: {
      accept: "application/json",
      "user-agent": "Mozilla/5.0 CRE collector",
    },
  });
  if (!res.ok) throw new Error(`Newmark Algolia HTTP ${res.status}`);
  return res.json();
}

export function normalizePersonName(value: any): string | null {
  return clean(value)?.toLowerCase().replace(/\s+/g, " ") ?? null;
}

export function classifyNewmarkPeopleLookup(
  result: any,
  name: string | null,
  strict = requireFreshDetails()
): NewmarkPeopleLookup {
  const key = normalizePersonName(name);
  if (!key) return { status: "verified_absent" };
  if (!Array.isArray(result?.hits)) {
    const error = "Newmark People index response has no hits array";
    if (strict) throw new Error(error);
    return { status: "failed", error };
  }
  if (
    !Number.isInteger(result.nbHits)
    || result.nbHits < 0
    || result.nbHits < result.hits.length
    || result.hits.some(
      (hit: any) => !hit || typeof hit !== "object" || Array.isArray(hit)
    )
  ) {
    const error =
      "Newmark People index response has incoherent hits/nbHits";
    if (strict) throw new Error(error);
    return { status: "failed", error };
  }
  const person = result.hits.find((hit: any) => {
    const fullName = normalizePersonName(hit?.fullName ?? hit?.title);
    return fullName === key;
  });
  return person
    ? { status: "matched", person }
    : { status: "verified_absent" };
}

export function newmarkPeopleFailure(
  err: unknown,
  name: string | null,
  strict = requireFreshDetails()
): NewmarkPeopleLookup {
  const error = err instanceof Error ? err.message : String(err);
  if (strict) {
    throw new Error(`Newmark people lookup failed for ${name ?? "unknown"}: ${error}`);
  }
  return { status: "failed", error };
}

export function newmarkPeopleListingFields(
  peopleLookup: NewmarkPeopleLookup,
  monitor = false
): {
  contactsDetailed?: any[];
  preserveChildCollections?: true;
  newmarkPeopleLookupStatus?: NewmarkPeopleLookup["status"];
} {
  if (!monitor && peopleLookup.status === "failed") {
    return {
      preserveChildCollections: true,
      newmarkPeopleLookupStatus: "failed",
    };
  }
  const person =
    peopleLookup.status === "matched" ? peopleLookup.person : null;
  const contactsDetailed = person
    ? [
        {
          name: clean(person.fullName) ?? clean(person.title),
          title: clean(person.positionJobTitle),
          email: clean(person.email),
          phone:
            stripHtmlText(person.phone)
            ?? stripHtmlText(person.mobilePhoneNumber),
          company: "Newmark",
          office: Array.isArray(person.offices)
            ? clean(person.offices.join(", "))
            : clean(person.offices),
          profileUrl: newmarkAbsoluteUrl(person.url),
          avatarUrl: Array.isArray(person.thumbnails)
            ? clean(person.thumbnails.at(-1)?.url)
            : null,
        },
      ]
    : [];
  return {
    contactsDetailed,
    newmarkPeopleLookupStatus: monitor ? undefined : peopleLookup.status,
  };
}

export function newmarkState(hit: any): string | null {
  const state = clean(hit.state);
  if (state) return state;
  const stateCode = clean(hit.state_code);
  if (stateCode && /^[A-Za-z]{2}$/.test(stateCode)) return stateCode.toUpperCase();
  const city = clean(hit.city)?.toLowerCase();
  const zip = clean(hit.zip);
  if (city === "washington" && zip?.startsWith("200")) return "DC";
  return null;
}

export function newmarkAbsoluteUrl(value: any): string | null {
  const url = clean(value);
  if (!url) return null;
  try {
    return new URL(url, "https://www.nmrk.com").toString();
  } catch {
    return null;
  }
}

// Collect every gallery image URL a Newmark Algolia hit exposes (full set, no
// truncation), absolutized against nmrk.com. Mirrors the existing `thumbnails`
// shape ({ url }) but keeps ALL entries instead of only the last one.
export function newmarkGalleryUrls(hit: any): string[] {
  const out: string[] = [];
  for (const t of hit?.thumbnails ?? []) {
    const u = newmarkAbsoluteUrl(t?.url ?? t);
    if (u) out.push(u);
  }
  for (const t of hit?.images ?? []) {
    const u = newmarkAbsoluteUrl(typeof t === "string" ? t : t?.url);
    if (u) out.push(u);
  }
  return [...new Set(out)];
}

// Candidate media / virtual-tour URLs the Algolia record may carry. harvestDetail
// classifies and drops anything that is not a recognized media url, so probing a
// defensive key set is safe (non-media values fall through to links/other).
const NEWMARK_MEDIA_KEYS = [
  "video_url", "videoUrl", "virtual_tour_url", "virtualTourUrl",
  "tour_url", "tourUrl", "matterport_url", "matterportUrl", "video", "virtualTour",
];
// Candidate document URLs (offering memorandum / brochure / flyer / marketing).
const NEWMARK_DOC_KEYS = [
  "brochure_url", "brochureUrl", "flyer_url", "flyerUrl",
  "marketing_package_url", "marketingPackageUrl", "om_url", "omUrl", "brochure",
];

export function newmarkExtraUrls(hit: any): { media: string[]; docs: string[] } {
  const media: string[] = [];
  const docs: string[] = [];
  for (const k of NEWMARK_MEDIA_KEYS) {
    const u = newmarkAbsoluteUrl(hit?.[k]);
    if (u) media.push(u);
  }
  for (const k of NEWMARK_DOC_KEYS) {
    const u = newmarkAbsoluteUrl(hit?.[k]);
    if (u) docs.push(u);
  }
  return { media, docs };
}

export function newmarkCoverageTruncated(
  total: number,
  collected: number,
  max: number,
  partitionTruncated = false
): boolean {
  const expected = Math.min(max, total);
  return (
    partitionTruncated ||
    collected < expected ||
    (Number.isFinite(max) && expected < total)
  );
}

export function assertNewmarkAlgoliaInventoryPage(
  result: any,
  context: string,
  strict = requireFreshDetails()
): any {
  if (!Array.isArray(result?.hits)) {
    throw new Error(`Newmark Algolia ${context} response has no hits array`);
  }
  if (!strict) return result;
  const total = result.nbHits;
  if (!Number.isInteger(total) || total < 0) {
    throw new Error(
      `Newmark Algolia ${context} response requires a finite nonnegative integer nbHits`
    );
  }
  if (total < result.hits.length) {
    throw new Error(
      `Newmark Algolia ${context} response nbHits ${total} is below returned hits ${result.hits.length}`
    );
  }
  return result;
}

export async function srcNewmarkAlgoliaLegacy(
  tx: Tx,
  max: number,
  monitor: boolean
): Promise<SourceResult> {
  const sourceUrl = "https://www.nmrk.com/properties";
  const strictFreshness = requireFreshDetails();
  if (!newmarkCreds) {
    for (const waitFor of [3000, 8000]) {
      // This page only bootstraps public Algolia routing credentials. Allow the
      // local Firecrawl cache to bridge a temporary Cloudflare denial; every
      // inventory and People query below still goes directly to live Algolia
      // and strict mode validates hits/nbHits before admitting an artifact.
      const html = await scrapeRaw(sourceUrl, {
        waitFor,
        maxAge: NEWMARK_BOOTSTRAP_MAX_AGE_MS,
      });
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
    return assertNewmarkAlgoliaInventoryPage(
      await newmarkAlgoliaJson(
      `https://${appId}-dsn.algolia.net/1/indexes/${indexName}?x-algolia-application-id=${appId}&x-algolia-api-key=${searchKey}&query=&hitsPerPage=${hitsPerPage}&page=${page}&facets=${encodeURIComponent(facetsField)}&facetFilters=${facetFilters}`
      ),
      `facet page ${page}`,
      strictFreshness
    );
  };
  const queryFilters = async (filters: string, hitsPerPage: number, page = 0) =>
    assertNewmarkAlgoliaInventoryPage(
      await newmarkAlgoliaJson(
        `https://${appId}-dsn.algolia.net/1/indexes/${indexName}?x-algolia-application-id=${appId}&x-algolia-api-key=${searchKey}&query=&hitsPerPage=${hitsPerPage}&page=${page}&filters=${encodeURIComponent(filters)}`
      ),
      `filtered page ${page}`,
      strictFreshness
    );
  const lookupPerson = (name: string | null): Promise<NewmarkPeopleLookup> => {
    const key = normalizePersonName(name);
    if (!key) return Promise.resolve({ status: "verified_absent" });
    if (!newmarkPeopleCache.has(key)) {
      const run = (async () => {
        const facetFilters = encodeURIComponent(JSON.stringify(["sectionGroup:People", "siteHandle:enUs"]));
        const result = await newmarkAlgoliaJson(
          `https://${appId}-dsn.algolia.net/1/indexes/${indexName}?x-algolia-application-id=${appId}&x-algolia-api-key=${searchKey}&query=${encodeURIComponent(name ?? "")}&hitsPerPage=5&page=0&facetFilters=${facetFilters}`
        );
        return classifyNewmarkPeopleLookup(result, name, strictFreshness);
      })().catch((err) => {
        console.error(`  newmark: people lookup failed for ${name}: ${err}`);
        return newmarkPeopleFailure(err, name, strictFreshness);
      });
      newmarkPeopleCache.set(key, run);
      void run.then(
        (outcome) => {
          if (
            outcome.status === "failed"
            && newmarkPeopleCache.get(key) === run
          ) {
            newmarkPeopleCache.delete(key);
          }
        },
        () => {
          if (newmarkPeopleCache.get(key) === run) {
            newmarkPeopleCache.delete(key);
          }
        }
      );
    }
    return newmarkPeopleCache.get(key)!;
  };

  const first = await query([], 1000);
  const total: number = first.nbHits ?? first.hits.length;
  const hitMap = new Map<string, any>();
  for (const h of first.hits) hitMap.set(h.objectID ?? h.slug ?? JSON.stringify(h), h);

  // Algolia caps retrievable hits (~1000/query). Split by state facet for full coverage.
  // Set when a facet (or facet combo, or the no-state recovery) exceeds the
  // ~1000-hit cap with no further way to sub-split: that pass KNOWINGLY left
  // hits unreachable, so the enumeration is partial for this run only.
  let coverageTruncated = false;
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
          coverageTruncated = true;
          return;
        }
        console.error(`  newmark/${tx}: state ${st} has ${r.nbHits} hits, sub-splitting across ${types.length} property types`);
        await pmap(types, 2, async (pt) => {
          const r2 = await query([`state:${st}`, `property_types:${pt}`], 1000);
          for (const h of r2.hits ?? []) hitMap.set(h.objectID ?? h.slug ?? JSON.stringify(h), h);
          if ((r2.nbHits ?? 0) > 1000) {
            console.error(`  newmark/${tx}: WARNING ${st}/${pt} still ${r2.nbHits} hits, >1000 cap; coverage truncated`);
            coverageTruncated = true;
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
        coverageTruncated = true;
      }
    }
  }

  coverageTruncated = newmarkCoverageTruncated(total, hitMap.size, max, coverageTruncated);
  if (coverageTruncated && hitMap.size < Math.min(max, total)) {
    console.error(
      `  newmark/${tx}: WARNING recovered ${hitMap.size}/${Math.min(max, total)} expected Algolia hits; coverage truncated`
    );
  }
  const inventoryObservedAt = new Date().toISOString();
  const sourceObservation = detailObservation(
    "newmark_algolia_public_record",
    "live",
    inventoryObservedAt
  );
  const hits = [...hitMap.values()].slice(0, Math.min(max, Number.MAX_SAFE_INTEGER));
  const listings = await pmap(hits, Math.min(CONCURRENCY, 4), async (h: any) => {
    // Monitor mode: skip the per-hit People-Algolia contact lookup (the only
    // detail call here); all other hit fields are free in the enumeration.
    const peopleLookup: NewmarkPeopleLookup = monitor
      ? { status: "verified_absent" }
      : await lookupPerson(clean(h.broker_name));
    const peopleFields = newmarkPeopleListingFields(peopleLookup, monitor);
    // Capture-everything (FULL PATH ONLY): the Algolia enumeration is the same
    // payload monitor consumes, so media/links/gallery promotion is gated behind
    // `!monitor` to keep the monitor artifact byte-identical (cre_monitor.py must
    // never see media). harvestDetail classifies the hit's candidate media/doc/
    // tour URLs over a synthetic (rawHtml-less) doc and dedups them.
    const propUrl = h.url ? `https://www.nmrk.com${h.url}` : undefined;
    let media: ReturnType<typeof harvestDetail>["media"] | undefined;
    let links: ReturnType<typeof harvestDetail>["links"] | undefined;
    let documents: ReturnType<typeof harvestDetail>["documents"] | undefined;
    let photos: string[] = (h.thumbnails ?? []).slice(-1).map((t: any) => t.url);
    if (!monitor) {
      const gallery = newmarkGalleryUrls(h);
      const { media: mediaUrls, docs: docUrls } = newmarkExtraUrls(h);
      const synthetic: ScrapedDoc = { rawHtml: "", markdown: "", links: [] };
      const harvested = harvestDetail(synthetic, {
        extraMedia: mediaUrls,
        extraDocs: docUrls,
        extraImages: gallery,
        baseUrl: propUrl,
      });
      if (harvested.media.length) media = harvested.media;
      if (harvested.links.length) links = harvested.links;
      if (harvested.documents.length) documents = harvested.documents;
      // Full gallery (no truncation) on the full path; monitor keeps slice(-1).
      if (gallery.length) photos = gallery;
    }
    // WS1 scalar lift: fields from rawNewmarkHit (h is the Algolia hit = rawNewmarkHit).
    // market, units, statusBadge, propertySubtype are new camelCase fields per
    // Phase-2 Data-Lift Contract Section B. county/submarket already emitted above.
    // canonicalUrl is the absolute listing URL (already in propUrl / url).
    const salePriceUsd = tx === "sale" ? newmarkSalePrice(h.sale_price) : null;
    const units = typeof h.number_of_units === "number" && h.number_of_units > 0
      ? h.number_of_units
      : null;
    return {
    id: clean(h.slug),
    name: clean(h.title),
    headline: clean(h.content),
    description: clean(h.content),
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
    market: clean(h.market) ?? null,
    country: clean(h.country_code) ?? "US",
    latitude: num(h.latitude),
    longitude: num(h.longitude),
    salePriceUsd,
    salePriceText: tx === "sale" && !h.sale_price ? "Contact broker for pricing" : null,
    buildingSizeSqft: num(h.building_size_sf),
    lotSizeAcres: num(h.lot_size_acres),
    units,
    // statusBadge gated behind `!monitor` (mirroring the media/links promotion
    // above): newmark has no native STATUS_SOURCE_PATHS, so letting the badge
    // into the monitor enumeration artifact would make norm_status non-None for
    // any future terminal feed value and shift the cre_source_index fingerprint.
    // Keeping it full-path-only makes monitor byte-identicality structural.
    statusBadge: monitor ? null : (clean(h.status) ?? null),
    propertySubtype: clean(h.property_subtype) ?? null,
    canonicalUrl: propUrl ?? null,
    brokerIds: [],
    ...peopleFields,
    newmarkBrokerProvenance: {
      broker_name: clean(h.broker_name),
      broker_id: h.broker_id ?? null,
      broker_ids: Array.isArray(h.broker_ids) ? h.broker_ids : [],
      second_broker_id: h.second_broker_id ?? null,
      third_broker_id: h.third_broker_id ?? null,
    },
    rawNewmarkHit: h,
    photos,
    media,
    links,
    documents,
    url: propUrl ?? null,
    lastUpdated: clean(h.updateDate)?.slice(0, 10) ?? null,
    inventoryObservedAt,
    detailObservedAt: sourceObservation.observedAt,
    freshnessProvenance: {
      detailScope: "source_native_public_record",
      generationId: sourceObservation.generationId,
      method: sourceObservation.method,
      cacheDisposition: sourceObservation.cacheDisposition,
    },
    };
  });
  return {
    company: "Newmark",
    sourceUrl,
    method: "Newmark Algolia search API (JSON; credentials read from the page; state/property-type split plus no-state recovery)",
    totalAvailable: total,
    listings,
    // True only when an Algolia facet exceeded the ~1000-hit cap with no further
    // sub-split this run; the enumeration is then a known under-count.
    truncated: coverageTruncated,
  };
}

const NEWMARK_NIM_API =
  "https://api-public.nim.nmrk.com/api/properties/search";
export const NEWMARK_NIM_PAGE_SIZE = 100;
export const NEWMARK_NIM_MAX_ATTEMPTS = 6;
const NEWMARK_NIM_TRANSIENT_STATUSES = new Set([
  408,
  425,
  429,
  500,
  502,
  503,
  504,
]);
const NEWMARK_NIM_US_REGIONS = new Set([
  "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
  "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
  "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
  "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
  "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
  "DC", "PR", "VI", "GU", "AS", "MP",
]);

function newmarkNimType(tx: Tx): 1 | 2 {
  return tx === "sale" ? 2 : 1;
}

export function assertNewmarkNimPage(
  result: any,
  context: string,
  page: number,
  take: number,
  expectedTotal: number | null,
  strict = requireFreshDetails()
): { data: any[]; total: number } {
  if (!result || typeof result !== "object" || Array.isArray(result)) {
    throw new Error(`Newmark NIM ${context} response is not an object`);
  }
  if (!Array.isArray(result.data)) {
    throw new Error(`Newmark NIM ${context} response has no data array`);
  }
  if (!Number.isInteger(result.total) || result.total < 0) {
    throw new Error(
      `Newmark NIM ${context} response requires a finite nonnegative integer total`
    );
  }
  if (expectedTotal !== null && result.total !== expectedTotal) {
    throw new Error(
      `Newmark NIM ${context} total changed from ${expectedTotal} to ${result.total}`
    );
  }
  if (strict) {
    const expectedRows = Math.max(
      0,
      Math.min(take, result.total - page * take)
    );
    if (result.data.length !== expectedRows) {
      throw new Error(
        `Newmark NIM ${context} returned ${result.data.length}/${expectedRows} expected rows`
      );
    }
    for (const [index, row] of result.data.entries()) {
      if (
        !row
        || typeof row !== "object"
        || Array.isArray(row)
        || !clean(row.id)
        || !clean(row.slug)
        || !Array.isArray(row.properties)
        || row.properties.length === 0
      ) {
        throw new Error(
          `Newmark NIM ${context} row ${index} lacks provider identity or properties`
        );
      }
    }
  }
  return result;
}

export async function newmarkNimPage(
  tx: Tx,
  page: number,
  take = NEWMARK_NIM_PAGE_SIZE,
  timeoutMs = 30_000,
  retryBaseMs = 1_000
): Promise<any> {
  const requestBody = JSON.stringify({
    type: newmarkNimType(tx),
    listingIds: [],
    propertyTypes: [],
    brokers: [],
    statuses: [],
    propertySubtypes: [],
    spaceTypes: [],
    buildingClasses: [],
    leaseTypes: [],
    excludeUnpriced: false,
    page,
    take,
    sortBy: "createdOn",
    isAscending: true,
  });
  for (let attempt = 1; attempt <= NEWMARK_NIM_MAX_ATTEMPTS; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let response: Response | undefined;
    let requestError: unknown;
    try {
      response = await fetch(NEWMARK_NIM_API, {
        method: "POST",
        headers: {
          accept: "application/json",
          "content-type": "application/json",
          "user-agent": "Mozilla/5.0 CRE collector",
        },
        body: requestBody,
        signal: controller.signal,
      });
      if (response.ok) {
        // Keep the same deadline through the response body. Fetch resolving
        // headers is not proof that the JSON payload has arrived.
        return await response.json();
      }
    } catch (error) {
      requestError = controller.signal.aborted
        ? new Error(`Newmark NIM request timed out after ${timeoutMs}ms`)
        : error;
    } finally {
      clearTimeout(timer);
    }
    if (requestError !== undefined) {
      if (attempt === NEWMARK_NIM_MAX_ATTEMPTS) throw requestError;
      await new Promise((resolve) =>
        setTimeout(
          resolve,
          Math.min(retryBaseMs * 2 ** (attempt - 1), 16_000)
        )
      );
      continue;
    }
    if (response === undefined) {
      throw new Error("Newmark NIM request ended without a response");
    }
    if (
      !NEWMARK_NIM_TRANSIENT_STATUSES.has(response.status)
      || attempt === NEWMARK_NIM_MAX_ATTEMPTS
    ) {
      throw new Error(
        `Newmark NIM HTTP ${response.status} after ${attempt} attempt(s)`
      );
    }
    await response.body?.cancel();
    const retryDelayMs = newmarkNimRetryDelayMs(
      response.status,
      response.headers.get("retry-after"),
      attempt,
      retryBaseMs
    );
    await new Promise((resolve) => setTimeout(resolve, retryDelayMs));
  }
  throw new Error("Newmark NIM request exhausted without a response");
}

export type NewmarkNimRegion = "us" | "non_us" | "ambiguous";

export function newmarkNimRetryDelayMs(
  status: number,
  retryAfterHeader: string | null,
  attempt: number,
  retryBaseMs = 1_000
): number {
  const defaultDelayMs = status === 429
    ? Math.min(10_000 * 2 ** (attempt - 1), 60_000)
    : Math.min(retryBaseMs * 2 ** (attempt - 1), 16_000);
  if (retryAfterHeader === null || retryAfterHeader.trim() === "") {
    return defaultDelayMs;
  }
  const retryAfterSeconds = Number(retryAfterHeader);
  return Number.isFinite(retryAfterSeconds) && retryAfterSeconds >= 0
    ? Math.min(retryAfterSeconds * 1_000, 60_000)
    : defaultDelayMs;
}

export function newmarkNimPropertyRegion(property: any): NewmarkNimRegion {
  const country = clean(property?.countryCode)?.toUpperCase();
  if (country) {
    return country === "US" || country === "USA" ? "us" : "non_us";
  }
  const state = clean(property?.stateAbbreviation)?.toUpperCase();
  const postalCode = clean(property?.zip);
  if (
    state
    && NEWMARK_NIM_US_REGIONS.has(state)
    && postalCode
    && /^\d{5}(?:-\d{4})?$/.test(postalCode)
  ) {
    return "us";
  }
  return "ambiguous";
}

export function newmarkNimCanonicalIdentity(record: any): {
  id: string | null;
  url: string | null;
} {
  const slug = clean(record?.slug);
  const safeSlug =
    slug
    && slug !== "."
    && slug !== ".."
    && /^[A-Za-z0-9._~-]+$/.test(slug)
      ? slug
      : null;
  const providerUrl = clean(record?.externalWebsiteUrl);
  if (!providerUrl) {
    if (slug && !safeSlug) {
      throw new Error("Newmark NIM fallback slug identity is unsafe");
    }
    return safeSlug
      ? {
          id: safeSlug,
          url: `https://www.nmrk.com/properties/${encodeURIComponent(safeSlug)}`,
        }
      : { id: null, url: null };
  }
  let parsed: URL;
  try {
    parsed = new URL(providerUrl);
  } catch {
    throw new Error("Newmark NIM externalWebsiteUrl is not a valid URL");
  }
  const hostname = parsed.hostname.toLowerCase();
  if (
    parsed.protocol !== "https:"
    || parsed.username
    || parsed.password
    || parsed.port
  ) {
    throw new Error("Newmark NIM externalWebsiteUrl is not a safe HTTPS URL");
  }
  if (
    hostname === "my.rcm1.com"
    || hostname === "properties.nmrk.com"
  ) {
    if (slug && !safeSlug) {
      throw new Error("Newmark NIM fallback slug identity is unsafe");
    }
    return safeSlug
      ? {
          id: safeSlug,
          url: `https://www.nmrk.com/properties/${encodeURIComponent(safeSlug)}`,
        }
      : { id: null, url: null };
  }
  if (
    !["www.nmrk.com", "nmrk.com"].includes(hostname)
  ) {
    throw new Error(
      `Newmark NIM externalWebsiteUrl has an unsupported host: ${hostname}`
    );
  }
  const match = parsed.pathname.match(/^\/properties\/([^/]+)\/?$/);
  if (!match) {
    throw new Error("Newmark NIM externalWebsiteUrl has an unexpected path");
  }
  let id: string;
  try {
    id = decodeURIComponent(match[1]);
  } catch (error) {
    throw new Error(
      `Newmark NIM canonical URL has an invalid encoded identity: ${
        error instanceof Error ? error.message : String(error)
      }`
    );
  }
  if (
    id === "."
    || id === ".."
    || !/^[A-Za-z0-9._~-]+$/.test(id)
  ) {
    throw new Error("Newmark NIM canonical identity is unsafe");
  }
  return {
    id,
    url: `https://www.nmrk.com/properties/${match[1]}`,
  };
}

export function newmarkNimMeasurementFields(property: any): {
  buildingSizeSqft: number | null;
  lotSizeAcres: number | null;
  units: number | null;
  kind: "building_sqft" | "lot_acres" | "units" | null;
  normalizedValue: number | null;
} {
  const unit = clean(property?.unitOfMeasurement)?.toLowerCase();
  const size = num(property?.size);
  if (size !== null && size < 0) {
    throw new Error("Newmark NIM measurement must be positive");
  }
  if (unit === "units") {
    if (size !== null && !Number.isInteger(size)) {
      throw new Error("Newmark NIM unit count must be a positive integer");
    }
    return {
      buildingSizeSqft: null,
      lotSizeAcres: null,
      units: size,
      kind: "units",
      normalizedValue: size,
    };
  }
  if (unit === "acres") {
    return {
      buildingSizeSqft: null,
      lotSizeAcres: size,
      units: null,
      kind: "lot_acres",
      normalizedValue: size,
    };
  }
  if (unit === "hectares") {
    const acres = size === null ? null : size * 2.47105381;
    return {
      buildingSizeSqft: null,
      lotSizeAcres: acres,
      units: null,
      kind: "lot_acres",
      normalizedValue: acres,
    };
  }
  if (unit === "sq. meters") {
    // NIM's `size` is already its normalized square-foot value; `sizeSf`
    // carries the display-unit value despite the misleading field name.
    return {
      buildingSizeSqft: size,
      lotSizeAcres: null,
      units: null,
      kind: "building_sqft",
      normalizedValue: size,
    };
  }
  if (unit && unit !== "sq. ft.") {
    throw new Error(
      `Newmark NIM has an unsupported unit of measurement: ${unit}`
    );
  }
  const buildingSizeSqft =
    unit === "sq. ft."
      ? size ?? num(property?.sizeSf)
      : num(property?.sizeSf) ?? size;
  return {
    buildingSizeSqft,
    lotSizeAcres: null,
    units: null,
    kind: buildingSizeSqft === null ? null : "building_sqft",
    normalizedValue: buildingSizeSqft,
  };
}

export function mapNewmarkNimListing(
  record: any,
  tx: Tx,
  inventoryObservedAt: string,
  strict = requireFreshDetails()
): any | null {
  const properties = Array.isArray(record?.properties) ? record.properties : [];
  if (strict && properties.length === 0) {
    throw new Error(
      `Newmark NIM record ${clean(record?.id) ?? "unknown"} has no property geography`
    );
  }
  const property = properties.find(
    (candidate: any) => newmarkNimPropertyRegion(candidate) === "us"
  );
  if (
    !property
    && strict
    && properties.some(
      (candidate: any) => newmarkNimPropertyRegion(candidate) === "ambiguous"
    )
  ) {
    throw new Error(
      `Newmark NIM record ${clean(record?.id) ?? "unknown"} has ambiguous geography`
    );
  }
  const canonicalIdentity = newmarkNimCanonicalIdentity(record);
  if (!property || !canonicalIdentity.id || !canonicalIdentity.url) return null;
  const observation = detailObservation(
    "newmark_nim_public_inventory",
    "live",
    inventoryObservedAt
  );
  const priceText = clean(record?.priceSummary);
  const measurements = newmarkNimMeasurementFields(property);
  const candidateSalePrice =
    tx === "sale" ? newmarkSalePrice(priceText) : null;
  const rejectSalePrice =
    candidateSalePrice !== null
    && measurements.buildingSizeSqft !== null
    && measurements.buildingSizeSqft > 100
    && candidateSalePrice / measurements.buildingSizeSqft > 10_000;
  const propertyType = clean(property?.propertyTypeLabelOverride);
  const mainImageUrl = clean(record?.mainImageUrl);
  const cardPhotos =
    mainImageUrl && /^https?:\/\//i.test(mainImageUrl)
      ? [mainImageUrl]
      : [];
  return {
    id: canonicalIdentity.id,
    name:
      clean(record?.name)
      ?? clean(property?.address)
      ?? canonicalIdentity.id,
    transactionType: tx === "sale" ? "Sale" : "Lease",
    assetType: propertyType,
    street: clean(property?.address),
    city: clean(property?.city),
    state:
      clean(property?.stateAbbreviation)
      ?? clean(property?.stateDescription),
    postalCode: clean(property?.zip),
    county: clean(property?.county),
    country: "US",
    latitude: num(property?.latitude),
    longitude: num(property?.longitude),
    salePriceUsd: rejectSalePrice ? null : candidateSalePrice,
    salePriceText: tx === "sale" ? priceText : null,
    buildingSizeSqft: measurements.buildingSizeSqft,
    lotSizeAcres: measurements.lotSizeAcres,
    units: measurements.units,
    canonicalUrl: canonicalIdentity.url,
    url: canonicalIdentity.url,
    lastUpdated: clean(record?.modifiedOn)?.slice(0, 10) ?? null,
    inventoryObservedAt,
    preserveChildCollections: true,
    newmarkNimMeasurement: {
      kind: measurements.kind,
      sourceUnit: clean(property?.unitOfMeasurement),
      normalizedValue: measurements.normalizedValue,
    },
    newmarkNimPriceRejected: rejectSalePrice
      ? "implausible_price_per_square_foot"
      : null,
    photos: cardPhotos,
    freshnessProvenance: {
      detailScope: "authoritative_inventory_feed",
      generationId: observation.generationId,
      method: observation.method,
      cacheDisposition: observation.cacheDisposition,
    },
    rawNewmarkNimRecord: record,
  };
}

export async function srcNewmark(
  tx: Tx,
  max: number,
  _monitor: boolean
): Promise<SourceResult> {
  const strict = requireFreshDetails();
  const first = assertNewmarkNimPage(
    await newmarkNimPage(tx, 0),
    `${tx} page 0`,
    0,
    NEWMARK_NIM_PAGE_SIZE,
    null,
    strict
  );
  const pageCount = Math.ceil(first.total / NEWMARK_NIM_PAGE_SIZE);
  const remainingPages = Array.from(
    { length: Math.max(0, pageCount - 1) },
    (_, index) => index + 1
  );
  const remaining = await pmap(
    remainingPages,
    1,
    async (page) =>
      assertNewmarkNimPage(
        await newmarkNimPage(tx, page),
        `${tx} page ${page}`,
        page,
        NEWMARK_NIM_PAGE_SIZE,
        first.total,
        strict
      )
  );
  const globalRows = [
    ...first.data,
    ...remaining.flatMap((page) => page.data),
  ];
  const uniqueProviderIds = new Set(
    globalRows.map((record) => clean(record?.id))
  );
  if (
    strict
    && (
      globalRows.length !== first.total
      || uniqueProviderIds.size !== first.total
      || uniqueProviderIds.has(null)
    )
  ) {
    throw new Error(
      `Newmark NIM ${tx} reconciliation failed: `
      + `${globalRows.length} rows, ${uniqueProviderIds.size} unique IDs, `
      + `${first.total} provider total`
    );
  }

  const inventoryObservedAt = new Date().toISOString();
  const usListings = globalRows
    .map((record) =>
      mapNewmarkNimListing(record, tx, inventoryObservedAt, strict)
    )
    .filter((listing): listing is any => listing !== null);
  const uniqueSlugs = new Set(usListings.map((listing) => listing.id));
  if (strict && uniqueSlugs.size !== usListings.length) {
    throw new Error(
      `Newmark NIM ${tx} output identity reconciliation failed: `
      + `${usListings.length} rows, ${uniqueSlugs.size} unique slugs`
    );
  }
  const capped = usListings.slice(
    0,
    Math.min(max, Number.MAX_SAFE_INTEGER)
  );
  return {
    company: "Newmark",
    sourceUrl: "https://nim.nmrk.com/properties?mode=external",
    method:
      "Newmark NIM public search API (complete ascending pagination; US inventory filtered after global reconciliation)",
    totalAvailable: usListings.length,
    listings: capped,
    truncated: capped.length < usListings.length,
  };
}
