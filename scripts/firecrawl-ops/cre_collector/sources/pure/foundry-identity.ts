import * as cheerio from "cheerio";

import { dedupeStrings } from "../../lib/html.js";
import { clean } from "../../lib/util.js";

/** Pure Foundry sitemap and WordPress identity validation. */
export const FOUNDRY_HOST = "https://www.foundrycommercial.com";
export const FOUNDRY_SITEMAP_URL = `${FOUNDRY_HOST}/sitemap.xml`;

const FOUNDRY_PROPERTY_SITEMAP_PATH = /(?:^|\/)property-sitemap(?:\d+)?\.xml$/i;

export function foundryUrl(value: string, kind: "detail" | "sitemap"): string | null {
  try {
    const parsed = new URL(value, FOUNDRY_HOST);
    if (
      parsed.protocol !== "https:"
      || parsed.hostname.toLowerCase().replace(/^www\./, "") !== "foundrycommercial.com"
      || parsed.username
      || parsed.password
      || parsed.port
      || parsed.search
      || parsed.hash
    ) {
      return null;
    }
    if (/\.pdf$/i.test(parsed.pathname)) return null;
    if (kind === "detail" && !/^\/property\/[^/]+\/?$/i.test(parsed.pathname)) return null;
    if (
      kind === "sitemap"
      && !/^\/(?:sitemap|sitemap_index|property-sitemap(?:\d+)?)\.xml$/i.test(parsed.pathname)
    ) {
      return null;
    }
    return parsed.toString();
  } catch {
    return null;
  }
}

export function samePage(left: string, right: string): boolean {
  const normalize = (value: string) => {
    const parsed = new URL(value);
    return `${parsed.hostname.toLowerCase().replace(/^www\./, "")}${parsed.pathname
      .replace(/\/+$/, "")
      .toLowerCase()}`;
  };
  try {
    return normalize(left) === normalize(right);
  } catch {
    return false;
  }
}

function sitemapLocations(
  xml: string,
  rootName: "sitemapindex" | "urlset",
  entryName: "sitemap" | "url",
  label: string,
): string[] {
  if (!xml.trim()) throw new Error(`${label} is empty`);
  const $ = cheerio.load(xml, { xmlMode: true });
  const roots = $.root().children().toArray();
  if (
    roots.length !== 1
    || roots[0].type !== "tag"
    || roots[0].name.toLowerCase() !== rootName
  ) {
    throw new Error(`${label} requires one ${rootName} root`);
  }
  const entries = $(roots[0]).children().toArray();
  if (
    entries.length === 0
    || entries.some((entry) => entry.type !== "tag" || entry.name.toLowerCase() !== entryName)
  ) {
    throw new Error(`${label} requires ${entryName} child elements`);
  }
  return entries.map((entry, index) => {
    const locs = $(entry)
      .children()
      .filter((_, child) => child.type === "tag" && child.name.toLowerCase() === "loc")
      .toArray();
    const location = locs.length === 1 ? clean($(locs[0]).text()) : null;
    if (!location) throw new Error(`${label} ${entryName} ${index} requires exactly one loc`);
    return location;
  });
}

export function foundryPropertySitemaps(indexXml: string): string[] {
  const locations = sitemapLocations(indexXml, "sitemapindex", "sitemap", "Foundry sitemap index");
  const urls = dedupeStrings(
    locations.flatMap((value, index) => {
      let propertyShaped = false;
      try {
        propertyShaped = FOUNDRY_PROPERTY_SITEMAP_PATH.test(new URL(value, FOUNDRY_HOST).pathname);
      } catch {
        propertyShaped = FOUNDRY_PROPERTY_SITEMAP_PATH.test(value.split(/[?#]/, 1)[0]);
      }
      if (!propertyShaped) return [];
      const url = foundryUrl(value, "sitemap");
      if (!url) throw new Error(`Foundry sitemap index property loc ${index} is invalid`);
      return [url];
    }),
  );
  if (!urls.length) throw new Error("Foundry sitemap index has no valid property sitemap");
  return urls;
}

export function foundryPropertyUrls(propertyXml: string): string[] {
  const locations = sitemapLocations(propertyXml, "urlset", "url", "Foundry property sitemap");
  return dedupeStrings(
    locations.map((value, index) => {
      const url = foundryUrl(value, "detail");
      if (!url) throw new Error(`Foundry property sitemap URL ${index} is invalid`);
      return url;
    }),
  );
}

export function foundryProviderIdentity(html: string, requestedUrl: string): string | null {
  const requested = foundryUrl(requestedUrl, "detail");
  if (!requested) return null;
  const $ = cheerio.load(html);
  const canonicalRaw = clean($("link[rel='canonical']").first().attr("href"))
    ?? clean($("meta[property='og:url']").first().attr("content"));
  const canonical = canonicalRaw ? foundryUrl(canonicalRaw, "detail") : null;
  if (!canonical || !samePage(canonical, requested)) return null;

  const shortlink = clean($("link[rel='shortlink']").first().attr("href"));
  if (!shortlink) return null;
  try {
    const parsed = new URL(shortlink);
    if (
      parsed.origin !== FOUNDRY_HOST
      || parsed.pathname !== "/"
      || parsed.hash
      || parsed.username
      || parsed.password
      || parsed.port
      || parsed.searchParams.size !== 1
      || parsed.searchParams.getAll("p").length !== 1
    ) {
      return null;
    }
    const rawId = parsed.searchParams.get("p");
    const id = rawId && /^[1-9]\d*$/.test(rawId) ? Number(rawId) : Number.NaN;
    return Number.isSafeInteger(id) ? String(id) : null;
  } catch {
    return null;
  }
}

/** Extract the provider-owned property notes from a detail page. */
export function foundryPropertyNotes(html: string): string[] {
  const $ = cheerio.load(html);
  return $(".property-notes li")
    .map((_, element) => clean($(element).text()))
    .get()
    .filter((value): value is string => Boolean(value));
}

/** Extract the provider's explicit property-status note from a detail page. */
export function foundryExplicitStatus(html: string): string | null {
  return foundryPropertyNotes(html)[0] ?? null;
}
