/**
 * Side-effect-free JLL request-shape and identity parsing shared by the
 * collector and C10 receipts.  Keep this module free of cache, filesystem,
 * and scraper imports: it is safe to load in the sealed receipt lane.
 */
import * as cheerio from "cheerio";

export const JLL_SEARCH_PAGE_SIZE = 50;
export const JLL_GRAPHQL_URL = "https://property.jll.com/api/graphql";
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
        rentPrice { amount currency unit }
        salePrice { amount currency unit }
        hidePrice
        pageUrl
        latitude
        longitude
        city
        state
        postcode
        surfaceAreas { value unit label alternativeUnit showEstimateDesks metrics { value unit } }
      }
    }
  }
`;

export function normalizedJllListingUrl(href: string): string {
  const url = new URL(href.startsWith("http") ? href : `https://property.jll.com${href}`);
  url.hash = "";
  url.search = "";
  return url.toString().replace(/\/$/, "");
}

export function jllGraphqlVariables(
  tx: "sale" | "lease",
  propertyType: string,
  page: number,
): Record<string, unknown> {
  if (!Number.isInteger(page) || page < 1) throw new Error(`JLL GraphQL page must be a positive integer, received ${page}`);
  return {
    market: "us", language: "en", propertyTypes: [propertyType],
    tenureTypes: [tx === "sale" ? "sale" : "rent"], skip: (page - 1) * JLL_SEARCH_PAGE_SIZE,
    take: JLL_SEARCH_PAGE_SIZE,
    orderBy: { field: "dateModified", direction: "desc", imagePriority: true },
  };
}

export function parseJllGraphqlSearchEnvelope(payload: unknown): { readonly total: number; readonly items: readonly Record<string, unknown>[] } {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("JLL GraphQL response is not an object");
  const record = payload as { errors?: unknown; data?: { properties?: unknown } };
  if (record.errors !== undefined && (!Array.isArray(record.errors) || record.errors.length > 0)) {
    throw new Error("JLL GraphQL response contains errors");
  }
  const properties = record.data?.properties;
  if (!properties || typeof properties !== "object" || Array.isArray(properties)) throw new Error("JLL GraphQL response lacks data.properties");
  const shape = properties as { count?: unknown; items?: unknown };
  if (!Number.isInteger(shape.count) || (shape.count as number) < 0) throw new Error("JLL GraphQL response lacks a finite nonnegative count");
  if (!Array.isArray(shape.items) || shape.items.some((item) => !item || typeof item !== "object" || Array.isArray(item))) {
    throw new Error("JLL GraphQL response lacks a properties.items array");
  }
  return { total: shape.count as number, items: shape.items as readonly Record<string, unknown>[] };
}

export function jllNextData(rawHtml: string): unknown | null {
  const $ = cheerio.load(rawHtml);
  const text = $("#__NEXT_DATA__").first().text();
  if (!text) return null;
  try { return JSON.parse(text); } catch { return null; }
}
