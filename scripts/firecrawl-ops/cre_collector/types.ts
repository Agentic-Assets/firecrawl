// types.ts - extracted verbatim from collect.ts (see tasks/tmp backup)


export type Tx = "sale" | "lease";

// Canonical source-key tuple (one source of truth for collect.ts and
// lib/enrich.ts). Exported here so the enrichment registry can type itself as
// Partial<Record<SourceKey, ...>> without importing collect.ts. The tuple is
// byte-identical to the prior collect.ts-local literals; runtime behavior is
// unchanged.
export const SOURCE_KEYS = [
  "cbre",
  "cbre-dealflow",
  "jll",
  "jll-investor",
  "cushman-wakefield",
  "colliers",
  "colliers-main",
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
  "srs",
  "hanley",
  "kidder-mathews",
] as const;
export type SourceKey = (typeof SOURCE_KEYS)[number];

export type ScrapeOpts = {
  waitFor?: number;
  proxy?: "stealth" | "basic" | "auto";
  timeout?: number;
  jsonAttempts?: number;
  jsonBackoffMs?: number;
};
// One result block from the Firecrawl `attributes` format: a flat list of the
// `attribute` values harvested from every element matching `selector`. The local
// self-hosted fork returns exactly this shape (verified against
// http://localhost:3002 POST /v2/scrape: `data.attributes` is
// `[{selector, attribute, values}]`). harvestDetail() consumes it as the
// preferred structured path; when the fork omits the format it degrades to a
// rawHtml regex fallback (so this stays optional everywhere).
export interface AttrBlock {
  selector: string;
  attribute: string;
  values: string[];
}

// One captured video / virtual-tour / matterport / 360 media URL for a listing.
// Routed by harvestDetail() into the cre_listing_media child table. Optional on a
// listing (listings are `any`, so this exists for harvest.ts + cre_ingest.py
// contract parity, not compile-time enforcement).
export interface MediaItem {
  mediaType: "video" | "virtual_tour" | "matterport" | "other";
  provider: string | null;
  url: string;
  embedUrl: string | null;
  title: string | null;
}

// One captured outbound link for a listing (external listing, social, map, or
// other). Routed by harvestDetail() into the cre_listing_links child table.
// Broker-bio links are intentionally NOT carried here: they already live in
// cre_listing_contacts.profile_url.
export interface LinkItem {
  url: string;
  rel: string | null;
  linkType: "external_listing" | "social" | "map" | "broker_bio" | "document" | "video" | "other";
}

// One captured document for a listing (offering memorandum, brochure, flyer,
// floor plan, financials, rent roll, or other). Routed by harvestDetail() into
// the EXISTING cre_listing_documents child table (the doc_type CHECK is widened
// additively in sql/011 to add financials/rent_roll).
export interface DocItem {
  url: string;
  title: string | null;
  docType: "om" | "brochure" | "flyer" | "floor_plan" | "financials" | "rent_roll" | "other";
}

export type ScrapedDoc = {
  rawHtml: string;
  markdown: string;
  links: string[];
  // Full image gallery from the `images` format (verified supported by the
  // local fork). Possibly-undefined when a fork omits the format; harvestDetail()
  // then regex-extracts gallery URLs from rawHtml.
  images?: string[];
  // Per-selector attribute harvest from the `attributes` format. Possibly-
  // undefined when a fork omits the format; harvestDetail() falls back to a
  // rawHtml regex over iframe/video-source/data-video-url.
  attributes?: AttrBlock[];
  metadata?: Record<string, any>;
};

// ---------- source adapters ----------
// Each adapter returns { company, sourceUrl, method, totalAvailable, listings, note? }.
// Listings share one field vocabulary (prune() drops what a source lacks):
// id, name, headline, transactionType, assetType, description, street, city, state,
// postalCode, country, latitude, longitude, salePriceUsd, salePriceText, capRatePct,
// leaseRateText, sizeText, buildingSizeSqft, lotSizeAcres, brokerIds, brochures,
// photos, url, lastUpdated, markdown,
//   media?: MediaItem[]   (video / virtual-tour / matterport / 360 -> cre_listing_media),
//   links?: LinkItem[]    (external / social / map / other          -> cre_listing_links).
// brochures/documents items may now carry a `docType` (DocItem.docType); harvest
// emits classified DocItem[] on the existing documents channel.
//
// Phase-2 Data-Lift field vocabulary (WS1/WS2/WS3/WS4 additions; additive only).
// cre_ingest.py to_row() reads these camelCase keys; all nullable / optional.
// Existing fields newly populated by WS1 (no rename; already in to_row):
//   submarket, market, county, units, yearBuilt, occupancyRate, availableSf,
//   minDivisibleSf, maxDivisibleSf, capRatePct, salePricePerSf, noi, floors,
//   zoning, highlights, amenities, description, canonicalUrl.
//
// New institutional fields (WS3):
//   buildingClass?:        "A"|"B"|"C"|"D"  -> building_class (A/B/C/D or NULL)
//   propertySubtype?:      string            -> property_subtype (free text, max 96 chars)
//   apn?:                  string            -> apn (assessor parcel number, max 64 chars)
//   tenantName?:           string            -> tenant_name (NNN tenant, max 256 chars)
//   guarantor?:            string            -> guarantor (lease guarantor, max 256 chars)
//   leaseYearsRemaining?:  number            -> lease_years_remaining (0-99)
//   pricePerUnit?:         number            -> price_per_unit (USD, > 0)
//   grm?:                  number            -> grm (gross rent multiplier, 0-100)
//   pricePerAcre?:         number            -> price_per_acre (USD/acre, > 0)
//   numRooms?:             number            -> num_rooms (hotel rooms, integer > 0)
//   revpar?:               number            -> revpar (hotel revenue/available room, USD > 0)
//   clearHeightFt?:        number            -> clear_height_ft (industrial, 0-200 ft)
//   dockDoors?:            number            -> dock_doors (integer >= 0)
//   driveInDoors?:         number            -> drive_in_doors (integer >= 0)
//   powerService?:         string            -> power_service (e.g. "2000A 480V", max 128 chars)
//   railServed?:           boolean           -> rail_served (true/false/null)
//   statusBadge?:          string            -> routes to existing OPT-IN status gate ONLY;
//                                              NEVER a direct column write; never auto-activates
//   extraFacts?:           Record<string,any> -> extra_facts jsonb (long-tail, snake_case keys)
//   leaseRateType?:        string            -> lease_rate_type (norm_lease_rate_type)
//   leaseRateMin?:         number            -> lease_rate_min ($/SF/yr)
//   leaseRateMax?:         number            -> lease_rate_max ($/SF/yr)
//   omFacts?:              OmFactRow[]       -> cre_listing_om_facts rows (WS2 only)
//
// Geo fields (WS4, derived by ingest/backfill from postalCode/latitude/longitude):
//   (ingest-derived: cbsa_code, cbsa_name, geo_source; adapters do NOT compute these)
//
// Contact license field (rides existing contactsDetailed[] channel):
//   contactsDetailed?: Array<{ ..., license?: string }>  (license -> cre_listing_contacts.license)

/** One OM-parse fact row; carries parse provenance (WS2). */
export interface OmFactRow {
  fact_group: "scalar" | "unit_mix" | "rent_roll";
  fact_key: string;
  fact_value_text?: string | null;
  fact_value_num?: number | null;
  unit_count?: number | null;
  source_doc_url: string;
  parser_version: string;
  confidence?: number | null;
}

export type SourceResult = {
  company: string;
  sourceUrl: string;
  method: string;
  totalAvailable: number | null;
  listings: any[];
  note?: string;
  // Set true when this pass KNOWINGLY returned a partial enumeration (hit a
  // provider cap, PAGE_CAP, or a short/partial read) WITHOUT throwing. Monitor
  // mode treats a truncated pass like an errored one: the downstream coverage
  // gate refuses disappearance for the source (not overridable by
  // --force-disappear), so a silent under-count can never fire false disappeared
  // events. Metadata only; it never changes collected listings or the ingest path.
  truncated?: boolean;
};
