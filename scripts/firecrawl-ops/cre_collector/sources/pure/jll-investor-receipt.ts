/** Side-effect-free JLL Investor request and identity helpers for C10. */
import * as cheerio from "cheerio";

export const JLL_INVESTOR_HOST = "https://invest.jll.com";
export const JLL_INVESTOR_SEARCH_URL = "https://invest.jll.com/us/en/property-search?filter=%7B%22location%22%3A%5B%22United%20States%22%5D%7D";
export const JLL_INVESTOR_SEARCH_PAGE_SIZE = 50;

function clean(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function nextData(rawHtml: string): any | null {
  const text = cheerio.load(rawHtml)("#__NEXT_DATA__").first().text();
  try { return text ? JSON.parse(text) : null; } catch { return null; }
}

export function jllInvestorBuildId(rawHtml: string): string | null {
  const buildId = clean(nextData(rawHtml)?.buildId);
  return buildId && /^[A-Za-z0-9_-]+$/.test(buildId) ? buildId : null;
}

export function jllInvestorStructuredListing(payload: any): any | null {
  const listing = payload?.pageProps?.initialState?.pdp?.listing;
  return listing && typeof listing === "object" ? listing : null;
}

export function jllInvestorUrlFromAlias(alias: string | null): string | null {
  const value = clean(alias);
  if (!value) return null;
  if (/^https?:\/\//i.test(value)) return value;
  const path = value.startsWith("/us/en/listings/") ? value : `/us/en/listings/${value.replace(/^\/+/, "")}`;
  return `${JLL_INVESTOR_HOST}${path}`;
}

export function jllInvestorDetailRoute(buildId: string, publicUrl: string): { alias: string; url: string } {
  if (!/^[A-Za-z0-9_-]+$/.test(buildId)) throw new Error("invalid JLL Investor Next.js build id");
  let parsed: URL;
  try { parsed = new URL(publicUrl); } catch { throw new Error("invalid JLL Investor detail URL"); }
  if (parsed.protocol !== "https:" || parsed.hostname.toLowerCase() !== "invest.jll.com" || parsed.port || parsed.username || parsed.password || parsed.search || parsed.hash) throw new Error("unsafe JLL Investor detail URL");
  const match = parsed.pathname.match(/^\/us\/en\/listings\/([^/]+)\/([^/]+)\/?$/);
  if (!match) throw new Error("unsupported JLL Investor detail path");
  const segment = (value: string) => {
    let decoded: string;
    try { decoded = decodeURIComponent(value); } catch { throw new Error("invalid JLL Investor detail path encoding"); }
    if (!decoded || decoded === "." || decoded === ".." || decoded.includes("/") || decoded.includes("\\") || !/^[A-Za-z0-9][A-Za-z0-9._~-]*$/.test(decoded)) throw new Error("unsafe JLL Investor detail path segment");
    return decoded;
  };
  const asset = segment(match[1]!); const slug = segment(match[2]!); const alias = `${asset}/${slug}`;
  const route = [JLL_INVESTOR_HOST, "_next", "data", encodeURIComponent(buildId), "us", "en", "listings", encodeURIComponent(asset), `${encodeURIComponent(slug)}.json`].join("/");
  const query = new URLSearchParams({ region: "us", locale: "en", asset, alias: slug });
  return { alias, url: `${route}?${query.toString()}` };
}

export function jllInvestorSearchPageUrl(page: number): string {
  if (!Number.isInteger(page) || page < 1) throw new Error("invalid JLL Investor search page");
  return page === 1 ? JLL_INVESTOR_SEARCH_URL : `${JLL_INVESTOR_SEARCH_URL}&page=${page}`;
}

export function parseJllInvestorSearchPage(rawHtml: string, expectedPage: number): { count: number; page: number; rows: any[] } {
  const search = nextData(rawHtml)?.props?.pageProps?.initialState?.advancedSearch;
  const filters = Array.isArray(search?.filters) ? search.filters : [];
  if (filters.length !== 1 || clean(filters[0]?.key) !== "location" || clean(filters[0]?.value) !== "United States" || clean(filters[0]?.label) !== "United States" || clean(filters[0]?.type) !== "collection") throw new Error("JLL Investor search page lacks the exact United States filter state");
  const { count, searchPage: page, listings: rows } = search ?? {};
  if (!Number.isInteger(count) || count <= 0) throw new Error("JLL Investor search page lacks a positive integer count");
  if (!Number.isInteger(page) || page !== expectedPage) throw new Error(`JLL Investor search page mismatch: expected ${expectedPage}, received ${String(page)}`);
  if (!Array.isArray(rows)) throw new Error("JLL Investor search page lacks a listings array");
  const pages = Math.ceil(count / JLL_INVESTOR_SEARCH_PAGE_SIZE);
  const expectedRows = expectedPage < pages ? JLL_INVESTOR_SEARCH_PAGE_SIZE : count - JLL_INVESTOR_SEARCH_PAGE_SIZE * (pages - 1);
  if (expectedPage > pages || rows.length !== expectedRows) throw new Error(`JLL Investor search page ${expectedPage} has an invalid row count`);
  const ids = new Set<string>();
  const urls = new Set<string>();
  for (const row of rows) {
    const id = clean(row?.id);
    const url = jllInvestorUrlFromAlias(row?.alias);
    if (!id || !/^006[A-Za-z0-9]{15}$/.test(id) || !url) throw new Error(`JLL Investor search page ${expectedPage} has an invalid listing identity`);
    jllInvestorDetailRoute("search-proof", url);
    if (ids.has(id) || urls.has(url)) throw new Error(`JLL Investor search page ${expectedPage} has a duplicate identity`);
    ids.add(id); urls.add(url);
  }
  return { count, page, rows };
}
