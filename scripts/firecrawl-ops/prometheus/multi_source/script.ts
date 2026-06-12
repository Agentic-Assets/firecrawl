import Firecrawl from "@mendable/firecrawl-js";
import * as cheerio from "cheerio";
import { parseArgs } from "node:util";

const apiKey = process.env.FIRECRAWL_API_KEY;
if (!apiKey) {
  console.error("FIRECRAWL_API_KEY is not set");
  process.exit(1);
}
const firecrawl = new Firecrawl({ apiKey });

// ---------- CLI ----------

const SOURCE_KEYS = [
  "cbre",
  "cbre-dealflow",
  "jll",
  "jll-investor",
  "cushman-wakefield",
  "colliers",
  "newmark",
  "marcus-millichap",
  "avison-young",
  "savills",
  "svn",
  "nai-global",
  "lee-associates",
  "transwestern",
] as const;

const { values: flags } = parseArgs({
  strict: true,
  options: {
    source: { type: "string" }, // --source=jll | all (default all)
    "max-items": { type: "string" }, // per-source cap, default 25
  },
});
const sourceArg = (flags.source ?? "all").toLowerCase();
if (sourceArg !== "all" && !SOURCE_KEYS.includes(sourceArg as any)) {
  console.error(
    `OUT_OF_SCOPE: unknown source '${sourceArg}'. Valid: all, ${SOURCE_KEYS.join(", ")}`
  );
  process.exit(1);
}
const MAX_ITEMS = Math.max(1, Math.min(500, Number(flags["max-items"] ?? "25")));

// ---------- shared helpers ----------

function clean(s: any): string | null {
  if (typeof s !== "string") return null;
  const t = s.replace(/\s+/g, " ").trim();
  return t || null;
}

function num(v: any): number | null {
  return typeof v === "number" && isFinite(v) && v !== 0 ? v : null;
}

function moneyToNumber(t: string | null): number | null {
  if (!t) return null;
  const m = t.replace(/,/g, "").match(/\$\s*([0-9]+(?:\.[0-9]+)?)/);
  return m ? Number(m[1]) : null;
}

// Strip uninformative values: null/undefined, empty strings/arrays/objects, false flags.
function prune(v: any): any {
  if (v === null || v === undefined || v === false || v === "") return undefined;
  if (Array.isArray(v)) {
    const arr = v.map(prune).filter((x) => x !== undefined);
    return arr.length ? arr : undefined;
  }
  if (typeof v === "object") {
    const out: Record<string, any> = {};
    for (const [k, val] of Object.entries(v)) {
      const p = prune(val);
      if (p !== undefined) out[k] = p;
    }
    return Object.keys(out).length ? out : undefined;
  }
  return v;
}

async function scrapeRaw(url: string, waitFor = 0): Promise<string> {
  let lastErr: unknown = null;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const doc = await firecrawl.scrape(url, {
        formats: ["rawHtml"],
        ...(waitFor ? { waitFor } : {}),
        integration: "prometheus",
      });
      const body = (doc as any).rawHtml ?? "";
      if (!body) throw new Error("empty response body");
      return body;
    } catch (err) {
      lastErr = err;
      console.error(`scrape attempt ${attempt} failed for ${url}: ${err}`);
      await new Promise((r) => setTimeout(r, 2500 * attempt));
    }
  }
  throw lastErr;
}

async function scrapeJson(url: string): Promise<any> {
  const body = await scrapeRaw(url);
  try {
    return JSON.parse(body);
  } catch {
    const start = body.indexOf("{");
    const end = body.lastIndexOf("}");
    if (start === -1 || end === -1) {
      throw new Error(`response from ${url} contained no JSON object`);
    }
    return JSON.parse(body.slice(start, end + 1));
  }
}

// ---------- broker dedupe table (shared across sources) ----------

const brokerIndex = new Map<string, number>();
const brokers: any[] = [];

function brokerRef(b: {
  name: string | null;
  email?: string | null;
  phone?: string | null;
  office?: string | null;
  avatarUrl?: string | null;
  company: string;
}): number | null {
  if (!b.name && !b.email) return null;
  const key = (b.email ?? "") + "|" + (b.name ?? "") + "|" + b.company;
  const existing = brokerIndex.get(key);
  if (existing !== undefined) {
    const rec = brokers[existing];
    if (!rec.phone && b.phone) rec.phone = b.phone;
    if (!rec.office && b.office) rec.office = b.office;
    if (!rec.avatarUrl && b.avatarUrl) rec.avatarUrl = b.avatarUrl;
    return existing;
  }
  const idx = brokers.length;
  brokers.push({
    name: b.name ?? null,
    email: b.email ?? null,
    phone: b.phone ?? null,
    office: b.office ?? null,
    avatarUrl: b.avatarUrl ?? null,
    company: b.company,
  });
  brokerIndex.set(key, idx);
  return idx;
}

// ---------- source adapters ----------
// Every adapter returns { company, sourceUrl, method, totalAvailable, listings, note? }
// and every listing uses the same field vocabulary (prune() drops what a source lacks):
// id, name, headline, transactionType, assetType, description, street, city, state,
// postalCode, country, latitude, longitude, salePriceUsd, salePriceText, capRatePct,
// sizeText, buildingSizeSqft, lotSizeAcres, brokerIds, brochures, photos, url, lastUpdated

type SourceResult = {
  company: string;
  sourceUrl: string;
  method: string;
  totalAvailable: number | null;
  listings: any[];
  note?: string;
};

async function srcCbre(max: number): Promise<SourceResult> {
  const sourceUrl =
    "https://www.cbre.com/properties/properties-for-lease/commercial-space?aspects=isSale";
  const pageSize = Math.min(max, 200);
  const data = await scrapeJson(
    `https://www.cbre.com/listings-api/propertylistings/query?site=us-comm&Common.Aspects=isSale&PageSize=${pageSize}&Page=1`
  );
  if (typeof data.DocumentCount !== "number" || !Array.isArray(data.Documents)) {
    throw new Error("CBRE listings API response is missing DocumentCount/Documents fields");
  }
  const docs = data.Documents.flat().slice(0, max);
  const text = (loc: any) =>
    Array.isArray(loc) && loc.length ? clean(loc[0]["Common.Text"]) : null;
  const listings = docs.map((d: any) => {
    const addr = d["Common.ActualAddress"] ?? {};
    const charges: any[] = Array.isArray(d["Common.Charges"]) ? d["Common.Charges"] : [];
    const sale = charges.find(
      (c: any) => c["Common.ChargeKind"] === "SalePrice" && num(c["Common.Amount"])
    );
    const coord = d["Common.Coordinate"] ?? {};
    const aspects: string[] = Array.isArray(d["Common.Aspects"]) ? d["Common.Aspects"] : [];
    const name = clean(addr["Common.Line1"]);
    const street = clean(addr["Common.Line2"]);
    const city = clean(addr["Common.Locallity"]);
    const state = clean(addr["Common.Region"]);
    const zip = clean(addr["Common.PostCode"]);
    const slug = [name, street, city, state, zip]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "");
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
    return {
      id: d["Common.PrimaryKey"],
      name,
      headline: text(d["Common.Strapline"]),
      transactionType: aspects.includes("isLetting") ? "Sale/Lease" : "Sale",
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
      salePriceText: sale ? null : "Contact broker for pricing",
      buildingSizeSqft: num(d["Dynamic.TotalArea"]),
      brokerIds,
      brochures: (Array.isArray(d["Common.Brochures"]) ? d["Common.Brochures"] : []).map(
        (b: any) => ({
          name: clean(b["Common.BrochureName"]),
          url: clean(b["Common.Uri"])?.startsWith("http")
            ? clean(b["Common.Uri"])
            : `https://www.cbre.com${clean(b["Common.Uri"]) ?? ""}`,
        })
      ),
      photos: (Array.isArray(d["Common.Photos"]) ? d["Common.Photos"] : [])
        .map((p: any) => {
          const r = (p["Common.ImageResources"] ?? []).find(
            (x: any) => x["Common.Breakpoint"] === "original"
          ) ?? (p["Common.ImageResources"] ?? [])[0];
          const u = r && clean(r["Common.Resource.Uri"]);
          return u ? (u.startsWith("http") ? u : `https://www.cbre.com${u}`) : null;
        })
        .filter(Boolean),
      url: `https://www.cbre.com/properties/properties-for-lease/commercial-space/details/${d["Common.PrimaryKey"]}/${slug}`,
      lastUpdated: clean(d["Common.LastUpdated"])?.slice(0, 10) ?? null,
    };
  });
  return {
    company: "CBRE",
    sourceUrl,
    method: "CBRE public listings API (JSON)",
    totalAvailable: data.DocumentCount,
    listings,
  };
}

async function srcBuildout(
  company: string,
  pluginKey: string,
  listingsPage: string,
  max: number
): Promise<SourceResult> {
  const listings: any[] = [];
  let total: number | null = null;
  for (let page = 0; listings.length < max && page < 20; page++) {
    const data = await scrapeJson(
      `https://buildout.com/plugins/${pluginKey}/inventory.json?page=${page}&sale=true`
    );
    const inv: any[] = data.inventory ?? [];
    total = data.meta?.total ?? total;
    if (!inv.length) break;
    for (const x of inv) {
      if (listings.length >= max) break;
      if (x.sale !== true) continue;
      const attrs = new Map<string, string>(
        (x.index_attributes ?? []).map((p: any) => [String(p[0]), String(p[1])])
      );
      const priceText = attrs.get("Price") ?? null;
      const sizeText =
        attrs.get("Building Size") ?? attrs.get("Lot Size") ?? clean(x.size_summary);
      const brokerIds = (x.broker_contacts ?? [])
        .map((b: any) =>
          brokerRef({
            name: clean(b.name),
            email: clean(b.email),
            phone: clean(b.phone),
            avatarUrl: clean(b.photo_url),
            company,
          })
        )
        .filter((v: number | null): v is number => v !== null);
      listings.push({
        id: x.id != null ? String(x.id) : null,
        name: clean(x.display_name) ?? clean(x.name),
        transactionType: x.also_for_sale_or_lease ? "Sale/Lease" : "Sale",
        assetType: clean(x.property_sub_type_name) ?? attrs.get("Property Type") ?? null,
        street: clean(x.address),
        city: clean(x.city),
        state: clean(x.state),
        postalCode: clean(x.zip),
        country: "US",
        latitude: num(x.latitude),
        longitude: num(x.longitude),
        salePriceUsd: moneyToNumber(priceText),
        salePriceText: priceText,
        sizeText,
        brokerIds,
        brochures: x.pdf_url ? [{ name: "Listing brochure (PDF)", url: x.pdf_url }] : [],
        photos: [x.photo_url, x.large_thumbnail_url].filter(Boolean).slice(0, 1),
        url: clean(x.show_link),
        underContract: x.under_contract === true,
      });
    }
  }
  return {
    company,
    sourceUrl: listingsPage,
    method: "Buildout plugin inventory API (JSON)",
    totalAvailable: total,
    listings,
  };
}

async function srcNewmark(max: number): Promise<SourceResult> {
  const sourceUrl = "https://www.nmrk.com/properties";
  const html = await scrapeRaw(sourceUrl);
  const appId = html.match(/algoliaAppId='([^']+)'/)?.[1];
  const searchKey = html.match(/algoliaSearchApiKey='([^']+)'/)?.[1];
  const indexName = html.match(/algoliaIndexName='([^']+)'/)?.[1] ?? "prod_entries";
  if (!appId || !searchKey) {
    throw new Error("could not extract Algolia credentials from nmrk.com/properties");
  }
  const facetFilters = encodeURIComponent(
    JSON.stringify(["sectionGroup:Properties", "saleOrLease:Sale", "country_code:US", "siteHandle:enUs"])
  );
  const data = await scrapeJson(
    `https://${appId}-dsn.algolia.net/1/indexes/${indexName}?x-algolia-application-id=${appId}&x-algolia-api-key=${searchKey}&query=&hitsPerPage=${Math.min(max, 1000)}&facetFilters=${facetFilters}`
  );
  if (!Array.isArray(data.hits)) throw new Error("Newmark Algolia response has no hits array");
  const listings = data.hits.slice(0, max).map((h: any) => ({
    id: clean(h.slug),
    name: clean(h.title),
    headline: clean(h.content),
    transactionType: "Sale",
    assetType: Array.isArray(h.property_types) ? h.property_types.join(", ") : clean(h.property_type),
    street: clean(h.address),
    city: clean(h.city),
    state: clean(h.state),
    postalCode: clean(h.zip),
    county: clean(h.county),
    submarket: clean(h.submarket),
    country: clean(h.country_code) ?? "US",
    latitude: num(h.latitude),
    longitude: num(h.longitude),
    salePriceUsd: num(h.sale_price),
    salePriceText: h.sale_price ? null : "Contact broker for pricing",
    buildingSizeSqft: num(h.building_size_sf),
    lotSizeAcres: num(h.lot_size_acres),
    brokerIds: [],
    photos: (h.thumbnails ?? []).slice(-1).map((t: any) => t.url),
    url: h.url ? `https://www.nmrk.com${h.url}` : null,
    lastUpdated: clean(h.updateDate)?.slice(0, 10) ?? null,
  }));
  return {
    company: "Newmark",
    sourceUrl,
    method: "Newmark Algolia search API (JSON; credentials read from the page)",
    totalAvailable: data.nbHits ?? null,
    listings,
  };
}

async function srcJll(max: number): Promise<SourceResult> {
  const sourceUrl = "https://property.jll.com/search?tenureTypes=sale";
  const listings: any[] = [];
  let total: number | null = null;
  for (let page = 1; listings.length < max && page <= 5; page++) {
    const html = await scrapeRaw(`${sourceUrl}&page=${page}`, 8000);
    const $ = cheerio.load(html);
    total =
      total ??
      (Number((($("h2").text() || html).match(/([0-9][0-9,]*)\s+properties/i) ?? [])[1]?.replace(/,/g, "")) ||
        null);
    const seenHere = new Set<string>();
    $('a.text-base[href*="/listings/"]').each((_, el) => {
      if (listings.length >= max) return;
      const href = $(el).attr("href")!;
      if (seenHere.has(href)) return;
      seenHere.add(href);
      // collect leaf text nodes in document order (cheerio .text() concatenates without separators)
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
      const addr = lines.find((l) => /,\s*[A-Z]{2}[, ]/.test(l) || /,\s*[A-Z]{2}$/.test(l.replace(/,?\s*\d{5}$/, ""))) ?? lines[1] ?? null;
      const m = (addr ?? "").match(/^(.*?),\s*([A-Z]{2}),?\s*(\d{5})?/);
      listings.push({
        id: href.split("/listings/")[1] ?? null,
        name: lines[0] ?? null,
        transactionType: "Sale",
        city: m ? clean(m[1]) : null,
        state: m ? m[2] : null,
        postalCode: m?.[3] ?? null,
        country: "US",
        salePriceUsd: moneyToNumber(priceText),
        salePriceText: priceText,
        sizeText,
        brokerIds: [],
        url: href.startsWith("http") ? href : `https://property.jll.com${href}`,
      });
    });
    if (seenHere.size === 0) break;
  }
  if (!listings.length) throw new Error("no listing cards found on JLL search page");
  return {
    company: "JLL",
    sourceUrl,
    method: "Rendered search pages parsed (cards)",
    totalAvailable: total,
    listings,
  };
}

async function srcJllInvestor(max: number): Promise<SourceResult> {
  const sourceUrl =
    "https://invest.jll.com/us/en/property-search?filter=%7B%22location%22%3A%5B%22United%20States%22%5D%7D";
  const html = await scrapeRaw(sourceUrl, 8000);
  const $ = cheerio.load(html);
  const total =
    Number((html.match(/([0-9][0-9,]*)\s+results/i) ?? [])[1]?.replace(/,/g, "")) || null;
  const seen = new Set<string>();
  const listings: any[] = [];
  $('a[href*="/us/en/listings/"]').each((_, el) => {
    if (listings.length >= max) return;
    const href = $(el).attr("href")!;
    const abs = href.startsWith("http") ? href : `https://invest.jll.com${href}`;
    if (seen.has(abs)) return;
    seen.add(abs);
    const card = $(el).closest("li,article,div[class]");
    const txt = clean(card.text()) ?? "";
    const img = card.find("img").attr("src") ?? null;
    const slugParts = abs.split("/listings/")[1]?.split("/") ?? [];
    listings.push({
      id: slugParts.slice(-1)[0] ?? null,
      name: clean(card.find("h3,h4").first().text()) ?? clean(slugParts.slice(-1)[0]?.replace(/-/g, " ")) ?? null,
      transactionType: "Sale (investment)",
      assetType: clean(slugParts.length > 1 ? slugParts[0]?.replace(/-/g, " ") : null),
      status: /under contract/i.test(txt) ? "Under Contract" : /closed/i.test(txt) ? "Closed" : "Active",
      brokerIds: [],
      photos: img ? [img] : [],
      url: abs,
    });
  });
  if (!listings.length) throw new Error("no listing cards found on JLL Investor Center search page");
  return {
    company: "JLL Investor Center",
    sourceUrl,
    method: "Rendered search page parsed (cards)",
    totalAvailable: total,
    listings,
  };
}

async function srcCushman(max: number): Promise<SourceResult> {
  const sourceUrl = "https://www.cushmanwakefield.com/en/united-states/properties/invest/search";
  const html = await scrapeRaw(sourceUrl, 10000);
  const $ = cheerio.load(html);
  const seen = new Set<string>();
  const listings: any[] = [];
  $('a[href*="/for-sale/"], a[href*="/properties/sale/"], a[href*="/properties/for-sale/"]').each(
    (_, el) => {
      if (listings.length >= max) return;
      const href = $(el).attr("href")!;
      if (!/\/(for-sale|sale)\//.test(href) || /\/search/.test(href)) return;
      const abs = href.startsWith("http") ? href : `https://www.cushmanwakefield.com${href}`;
      if (seen.has(abs)) return;
      seen.add(abs);
      const card = $(el).closest(".cw-search-card").length
        ? $(el).closest(".cw-search-card")
        : $(el);
      const name = clean(card.find(".cw-search-card__title").first().text());
      const address = clean(card.find(".cw-search-card__address").first().text());
      const priceText = clean(card.find(".cw-search-card__price").first().text())?.replace(/^Sale Price:\s*/i, "") ?? null;
      const img = card.find("img").attr("src") ?? null;
      const parts = abs.split("/").filter(Boolean);
      const di = parts.indexOf("for-sale");
      const zipM = (address ?? "").match(/\b(\d{5})\b/);
      listings.push({
        id: parts.slice(-1)[0] ?? null,
        name: name ?? clean(parts.slice(-2)[0]?.replace(/-/g, " ")),
        transactionType: "Sale",
        assetType: di > 0 && parts[di + 1] ? clean(parts[di + 1].replace(/-/g, " ")) : null,
        street: address,
        city: clean((parts[di + 3] ?? "").replace(/-/g, " ")),
        state: (parts[di + 2] ?? "").toUpperCase().slice(0, 2) || null,
        postalCode: zipM ? zipM[1] : null,
        country: "US",
        salePriceUsd: moneyToNumber(priceText),
        salePriceText: priceText,
        brokerIds: [],
        photos: img ? [img] : [],
        url: abs,
      });
    }
  );
  if (!listings.length) {
    throw new Error("no for-sale listing links found on Cushman & Wakefield US sale search page");
  }
  return {
    company: "Cushman & Wakefield",
    sourceUrl,
    method: "Rendered sale-search page parsed (cards); note: C&W's full result API is POST-only",
    totalAvailable: null,
    listings,
    note: "Coverage limited to listings rendered on the first page of the US sale search.",
  };
}

async function srcMarcusMillichap(max: number): Promise<SourceResult> {
  const sourceUrl = "https://www.marcusmillichap.com/properties";
  const html = await scrapeRaw(sourceUrl, 9000);
  const $ = cheerio.load(html);
  const seen = new Set<string>();
  const listings: any[] = [];
  $('a[href*="marcusmillichap.com/properties/"], a[href^="/properties/"]').each((_, el) => {
    if (listings.length >= max) return;
    const href = $(el).attr("href")!;
    if (!/properties\/\d+/.test(href)) return;
    const abs = href.startsWith("http") ? href : `https://www.marcusmillichap.com${href}`;
    if (seen.has(abs)) return;
    seen.add(abs);
    let card = $(el);
    if (!card.find("h2").length) {
      const parent = card.parents().filter((__, p) => $(p).find("h2").length > 0).first();
      if (parent.length) card = parent;
    }
    const heading = clean(card.find("h2").first().text()); // "Walgreens | Kendallville, IN"
    const nameOnly = heading?.split("|")[0]?.trim() ?? null;
    const location = clean(card.find(".mm-location").first().text());
    const m = (location ?? "").match(/^(.*?),\s*([A-Z]{2})$/);
    const priceText =
      clean(card.find(".mm-listing-price").first().text())?.replace(/^Listing Price:\s*/i, "") ?? null;
    const capRate = (clean(card.find(".mm-cap-rate").first().text()) ?? "").match(/([0-9.]+)%/)?.[1];
    const sizeLine = clean(card.find(".mm-size").first().text());
    const idMatch = abs.match(/properties\/(\d+)/);
    listings.push({
      id: idMatch ? idMatch[1] : null,
      name: nameOnly,
      transactionType: "Sale",
      assetType: clean(card.find("h3").first().text()),
      city: m ? clean(m[1]) : location,
      state: m ? m[2] : null,
      country: "US",
      salePriceUsd: moneyToNumber(priceText),
      salePriceText: priceText,
      capRatePct: capRate ? Number(capRate) : null,
      sizeText: sizeLine,
      brokerIds: [],
      photos: [card.find("img").attr("src")].filter(Boolean),
      url: abs,
    });
  });
  if (!listings.length) throw new Error("no listing cards found on Marcus & Millichap properties page");
  return {
    company: "Marcus & Millichap",
    sourceUrl,
    method: "Rendered properties page parsed (cards); full result API is POST-only",
    totalAvailable: null,
    listings,
    note: "Coverage limited to listings rendered on the first page (sorted by default).",
  };
}

async function srcAvisonYoung(max: number): Promise<SourceResult> {
  const sourceUrl = "https://www.avisonyoung.us/properties/#/?transaction=sale&view=sidebar&status=active";
  const html = await scrapeRaw(sourceUrl, 14000);
  const $ = cheerio.load(html);
  const listings: any[] = [];
  $('a[id^="sidebar_item_"]').each((_, el) => {
    if (listings.length >= max) return;
    const href = $(el).attr("href");
    const card = $(el);
    const badge = clean(card.find('[class*="figure__badge"]').text())?.toLowerCase() ?? "";
    if (badge && !badge.includes("sale")) return;
    const name = clean(card.find('[class*="item__heading"]').first().text());
    const below = clean(card.find('[class*="heading_below"]').first().text());
    const pairs: Record<string, string> = {};
    card.find('[class*="details__item"]').each((_, d) => {
      const label = clean($(d).find('[class*="item_label"]').text());
      const value = clean($(d).find('[class*="item_value"]').text());
      if (label && value) pairs[label] = value;
    });
    const m = (below ?? "").match(/^(.*?),\s*([A-Z]{2})$/);
    const sizeLabel = Object.keys(pairs).find((k) => /square feet|acre/i.test(k));
    listings.push({
      id: $(el).attr("id")?.replace("sidebar_item_", "") ?? null,
      name,
      transactionType: badge.includes("lease") ? "Sale/Lease" : "Sale",
      city: m ? clean(m[1]) : below,
      state: m ? m[2] : null,
      country: "US",
      sizeText: sizeLabel ? `${pairs[sizeLabel]} ${sizeLabel}` : null,
      details: Object.keys(pairs).length ? pairs : null,
      brokerIds: [],
      url: href ? (href.startsWith("http") ? href : `https://www.avisonyoung.us${href}`) : null,
    });
  });
  if (!listings.length) throw new Error("no sidebar listing items found on Avison Young properties app");
  return {
    company: "Avison Young (US)",
    sourceUrl,
    method: "Rendered SharpLaunch search app parsed (sidebar items)",
    totalAvailable: $('a[id^="sidebar_item_"]').length || null,
    listings,
  };
}

async function srcSavills(max: number): Promise<SourceResult> {
  const sourceUrl =
    "https://search.savills.com/com/en/list/property-for-sale/united-states-of-america";
  const listings: any[] = [];
  let total: number | null = null;
  for (let page = 1; listings.length < max && page <= 4; page++) {
    const url = page === 1 ? sourceUrl : `${sourceUrl}/page/${page}`;
    const html = await scrapeRaw(url, 6000);
    const $ = cheerio.load(html);
    total =
      total ??
      (Number((html.match(/([0-9][0-9,]*)\s+Properties for sale/i) ?? [])[1]?.replace(/,/g, "")) || null);
    const seenHere = new Set<string>();
    $('a[href*="/property-detail/"]').each((_, el) => {
      if (listings.length >= max) return;
      const href = $(el).attr("href")!;
      const abs = href.startsWith("http") ? href : `https://search.savills.com${href}`;
      if (seenHere.has(abs) || listings.some((l) => l.url === abs)) return;
      seenHere.add(abs);
      let card = $(el);
      if (!card.find("[class*='sv-details__address1']").length) {
        const parent = card
          .parents()
          .filter((__, p) => $(p).find("[class*='sv-details__address1']").length > 0)
          .first();
        if (parent.length) card = parent;
      }
      const name = clean(card.find("[class*='sv-details__address1']").first().text());
      const address2 = clean(card.find("[class*='sv-details__address2']").first().text());
      const priceBlock = clean(card.find(".sv-property-price").first().text());
      const priceText = (priceBlock?.match(/(?:US\$|\$|€|£)\s?[0-9][0-9,.]*(?:\s?million)?/i) ?? [])[0] ?? priceBlock ?? null;
      const sizeText =
        (clean(card.text())?.match(/\(([0-9][0-9,.]*\s*sq ?ft)\)/i) ?? [])[1] ??
        (clean(card.text())?.match(/([0-9][0-9,.]*\s*(?:sq ?ft|acres?|m²))/i) ?? [])[1] ??
        null;
      const brokerIds = [
        brokerRef({
          name: clean(card.find("[class*='sv-details__contacts-name']").first().text()),
          phone: clean(card.find("[class*='sv-details__contacts-phone']").first().text()),
          company: "Savills",
        }),
      ].filter((x): x is number => x !== null);
      const m = (address2 ?? "").match(/^(.*?),\s*(.*?),\s*([A-Z]{2})\s*(\d{5})?/);
      const img = card.find("img").attr("src") ?? card.find("img").attr("data-src") ?? null;
      listings.push({
        id: abs.split("/property-detail/")[1] ?? null,
        name,
        transactionType: "Sale",
        city: m ? clean(m[1]) : address2,
        state: m ? m[3] : null,
        postalCode: m?.[4] ?? null,
        country: "US",
        salePriceUsd: /\$/.test(priceText ?? "") ? moneyToNumber(priceText) : null,
        salePriceText: priceText,
        sizeText,
        brokerIds,
        photos: img && !img.startsWith("data:") ? [img] : [],
        url: abs,
      });
    });
    if (!seenHere.size) break;
  }
  if (!listings.length) throw new Error("no property-detail links found on Savills list page");
  return {
    company: "Savills",
    sourceUrl,
    method: "Server-rendered list pages parsed (cards), paginated via /page/N",
    totalAvailable: total,
    listings,
  };
}

async function srcNaiGlobal(max: number): Promise<SourceResult> {
  const sourceUrl = "https://ab.infabode.com/nai-global/listings3";
  const html = await scrapeRaw(sourceUrl, 8000);
  const $ = cheerio.load(html);
  const listings: any[] = [];
  $("div.listing-card").each((_, el) => {
    if (listings.length >= max) return;
    const card = $(el);
    const type = clean(card.find(".listing-card-header").first().text());
    if (type && !/sale/i.test(type)) return;
    const title = clean(card.find(".listing-card-title").first().text());
    const summary = clean(card.find(".listing-card-summary").first().text());
    const contentType = clean(card.find(".listing-card-content-type").first().text());
    // location is its own text node between the title and the summary
    const leafTexts: string[] = [];
    card
      .find("*")
      .addBack()
      .contents()
      .each((__, n) => {
        if (n.type === "text") {
          const t = clean((n as any).data);
          if (t) leafTexts.push(t);
        }
      });
    const locLine = leafTexts.find((t) => /^.{2,60}, [A-Z]{2}, United States$/.test(t)) ?? null;
    const m = (locLine ?? "").match(/^(.*?),\s*([A-Z]{2}),/);
    const publisher = (card.text().match(/Published by\s*([A-Za-z0-9 .,&'-]+?)(?:\d+ (?:day|hour|week|month)|$)/) ?? [])[1] ?? null;
    const img = card.find("img").first().attr("src") ?? null;
    listings.push({
      name: title,
      transactionType: "Sale",
      assetType: contentType,
      description: summary,
      city: m ? clean(m[1]) : null,
      state: m ? m[2] : null,
      country: "US",
      listingOffice: clean(publisher ?? ""),
      brokerIds: [],
      photos: img ? [img] : [],
      url: sourceUrl,
    });
  });
  if (!listings.length) throw new Error("no listing cards found on NAI Global listings page");
  return {
    company: "NAI Global",
    sourceUrl,
    method: "Rendered Infabode listings widget parsed (cards, infinite scroll — first batch)",
    totalAvailable: null,
    listings,
    note: "Cards are not individually linked; the listings page URL is provided. Coverage limited to the first rendered batch.",
  };
}

async function srcCbreDealflow(max: number): Promise<SourceResult> {
  const sourceUrl = "https://www.cbredealflow.com/";
  const html = await scrapeRaw(sourceUrl, 8000);
  const $ = cheerio.load(html);
  const total =
    Number((html.match(/([0-9][0-9,]*)\s*ASSETS LISTED/i) ?? [])[1]?.replace(/,/g, "")) || null;
  // Each asset has several anchors sharing one landing.aspx URL (image, description,
  // title). Group by URL, then use the shortest text as the name and the longest as
  // the description.
  const byHref = new Map<string, { texts: string[]; img: string | null; ctx: string }>();
  $('a[href*="landing.aspx"]').each((_, el) => {
    const href = $(el).attr("href")!;
    const abs = href.startsWith("http") ? href : `https://www.cbredealflow.com${href}`;
    const rec = byHref.get(abs) ?? { texts: [], img: null, ctx: "" };
    const t = clean($(el).text());
    if (t) rec.texts.push(t);
    rec.img = rec.img ?? $(el).find("img").attr("src") ?? null;
    if (!rec.ctx) rec.ctx = clean($(el).closest("td,li,div[class]").parent().text()) ?? "";
    byHref.set(abs, rec);
  });
  const listings: any[] = [];
  for (const [abs, rec] of byHref) {
    if (listings.length >= max) break;
    const texts = rec.texts.sort((a, b) => a.length - b.length);
    const name = texts[0] ?? null;
    if (!name || name.length < 4) continue;
    const description = texts.length > 1 ? texts[texts.length - 1] : null;
    const typeCountry = (rec.ctx.match(/(Office|Industrial|Retail|Multifamily|Land|Hotel|Mixed[- ]Use|Healthcare|Self Storage|Data Cent[a-z]+|Senior Housing|Debt|Other)\s*\|?\s*(United States|[A-Za-z ]{4,30})/) ?? []) as any[];
    // the card text concatenates the name and the city — remove the name and UI labels first
    const ctxClean = rec.ctx
      .split(name).join(" ")
      .replace(/\b(Details|Contacts|Available|New Listing|Featured)\b/g, " ");
    const cityMatches = [...ctxClean.matchAll(/([A-Z][A-Za-z .'-]{1,30}?)[‚,]\s*([A-Z]{2})\b/g)];
    const cityState = (cityMatches[cityMatches.length - 1] ?? []) as any[];
    listings.push({
      name,
      transactionType: "Sale (investment)",
      assetType: typeCountry[1] ?? null,
      description: description && description !== name ? description : null,
      city: cityState[1] ? clean(String(cityState[1]).split(".").pop() ?? "") : null,
      state: cityState[2] ?? null,
      country: typeCountry[2] ? clean(typeCountry[2]) : null,
      brokerIds: [],
      photos: rec.img ? [rec.img] : [],
      url: abs,
    });
  }
  if (!listings.length) throw new Error("no asset cards found on CBRE Deal Flow homepage");
  return {
    company: "CBRE Deal Flow",
    sourceUrl,
    method: "Rendered public homepage grid parsed (cards)",
    totalAvailable: total,
    listings,
    note: "Deal rooms and full financial detail require registration; public card data only. Coverage limited to the first page of the grid.",
  };
}

const UNSUPPORTED: Record<string, string> = {
  colliers:
    "Colliers' property search (colliers.com/en/properties) loads results only through Coveo's POST-only search API behind a consent wall, which this collector cannot call. No public GET endpoint or server-rendered listing markup was found.",
  transwestern:
    "Transwestern's property search (transwestern.com/properties) is a map-driven app whose data loads via POST requests only, with no public GET endpoint or server-rendered listing markup.",
};

// ---------- main ----------

async function runSource(key: string, max: number): Promise<SourceResult> {
  switch (key) {
    case "cbre":
      return srcCbre(max);
    case "cbre-dealflow":
      return srcCbreDealflow(max);
    case "jll":
      return srcJll(max);
    case "jll-investor":
      return srcJllInvestor(max);
    case "cushman-wakefield":
      return srcCushman(max);
    case "newmark":
      return srcNewmark(max);
    case "marcus-millichap":
      return srcMarcusMillichap(max);
    case "avison-young":
      return srcAvisonYoung(max);
    case "savills":
      return srcSavills(max);
    case "svn":
      return srcBuildout("SVN", "b933480474026c41d248b77156c84aef37dcac68", "https://svn.com/properties/", max);
    case "lee-associates":
      return srcBuildout("Lee & Associates", "9a64a93980aeae8db347e72cdfa8ca61017acc9a", "https://www.lee-associates.com/properties/", max);
    case "nai-global":
      return srcNaiGlobal(max);
    default:
      throw new Error(`unhandled source ${key}`);
  }
}

async function main() {
  const requested = sourceArg === "all" ? [...SOURCE_KEYS] : [sourceArg];

  if (requested.length === 1 && UNSUPPORTED[requested[0]]) {
    throw new Error(`OUT_OF_SCOPE: source '${requested[0]}' is not supported — ${UNSUPPORTED[requested[0]]}`);
  }

  const sources: any[] = [];
  const listings: any[] = [];

  for (const key of requested) {
    if (UNSUPPORTED[key]) {
      console.error(`skipping unsupported source ${key}`);
      sources.push({ sourceKey: key, supported: false, note: UNSUPPORTED[key] });
      continue;
    }
    console.error(`collecting ${key} (max ${MAX_ITEMS})...`);
    try {
      const res = await runSource(key, MAX_ITEMS);
      sources.push({
        sourceKey: key,
        supported: true,
        company: res.company,
        sourceUrl: res.sourceUrl,
        method: res.method,
        totalAvailableOnSource: res.totalAvailable,
        listingsCollected: res.listings.length,
        note: res.note ?? null,
      });
      for (const l of res.listings) {
        listings.push(prune({ sourceKey: key, sourceCompany: res.company, ...l }));
      }
      console.error(`  ${key}: ${res.listings.length} listings (source total: ${res.totalAvailable ?? "unknown"})`);
    } catch (err) {
      if (requested.length === 1) throw err;
      console.error(`  ${key} FAILED: ${err}`);
      sources.push({ sourceKey: key, supported: true, error: String(err).slice(0, 300) });
    }
  }

  const succeeded = sources.filter((s) => s.listingsCollected > 0).length;
  if (listings.length === 0) {
    throw new Error("no listings collected from any source");
  }
  console.error(`done: ${listings.length} listings from ${succeeded} sources, ${brokers.length} unique brokers`);

  const out = {
    description:
      "Commercial real estate for-sale listings collected from major brokerage websites, normalized to a common structure.",
    maxItemsPerSource: MAX_ITEMS,
    sources,
    listings,
    brokers: brokers.map(prune),
    totalListings: listings.length,
  };
  process.stdout.write(JSON.stringify(out));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
