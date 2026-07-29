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

export async function srcNewmark(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  const sourceUrl = "https://www.nmrk.com/properties";
  const strictFreshness = requireFreshDetails();
  if (!newmarkCreds) {
    for (const waitFor of [3000, 8000]) {
      const html = await scrapeRaw(sourceUrl, {
        waitFor,
        ...(strictFreshness ? { maxAge: 0 } : {}),
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
