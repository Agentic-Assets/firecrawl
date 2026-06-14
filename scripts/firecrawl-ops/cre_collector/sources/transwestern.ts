// sources/transwestern.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { dedupeStrings, titleFromFilename } from "../lib/html.js";
import { scrapeDoc, scrapeJson } from "../lib/scrape.js";
import { ScrapedDoc, SourceResult, Tx } from "../types.js";
import { clean, num, pmap, prune } from "../lib/util.js";


// --- Transwestern: public properties GET feed plus detail enrichment ---

export const TRANSWESTERN_HOST = "https://transwestern.com";
export const TRANSWESTERN_BUCKETS: Record<Tx, string[]> = {
  sale: ["Sale", "Sale or Lease"],
  lease: ["Lease", "Sublease", "Sale or Lease"],
};

export function canonicalTranswesternUrl(href: string | null): string | null {
  const h = clean(href);
  if (!h || /^javascript:/i.test(h) || h === "-") return null;
  try {
    return new URL(h, TRANSWESTERN_HOST).toString();
  } catch {
    return null;
  }
}

export function transwesternFeedUrl(bucket: string): string {
  const params = new URLSearchParams({
    call: "ajax",
    search: "",
    Latitude: "",
    Longitude: "",
    DealsType: bucket,
    PropertyType: "0",
    MetroName: "",
    SubTypeIDs: "",
    TenancyTypes: "",
    CheckLeed: "false",
    IsEnergyStar: "false",
    MinPrice: "",
    MaxPrice: "",
    MinSize: "",
    MaxSize: "",
    SortType: "asc",
    SortColumn: "",
    class: "",
    TotalLotSizeMin: "",
    TotalLotSizeMax: "",
    NoOfUnitsMin: "",
    NoOfUnitsMax: "",
  });
  return `${TRANSWESTERN_HOST}/properties?${params.toString()}`;
}

export function transwesternDetailUrl(pageUrl: any): string | null {
  const slug = clean(String(pageUrl ?? ""));
  if (!slug || slug === "-") return null;
  return `${TRANSWESTERN_HOST}/property/${encodeURIComponent(slug).replace(/%2F/g, "/")}`;
}

export function transwesternTransactionType(bucket: string): string {
  if (/sale or lease/i.test(bucket)) return "Sale/Lease";
  if (/sublease/i.test(bucket)) return "Sublease";
  if (/lease/i.test(bucket)) return "Lease";
  return "Sale";
}

export function transwesternSizeText(row: any): string | null {
  const size = num(Number(row.PropertySize));
  return size ? `${size.toLocaleString("en-US")} SF` : null;
}

export function transwesternPriceText(row: any, tx: Tx): string | null {
  const price = num(Number(row.Price));
  if (!price) return tx === "sale" ? "Contact broker for pricing" : null;
  return `$${price.toLocaleString("en-US")}`;
}

export function parseTranswesternFacts($: cheerio.CheerioAPI): Record<string, string> {
  const facts: Record<string, string> = {};
  $("li, .property-detail li, .property-facts li").each((_, el) => {
    const label = clean($(el).find("b,strong").first().text()?.replace(/:$/, ""));
    if (!label) return;
    const value = clean($(el).text().replace($(el).find("b,strong").first().text(), ""));
    if (value) facts[label] = value.replace(/^:\s*/, "");
  });
  return facts;
}

export function parseTranswesternAvailability($: cheerio.CheerioAPI): any[] {
  const rows: any[] = [];
  $("#tblAvailability tr").each((_, tr) => {
    const cells = $(tr)
      .find("th,td")
      .map((__, td) => clean($(td).text()))
      .get()
      .filter(Boolean);
    if (cells.length < 2 || /suite/i.test(cells.join(" ")) && $(tr).find("th").length) return;
    rows.push({
      suite: cells[0] ?? null,
      size: cells[1] ?? null,
      rate: cells[2] ?? null,
      type: cells[3] ?? null,
      raw: cells,
    });
  });
  return rows;
}

export function extractTranswesternContacts(doc: ScrapedDoc): any[] {
  const $ = cheerio.load(doc.rawHtml);
  const contactsByKey = new Map<string, any>();
  $(".PropertyVcard .v-card, .v-card").each((_, el) => {
    const card = $(el);
    const profileUrl = canonicalTranswesternUrl(
      card.find('a[href^="/"]:not([href*="vcard-generator"])').first().attr("href") ?? null
    );
    const vcardUrl = canonicalTranswesternUrl(
      card.find('a[href*="vcard-generator"]').first().attr("href") ?? null
    );
    const avatarUrl = canonicalTranswesternUrl(card.find("img").first().attr("src") ?? null);
    const phone =
      clean(card.find('a[href^="tel:"]').first().text()) ??
      clean(card.text().match(/(\+?1?\s*\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4})/)?.[1] ?? null);
    const email = clean(card.find('a[href^="mailto:"]').first().attr("href")?.replace(/^mailto:/i, ""));
    const linkText = clean(
      card.find('a[href^="/"]:not([href*="vcard-generator"])').first().text()
    );
    const name =
      clean(card.find(".name, .broker-name, h3, h4").first().text()) ??
      linkText ??
      clean(card.find("strong").first().text());
    const title =
      clean(card.find(".title, .job-title").first().text()) ??
      clean(
        card
          .text()
          .split("\n")
          .map((s) => s.trim())
          .find((s) => /associate|director|broker|principal|vice president|managing/i.test(s)) ??
          null
      );
    const key = profileUrl ?? vcardUrl ?? email ?? name;
    if (!key) return;
    contactsByKey.set(key, {
      name,
      title,
      email,
      phone,
      company: "Transwestern",
      profileUrl,
      avatarUrl,
      vcardUrl,
    });
  });
  return [...contactsByKey.values()].filter(
    (c) => c.name || c.email || c.phone || c.profileUrl || c.avatarUrl || c.vcardUrl
  );
}

export function extractTranswesternDocuments(doc: ScrapedDoc): any[] {
  const $ = cheerio.load(doc.rawHtml);
  const candidates: string[] = [];
  $('#tblAttachments a[href], a.download-att-btn[href], a.download-flyer-btn[href], a[href$=".pdf"], a[href*=".pdf"], a[href*="twurls.com"]').each(
    (_, el) => {
      const u = canonicalTranswesternUrl($(el).attr("href") ?? null);
      if (u) candidates.push(u);
    }
  );
  for (const link of doc.links ?? []) {
    if (/\.pdf(?:\?|$)|twurls\.com/i.test(link)) {
      const u = canonicalTranswesternUrl(link);
      if (u) candidates.push(u);
    }
  }
  return dedupeStrings(candidates)
    .filter((url) => !/\/Upload\/TREC\/|\/privacy-policy(?:\?|$)|health1\.aetna\.com/i.test(url))
    .map((url) => ({ name: titleFromFilename(url), url }));
}

export function extractTranswesternPhotos(doc: ScrapedDoc, feedImage: string | null): string[] {
  const $ = cheerio.load(doc.rawHtml);
  const candidates: Array<string | null> = [feedImage];
  $('.photos-list a.chocolat-image[href], a.chocolat-image[href], a[href*="/images/"], img[src*="/images/"]').each(
    (_, el) => {
      candidates.push(canonicalTranswesternUrl($(el).attr("href") ?? $(el).attr("src") ?? null));
    }
  );
  return dedupeStrings(candidates).filter(
    (url) =>
      !/\.pdf(?:\?|$)/i.test(url) &&
      !/\/assets\/images\/(?:mail|comment|connect-image|tw-logo|Transwestern_2023|tw_gl|transwestern-mapmarker)/i.test(url)
  );
}

export function transwesternDescription($: cheerio.CheerioAPI, doc: ScrapedDoc): string | null {
  const candidate =
    clean($(".property-description, .PropertyDescription, #overview").first().text()) ??
    clean(doc.markdown.match(/Overview\s*([\s\S]{1,1800}?)(?:\n[A-Z][A-Za-z ]+\n|\n#{1,6}\s|$)/i)?.[1]);
  if (
    !candidate ||
    /TREC Information About Brokerage Services|Privacy Policy|Copyright\s+Transwestern|Sitemap|Working-at-Transwestern/i.test(
      candidate
    )
  ) {
    return null;
  }
  return candidate;
}

export async function enrichTranswesternListing(row: any, bucket: string, tx: Tx, monitor: boolean): Promise<any> {
  const detailUrl = transwesternDetailUrl(row.PageUrl);
  const feedImage = canonicalTranswesternUrl(clean(row.PropertyImage));
  const base = {
    id: clean(String(row.PageUrl ?? "")),
    name: clean(row.BuildingName),
    transactionType: transwesternTransactionType(bucket),
    assetType: clean(row.PropertyTypeName),
    street: clean(row.FullAddress),
    city: clean(row.City),
    state: clean(row.State)?.toUpperCase() ?? null,
    postalCode: clean(row.ZipCode),
    country: "US",
    latitude: row.Latitude != null ? Number(row.Latitude) : null,
    longitude: row.Longitude != null ? Number(row.Longitude) : null,
    salePriceUsd: tx === "sale" ? num(Number(row.Price)) : null,
    salePriceText: tx === "sale" ? transwesternPriceText(row, tx) : null,
    sizeText: transwesternSizeText(row),
    buildingSizeSqft: num(Number(row.PropertySize)),
    brokerIds: [],
    photos: feedImage ? [feedImage] : [],
    url: detailUrl,
    rawTranswesternFeed: row,
    transwesternBucket: bucket,
  };
  // Monitor mode: emit the freely-available feed fields only (id/url/price/size)
  // and skip the detail scrape. Status has no feed field, so it stays absent
  // (do not render just to recover status), exactly per the design 14.1 intent.
  if (monitor) return prune(base);
  if (!detailUrl) return prune({ ...base, detailError: "missing or invalid PageUrl" });
  try {
    const doc = await scrapeDoc(detailUrl, { waitFor: 1500, timeout: 60000 });
    const $ = cheerio.load(doc.rawHtml);
    const facts = parseTranswesternFacts($);
    const availability = parseTranswesternAvailability($);
    const contactsDetailed = extractTranswesternContacts(doc);
    const brokerIds = contactsDetailed
      .map((c) =>
        brokerRef({
          name: clean(c.name),
          email: clean(c.email),
          phone: clean(c.phone),
          office: clean(c.office),
          avatarUrl: clean(c.avatarUrl),
          company: "Transwestern",
        })
      )
      .filter((id: number | null): id is number => id !== null);
    const coordMatch = doc.rawHtml.match(/myLatLng\s*=\s*\{\s*lat:\s*(-?[0-9.]+),\s*lng:\s*(-?[0-9.]+)/i);
    const description = transwesternDescription($, doc);
    const leaseRateText =
      availability.map((a) => clean(a.rate)).find((rate) => rate && /\$|psf|sf|negotiable/i.test(rate)) ??
      null;
    return prune({
      ...base,
      name: clean($("h1").first().text()) ?? base.name,
      description,
      latitude: base.latitude ?? (coordMatch ? Number(coordMatch[1]) : null),
      longitude: base.longitude ?? (coordMatch ? Number(coordMatch[2]) : null),
      leaseRateText: tx === "lease" ? leaseRateText : null,
      brokerIds,
      contactsDetailed,
      brochures: extractTranswesternDocuments(doc),
      photos: extractTranswesternPhotos(doc, feedImage),
      transwesternFacts: facts,
      availability,
      detailScrape: {
        url: detailUrl,
        markdownLength: doc.markdown.length,
        rawHtmlLength: doc.rawHtml.length,
        linkCount: doc.links.length,
      },
    });
  } catch (err) {
    console.error(`  transwestern/${tx}: detail failed for ${detailUrl}: ${err}`);
    return prune({
      ...base,
      detailError: String(err),
    });
  }
}

export async function srcTranswestern(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  const buckets = TRANSWESTERN_BUCKETS[tx];
  const rowsBySlug = new Map<string, { row: any; bucket: string }>();
  const bucketCounts: Record<string, number> = {};
  for (const bucket of buckets) {
    const data = await scrapeJson(transwesternFeedUrl(bucket), { timeout: 60000 });
    const rows = Array.isArray(data) ? data : [];
    bucketCounts[bucket] = rows.length;
    console.error(`  transwestern/${tx}/${bucket}: ${rows.length} feed rows`);
    for (const row of rows) {
      const slug = clean(String(row.PageUrl ?? ""));
      if (!slug || slug === "-") continue;
      if (!rowsBySlug.has(slug)) rowsBySlug.set(slug, { row, bucket });
    }
  }
  const selected = [...rowsBySlug.values()].slice(0, Math.min(max, Number.MAX_SAFE_INTEGER));
  let done = 0;
  const listings = await pmap(selected, CONCURRENCY, async ({ row, bucket }) => {
    const listing = await enrichTranswesternListing(row, bucket, tx, monitor);
    done++;
    if (done % 25 === 0 || done === selected.length) {
      console.error(`  transwestern/${tx}: detail enriched ${done}/${selected.length}`);
    }
    return listing;
  });
  const total = [...new Set([...rowsBySlug.keys()])].length;
  return {
    company: "Transwestern",
    sourceUrl: "https://transwestern.com/properties",
    method: "Public /properties?call=ajax GET feed by DealsType plus detail-page raw HTML enrichment",
    totalAvailable: total,
    listings,
    note: `Bucket counts before slug de-dupe: ${Object.entries(bucketCounts)
      .map(([bucket, count]) => `${bucket}=${count}`)
      .join(", ")}. Rows with invalid PageUrl are skipped.`,
  };
}
