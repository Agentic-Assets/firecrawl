// sources/avison-young.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { decodeHtmlEntities, dedupeStrings, firstJsonLd, stripHtmlText, titleFromFilename } from "../lib/html.js";
import { scrapeDoc } from "../lib/scrape.js";
import { ScrapedDoc, SourceResult, Tx } from "../types.js";
import { boundedInt, clean, num, pmap, prune } from "../lib/util.js";


// --- Avison Young: SharpLaunch search app ---

export const AVISON_YOUNG_PAGE_URL =
  "https://www.avisonyoung.us/properties/#/?transaction=sale&view=sidebar&status=active";
export const AVISON_YOUNG_API_BASE = "https://pse-api.sharplaunch.com/data";
export const AVISON_YOUNG_FALLBACK_API_KEY = "b9fda00f3d4d7f623665270841e32176";
export const AVISON_YOUNG_CDN_BASE = "https://cdn.sharplaunch.com";
export const AVISON_YOUNG_HOST = "https://www.avisonyoung.us";
export const AVISON_YOUNG_DETAIL_CONCURRENCY = boundedInt(
  process.env.AVISON_YOUNG_DETAIL_CONCURRENCY,
  CONCURRENCY,
  1,
  CONCURRENCY
);

export let avisonYoungCache:
  | {
      apiKey: string;
      websiteRows: any[];
      teamMembers: Map<string, any>;
    }
  | null = null;

export async function fetchAvisonYoungApiKey(): Promise<string> {
  try {
    const res = await fetch(AVISON_YOUNG_PAGE_URL, {
      headers: { "User-Agent": "Mozilla/5.0 CRE collector" },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const html = await res.text();
    const key = html.match(/SharpLaunch\.PSE\.create\(\s*['"]([a-f0-9]{32})['"]/i)?.[1];
    if (key) return key;
    console.error("  avison-young: SharpLaunch key not found on page; using documented fallback");
  } catch (err) {
    console.error(`  avison-young: failed to fetch page key (${err}); using documented fallback`);
  }
  return AVISON_YOUNG_FALLBACK_API_KEY;
}

export async function fetchAvisonYoungEntity(entity: string, apiKey: string): Promise<any[]> {
  const url = new URL(AVISON_YOUNG_API_BASE);
  url.searchParams.set("entity", entity);
  if (entity === "website") url.searchParams.set("status", "active");
  const res = await fetch(url, { headers: { "X-Api-Key": apiKey } });
  if (!res.ok) throw new Error(`Avison Young SharpLaunch ${entity} API HTTP ${res.status}`);
  const data = await res.json();
  const items = Array.isArray((data as any).items) ? (data as any).items : [];
  if (!items.length) throw new Error(`Avison Young SharpLaunch ${entity} API returned no items`);
  return items;
}

export async function getAvisonYoungFeed(): Promise<{
  apiKey: string;
  websiteRows: any[];
  teamMembers: Map<string, any>;
}> {
  if (avisonYoungCache) return avisonYoungCache;
  const apiKey = await fetchAvisonYoungApiKey();
  const [websiteRows, teamRows] = await Promise.all([
    fetchAvisonYoungEntity("website", apiKey),
    fetchAvisonYoungEntity("team_member", apiKey),
  ]);
  const teamMembers = new Map<string, any>();
  for (const member of teamRows) {
    if (member?.id != null) teamMembers.set(String(member.id), member);
  }
  avisonYoungCache = { apiKey, websiteRows, teamMembers };
  console.error(
    `  avison-young: cached SharpLaunch feed (${websiteRows.length} active rows, ${teamMembers.size} team members)`
  );
  return avisonYoungCache;
}

export function sharpLaunchCdnUrl(path: any): string | null {
  const p = clean(path);
  if (!p) return null;
  if (/^https?:\/\//i.test(p)) return p;
  return `${AVISON_YOUNG_CDN_BASE}/${p.replace(/^\/+/, "")}`;
}

export function avisonYoungAbsoluteUrl(value: any, base = AVISON_YOUNG_HOST): string | null {
  const raw = clean(value);
  if (!raw || /^javascript:/i.test(raw) || /^mailto:/i.test(raw) || /^tel:/i.test(raw)) return null;
  try {
    return new URL(decodeHtmlEntities(raw), base).toString();
  } catch {
    return null;
  }
}

export function avisonYoungDetailLimit(max: number, selectedCount: number): number {
  if (process.env.AVISON_YOUNG_DETAIL_LIMIT !== undefined) {
    return boundedInt(process.env.AVISON_YOUNG_DETAIL_LIMIT, 0, 0, selectedCount);
  }
  return Number.isFinite(max) ? selectedCount : 0;
}

export function extractAvisonYoungUrls(doc: ScrapedDoc, baseUrl: string): string[] {
  const $ = cheerio.load(doc.rawHtml);
  const candidates: Array<string | null> = [...doc.links];
  $("a[href], img[src], source[src], [data-src], [data-href]").each((_, el) => {
    candidates.push(
      $(el).attr("href") ?? $(el).attr("src") ?? $(el).attr("data-src") ?? $(el).attr("data-href") ?? null
    );
  });
  for (const match of doc.rawHtml.match(/https?:\/\/[^"'<>\\\s)]+/gi) ?? []) {
    candidates.push(match);
  }
  return dedupeStrings(
    candidates
      .map((url) => avisonYoungAbsoluteUrl(url, baseUrl))
      .filter((url: string | null): url is string => Boolean(url))
      .map((url) => url.replace(/&amp;/g, "&").replace(/[)"'\]>]+$/g, ""))
  );
}

export function extractAvisonYoungDocuments(docs: Array<{ doc: ScrapedDoc; url: string }>): any[] {
  const urls = dedupeStrings(
    docs
      .flatMap(({ doc, url }) => extractAvisonYoungUrls(doc, url))
      .filter((url) => {
        try {
          const u = new URL(url);
          return /\.pdf(?:[?#].*)?$/i.test(u.pathname + u.search) && /(^|\.)sharplaunch\.com$/i.test(u.hostname);
        } catch {
          return false;
        }
      })
  );
  return urls.map((url) => ({ name: titleFromFilename(url), url }));
}

export function isAvisonYoungPropertyPhoto(url: string): boolean {
  try {
    const u = new URL(url);
    const filename = u.pathname.split("/").pop()?.toLowerCase() ?? "";
    return (
      u.hostname === "cdn.sharplaunch.com" &&
      /\.(?:jpe?g|png|webp)(?:[?#].*)?$/i.test(u.pathname + u.search) &&
      !/\/media\//i.test(u.pathname) &&
      // Exclude 150x150 dimension prefix (broker headshots/avatars)
      !/\/150x150\//i.test(u.pathname) &&
      // Exclude known non-property images (logo, generic header)
      !/ay_logo/i.test(filename) &&
      !/sharplaunch_header/i.test(filename) &&
      (/\/website-\d+\//i.test(u.pathname) || /\/v2\/client-\d+\//i.test(u.pathname))
    );
  } catch {
    return false;
  }
}

export function extractAvisonYoungPhotos(docs: Array<{ doc: ScrapedDoc; url: string }>, fallback: string[]): string[] {
  const urls = docs
    .flatMap(({ doc, url }) => extractAvisonYoungUrls(doc, url))
    .filter(isAvisonYoungPropertyPhoto);
  // Apply the same filter to fallback so non-property feed images are excluded too
  const filteredFallback = fallback.filter(isAvisonYoungPropertyPhoto);
  return dedupeStrings([...urls, ...filteredFallback]);
}

export function extractAvisonYoungJsonLd(docs: Array<{ doc: ScrapedDoc; url: string }>): any | null {
  for (const { doc } of docs) {
    const listing = firstJsonLd(doc.rawHtml, "RealEstateListing");
    if (listing) return listing;
  }
  return null;
}

export function avisonYoungNameSlug(name: string | null): string | null {
  if (!name) return null;
  return clean(
    name
      .toLowerCase()
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
  );
}

export function extractAvisonYoungContactUrls(docs: Array<{ doc: ScrapedDoc; url: string }>): {
  profileLinks: Array<{ url: string; text: string | null; slug: string | null }>;
  vcardLinks: string[];
} {
  const profileLinks: Array<{ url: string; text: string | null; slug: string | null }> = [];
  const vcardLinks: string[] = [];
  for (const { doc, url: baseUrl } of docs) {
    const $ = cheerio.load(doc.rawHtml);
    $('a[href*="/professionals/-/ayp/view/"]').each((_, el) => {
      const href = avisonYoungAbsoluteUrl($(el).attr("href"), baseUrl);
      if (!href) return;
      try {
        const u = new URL(href);
        if (u.hostname !== "www.avisonyoung.us") return;
        const slug = clean(u.pathname.match(/\/professionals\/-\/ayp\/view\/([^/]+)/i)?.[1] ?? null);
        profileLinks.push({ url: u.toString(), text: clean($(el).text()), slug });
      } catch {
        /* ignore malformed contact URL */
      }
    });
    $('a[href*="vcard"], a[href*="vcf"], a[href*="GetVCard"]').each((_, el) => {
      const href = avisonYoungAbsoluteUrl($(el).attr("href"), baseUrl);
      if (href) vcardLinks.push(href);
    });
  }
  const seenProfiles = new Map<string, { url: string; text: string | null; slug: string | null }>();
  for (const link of profileLinks) seenProfiles.set(link.url, link);
  return {
    profileLinks: [...seenProfiles.values()],
    vcardLinks: dedupeStrings(vcardLinks),
  };
}

export function enrichAvisonYoungContacts(contacts: any[], docs: Array<{ doc: ScrapedDoc; url: string }>): any[] {
  if (!contacts.length || !docs.length) return contacts;
  const { profileLinks, vcardLinks } = extractAvisonYoungContactUrls(docs);
  if (!profileLinks.length && !vcardLinks.length) return contacts;
  return contacts.map((contact) => {
    const nameSlug = avisonYoungNameSlug(clean(contact?.name));
    const profile =
      profileLinks.find((link) => nameSlug && link.slug === nameSlug) ??
      profileLinks.find((link) => nameSlug && link.slug?.includes(nameSlug)) ??
      (contacts.length === 1 && profileLinks.length === 1 ? profileLinks[0] : null);
    const vcardUrl = contacts.length === 1 && vcardLinks.length === 1 ? vcardLinks[0] : null;
    return prune({
      ...contact,
      profileUrl: clean(contact?.profileUrl) ?? profile?.url,
      vcardUrl: clean(contact?.vcardUrl) ?? vcardUrl,
    });
  });
}

export function isAvisonYoungUsCompatible(row: any): boolean {
  const country = clean(row.country)?.toLowerCase();
  if (country) return ["us", "usa", "united states", "united states of america"].includes(country);
  const state = clean(row.state);
  return !!state && /^[A-Z]{2}$/.test(state);
}

export function avisonYoungTransactions(row: any): string[] {
  return (Array.isArray(row.transaction) ? row.transaction : [row.transaction])
    .map((t: any) => clean(String(t ?? ""))?.toLowerCase())
    .filter((t: string | null): t is string => !!t);
}

export function avisonYoungMatchesTx(row: any, tx: Tx): boolean {
  const transactions = avisonYoungTransactions(row);
  if (tx === "sale") return transactions.some((t) => t.includes("sale"));
  return transactions.some((t) => t.includes("lease") || t.includes("sublease"));
}

export function avisonYoungTransactionType(row: any): string {
  const transactions = avisonYoungTransactions(row);
  const hasSale = transactions.some((t) => t.includes("sale"));
  const hasLease = transactions.some((t) => t.includes("lease") || t.includes("sublease"));
  if (hasSale && hasLease) return "Sale/Lease";
  if (transactions.some((t) => t.includes("sublease"))) return "Sublease";
  return hasLease ? "Lease" : "Sale";
}

export function avisonYoungSizeText(row: any): string | null {
  const parts: string[] = [];
  if (num(row.total_surface_sqft)) parts.push(`${row.total_surface_sqft} SF total`);
  if (num(row.availabilities_min_surface_sqft) || num(row.availabilities_max_surface_sqft)) {
    const min = num(row.availabilities_min_surface_sqft);
    const max = num(row.availabilities_max_surface_sqft);
    parts.push(
      min && max && min !== max
        ? `${min} - ${max} SF available`
        : `${min ?? max} SF available`
    );
  }
  return clean(parts.join("; "));
}

export function avisonYoungLeaseRateText(row: any): string | null {
  const min = num(row.availabilities_min_rent);
  const max = num(row.availabilities_max_rent);
  if (!min && !max) return null;
  const value = min && max && min !== max ? `$${min} - $${max}` : `$${min ?? max}`;
  return `${value}/SF/YR`;
}

export function avisonYoungContact(member: any): any | null {
  if (!member) return null;
  const name = clean([member.first_name, member.last_name].map(clean).filter(Boolean).join(" "));
  const avatarUrl =
    member.media_id != null ? sharpLaunchCdnUrl(`media/${String(member.media_id)}`) : null;
  return prune({
    name,
    title: clean(member.title),
    email: clean(member.email),
    phone: clean(member.phone) ?? clean(member.phone_2),
    company: clean(member.company) ?? clean(member.location) ?? "Avison Young",
    avatarUrl,
  });
}

export function avisonYoungBaseListing(row: any, teamMembers: Map<string, any>): any {
  const contactsDetailed = (Array.isArray(row.team_member_ids) ? row.team_member_ids : [])
    .map((id: any) => avisonYoungContact(teamMembers.get(String(id))))
    .filter(Boolean);
  const brokerIds = contactsDetailed
    .map((c: any) =>
      brokerRef({
        name: clean(c.name),
        email: clean(c.email),
        phone: clean(c.phone),
        avatarUrl: clean(c.avatarUrl),
        company: "Avison Young",
      })
    )
    .filter((id: number | null): id is number => id !== null);
  const imageUrl = sharpLaunchCdnUrl(row.image_path);
  const externalUrl = clean(row.external_url);
  const sharpLaunchUrl = clean(row.url);
  const rawTypes = Array.isArray(row.type) ? row.type.map(clean).filter(Boolean) : [];
  return prune({
    id: row.id != null ? String(row.id) : null,
    name: clean(row.name) ?? clean(row.meta_title),
    headline: clean(row.meta_title),
    transactionType: avisonYoungTransactionType(row),
    assetType: rawTypes.length ? rawTypes.join(", ") : null,
    description: stripHtmlText(row.description) ?? clean(row.meta_description),
    street: clean(row.address),
    city: clean(row.city),
    state: clean(row.state),
    postalCode: clean(row.zip),
    country: clean(row.country) ?? "US",
    latitude: num(row.location?.lat),
    longitude: num(row.location?.lng),
    salePriceUsd: num(row.sale_price),
    salePriceText: row.sale_price ? `$${Number(row.sale_price).toLocaleString("en-US")}` : null,
    capRatePct: num(row.cap_rate),
    leaseRateText: avisonYoungLeaseRateText(row),
    sizeText: avisonYoungSizeText(row),
    buildingSizeSqft: num(row.total_surface_sqft),
    yearBuilt: num(row.yearbuilt),
    brokerIds,
    contactsDetailed,
    photos: imageUrl && isAvisonYoungPropertyPhoto(imageUrl) ? [imageUrl] : [],
    url: externalUrl ?? sharpLaunchUrl,
    externalUrl,
    sharpLaunchUrl,
    sourceFeedUrl: `${AVISON_YOUNG_API_BASE}?entity=website&status=active`,
    lastUpdated: clean(row.updated_at)?.slice(0, 10) ?? clean(row.on_market_at)?.slice(0, 10),
    rawSubtypes: rawTypes,
    saleUnitPrice: num(row.sale_unit_price),
    availableMinSqft: num(row.availabilities_min_surface_sqft),
    availableMaxSqft: num(row.availabilities_max_surface_sqft),
    rawSharpLaunch: row,
  });
}

export async function enrichAvisonYoungListing(base: any): Promise<any> {
  const detailUrls = dedupeStrings([clean(base.sharpLaunchUrl), clean(base.externalUrl)]).filter((url) =>
    /^https?:\/\//i.test(url)
  );
  if (!detailUrls.length) return prune({ ...base, detailError: "missing public detail URLs" });

  const docs: Array<{ doc: ScrapedDoc; url: string }> = [];
  const errors: string[] = [];
  for (const url of detailUrls) {
    try {
      docs.push({ url, doc: await scrapeDoc(url, { waitFor: 1000, timeout: 60000 }) });
    } catch (err) {
      errors.push(`${url}: ${String(err)}`);
    }
  }
  if (!docs.length) {
    return prune({ ...base, detailError: errors.join("; ") || "no detail pages scraped" });
  }

  const documents = extractAvisonYoungDocuments(docs);
  const photos = extractAvisonYoungPhotos(docs, Array.isArray(base.photos) ? base.photos : []);
  const listingLd = extractAvisonYoungJsonLd(docs);
  const contactsDetailed = enrichAvisonYoungContacts(
    Array.isArray(base.contactsDetailed) ? base.contactsDetailed : [],
    docs
  );
  const brokerIds = contactsDetailed
    .map((c: any) =>
      brokerRef({
        name: clean(c.name),
        email: clean(c.email),
        phone: clean(c.phone),
        avatarUrl: clean(c.avatarUrl),
        company: "Avison Young",
      })
    )
    .filter((id: number | null): id is number => id !== null);

  return prune({
    ...base,
    name: clean(listingLd?.name) ?? base.name,
    description: clean(listingLd?.description) ?? base.description,
    lastUpdated: clean(listingLd?.datePosted)?.slice(0, 10) ?? base.lastUpdated,
    brokerIds: brokerIds.length ? brokerIds : base.brokerIds,
    contactsDetailed,
    brochures: documents,
    photos: photos.length ? photos : base.photos,
    documentCount: documents.length,
    photoCount: photos.length || base.photos?.length,
    detailJsonLd: listingLd,
    detailScrape: {
      urls: docs.map(({ url }) => url),
      markdownLength: docs.reduce((sum, item) => sum + item.doc.markdown.length, 0),
      rawHtmlLength: docs.reduce((sum, item) => sum + item.doc.rawHtml.length, 0),
      linkCount: docs.reduce((sum, item) => sum + item.doc.links.length, 0),
      documentCount: documents.length,
      photoCount: photos.length,
      profileUrlCount: contactsDetailed.filter((c: any) => clean(c?.profileUrl)).length,
      vcardUrlCount: contactsDetailed.filter((c: any) => clean(c?.vcardUrl)).length,
    },
    detailError: errors.length ? errors.join("; ") : undefined,
  });
}

export async function srcAvisonYoung(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  const sourceUrl = `https://www.avisonyoung.us/properties/#/?transaction=${tx}&view=sidebar&status=active`;
  const { websiteRows, teamMembers } = await getAvisonYoungFeed();
  const rows = websiteRows
    .filter((row) => row?.status === "active")
    .filter(isAvisonYoungUsCompatible)
    .filter((row) => avisonYoungMatchesTx(row, tx))
    .sort((a, b) => Number(a.order_id ?? a.id ?? 0) - Number(b.order_id ?? b.id ?? 0));
  const want = Math.min(max, rows.length);
  const baseListings = rows.slice(0, want).map((row) => avisonYoungBaseListing(row, teamMembers));
  if (monitor) {
    // Monitor mode: emit the SharpLaunch feed base listings only and skip the
    // detail-page enrichment. id/price/cap rate/lastUpdated are all free in the
    // feed; the feed already filters to status=active.
    if (!baseListings.length) throw new Error(`no ${tx} listings found in Avison Young SharpLaunch feed`);
    return {
      company: "Avison Young (US)",
      sourceUrl,
      method: "SharpLaunch public website/team_member API base listings only (monitor mode; detail-page enrichment skipped)",
      totalAvailable: rows.length,
      listings: baseListings,
      note: "Monitor mode: SharpLaunch feed fields only (id, url, price, cap rate, lastUpdated, contacts from the team_member API); per-listing detail-page enrichment skipped.",
    };
  }
  const detailLimit = avisonYoungDetailLimit(max, baseListings.length);
  const enrichedListings = detailLimit
    ? await pmap(baseListings.slice(0, detailLimit), AVISON_YOUNG_DETAIL_CONCURRENCY, async (listing, idx) => {
        const enriched = await enrichAvisonYoungListing(listing);
        if ((idx + 1) % 10 === 0 || idx + 1 === detailLimit) {
          console.error(`  avison-young/${tx}: detail enriched ${idx + 1}/${detailLimit}`);
        }
        return enriched;
      })
    : [];
  const listings = [...enrichedListings, ...baseListings.slice(detailLimit)];
  if (!listings.length) throw new Error(`no ${tx} listings found in Avison Young SharpLaunch feed`);
  return {
    company: "Avison Young (US)",
    sourceUrl,
    method:
      "SharpLaunch public website/team_member API with bounded public detail-page enrichment for selected rows",
    totalAvailable: rows.length,
    listings,
    note:
      detailLimit > 0
        ? `Detail enrichment fetched public SharpLaunch/Avison pages for ${detailLimit} selected row(s); documents, images, profile URLs, VCard URLs, and JSON-LD are stored as URLs/raw public metadata only.`
        : "Full-feed run preserved as SharpLaunch-only by default. Set AVISON_YOUNG_DETAIL_LIMIT to enrich a bounded number of selected rows.",
  };
}
