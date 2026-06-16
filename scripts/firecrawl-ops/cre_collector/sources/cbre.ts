// sources/cbre.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { harvestDetail } from "../lib/harvest.js";
import { parseLeaseRate } from "../lib/parse.js";
import { scrapeJson } from "../lib/scrape.js";
import { DocItem, ScrapedDoc, ScrapeOpts, SourceResult, Tx } from "../types.js";
import { clean, num, pmap, prune } from "../lib/util.js";


// --- CBRE: internal listings JSON API, paginated, behind Cloudflare (stealth) ---

export function cbreAspect(tx: Tx): string {
  return tx === "sale" ? "isSale" : "isLetting";
}

export function cbreListingSlug(parts: {
  name: string | null;
  street: string | null;
  city: string | null;
  state: string | null;
  zip: string | null;
}): string {
  return [parts.name, parts.street, parts.city, parts.state, parts.zip]
    .filter(Boolean)
    .join(" ")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export function cbreListingUrl(primaryKey: string, slug: string): string {
  return `https://www.cbre.com/properties/properties-for-lease/commercial-space/details/${primaryKey}/${slug}`;
}

export function cbreBrochureUrl(uri: string | null): string {
  const u = clean(uri);
  return u?.startsWith("http") ? u : `https://www.cbre.com${u ?? ""}`;
}

export function cbrePhotoUrl(resourceUri: string | null): string | null {
  const u = clean(resourceUri);
  if (!u) return null;
  return u.startsWith("http") ? u : `https://www.cbre.com${u}`;
}

export function cbreTransactionType(aspects: string[]): string {
  const isSale = aspects.includes("isSale");
  const isLet = aspects.includes("isLetting");
  return isSale && isLet ? "Sale/Lease" : isLet ? "Lease" : "Sale";
}

// Lift stranded structured fields the CBRE listings-api doc exposes but the
// adapter previously dropped, onto the listing keys cre_ingest.to_row maps. CBRE
// is enumeration-only (no detail render), so this reads the JSON doc `d` directly.
// Cap rate, when present, arrives as a Charges entry of kind 'CapRate'/'Yield' or
// a Dynamic field; year built / floors / units arrive as Dynamic.* numerics. Only
// clearly-present values are lifted (prune drops the rest), so a sparse doc never
// clobbers good data. Never throws.
export function cbreStrandedStructured(d: any): Record<string, any> {
  const charges: any[] = Array.isArray(d?.["Common.Charges"]) ? d["Common.Charges"] : [];
  const capCharge = charges.find(
    (c: any) => /cap\s*rate|yield/i.test(String(c?.["Common.ChargeKind"] ?? "")) && num(c?.["Common.Amount"])
  );
  const capRatePct =
    num(capCharge?.["Common.Amount"]) ?? num(d?.["Dynamic.CapRate"]) ?? num(Number(d?.["Dynamic.CapRate"]));
  return prune({
    capRatePct,
    yearBuilt: num(d?.["Dynamic.YearBuilt"]) ?? num(Number(d?.["Dynamic.YearBuilt"])),
    floors: num(d?.["Dynamic.NumberOfFloors"]) ?? num(Number(d?.["Dynamic.NumberOfFloors"])),
    units: num(d?.["Dynamic.NumberOfUnits"]) ?? num(Number(d?.["Dynamic.NumberOfUnits"])),
    occupancyRate: num(d?.["Dynamic.OccupancyRate"]) ?? num(Number(d?.["Dynamic.OccupancyRate"])),
    zoning: clean(d?.["Dynamic.Zoning"]),
  }) ?? {};
}

// Classify CBRE brochures by their human BrochureName into typed DocItems
// (an "Offering Memorandum" / "Financials" brochure promotes to docType
// om/financials instead of a flat 'brochure'), then run them through harvestDetail
// so the classification + dedup logic is shared with every other source. CBRE is
// enumeration-only (no detail-page render), so there is no markdown / gallery /
// iframe surface and media/links are always empty. Classified docs ride the
// `documents` channel and `brochures` is left empty, so the same url is never
// inserted into cre_listing_documents twice (that table has no (listing_id,url)
// unique key). One code path => monitor and full are byte-identical for CBRE.
function cbreHarvestDocs(
  d: any,
  brochureItems: Array<{ name: string | null; url: string }>
): DocItem[] {
  // Pre-classify each brochure by its NAME (the url is an opaque CDN path that
  // rarely carries a keyword). A name-derived docType is passed as a typed
  // DocItem; harvestDetail trusts the given docType and dedups by url.
  const baseUrl = cbreListingUrl(String(d?.["Common.PrimaryKey"] ?? ""), "");
  const extraDocs: DocItem[] = brochureItems
    .filter((b) => /^https?:\/\//i.test(b.url))
    .map((b) => ({ url: b.url, title: b.name, docType: cbreDocTypeFromName(b.name) }));
  return harvestDetail({} as ScrapedDoc, { baseUrl, extraDocs }).documents;
}

// Extract the WS1 additive scalar fields from a stored CBRE raw_data blob.
// The blob is the JSON the adapter emits (leaseRateText, headline/url already
// present); this function re-derives the NEW camelCase fields from them so tests
// can assert the parse without a network call. Pure: no side effects, never throws.
export function cbreNewFieldsFromRawData(raw: any): {
  canonicalUrl: string | null;
  highlights: string | null;
  leaseRateMin: number | null;
  leaseRateMax: number | null;
  leaseRateType: string | null;
} {
  const url = clean(raw?.url) ?? null;
  const headline = clean(raw?.headline) ?? null;
  const lrt = clean(raw?.leaseRateText) ?? null;
  const lr = parseLeaseRate(lrt);
  return {
    canonicalUrl: url,
    highlights: headline,
    leaseRateMin: lr.min,
    leaseRateMax: lr.max,
    leaseRateType: lr.type,
  };
}

// Map a CBRE brochure display name to a DocItem docType. Mirrors the harvester's
// keyword buckets (most-specific first); defaults to 'brochure' (the prior CBRE
// behavior) when no documentary keyword is present.
export function cbreDocTypeFromName(name: string | null): DocItem["docType"] {
  const hay = (name ?? "").toLowerCase();
  if (/rent[-_ ]?roll/.test(hay)) return "rent_roll";
  if (/financ|pro[-_ ]?forma|proforma|\bt-?12\b/.test(hay)) return "financials";
  if (/floor[-_ ]?plan|site[-_ ]?plan/.test(hay)) return "floor_plan";
  if (/offering|memorandum|\bom\b|teaser/.test(hay)) return "om";
  if (/flyer/.test(hay)) return "flyer";
  return "brochure";
}

export async function srcCbre(tx: Tx, max: number, _monitor: boolean): Promise<SourceResult> {
  // Enumeration-only source: the listings-api JSON already returns fully mapped
  // rows with no per-listing detail render, so monitor output == full output.
  const aspect = cbreAspect(tx);
  const opts: ScrapeOpts = { proxy: "stealth", waitFor: 4000, timeout: 120000 };
  const base = `https://www.cbre.com/listings-api/propertylistings/query?site=us-comm&Common.Aspects=${aspect}&PageSize=200`;
  const first = await scrapeJson(`${base}&Page=1`, opts);
  if (typeof first.DocumentCount !== "number" || !Array.isArray(first.Documents)) {
    throw new Error("CBRE listings API response is missing DocumentCount/Documents fields");
  }
  const total: number = first.DocumentCount;
  const want = Math.min(max, total);
  const pages = Math.ceil(want / 200);
  console.error(`  cbre/${tx}: ${total} total, fetching ${pages} page(s)`);
  const docsArr: any[][] = [first.Documents.flat()];
  if (pages > 1) {
    const pageNums = Array.from({ length: pages - 1 }, (_, i) => i + 2);
    const rest = await pmap(pageNums, CONCURRENCY, async (p) => {
      const d = await scrapeJson(`${base}&Page=${p}`, opts);
      console.error(`  cbre/${tx}: page ${p}/${pages} (${(d.Documents ?? []).flat().length} docs)`);
      return Array.isArray(d.Documents) ? d.Documents.flat() : [];
    });
    docsArr.push(...rest);
  }
  // Only page 1 is structurally validated; a later page that returns parseable
  // JSON without a Documents array silently contributes []. If the total
  // collected falls short of `want` (= min(max, DocumentCount)), an empty/short
  // later page truncated this pass. This excludes --max-items (folded into
  // `want`) and natural exhaustion (a complete run reaches `want`).
  const collectedDocs = docsArr.flat();
  const truncated = collectedDocs.length < want;
  const docs = collectedDocs.slice(0, want);
  const text = (loc: any) =>
    Array.isArray(loc) && loc.length ? clean(loc[0]["Common.Text"]) : null;
  const listings = docs.map((d: any) => {
    const addr = d["Common.ActualAddress"] ?? {};
    const charges: any[] = Array.isArray(d["Common.Charges"]) ? d["Common.Charges"] : [];
    const sale = charges.find(
      (c: any) => c["Common.ChargeKind"] === "SalePrice" && num(c["Common.Amount"])
    );
    const rent = charges.find(
      (c: any) => c["Common.ChargeKind"] === "Rent" && num(c["Common.Amount"])
    );
    const coord = d["Common.Coordinate"] ?? {};
    const aspects: string[] = Array.isArray(d["Common.Aspects"]) ? d["Common.Aspects"] : [];
    const name = clean(addr["Common.Line1"]);
    const street = clean(addr["Common.Line2"]);
    const city = clean(addr["Common.Locallity"]);
    const state = clean(addr["Common.Region"]);
    const zip = clean(addr["Common.PostCode"]);
    const slug = cbreListingSlug({ name, street, city, state, zip });
    const brokerIds = (Array.isArray(d["Common.Agents"]) ? d["Common.Agents"] : [])
      .map((a: any) =>
        brokerRef({
          name: clean(a["Common.AgentName"]),
          email: clean(a["Common.EmailAddress"]),
          phone: clean(a["Common.TelephoneNumber"]),
          office: clean(a["Common.AgentOffice"]),
          company: "CBRE",
        })
      )
      .filter((x: number | null): x is number => x !== null);
    const brochureItems = (Array.isArray(d["Common.Brochures"]) ? d["Common.Brochures"] : []).map(
      (b: any) => ({
        name: clean(b["Common.BrochureName"]),
        url: cbreBrochureUrl(clean(b["Common.Uri"])),
      })
    );
    const photoUrls = (Array.isArray(d["Common.Photos"]) ? d["Common.Photos"] : [])
      .map((p: any) => {
        const r =
          (p["Common.ImageResources"] ?? []).find((x: any) => x["Common.Breakpoint"] === "original") ??
          (p["Common.ImageResources"] ?? [])[0];
        return r ? cbrePhotoUrl(clean(r["Common.Resource.Uri"])) : null;
      })
      .filter((u: string | null): u is string => Boolean(u));
    const listingUrl = cbreListingUrl(d["Common.PrimaryKey"], slug);
    const leaseRateText = rent
      ? `${rent["Common.Amount"]} ${clean(rent["Common.ChargeCurrency"]) ?? "USD"}/${clean(rent["Common.ChargeInterval"]) ?? ""} ${clean(rent["Common.ChargeBasis"]) ?? ""}`.trim()
      : null;
    const lr = parseLeaseRate(leaseRateText);
    return {
      id: d["Common.PrimaryKey"],
      name,
      headline: text(d["Common.Strapline"]),
      // WS1: lift highlights from the CBRE strapline/headline field
      highlights: text(d["Common.Strapline"]) ?? undefined,
      transactionType: cbreTransactionType(aspects),
      assetType: clean(d["Common.UsageType"]),
      description: text(d["Common.LongDescription"]),
      street,
      city,
      state,
      postalCode: zip,
      country: clean(addr["Common.Country"]),
      latitude: typeof coord.lat === "number" ? coord.lat : null,
      longitude: typeof coord.lon === "number" ? coord.lon : null,
      salePriceUsd: sale ? sale["Common.Amount"] : null,
      salePriceText: sale || tx === "lease" ? null : "Contact broker for pricing",
      leaseRateText,
      // WS1: parse lease rate into typed camelCase fields via parseLeaseRate
      leaseRateMin: lr.min ?? undefined,
      leaseRateMax: lr.max ?? undefined,
      leaseRateType: lr.type ?? undefined,
      buildingSizeSqft: num(d["Dynamic.TotalArea"]),
      ...cbreStrandedStructured(d),
      brokerIds,
      // Brochures are classified by name into typed DocItems on the `documents`
      // channel; `brochures` left empty to avoid a double-insert (no unique key
      // on cre_listing_documents). CBRE has no detail page, so no media/links.
      brochures: [],
      documents: cbreHarvestDocs(d, brochureItems),
      photos: photoUrls,
      url: listingUrl,
      // WS1: canonicalUrl from the listing URL (col currently ~0%; raw_data->>'url' ~92%)
      canonicalUrl: listingUrl,
      lastUpdated: clean(d["Common.LastUpdated"])?.slice(0, 10) ?? null,
      created: clean(d["Common.Created"])?.slice(0, 10) ?? null,
    };
  });
  return {
    company: "CBRE",
    sourceUrl: `https://www.cbre.com/properties (${aspect})`,
    method: "CBRE public listings API (JSON, paginated, stealth proxy)",
    totalAvailable: total,
    listings,
    truncated,
  };
}
