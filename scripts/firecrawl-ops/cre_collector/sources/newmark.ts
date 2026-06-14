// sources/newmark.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import { CONCURRENCY } from "../lib/config.js";
import { stripHtmlText } from "../lib/html.js";
import { scrapeRaw } from "../lib/scrape.js";
import { SourceResult, Tx } from "../types.js";
import { clean, num, pmap } from "../lib/util.js";


// --- Newmark: Algolia search API, credentials read from the page ---

export let newmarkCreds: { appId: string; searchKey: string; indexName: string } | null = null;
export const newmarkPeopleCache = new Map<string, Promise<any | null>>();

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

export async function srcNewmark(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  const sourceUrl = "https://www.nmrk.com/properties";
  if (!newmarkCreds) {
    for (const waitFor of [3000, 8000]) {
      const html = await scrapeRaw(sourceUrl, { waitFor });
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
    return newmarkAlgoliaJson(
      `https://${appId}-dsn.algolia.net/1/indexes/${indexName}?x-algolia-application-id=${appId}&x-algolia-api-key=${searchKey}&query=&hitsPerPage=${hitsPerPage}&page=${page}&facets=${encodeURIComponent(facetsField)}&facetFilters=${facetFilters}`
    );
  };
  const queryFilters = async (filters: string, hitsPerPage: number, page = 0) =>
    newmarkAlgoliaJson(
      `https://${appId}-dsn.algolia.net/1/indexes/${indexName}?x-algolia-application-id=${appId}&x-algolia-api-key=${searchKey}&query=&hitsPerPage=${hitsPerPage}&page=${page}&filters=${encodeURIComponent(filters)}`
    );
  const lookupPerson = (name: string | null): Promise<any | null> => {
    const key = normalizePersonName(name);
    if (!key) return Promise.resolve(null);
    if (!newmarkPeopleCache.has(key)) {
      const run = (async () => {
        const facetFilters = encodeURIComponent(JSON.stringify(["sectionGroup:People", "siteHandle:enUs"]));
        const result = await newmarkAlgoliaJson(
          `https://${appId}-dsn.algolia.net/1/indexes/${indexName}?x-algolia-application-id=${appId}&x-algolia-api-key=${searchKey}&query=${encodeURIComponent(name ?? "")}&hitsPerPage=5&page=0&facetFilters=${facetFilters}`
        );
        const hits = Array.isArray(result.hits) ? result.hits : [];
        return (
          hits.find((h: any) => {
            const fullName = normalizePersonName(h.fullName ?? h.title);
            return fullName === key;
          }) ?? null
        );
      })().catch((err) => {
        console.error(`  newmark: people lookup failed for ${name}: ${err}`);
        return null;
      });
      newmarkPeopleCache.set(key, run);
    }
    return newmarkPeopleCache.get(key)!;
  };

  const first = await query([], 1000);
  if (!Array.isArray(first.hits)) throw new Error("Newmark Algolia response has no hits array");
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

  const hits = [...hitMap.values()].slice(0, Math.min(max, Number.MAX_SAFE_INTEGER));
  const listings = await pmap(hits, Math.min(CONCURRENCY, 4), async (h: any) => {
    // Monitor mode: skip the per-hit People-Algolia contact lookup (the only
    // detail call here); all other hit fields are free in the enumeration.
    const person = monitor ? null : await lookupPerson(clean(h.broker_name));
    const contactsDetailed = person
      ? [
          {
            name: clean(person.fullName) ?? clean(person.title),
            title: clean(person.positionJobTitle),
            email: clean(person.email),
            phone: stripHtmlText(person.phone) ?? stripHtmlText(person.mobilePhoneNumber),
            company: "Newmark",
            office: Array.isArray(person.offices) ? clean(person.offices.join(", ")) : clean(person.offices),
            profileUrl: newmarkAbsoluteUrl(person.url),
            avatarUrl: Array.isArray(person.thumbnails) ? clean(person.thumbnails.at(-1)?.url) : null,
          },
        ]
      : [];
    return {
    id: clean(h.slug),
    name: clean(h.title),
    headline: clean(h.content),
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
    country: clean(h.country_code) ?? "US",
    latitude: num(h.latitude),
    longitude: num(h.longitude),
    salePriceUsd: tx === "sale" ? num(h.sale_price) : null,
    salePriceText: tx === "sale" && !h.sale_price ? "Contact broker for pricing" : null,
    buildingSizeSqft: num(h.building_size_sf),
    lotSizeAcres: num(h.lot_size_acres),
    brokerIds: [],
    contactsDetailed,
    newmarkBrokerProvenance: {
      broker_name: clean(h.broker_name),
      broker_id: h.broker_id ?? null,
      broker_ids: Array.isArray(h.broker_ids) ? h.broker_ids : [],
      second_broker_id: h.second_broker_id ?? null,
      third_broker_id: h.third_broker_id ?? null,
    },
    rawNewmarkHit: h,
    photos: (h.thumbnails ?? []).slice(-1).map((t: any) => t.url),
    url: h.url ? `https://www.nmrk.com${h.url}` : null,
    lastUpdated: clean(h.updateDate)?.slice(0, 10) ?? null,
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
