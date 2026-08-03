// lib/enrich.ts - targeted-detail (enrich) mode registry.
//
// The enrichment worker (cre_enrich.py) claims a batch of new/changed listings
// from credeals.cre_enrichment_queue and asks collect.ts to render ONLY those
// detail pages (collect.ts --enrich-input=<claim.json>). Each claimed item is an
// EnrichItem; collect.ts groups items by sourceKey and dispatches each group to
// its registered SourceEnricher (genericEnricher when none is registered).
//
// Two invariants every enricher MUST honor (see ENRICHMENT_WORKER_DESIGN):
//   1. The queue carries the FOLDED/PREFIXED ingest external id (main:, investor:,
//      dealflow:), but the artifact must carry the NATIVE source id, because
//      cre_ingest.to_row re-applies the prefix on re-ingest. So each enricher
//      strips its own SOURCE_TO_BROKERAGE prefix off EnrichItem.externalId to
//      rebuild the native id; otherwise re-ingest double-prefixes (main:main:...)
//      and the row dead-letters.
//   2. Completion is matched by URL: every enricher echoes EnrichItem.url onto
//      its output listing.url so the worker can mark a claimed row done iff its
//      url appears in the emitted artifact.
import { clean, pmap, prune } from "./util.js";
import { scrapeDoc } from "./scrape.js";
import { firstJsonLd, jsonLdObjects } from "./html.js";
import { CONCURRENCY } from "./config.js";
import {
  ColliersMainEntry,
  parseColliersMainDetail,
  scrapeColliersMainDetailDoc,
} from "../sources/colliers-main.js";
import { enrichJllInvestorListing } from "../sources/jll-investor.js";
import {
  enrichBuildoutDetail,
  type BuildoutDetailConfig,
} from "../sources/buildout.js";
import { REGISTERED_BUILDOUT_FIRMS } from "../sources/buildout-registry.js";
import { getAvisonYoungFeed, avisonYoungBaseListing } from "../sources/avison-young.js";
import { enrichMarcusListing } from "../sources/marcus-millichap.js";
import { mapSrsListing, srsFetchAll, srsTenure } from "../sources/srs.js";
import { kidderFetchAll, kidderTenure, mapKidderListing } from "../sources/kidder-mathews.js";
import type { SourceKey } from "../types.js";

export type EnrichItem = {
  sourceKey: string;
  externalId: string;
  url: string;
  transaction?: "sale" | "lease";
};

export interface SourceEnricher {
  // Scrape + parse the given listings' detail pages into standard listing rows.
  // Each returned row MUST carry listing.url === the input EnrichItem.url.
  // Omit a row (do not push) on unrecoverable per-item failure; the worker then
  // leaves that claimed row queued and the weekly full scrape refreshes it.
  enrich(items: EnrichItem[]): Promise<any[]>;
}

// colliers-main: strip the "main:" fold prefix to rebuild the native usa#####
// id, build a minimal ColliersMainEntry, then reuse the source's exported
// scrape + parse. parseColliersMainDetail echoes entry.url onto listing.url.
export const colliersMainEnricher: SourceEnricher = {
  async enrich(items: EnrichItem[]): Promise<any[]> {
    const rows = await pmap(items, CONCURRENCY, async (item) => {
      const nativeId = item.externalId.replace(/^main:/, "");
      const entry: ColliersMainEntry = { url: item.url, lastmod: null, id: nativeId };
      try {
        const doc = await scrapeColliersMainDetailDoc(item.url);
        const listing = parseColliersMainDetail(entry, doc);
        // parseColliersMainDetail tombstones not-found / no-structured-data pages
        // with a `skip` marker (still carries url + native id). Those are valid
        // additive rows; keep them so the worker marks the claim done by url.
        return listing;
      } catch (err) {
        // Transient detail failure (e.g. unrecovered Cloudflare challenge). Omit
        // the row so the worker leaves the claim queued for a later retry.
        console.error(`  enrich/colliers-main: detail failed for ${item.url}: ${err}`);
        return null;
      }
    });
    return rows.filter(Boolean);
  },
};

// jll-investor: strip the "investor:" fold prefix to rebuild the native
// Salesforce listing.id, then reuse enrichJllInvestorListing directly (it already
// echoes base.url through unchanged and degrades a __NEXT_DATA__ miss to a
// detailError row, which the worker leaves queued). The native id is overwritten
// from the detail listing.id on success; it equals nativeId for an enriched row.
export const jllInvestorEnricher: SourceEnricher = {
  async enrich(items: EnrichItem[]): Promise<any[]> {
    const rows = await pmap(items, CONCURRENCY, async (item) => {
      const nativeId = item.externalId.replace(/^investor:/, "");
      const row = await enrichJllInvestorListing({
        id: nativeId,
        url: item.url,
        transactionType: "Sale (investment)",
        brokerIds: [],
        photos: [],
      });
      // A detailError row carries no fresh detail; omit it so the claim stays
      // queued rather than re-ingesting an empty row.
      if (!row || row.detailError) return null;
      return row;
    });
    return rows.filter(Boolean);
  },
};

// Buildout (svn / lee-associates) Tier-B detail enricher. The bulk srcBuildout
// path reads inventory.json only; the per-property media / virtual-tour / full
// image gallery / OM documents live behind the Buildout detail IFRAME, which the
// bulk path never renders. This enricher resolves that iframe content URL from
// the listing url (the show_link `?propertyId=` slug), scrapes it with the
// capture-everything format set, and runs harvestDetail. svn/lee are unfolded
// singleton sources, so the EnrichItem externalId is already the native id (no
// fold prefix to strip); completion is URL-keyed via the echoed item.url, and
// cre_ingest.to_row recomputes the Buildout external_id from that url. A
// derivation or scrape failure omits the row so the worker leaves the claim
// queued for the weekly additive backstop.
export function buildoutEnricherFor(
  detailConfig?: BuildoutDetailConfig
): SourceEnricher {
  return {
    async enrich(items: EnrichItem[]): Promise<any[]> {
      const rows = await pmap(items, CONCURRENCY, async (item) => {
        try {
          return await enrichBuildoutDetail(
            item.sourceKey,
            item.url,
            detailConfig
          );
        } catch (err) {
          console.error(`  enrich/buildout: detail failed for ${item.url}: ${err}`);
          return null;
        }
      });
      return rows.filter(Boolean);
    },
  };
}

export const buildoutEnricher: SourceEnricher = buildoutEnricherFor();

export const REGISTERED_BUILDOUT_ENRICHERS = Object.fromEntries(
  REGISTERED_BUILDOUT_FIRMS.flatMap((definition) =>
    definition.detailConfig
      ? [[definition.sourceKey, buildoutEnricherFor(definition.detailConfig)]]
      : []
  )
) as Partial<Record<SourceKey, SourceEnricher>>;

// The following adapters deliberately replay their established source APIs
// rather than using the generic Firecrawl/JSON-LD fallback. The queue is fed by
// monitor records for these sources, whose public detail pages do not guarantee
// JSON-LD. A thin url-only fallback row would update scraped_at and clear the
// queue without refreshing canonical source facts.
export const marcusEnricher: SourceEnricher = {
  async enrich(items: EnrichItem[]): Promise<any[]> {
    const rows = await pmap(items, CONCURRENCY, async (item) => {
      const row = await enrichMarcusListing({
        id: nativeIdFor(item),
        url: item.url,
        transactionType: "Sale",
        photos: [],
        brokerIds: [],
      });
      if (!row || row.detailError) return null;
      return { ...row, url: item.url };
    });
    return rows.filter(Boolean);
  },
};

export const avisonYoungEnricher: SourceEnricher = {
  async enrich(items: EnrichItem[]): Promise<any[]> {
    const { websiteRows, teamMembers } = await getAvisonYoungFeed();
    const byId = new Map(websiteRows.map((row) => [String(row?.id ?? ""), row]));
    return items.flatMap((item) => {
      const sourceRow = byId.get(nativeIdFor(item));
      if (!sourceRow) return [];
      const row = avisonYoungBaseListing(sourceRow, teamMembers);
      return row ? [{ ...row, url: item.url }] : [];
    });
  },
};

function rowForClaim(
  items: EnrichItem[],
  sourceRows: any[],
  map: (row: any, tx: "sale" | "lease") => any,
  tenure: (row: any) => { isSale: boolean; isLease: boolean }
): any[] {
  const byId = new Map<string, any>();
  for (const sourceRow of sourceRows) {
    const tx = tenure(sourceRow).isSale ? "sale" : "lease";
    const mapped = map(sourceRow, tx);
    if (mapped?.id != null) byId.set(String(mapped.id), mapped);
  }
  return items.flatMap((item) => {
    const row = byId.get(nativeIdFor(item));
    return row ? [{ ...row, url: item.url }] : [];
  });
}

export const srsEnricher: SourceEnricher = {
  async enrich(items: EnrichItem[]): Promise<any[]> {
    const { items: sourceRows } = await srsFetchAll(Number.POSITIVE_INFINITY);
    return rowForClaim(items, sourceRows, mapSrsListing, srsTenure);
  },
};

export const kidderMathewsEnricher: SourceEnricher = {
  async enrich(items: EnrichItem[]): Promise<any[]> {
    const { items: sourceRows } = await kidderFetchAll(Number.POSITIVE_INFINITY);
    return rowForClaim(items, sourceRows, mapKidderListing, kidderTenure);
  },
};

// Best-effort generic fallback for any source without a bespoke enricher: scrape
// the URL and lift price/status/description/geo from a RealEstateListing (or any)
// JSON-LD block where the page exposes one. A page with no useful JSON-LD yields
// no row: emitting a thin url-only row would falsely complete the queue item and
// overwrite raw source payload with an unverified fallback.
export const genericEnricher: SourceEnricher = {
  async enrich(items: EnrichItem[]): Promise<any[]> {
    const rows = await pmap(items, CONCURRENCY, async (item) => {
      try {
        const doc = await scrapeDoc(item.url);
        const row = parseGenericJsonLd(item, doc.rawHtml ?? "");
        return row?.genericEnrich?.hadJsonLd ? row : null;
      } catch (err) {
        console.error(`  enrich/generic: scrape failed for ${item.url}: ${err}`);
        return null;
      }
    });
    return rows.filter(Boolean);
  },
};

// Pure transform: lift best-effort listing fields out of a page's JSON-LD. No
// network. Echoes item.url. Exported for the unit test (fixture HTML in, row out).
export function parseGenericJsonLd(item: EnrichItem, rawHtml: string): any {
  const ld =
    firstJsonLd(rawHtml, "RealEstateListing") ??
    firstJsonLd(rawHtml, "Product") ??
    firstJsonLd(rawHtml, "Offer") ??
    jsonLdObjects(rawHtml).find((o) => o && (o.offers || o.price || o.name)) ??
    null;
  const offer = ld?.offers ?? ld;
  // JSON-LD offers.price is a bare numeric (string or number) per schema.org, not
  // a "$"-prefixed display string, so coerce with coerceNum() (num() only accepts
  // a number type; JSON-LD commonly serializes price/geo as strings).
  const rawPrice = offer?.price ?? offer?.priceSpecification?.price ?? null;
  const salePriceUsd = coerceNum(rawPrice);
  const priceText = rawPrice != null ? String(rawPrice) : null;
  const lat = coerceNum(ld?.geo?.latitude);
  const lng = coerceNum(ld?.geo?.longitude);
  return prune({
    id: nativeIdFor(item),
    name: clean(ld?.name),
    description: clean(ld?.description),
    salePriceUsd,
    salePriceText: priceText != null ? String(priceText) : null,
    latitude: lat,
    longitude: lng,
    url: item.url,
    genericEnrich: { jsonLdType: clean(ld?.["@type"]) ?? null, hadJsonLd: ld != null },
  });
}

// Coerce a JSON-LD scalar (number or numeric string) to a finite nonzero number,
// else null. num() rejects strings; JSON-LD often serializes price/geo as strings.
function coerceNum(v: any): number | null {
  if (v == null) return null;
  const n = typeof v === "number" ? v : Number(String(v).replace(/,/g, "").trim());
  return Number.isFinite(n) && n !== 0 ? n : null;
}

// Strip the known fold prefix off a folded external id to recover the native id
// the artifact must carry. Falls through to the raw external id when no known
// prefix is present (an unfolded singleton source).
function nativeIdFor(item: EnrichItem): string {
  return item.externalId.replace(/^(main:|investor:|dealflow:)/, "");
}

// cbre is intentionally ABSENT: cbre is enumeration-only (the listings-api JSON
// already returns fully mapped rows; monitor output equals full output), so there
// is no per-listing detail endpoint to enrich. cbre new/changed rows ride the
// weekly additive backstop.
export const ENRICHERS: Partial<Record<SourceKey, SourceEnricher>> = {
  "colliers-main": colliersMainEnricher,
  "jll-investor": jllInvestorEnricher,
  // svn / lee-associates: bulk path is inventory-only; the detail iframe (media,
  // tours, full gallery, OM docs) is captured Tier-B via the Buildout enricher.
  svn: buildoutEnricher,
  "lee-associates": buildoutEnricher,
  "marcus-millichap": marcusEnricher,
  "avison-young": avisonYoungEnricher,
  srs: srsEnricher,
  "kidder-mathews": kidderMathewsEnricher,
  ...REGISTERED_BUILDOUT_ENRICHERS,
};

// Group claim items by sourceKey, dropping items missing a key or url (a row with
// no url could never be marked done, since completion is URL-keyed). Pure: no
// network, no enricher dispatch. Exported so collect.ts --enrich-input and the
// unit test share one grouping definition.
export function groupEnrichItems(items: EnrichItem[]): Map<string, EnrichItem[]> {
  const groups = new Map<string, EnrichItem[]>();
  for (const item of items) {
    const key = item?.sourceKey;
    if (!key || !item.url) continue;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(item);
  }
  return groups;
}

export type EnrichGroupResult = { sources: any[]; listings: any[] };

// Dispatch each grouped source to its enricher (resolved via resolveEnricher, so
// the unit test can inject a fake and stay no-network) and assemble the standard
// sources[]/listings[] arrays. Honors the two enrich invariants: every emitted
// row is tagged with its input transactionMode by URL, and a row without a url is
// dropped (it could never be URL-matched done). A per-source enricher throw is
// caught and recorded as a sources[].error, exactly like the full-collect path.
export async function runEnrichGroups(
  groups: Map<string, EnrichItem[]>,
  resolveEnricher: (key: string) => { enricher: SourceEnricher; label: string },
  companyFor: (key: string) => string
): Promise<EnrichGroupResult> {
  const sources: any[] = [];
  const listings: any[] = [];
  for (const [key, groupItems] of groups) {
    const { enricher, label } = resolveEnricher(key);
    const company = companyFor(key);
    console.error(`enriching ${key} (${groupItems.length} item(s), enricher=${label})...`);
    try {
      const rows = await enricher.enrich(groupItems);
      // Echo-guard: every emitted row MUST carry its input url for URL-keyed
      // completion. Index input transaction by url so the correct
      // transactionMode tag is attached (and a url-less row is dropped).
      const urlToTx = new Map<string, "sale" | "lease">();
      for (const it of groupItems) urlToTx.set(it.url, it.transaction ?? "sale");
      let emitted = 0;
      for (const l of rows) {
        if (!l || !l.url) continue;
        const tx = urlToTx.get(l.url) ?? "sale";
        listings.push(prune({ sourceKey: key, sourceCompany: company, transactionMode: tx, ...l }));
        emitted++;
      }
      sources.push({
        sourceKey: key,
        supported: true,
        company,
        enricher: label,
        requested: groupItems.length,
        listingsCollected: emitted,
      });
      console.error(`  ${key}: ${emitted}/${groupItems.length} enriched (enricher=${label})`);
    } catch (err) {
      console.error(`  ${key} FAILED: ${err}`);
      sources.push({
        sourceKey: key,
        supported: true,
        enricher: label,
        requested: groupItems.length,
        error: String(err).slice(0, 300),
      });
    }
  }
  return { sources, listings };
}

// Resolve a source key to its registered enricher or the generic fallback. The
// label (bespoke|generic) is surfaced in the artifact so enricher coverage is
// visible. Exported so collect.ts and the unit test resolve identically.
export function resolveEnricher(key: string): { enricher: SourceEnricher; label: string } {
  const bespoke = ENRICHERS[key as SourceKey];
  return bespoke ? { enricher: bespoke, label: "bespoke" } : { enricher: genericEnricher, label: "generic" };
}
