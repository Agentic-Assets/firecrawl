import * as cheerio from "cheerio";
import { createHash } from "node:crypto";
import { decodeHtmlEntities } from "../../lib/html.js";

function normalized(value: unknown): string {
  return typeof value === "string"
    ? decodeHtmlEntities(value).normalize("NFKC").replace(/\s+/g, " ").trim().toLowerCase()
    : "";
}

export function transwesternIdentityTuple(row: any): string[] {
  const tuple = [row?.BuildingName, row?.FullAddress, row?.City, row?.State, row?.ZipCode].map(normalized);
  if (tuple.some((value) => !value)) throw new Error("Transwestern published identity requires a complete name and address");
  return tuple;
}

export function transwesternIdentityDigest(row: any): string {
  return createHash("sha256").update(JSON.stringify(transwesternIdentityTuple(row))).digest("hex");
}

export function requireUniqueTranswesternPlaceholderIdentity(rows: any[]): void {
  const identities = new Set(rows.map(transwesternIdentityDigest));
  if (identities.size !== 1) throw new Error("Transwestern placeholder slug has conflicting feed identities");
}

export function transwesternPublishedPlaceholderUrl(xml: string): string {
  const $ = cheerio.load(xml, { xmlMode: true });
  if ($.root().children().length !== 1 || !$.root().children().first().is("urlset") || /<!DOCTYPE|<!ENTITY/i.test(xml)) {
    throw new Error("Transwestern published identity requires an official URL sitemap");
  }
  const matches = $.root().children("urlset").children("url").children("loc").map((_, el) => $(el).text().trim())
    .get().filter((url) => url === "https://transwestern.com/property/-");
  if (matches.length !== 1) throw new Error("Transwestern placeholder URL must occur exactly once in the official sitemap");
  return matches[0];
}

export function transwesternPrimaryIdentityMatches(rawHtml: string, row: any): boolean {
  let tuple: string[];
  try { tuple = transwesternIdentityTuple(row); } catch { return false; }
  const $ = cheerio.load(rawHtml);
  const banner = $(".property-dtl-bnr");
  const headings = banner.find("h1");
  const heading = normalized(headings.first().text());
  const addresses = banner.find(".lct-zipe-code");
  const normalizeAddress = (value: string) => normalized(value).replace(/,/g, "").replace(/\s+/g, " ");
  return banner.length === 1 && headings.length === 1 && heading === tuple[0] && addresses.length === 1 &&
    normalizeAddress(addresses.first().text()) === normalizeAddress(tuple.slice(1).join(" "));
}
