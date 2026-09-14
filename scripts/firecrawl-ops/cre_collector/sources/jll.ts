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
import { recordJllDetailCache } from "../lib/performance.js";
import { scrapeDoc } from "../lib/scrape.js";
import { DocItem, MediaItem, ScrapedDoc, SourceResult, Tx } from "../types.js";
import { boundedInt, clean, moneyToNumber, num, pmap, prune } from "../lib/util.js";
import {
  detailObservation,
  generationMatches,
  refreshGenerationId,
  requireFreshDetails,
} from "../lib/freshness.js";


// --- JLL: public GraphQL search + rendered detail pages ---

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
export const JLL_GRAPHQL_URL = "https://property.jll.com/api/graphql";
export const JLL_GRAPHQL_TIMEOUT_MS = boundedInt(
  process.env.JLL_GRAPHQL_TIMEOUT_MS,
  30000,
  1000,
  120000
);
export const JLL_GRAPHQL_RETRIES = boundedInt(process.env.JLL_GRAPHQL_RETRIES, 3, 1, 5);
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

export function assertJllIdentityReconciliation(
  listings: Iterable<{ id?: unknown; url?: unknown }>
): void {
  const idToUrl = new Map<string, string>();
  const urlToId = new Map<string, string>();
  for (const listing of listings) {
    const id = clean(listing?.id);
    const rawUrl = clean(listing?.url);
    if (!id || !rawUrl) {
      throw new Error("JLL inventory contains a missing provider id or URL");
    }
    const url = normalizedJllListingUrl(rawUrl);
    const priorUrl = idToUrl.get(id);
    if (priorUrl && priorUrl !== url) {
      throw new Error(`JLL provider id ${id} maps to multiple listing URLs`);
    }
    const priorId = urlToId.get(url);
    if (priorId && priorId !== id) {
      throw new Error(`JLL listing URL ${url} maps to multiple provider ids`);
    }
    idToUrl.set(id, url);
    urlToId.set(url, id);
  }
}

export const JLL_SEARCH_RESULTS_QUERY = `
  query SearchResults(
    $market: String!
    $language: String!
    $propertyTypes: [String!]
    $tenureTypes: [String!]
    $skip: Int
    $take: IntString = 50
    $orderBy: PropertiesOrderInput
  ) {
    properties(
      market: $market
      language: $language
      propertyTypes: $propertyTypes
      tenureTypes: $tenureTypes
      skip: $skip
      take: $take
      orderBy: $orderBy
    ) {
      count
      items {
        id
        title
        images
        address
        propertyTypes
        tenureTypes
        rentPrice {
          amount
          currency
          unit
        }
        salePrice {
          amount
          currency
          unit
        }
        hidePrice
        pageUrl
        latitude
        longitude
        city
        state
        postcode
        surfaceAreas {
          value
          unit
          label
          alternativeUnit
          showEstimateDesks
          metrics {
            value
            unit
          }
        }
      }
    }
  }
`;

class JllGraphqlRequestError extends Error {
  constructor(message: string, readonly retryable: boolean) {
    super(message);
    this.name = "JllGraphqlRequestError";
  }
}

export function jllGraphqlVariables(
  tx: Tx,
  propertyType: string,
  page: number
): Record<string, unknown> {
  if (!Number.isInteger(page) || page < 1) {
    throw new Error(`JLL GraphQL page must be a positive integer, received ${page}`);
  }
  return {
    market: "us",
    language: "en",
    propertyTypes: [propertyType],
    tenureTypes: [tx === "sale" ? "sale" : "rent"],
    skip: (page - 1) * JLL_SEARCH_PAGE_SIZE,
    take: JLL_SEARCH_PAGE_SIZE,
    orderBy: {
      field: "dateModified",
      direction: "desc",
      imagePriority: true,
    },
  };
}

type JllPriceSourceShape =
  | "absent"
  | "legacy_string"
  | "numeric_string"
  | "bare_number"
  | "structured"
  | "unsupported";
type JllWithholdingControl = "absent" | "visible" | "withheld" | "unknown";
type JllNormalizedPrice = {
  sourceShape: JllPriceSourceShape;
  text: string | null;
  amount: number | null;
  currency: string | null;
  unit: string | null;
};

function jllPublicPriceAmount(value: unknown): number | null {
  if (typeof value === "number") {
    return Number.isFinite(value) && value > 0 ? value : null;
  }
  if (typeof value !== "string") return null;
  const text = clean(value);
  if (!text || !/^(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$/.test(text)) {
    return null;
  }
  const amount = Number(text.replace(/,/g, ""));
  return Number.isFinite(amount) && amount > 0 ? amount : null;
}

function jllFormattedPriceText(
  amount: number,
  currency: string | null,
  unit: string | null
): string {
  const amountText = amount.toLocaleString("en-US", {
    minimumFractionDigits: Number.isInteger(amount) ? 0 : 2,
    maximumFractionDigits: 2,
  });
  const prefix = !currency || currency.toUpperCase() === "USD" ? "$" : `${currency} `;
  return `${prefix}${amountText}${unit ? `/${unit}` : ""}`;
}

function jllNormalizedPrice(price: unknown): JllNormalizedPrice {
  if (price === null || price === undefined) {
    return { sourceShape: "absent", text: null, amount: null, currency: null, unit: null };
  }
  if (typeof price === "number") {
    const amount = jllPublicPriceAmount(price);
    return {
      sourceShape: "bare_number",
      text: amount === null ? null : jllFormattedPriceText(amount, null, null),
      amount,
      currency: null,
      unit: null,
    };
  }
  if (typeof price === "string") {
    const text = clean(price);
    if (text === null) {
      return { sourceShape: "legacy_string", text: null, amount: null, currency: null, unit: null };
    }
    const amount = jllPublicPriceAmount(text);
    if (amount !== null) {
      return {
        sourceShape: "numeric_string",
        text: jllFormattedPriceText(amount, null, null),
        amount,
        currency: null,
        unit: null,
      };
    }
    const legacyAmount = moneyToNumber(text);
    const unitMatch = text.match(/(?:\/|\bper\s+)([a-z0-9. ]+)/i);
    const unit = unitMatch ? clean(unitMatch[1]) : null;
    return {
      sourceShape: "legacy_string",
      text,
      amount: legacyAmount,
      currency: legacyAmount === null ? null : "USD",
      unit,
    };
  }
  if (typeof price !== "object" || Array.isArray(price)) {
    return { sourceShape: "unsupported", text: null, amount: null, currency: null, unit: null };
  }
  const value = price as { amount?: unknown; currency?: unknown; unit?: unknown };
  const amount = jllPublicPriceAmount(value.amount);
  const currency = clean(value.currency)?.toUpperCase() ?? null;
  const unit = clean(value.unit);
  return {
    sourceShape: "structured",
    text: amount === null ? null : jllFormattedPriceText(amount, currency, unit),
    amount,
    currency,
    unit,
  };
}

function jllPriceUsd(price: JllNormalizedPrice): number | null {
  const unit = price.unit?.trim().toLowerCase().replace(/\s+/g, " ") ?? null;
  const totalSaleUnit = unit === null || ["total", "total sale", "total_price"].includes(unit);
  return price.amount !== null && (!price.currency || price.currency === "USD") && totalSaleUnit
    ? price.amount
    : null;
}

const JLL_BASE_CURRENCY = "USD";

function jllLeasePriceText(price: JllNormalizedPrice): string | null {
  if (price.text === null) return null;
  // cre_listings lease-rate columns have no currency dimension.  Retain a
  // public foreign-currency value in jllDetail.pricing provenance, but never
  // route it through leaseRateText where the currency-free parser could stage
  // it as a USD rate.
  if (price.currency !== null && price.currency !== JLL_BASE_CURRENCY) return null;
  const unit = price.unit?.trim().toLowerCase().replace(/\s+/g, " ") ?? null;
  // A bare legacy display string is the established JLL lease representation.
  // Once a unit is provided, admit only explicit area/time lease-rate units.
  if (
    unit === null ||
    ["sf", "sq ft", "sqft", "square foot", "square feet", "sf/year", "sf/yr", "sf/month", "sf/mo"].includes(unit)
  ) {
    return price.text;
  }
  return null;
}

function jllWithholdingControl(value: unknown, key: string): JllWithholdingControl {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return "absent";
  const record = value as Record<string, unknown>;
  const controls: JllWithholdingControl[] = [];
  const legacyKey = key.toLowerCase();
  for (const [candidateKey, candidate] of Object.entries(record)) {
    const normalizedKey = candidateKey.toLowerCase();
    if (normalizedKey === legacyKey) {
      controls.push(candidate === true ? "withheld" : candidate === false ? "visible" : "unknown");
    } else if (normalizedKey === "pricewithholdingcontrol") {
      controls.push(
        typeof candidate === "string" && ["absent", "visible", "withheld", "unknown"].includes(candidate)
          ? (candidate as JllWithholdingControl)
          : "unknown"
      );
    }
  }
  if (!controls.length) return "absent";
  // Reconcile every present signal.  A concealment signal wins even if another
  // provider field is malformed; otherwise any ambiguity or disagreement
  // fails closed.  In particular, `hidePrice: false` cannot override an
  // explicit normalized `withheld`, nor can duplicate case variants be picked
  // by insertion order.
  if (controls.includes("withheld")) return "withheld";
  if (controls.includes("unknown")) return "unknown";
  if (controls.every((control) => control === "visible")) return "visible";
  if (controls.every((control) => control === "absent")) return "absent";
  return "unknown";
}

function jllReconciledWithholdingControl(...values: unknown[]): JllWithholdingControl {
  const controls = values
    .map((value) => jllWithholdingControl(value, "hidePrice"))
    .filter((control) => control !== "absent");
  if (!controls.length) return "absent";
  // The provider can repeat a normalized control at the top level and in the
  // search-card envelope. A concealment signal always prevents price exposure;
  // malformed or duplicate-case signals remain fail-closed as `unknown`.
  if (controls.includes("withheld")) return "withheld";
  if (controls.includes("unknown")) return "unknown";
  return controls.every((control) => control === "visible") ? "visible" : "unknown";
}

function jllStoredWithholdingControl(value: unknown): JllWithholdingControl {
  return jllWithholdingControl(value, "hidePrice");
}

function jllPriceWithheld(...controls: JllWithholdingControl[]): boolean {
  return controls.some((control) => control === "withheld" || control === "unknown");
}

const JLL_PRICE_CONTROL_KEYS = new Set(["hideprice", "pricewithholdingcontrol"]);
const JLL_DIRECT_PRICE_KEYS = new Set([
  "askingprice",
  "leaseratemax",
  "leaseratemin",
  "leaseratetext",
  "leaseratetype",
  "price",
  "priceperacre",
  "priceperunit",
  "pricing",
  "saleprice",
  "salepricepersf",
  "salepricetext",
  "salepriceusd",
]);
const JLL_FREE_TEXT_KEYS = new Set(["description", "highlights", "markdown", "summary"]);
const JLL_SENSITIVE_PARENT_CHILDREN = new Map([
  ["financials", new Set(["amount"])],
  ["futureeconomics", new Set(["consideration"])],
  ["dealeconomics", new Set(["amount"])],
]);
const JLL_MONEY_AMOUNT = "(?:\\d{1,3}(?:,\\d{3})+|\\d+)(?:\\.\\d+)?";
const JLL_MONEY_TOKEN = new RegExp(
  `(?:\\b(?:usd|cad|eur|gbp|jpy|aud|nzd|chf|hkd|sgd|cny|rmb|inr|mxn|brl|krw|aed|sar|sek|nok|dkk|pln|try|zar)\\s*|(?:us\\$|c\\$|a\\$)|[$€£¥])\\s*${JLL_MONEY_AMOUNT}(?:\\s*[kmb])?(?:\\s*/\\s*[a-z. ]+)?`,
  "i"
);
const JLL_MONEY_SUFFIX = new RegExp(`\\b${JLL_MONEY_AMOUNT}\\s*[kmb]\\b`, "i");

function jllSafePublicText(value: unknown): string | null {
  const text = clean(value);
  return text && !JLL_MONEY_TOKEN.test(text) && !JLL_MONEY_SUFFIX.test(text) ? text : null;
}

/** Apply the narrow JLL withheld-price raw-retention contract.
 *
 * Exact schema paths are removed rather than substring-matching field names,
 * preserving unrelated provenance such as currentTenants. A legacy
 * financials.amount is expressly a price payload. Hidden detail prose is
 * omitted entirely because a generic sanitizer cannot prove it has no monetary
 * disclosure.
 */
function jllRedactSensitivePriceFields(value: unknown, parentKey?: string): any {
  if (Array.isArray(value)) return value.map((item) => jllRedactSensitivePriceFields(item, parentKey));
  if (value === null || typeof value !== "object") return value;
  const output: Record<string, unknown> = {};
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    const normalizedKey = key.toLowerCase();
    if (!JLL_PRICE_CONTROL_KEYS.has(normalizedKey) && JLL_DIRECT_PRICE_KEYS.has(normalizedKey)) {
      continue;
    }
    if (JLL_SENSITIVE_PARENT_CHILDREN.get(parentKey ?? "")?.has(normalizedKey)) {
      continue;
    }
    if (JLL_FREE_TEXT_KEYS.has(normalizedKey)) {
      continue;
    }
    output[key] = jllRedactSensitivePriceFields(child, normalizedKey);
  }
  return output;
}

const JLL_WITHHELD_PUBLIC_BASE_KEYS = new Set([
  "id",
  "url",
  "canonicalUrl",
  "name",
  "headline",
  "transactionType",
  "assetType",
  "street",
  "city",
  "state",
  "postalCode",
  "country",
  "latitude",
  "longitude",
  "sizeText",
  "buildingSizeSqft",
  "lastUpdated",
  "detailObservedAt",
]);

function jllSafeTenantIdentities(value: unknown): Array<{ name: string }> {
  if (!Array.isArray(value)) return [];
  return value.flatMap((tenant) => {
    const record = tenant as Record<string, unknown> | null;
    const name = jllSafePublicText(record?.name ?? record?.tenantName);
    // An identity can contain ordinary digits (for example, 7-Eleven), but it
    // cannot be a currency-bearing value or prose disclosure.
    if (!name) return [];
    return [{ name }];
  });
}

function jllWithheldPublicProjection(value: unknown): Record<string, unknown> {
  const redacted = jllRedactSensitivePriceFields(value) as Record<string, unknown>;
  const projected: Record<string, unknown> = {};
  for (const key of JLL_WITHHELD_PUBLIC_BASE_KEYS) {
    if (!Object.hasOwn(redacted, key)) continue;
    const candidate = redacted[key];
    if (typeof candidate !== "string") {
      projected[key] = candidate;
      continue;
    }
    const safe = jllSafePublicText(candidate);
    if (safe) projected[key] = safe;
  }
  const tenants = jllSafeTenantIdentities(redacted.currentTenants);
  if (tenants.length) projected.currentTenants = tenants;
  if (redacted.jllSearchResult !== undefined) {
    projected.jllSearchResult = {
      priceWithholdingControl: jllStoredWithholdingControl(redacted.jllSearchResult),
    };
  }
  return projected;
}

function jllPriceProvenance(
  price: JllNormalizedPrice,
  withheld: boolean
): Record<string, unknown> {
  const provenance: Record<string, unknown> = {
    sourceShape: price.sourceShape,
    normalization: withheld ? "redacted" : price.text === null ? "unavailable" : "available",
  };
  if (!withheld && price.text !== null) {
    provenance.normalizedText = price.text;
    provenance.normalizedAmount = price.amount;
    provenance.currency = price.currency;
    provenance.unit = price.unit;
  }
  return provenance;
}

export function jllGraphqlPriceText(price: any): string | null {
  const normalized = jllNormalizedPrice(price);
  return normalized.sourceShape === "structured" ? normalized.text : null;
}

export function jllDetailPriceText(price: any): string | null {
  return jllNormalizedPrice(price).text;
}

export function jllGraphqlItemToListing(
  item: any,
  tx: Tx,
  propertyType: string,
  page: number,
  total: number
): any {
  const id = clean(item?.id);
  const pageUrl = clean(item?.pageUrl);
  if (!id) throw new Error(`JLL GraphQL ${propertyType} page ${page} item lacks an id`);
  if (!pageUrl) throw new Error(`JLL GraphQL ${propertyType} page ${page} item ${id} lacks pageUrl`);

  let url: string;
  try {
    url = normalizedJllListingUrl(pageUrl);
  } catch {
    throw new Error(
      `JLL GraphQL ${propertyType} page ${page} item ${id} has an invalid pageUrl`
    );
  }
  const parsedUrl = new URL(url);
  if (
    parsedUrl.protocol !== "https:" ||
    parsedUrl.hostname !== "property.jll.com" ||
    !parsedUrl.pathname.startsWith("/listings/")
  ) {
    throw new Error(
      `JLL GraphQL ${propertyType} page ${page} item ${id} has a non-listing pageUrl`
    );
  }

  const priceWithholdingControl = jllWithholdingControl(item, "hidePrice");
  const hiddenPrice = jllPriceWithheld(priceWithholdingControl);
  const salePrice = jllNormalizedPrice(item?.salePrice);
  const rentPrice = jllNormalizedPrice(item?.rentPrice);
  const buildingSizeSqft = jllSurfaceAreaSqft(item);
  const propertyTypes = Array.isArray(item?.propertyTypes)
    ? item.propertyTypes.map((value: any) => clean(value)).filter(Boolean)
    : [];
  const tenureTypes = Array.isArray(item?.tenureTypes)
    ? item.tenureTypes.map((value: any) => clean(value)).filter(Boolean)
    : [];

  return prune({
    id,
    name: clean(item?.title) ?? clean(item?.address),
    transactionType: tx === "sale" ? "Sale" : "Lease",
    assetType: jllPropertyTypeLabel(propertyType),
    street: clean(item?.address),
    city: clean(item?.city),
    state: clean(item?.state),
    postalCode: clean(item?.postcode),
    country: "US",
    latitude: num(item?.latitude),
    longitude: num(item?.longitude),
    salePriceUsd: tx === "sale" && !hiddenPrice ? jllPriceUsd(salePrice) : null,
    salePriceText: tx === "sale" && !hiddenPrice ? salePrice.text : null,
    leaseRateText: tx === "lease" && !hiddenPrice ? jllLeasePriceText(rentPrice) : null,
    sizeText:
      buildingSizeSqft === null
        ? null
        : `${buildingSizeSqft.toLocaleString("en-US")} SF`,
    buildingSizeSqft,
    photos: jllStringUrls(item?.images),
    brokerIds: [],
    url,
    jllPropertyTypeFilters: [propertyType],
    jllSearchPages: [page],
    jllFilterTotals: { [propertyType]: total },
    jllSearchResult: {
      propertyTypes,
      tenureTypes,
      surfaceAreas: item?.surfaceAreas,
      priceWithholdingControl,
    },
  });
}

export function parseJllGraphqlSearchPage(
  payload: any,
  tx: Tx,
  propertyType: string,
  page: number
): { total: number; listings: any[] } {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("JLL GraphQL response is not an object");
  }
  if (payload.errors !== undefined) {
    if (!Array.isArray(payload.errors) || payload.errors.length > 0) {
      throw new Error("JLL GraphQL response contains errors");
    }
  }
  const properties = payload?.data?.properties;
  if (!properties || typeof properties !== "object" || Array.isArray(properties)) {
    throw new Error("JLL GraphQL response lacks data.properties");
  }
  const total = properties.count;
  if (!Number.isInteger(total) || total < 0) {
    throw new Error("JLL GraphQL response lacks a finite nonnegative count");
  }
  if (!Array.isArray(properties.items)) {
    throw new Error("JLL GraphQL response lacks a properties.items array");
  }

  const listings = properties.items.map((item: any) =>
    jllGraphqlItemToListing(item, tx, propertyType, page, total)
  );
  const ids = listings.map((listing: any) => clean(listing?.id));
  const urls = listings.map((listing: any) => clean(listing?.url));
  if (
    ids.some((id: string | null) => !id) ||
    urls.some((url: string | null) => !url) ||
    new Set(ids).size !== listings.length ||
    new Set(urls).size !== listings.length
  ) {
    throw new Error(
      `JLL GraphQL ${propertyType} page ${page} contains duplicate or missing ids/urls`
    );
  }
  return { total, listings };
}

async function requestJllGraphqlPage(
  tx: Tx,
  propertyType: string,
  page: number
): Promise<any> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), JLL_GRAPHQL_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(JLL_GRAPHQL_URL, {
      method: "POST",
      headers: {
        accept: "application/json",
        "cache-control": "no-cache",
        "content-type": "application/json",
        pragma: "no-cache",
      },
      cache: "no-store",
      signal: controller.signal,
      body: JSON.stringify({
        query: JLL_SEARCH_RESULTS_QUERY,
        variables: jllGraphqlVariables(tx, propertyType, page),
        operationName: "SearchResults",
      }),
    });
  } catch (error) {
    clearTimeout(timer);
    throw new JllGraphqlRequestError(
      controller.signal.aborted
        ? `JLL GraphQL request timed out after ${JLL_GRAPHQL_TIMEOUT_MS}ms`
        : `JLL GraphQL transport failed: ${String(error)}`,
      true
    );
  }

  try {
    const contentType = response.headers.get("content-type") ?? "";
    const body = await response.text();
    if (!response.ok) {
      throw new JllGraphqlRequestError(
        `JLL GraphQL HTTP ${response.status}`,
        response.status === 408 || response.status === 429 || response.status >= 500
      );
    }
    if (!contentType.toLowerCase().includes("application/json")) {
      throw new JllGraphqlRequestError(
        `JLL GraphQL returned non-JSON content-type ${contentType || "<missing>"}`,
        false
      );
    }
    try {
      return JSON.parse(body);
    } catch {
      throw new JllGraphqlRequestError("JLL GraphQL returned malformed JSON", false);
    }
  } catch (error) {
    if (error instanceof JllGraphqlRequestError) throw error;
    throw new JllGraphqlRequestError(
      controller.signal.aborted
        ? `JLL GraphQL response timed out after ${JLL_GRAPHQL_TIMEOUT_MS}ms`
        : `JLL GraphQL response read failed: ${String(error)}`,
      true
    );
  } finally {
    clearTimeout(timer);
  }
}

export async function fetchJllSearchPage(tx: Tx, propertyType: string, page: number): Promise<{
  total: number | null;
  listings: any[];
}> {
  let lastError: unknown = null;
  for (let attempt = 1; attempt <= JLL_GRAPHQL_RETRIES; attempt++) {
    try {
      const payload = await requestJllGraphqlPage(tx, propertyType, page);
      const parsed = parseJllGraphqlSearchPage(payload, tx, propertyType, page);
      try {
        assertJllSearchPageCompleteness(parsed, page, null, requireFreshDetails());
      } catch (error) {
        throw new JllGraphqlRequestError(
          `JLL GraphQL page coverage failed: ${String(error)}`,
          true
        );
      }
      return parsed;
    } catch (error) {
      lastError = error;
      const retryable =
        error instanceof JllGraphqlRequestError && error.retryable;
      if (!retryable || attempt === JLL_GRAPHQL_RETRIES) throw error;
      console.error(
        `  jll/${tx}/${propertyType}: GraphQL page ${page} attempt ${attempt} failed ` +
          `(${String(error)}); retrying`
      );
      await new Promise((resolve) => setTimeout(resolve, 250 * attempt));
    }
  }
  throw lastError;
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
  let cached: ScrapedDoc | null;
  if (opts.refresh) {
    recordJllDetailCache("refresh_bypass");
    cached = null;
  } else {
    cached = readJllDetailCache(url);
    recordJllDetailCache(cached ? "hit" : "miss");
  }
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
  return dedupeStrings(values.map((value) => clean(value))).filter((value): value is string => {
    if (!value) return false;
    try {
      const url = new URL(value);
      return (
        (url.protocol === "http:" || url.protocol === "https:") &&
        url.hostname.length > 0 &&
        url.pathname.length > 1
      );
    } catch {
      // Assets are optional evidence.  A malformed brochure/media URL must
      // not turn an otherwise valid listing into a detail-enrichment failure.
      return false;
    }
  });
}

/** True only for a native or typed brochure with a usable public URL. */
export function jllHasUsableBrochure(normalized: unknown): boolean {
  if (normalized === null || typeof normalized !== "object" || Array.isArray(normalized)) {
    return false;
  }
  const record = normalized as Record<string, unknown>;
  const usableUrl = (value: unknown): boolean => {
    if (typeof value !== "string") return false;
    try {
      const url = new URL(value);
      return (
        (url.protocol === "http:" || url.protocol === "https:") &&
        url.hostname.length > 0 &&
        url.pathname.length > 1
      );
    } catch {
      return false;
    }
  };
  const brochures = record.brochures;
  if (
    Array.isArray(brochures) &&
    brochures.some((item) => usableUrl(typeof item === "string" ? item : (item as any)?.url))
  ) {
    return true;
  }
  const documents = record.documents;
  return (
    Array.isArray(documents) &&
    documents.some(
      (item: any) =>
        usableUrl(item?.url) &&
        String(item?.docType ?? item?.documentType ?? item?.type ?? "").toLowerCase() ===
          "brochure"
    )
  );
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

function jllFloorPlanUrl(value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null;
  const url = clean(value);
  if (!url) return null;
  try {
    const parsed = new URL(url);
    if (
      (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
      !parsed.hostname ||
      parsed.pathname.length <= 1
    ) {
      return null;
    }
  } catch {
    return null;
  }
  return url;
}

function jllFloorPlanUrlKey(url: string): string {
  const parsed = new URL(url);
  const path = parsed.pathname.replace(/\/+$/, "") || "/";
  return `${parsed.protocol.toLowerCase()}//${parsed.host.toLowerCase()}${path}${parsed.search}`;
}

export function jllReconcileDocumentChannels(
  brochureUrls: string[],
  harvestedDocuments: DocItem[],
  floorPlanDocuments: DocItem[]
): { brochures: string[]; documents: DocItem[] } {
  const floorPlansByKey = new Map(
    floorPlanDocuments.map((document) => [jllFloorPlanUrlKey(document.url), document])
  );
  const brochuresByKey = new Map<string, string>();
  for (const url of brochureUrls) {
    const key = jllFloorPlanUrlKey(url);
    if (!floorPlansByKey.has(key) && !brochuresByKey.has(key)) {
      brochuresByKey.set(key, url);
    }
  }

  const documentsByKey = new Map<string, DocItem>();
  for (const document of harvestedDocuments) {
    const key = jllFloorPlanUrlKey(document.url);
    if (!brochuresByKey.has(key) && !documentsByKey.has(key)) {
      documentsByKey.set(key, document);
    }
  }
  // Native floor-plan metadata is authoritative over heuristic page-link
  // classification, while Map#set preserves the original document position.
  for (const [key, document] of floorPlansByKey) {
    documentsByKey.set(key, document);
  }
  return {
    brochures: [...brochuresByKey.values()],
    documents: [...documentsByKey.values()],
  };
}

function jllFloorPlanEntryUrls(value: unknown): string[] {
  if (value === null || value === undefined) return [];
  if (typeof value === "string") {
    const url = jllFloorPlanUrl(value);
    return url ? [url] : [];
  }
  if (typeof value !== "object" || Array.isArray(value)) {
    return [];
  }

  const entry = value as Record<string, unknown>;
  const urls = [
    jllFloorPlanUrl(entry.url),
    jllFloorPlanUrl(entry.image),
  ].filter((url): url is string => url !== null);
  return urls;
}

/**
 * Promote every native JLL floor-plan URL as an explicitly typed floor_plan
 * document, including image floor plans. JLL has emitted both a legacy array
 * and the current `{ images, files }` object. These are optional assets, so a
 * malformed entry is skipped while valid sibling detail remains usable.
 */
export function jllStrandedDocs(property: any): DocItem[] {
  const floorPlans = property?.floorPlans;
  if (floorPlans === null || floorPlans === undefined) return [];

  let urls: string[];
  if (Array.isArray(floorPlans)) {
    urls = floorPlans.flatMap(jllFloorPlanEntryUrls);
  } else if (typeof floorPlans === "object") {
    const value = floorPlans as Record<string, unknown>;
    const images = Array.isArray(value.images) ? value.images : [];
    const files = Array.isArray(value.files) ? value.files : [];
    urls = [
      ...images.flatMap(jllFloorPlanEntryUrls),
      ...files.flatMap(jllFloorPlanEntryUrls),
    ];
  } else {
    return [];
  }

  const seen = new Set<string>();
  return urls.filter((url) => {
    const key = jllFloorPlanUrlKey(url);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).map((url) => ({
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
  // A listing can carry the same provider control at its top level and inside
  // jllSearchResult. Establish that boundary before a cached detail payload is
  // parsed so a malformed detail shape cannot restore a public search price.
  const baseWithholdingControl = jllReconciledWithholdingControl(
    base,
    base?.jllSearchResult
  );
  const basePriceWithheld = jllPriceWithheld(baseWithholdingControl);
  let failurePricing: Record<string, unknown> | null = null;
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
    if (!property) {
      return prune({
        ...(basePriceWithheld
          ? jllWithheldPublicProjection(base)
          : jllRedactSensitivePriceFields(base)),
        detailError: "missing property in __NEXT_DATA__",
      });
    }

    const detailId = clean(property.id);
    const detailUrlRaw = clean(property.pageUrl) ?? clean(pageProps?.relativeUrl);
    if (!detailId || detailId !== clean(base.id)) {
      throw new Error(
        `JLL detail provider id mismatch: expected ${clean(base.id) ?? "missing"}, ` +
          `received ${detailId ?? "missing"}`
      );
    }
    if (
      !detailUrlRaw ||
      normalizedJllListingUrl(detailUrlRaw) !== normalizedJllListingUrl(base.url)
    ) {
      throw new Error("JLL detail listing URL does not match enumerated inventory URL");
    }

    // Establish and enforce the price visibility boundary before parsing any
    // fallible detail shape.  A malformed floor-plan payload must not turn a
    // hidden price into the unmodified search-card fallback.
    const searchWithholdingControl = jllReconciledWithholdingControl(
      base,
      base?.jllSearchResult
    );
    const detailWithholdingControl = jllWithholdingControl(property, "hidePrice");
    const hiddenPrice = jllPriceWithheld(
      searchWithholdingControl,
      detailWithholdingControl
    );
    const salePrice = jllNormalizedPrice(property.salePrice);
    const rentPrice = jllNormalizedPrice(property.rentPrice);
    const pricing = {
      visibility: hiddenPrice ? "withheld" : "visible",
      searchWithholdingControl,
      detailWithholdingControl,
      sale: jllPriceProvenance(salePrice, hiddenPrice),
      lease: jllPriceProvenance(rentPrice, hiddenPrice),
    };
    failurePricing = hiddenPrice ? pricing : null;
    const publicBase = hiddenPrice ? jllWithheldPublicProjection(base) : base;

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
    const rawBrochures = jllStringUrls(property.brochures);
    const images = jllStringUrls(property.images);
    const url = normalizedJllListingUrl(base.url);
    const floorPlanDocuments = jllStrandedDocs(property);

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
      extraDocs: floorPlanDocuments,
      extraImages: images,
    });
    // A native floor-plan classification wins over both a brochure overlap and
    // heuristic page-link classification. All channels share the same normalized
    // URL identity so equivalent spellings cannot create duplicate child rows.
    const documentChannels = jllReconcileDocumentChannels(
      rawBrochures,
      harvested.documents,
      floorPlanDocuments
    );
    const brochureDocs = documentChannels.brochures.map((docUrl) => ({
      name: titleFromFilename(docUrl),
      url: docUrl,
    }));
    const documents = documentChannels.documents;
    const photos = dedupeStrings([...(images.length ? images : base.photos ?? []), ...harvested.images]);
    // Withheld detail gets a deliberately minimal, allowlisted representation.
    // Do not attempt to maintain a future blacklist for arbitrary provider
    // fields: price-bearing prose can surface in highlights or long-tail facts.
    const lifted = hiddenPrice ? {} : jllStrandedStructured(property);
    // `publicBase` already strips search-card prose, but successful detail
    // enrichment replaces it below.  Keep the visibility boundary for those
    // normalized detail values too.
    const description = jllDescription(property) ?? base.description;
    const markdown = doc.markdown || base.markdown;
    return prune({
      ...publicBase,
      detailObservedAt: doc.detailObservation?.observedAt,
      freshnessProvenance: {
        detailScope: "detail_page",
        generationId: doc.detailObservation?.generationId ?? null,
        method: doc.detailObservation?.method ?? "jll_detail",
        cacheDisposition: doc.detailObservation?.cacheDisposition ?? "live",
      },
      id: base.id,
      name: hiddenPrice
        ? jllSafePublicText(clean(property.title) ?? publicBase.name) ?? undefined
        : clean(property.title) ?? base.name,
      assetType: hiddenPrice
        ? jllSafePublicText(
            Array.isArray(property.propertyTypes)
              ? property.propertyTypes.map(jllPropertyTypeLabel).join(", ")
              : clean(property.propertyType) ?? publicBase.assetType
          ) ?? undefined
        : Array.isArray(property.propertyTypes)
          ? property.propertyTypes.map(jllPropertyTypeLabel).join(", ")
          : clean(property.propertyType) ?? base.assetType,
      description: hiddenPrice ? undefined : description,
      street: hiddenPrice
        ? jllSafePublicText(clean(property.address) ?? publicBase.street) ?? undefined
        : clean(property.address) ?? base.street,
      city: hiddenPrice
        ? jllSafePublicText(clean(property.city) ?? publicBase.city) ?? undefined
        : clean(property.city) ?? base.city,
      state: hiddenPrice
        ? jllSafePublicText(clean(property.state) ?? publicBase.state) ?? undefined
        : clean(property.state) ?? base.state,
      postalCode: hiddenPrice
        ? jllSafePublicText(clean(property.postcode) ?? publicBase.postalCode) ?? undefined
        : clean(property.postcode) ?? base.postalCode,
      latitude: num(property.latitude) ?? base.latitude,
      longitude: num(property.longitude) ?? base.longitude,
      salePriceUsd: hiddenPrice
        ? null
        : salePrice.sourceShape === "absent"
          ? base.salePriceUsd
          : jllPriceUsd(salePrice),
      salePriceText: hiddenPrice
        ? null
        : salePrice.sourceShape === "absent"
          ? base.salePriceText
          : salePrice.text,
      leaseRateText: hiddenPrice
        ? null
        : rentPrice.sourceShape === "absent"
          ? base.leaseRateText
          : jllLeasePriceText(rentPrice),
      sizeText: hiddenPrice
        ? jllSafePublicText(clean(property.surfaceArea) ?? publicBase.sizeText) ?? undefined
        : clean(property.surfaceArea) ?? base.sizeText,
      buildingSizeSqft: jllSurfaceAreaSqft(property) ?? base.buildingSizeSqft,
      ...lifted,
      brokerIds,
      contactsDetailed,
      brochures: brochureDocs,
      documents,
      media: harvested.media,
      links: harvested.links,
      photos,
      markdown: hiddenPrice ? undefined : markdown,
      url,
      lastUpdated: base.lastUpdated,
      jllDetail: {
        id: clean(property.id),
        refId: hiddenPrice ? jllSafePublicText(property.refId) ?? undefined : clean(property.refId),
        pageUrl: hiddenPrice ? jllSafePublicText(property.pageUrl) ?? undefined : clean(property.pageUrl),
        relativeUrl: hiddenPrice
          ? jllSafePublicText(pageProps?.relativeUrl) ?? undefined
          : clean(pageProps?.relativeUrl),
        pricing,
        tenureTypes: hiddenPrice ? undefined : property.tenureTypes,
        propertyTypes: hiddenPrice ? undefined : property.propertyTypes,
        labels: hiddenPrice ? undefined : property.labels,
        amenities: hiddenPrice ? undefined : property.amenities,
        amenitiesData: hiddenPrice ? undefined : property.amenitiesData,
        highlights: hiddenPrice ? undefined : property.highlights,
        customRefId: hiddenPrice
          ? jllSafePublicText(property.customRefId) ?? undefined
          : clean(property.customRefId),
        buildingClass: hiddenPrice
          ? jllSafePublicText(property.buildingClass) ?? undefined
          : clean(property.buildingClass),
        parkingDetails: hiddenPrice ? undefined : property.parkingDetails,
        locationDescription: hiddenPrice ? undefined : stripHtmlText(property.locationDescription),
        submarket: hiddenPrice ? undefined : clean(property.submarket),
        videos: hiddenPrice ? undefined : property.videos,
        virtualTours: hiddenPrice ? undefined : property.virtualTours,
        view360URLs: hiddenPrice ? undefined : property.view360URLs,
        floorPlans: hiddenPrice ? undefined : property.floorPlans,
        floorPlanAssetCount: floorPlanDocuments.length,
        brokerCount: contactsDetailed.length,
        brochureCount: documentChannels.brochures.length,
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
    // The error path is also a visibility boundary.  Redact recursively so a
    // search-card askingPrice or an old nested jllDetail price cannot survive a
    // malformed detail field; preserve only safe control/provenance fields.
    const redacted = basePriceWithheld || failurePricing !== null
      ? jllWithheldPublicProjection(base)
      : jllRedactSensitivePriceFields(base);
    const detail =
      failurePricing === null
        ? redacted.jllDetail
        : { ...(redacted.jllDetail ?? {}), pricing: failurePricing };
    return prune({ ...redacted, jllDetail: detail, detailError: String(err) });
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
  const observedIdentityPairs: Array<{ id?: unknown; url?: unknown }> = [];
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
        observedIdentityPairs.push(listing);
        const url = clean(listing?.url);
        if (url) urls.add(url);
      }
    }
    assertJllIdentityReconciliation(observedIdentityPairs);

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
      "Public JLL SearchResults GraphQL enumeration across propertyTypes filters, then detail __NEXT_DATA__ enrichment with URL-only assets",
    totalAvailable: total,
    listings: enriched,
    truncated: coverageTruncated,
    note: `Per-filter source totals before cross-filter de-dupe: ${totalEvidence}. Detail enrichment stores public brochure/image/profile URLs only and retains per-row detailError if a detail scrape fails.`,
  };
}
