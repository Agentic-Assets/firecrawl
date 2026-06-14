// types.ts - extracted verbatim from collect.ts (see tasks/tmp backup)


export type Tx = "sale" | "lease";

export type ScrapeOpts = {
  waitFor?: number;
  proxy?: "stealth" | "basic" | "auto";
  timeout?: number;
  jsonAttempts?: number;
  jsonBackoffMs?: number;
};
export type ScrapedDoc = {
  rawHtml: string;
  markdown: string;
  links: string[];
  metadata?: Record<string, any>;
};

// ---------- source adapters ----------
// Each adapter returns { company, sourceUrl, method, totalAvailable, listings, note? }.
// Listings share one field vocabulary (prune() drops what a source lacks):
// id, name, headline, transactionType, assetType, description, street, city, state,
// postalCode, country, latitude, longitude, salePriceUsd, salePriceText, capRatePct,
// leaseRateText, sizeText, buildingSizeSqft, lotSizeAcres, brokerIds, brochures,
// photos, url, lastUpdated

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
