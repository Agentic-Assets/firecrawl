// sources/savills.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { brokerRef } from "../lib/broker.js";
import { PAGE_CAP } from "../lib/config.js";
import { scrapeRaw } from "../lib/scrape.js";
import { SourceResult, Tx } from "../types.js";
import { clean, moneyToNumber, num } from "../lib/util.js";


// --- Savills: server-rendered list pages ---

export const US_STATE_NAME_TO_ABBR: Record<string, string> = {
  alabama: "AL",
  alaska: "AK",
  arizona: "AZ",
  arkansas: "AR",
  california: "CA",
  colorado: "CO",
  connecticut: "CT",
  delaware: "DE",
  florida: "FL",
  georgia: "GA",
  hawaii: "HI",
  idaho: "ID",
  illinois: "IL",
  indiana: "IN",
  iowa: "IA",
  kansas: "KS",
  kentucky: "KY",
  louisiana: "LA",
  maine: "ME",
  maryland: "MD",
  massachusetts: "MA",
  michigan: "MI",
  minnesota: "MN",
  mississippi: "MS",
  missouri: "MO",
  montana: "MT",
  nebraska: "NE",
  nevada: "NV",
  "new hampshire": "NH",
  "new jersey": "NJ",
  "new mexico": "NM",
  "new york": "NY",
  "north carolina": "NC",
  "north dakota": "ND",
  ohio: "OH",
  oklahoma: "OK",
  oregon: "OR",
  pennsylvania: "PA",
  "rhode island": "RI",
  "south carolina": "SC",
  "south dakota": "SD",
  tennessee: "TN",
  texas: "TX",
  utah: "UT",
  vermont: "VT",
  virginia: "VA",
  washington: "WA",
  "west virginia": "WV",
  wisconsin: "WI",
  wyoming: "WY",
};

export function inferStateFromZip(zip: string | null): string | null {
  if (!zip) return null;
  const prefix = Number(zip.slice(0, 3));
  if (!Number.isFinite(prefix)) return null;
  if (prefix >= 6 && prefix <= 9) return "PR";
  if (prefix >= 10 && prefix <= 27) return "MA";
  if (prefix >= 28 && prefix <= 29) return "RI";
  if (prefix >= 30 && prefix <= 38) return "NH";
  if (prefix >= 39 && prefix <= 49) return "ME";
  if (prefix >= 50 && prefix <= 59) return "IA";
  if (prefix >= 60 && prefix <= 149) return "NY";
  if (prefix >= 150 && prefix <= 196) return "PA";
  if (prefix >= 197 && prefix <= 199) return "DE";
  if (prefix >= 200 && prefix <= 205) return "DC";
  if (prefix >= 206 && prefix <= 219) return "MD";
  if (prefix >= 220 && prefix <= 246) return "VA";
  if (prefix >= 247 && prefix <= 268) return "WV";
  if (prefix >= 270 && prefix <= 289) return "NC";
  if (prefix >= 290 && prefix <= 299) return "SC";
  if (prefix >= 300 && prefix <= 319) return "GA";
  if (prefix >= 320 && prefix <= 349) return "FL";
  if (prefix >= 350 && prefix <= 369) return "AL";
  if (prefix >= 370 && prefix <= 385) return "TN";
  if (prefix >= 386 && prefix <= 397) return "MS";
  if (prefix >= 398 && prefix <= 399) return "GA";
  if (prefix >= 400 && prefix <= 427) return "KY";
  if (prefix >= 430 && prefix <= 459) return "OH";
  if (prefix >= 460 && prefix <= 479) return "IN";
  if (prefix >= 480 && prefix <= 499) return "MI";
  if (prefix >= 500 && prefix <= 528) return "IA";
  if (prefix >= 530 && prefix <= 549) return "WI";
  if (prefix >= 550 && prefix <= 567) return "MN";
  if (prefix >= 570 && prefix <= 577) return "SD";
  if (prefix >= 580 && prefix <= 588) return "ND";
  if (prefix >= 590 && prefix <= 599) return "MT";
  if (prefix >= 600 && prefix <= 629) return "IL";
  if (prefix >= 630 && prefix <= 658) return "MO";
  if (prefix >= 660 && prefix <= 679) return "KS";
  if (prefix >= 680 && prefix <= 693) return "NE";
  if (prefix >= 700 && prefix <= 714) return "LA";
  if (prefix >= 716 && prefix <= 729) return "AR";
  if (prefix >= 730 && prefix <= 749) return "OK";
  if (prefix >= 750 && prefix <= 799) return "TX";
  if (prefix >= 800 && prefix <= 816) return "CO";
  if (prefix >= 820 && prefix <= 831) return "WY";
  if (prefix >= 832 && prefix <= 838) return "ID";
  if (prefix >= 840 && prefix <= 847) return "UT";
  if (prefix >= 850 && prefix <= 865) return "AZ";
  if (prefix >= 870 && prefix <= 884) return "NM";
  if (prefix >= 889 && prefix <= 898) return "NV";
  if (prefix >= 900 && prefix <= 961) return "CA";
  if (prefix >= 967 && prefix <= 968) return "HI";
  if (prefix >= 970 && prefix <= 979) return "OR";
  if (prefix >= 980 && prefix <= 994) return "WA";
  if (prefix >= 995 && prefix <= 999) return "AK";
  return null;
}

export function parseSavillsUsLocation(address2: string | null): {
  city: string | null;
  state: string | null;
  postalCode: string | null;
} | null {
  if (!address2) return null;
  const postalCode = (address2.match(/\b\d{5}(?:-\d{4})?\b/) ?? [])[0] ?? null;
  const parts = address2
    .split(",")
    .map((p) => p.trim())
    .filter(Boolean);
  const state =
    parts
      .map((p) => p.match(/\b([A-Z]{2})\b/)?.[1] ?? US_STATE_NAME_TO_ABBR[p.toLowerCase()] ?? null)
      .find((s): s is string => s !== null) ?? inferStateFromZip(postalCode);
  if (!state && !postalCode) return null;
  const city =
    parts.find((p) => {
      const lower = p.toLowerCase();
      return !/^\d{5}(?:-\d{4})?$/.test(p) && !/\b[A-Z]{2}\b/.test(p) && !US_STATE_NAME_TO_ABBR[lower];
    }) ??
    clean(address2.replace(new RegExp(`\\b${state}\\b.*$`), "").replace(/\d{5}(?:-\d{4})?.*$/, "").replace(/,+$/, "")) ??
    null;
  return { city: clean(city), state, postalCode };
}

export function parseSavillsNextData(html: string): any | null {
  const raw = html.match(/<script id="__NEXT_DATA__" type="application\/json">([\s\S]*?)<\/script>/)?.[1];
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export function savillsNextDataProperties(html: string): any[] {
  const data = parseSavillsNextData(html);
  const props = data?.props?.initialReduxState?.properties;
  return props && typeof props === "object" ? Object.values(props) : [];
}

export function savillsTotalItems(html: string, fallback: number): number | null {
  const data = parseSavillsNextData(html);
  const total = data?.props?.initialReduxState?.listPage?.totalItems;
  if (typeof total === "number" && total > 0) return Math.max(total, fallback);
  const headingTotal = Number((html.match(/([0-9][0-9,]*)\s+Properties for (?:let|sale|rent)/i) ?? [])[1]?.replace(/,/g, ""));
  return Number.isFinite(headingTotal) && headingTotal > 0 ? Math.max(headingTotal, fallback) : fallback || null;
}

export function savillsSqft(text: string | null): number | null {
  const match = text?.match(/\(([0-9][0-9,.]*)\s*sq ?ft\)/i) ?? text?.match(/([0-9][0-9,.]*)\s*sq ?ft/i);
  return match ? Number(match[1].replace(/,/g, "")) : null;
}

export function savillsImageUrls(row: any): string[] {
  const urls = new Set<string>();
  for (const img of [...(row.ImagesGallery ?? []), ...(row.PropertyCardImagesGallery ?? [])]) {
    for (const key of ["ImageUrl_L", "ImageUrl_M", "ImageUrl_S", "ImageUrl"]) {
      const url = clean(img?.[key]);
      if (url?.startsWith("http")) urls.add(url);
    }
  }
  return [...urls];
}

export function savillsDocumentUrls(row: any): { name: string | null; url: string }[] {
  const docs: { name: string | null; url: string }[] = [];
  const add = (name: string | null, url: string | null) => {
    if (url?.startsWith("http") && /\.pdf(?:$|\?)/i.test(url) && !docs.some((d) => d.url === url)) {
      docs.push({ name, url });
    }
  };
  add("Floor plan", clean(row.FloorplanPDFUrl));
  for (const doc of row.BrochureGallery ?? []) {
    add(clean(doc?.Caption) ?? "Brochure", clean(doc?.ImageUrl));
  }
  return docs;
}

export function savillsContact(agent: any): any | null {
  const name = clean(agent?.AgentName);
  const email = clean(agent?.EmailAddress);
  const phone = clean(agent?.AgentPhoneNumber);
  if (!name && !email && !phone) return null;
  return {
    name,
    email,
    phone,
    office: clean(agent?.Office?.OfficeName),
    company: "Savills",
    avatarUrl: clean(agent?.AgentImageUrl),
  };
}

export async function srcSavillsCommercialLease(max: number): Promise<SourceResult> {
  const sourceUrl = "https://search.savills.com/com/en/list/commercial/property-to-let/united-states-of-america";
  const html = await scrapeRaw(sourceUrl, { waitFor: 6000 });
  const rows = savillsNextDataProperties(html).filter((row) => row?.IsCommercial === true);
  const selected = rows.slice(0, Math.min(max, rows.length));
  const listings: any[] = [];
  let nonUsFiltered = 0;
  for (const row of selected) {
    const location = parseSavillsUsLocation(clean(row.AddressLine2));
    if (!location) {
      nonUsFiltered++;
      continue;
    }
    const contactsDetailed = [savillsContact(row.PrimaryAgent), savillsContact(row.SecondaryAgent)].filter(Boolean);
    const brokerIds = contactsDetailed
      .map((contact) => brokerRef(contact))
      .filter((id): id is number => id !== null);
    const propertyType = clean(row.PropertyTypes?.[0]?.Caption);
    const detailId = clean(row.ExternalPropertyIDFormatted) ?? clean(row.ExternalPropertyID)?.toLowerCase();
    const url = detailId ? `https://search.savills.com/com/en/property-detail/${detailId}` : sourceUrl;
    listings.push({
      id: clean(row.ExternalPropertyID) ?? detailId,
      name: clean(row.AddressLine1) ?? clean(row.PropertyPageTitle),
      transactionType: "Lease",
      assetType: propertyType,
      street: clean(row.AddressLine1),
      city: location.city,
      state: location.state,
      postalCode: location.postalCode,
      country: "US",
      latitude: num(row.Latitude),
      longitude: num(row.Longitude),
      leaseRateText: clean(row.GuidePriceText) ?? clean(row.DisplayPriceText),
      sizeText: clean(row.SizeFormatted) ?? clean(row.FooterSizeFormatted),
      buildingSizeSqft: savillsSqft(clean(row.SizeFormatted) ?? clean(row.FooterSizeFormatted)),
      description: clean((row.LongDescription ?? []).map((part: any) => [part.Head, part.Body].filter(Boolean).join("\n")).join("\n\n")),
      brokerIds,
      contactsDetailed,
      brochures: savillsDocumentUrls(row),
      photos: savillsImageUrls(row),
      url,
      rawSavillsProperty: row,
    });
  }
  return {
    company: "Savills",
    sourceUrl,
    method: "Server-rendered commercial lease page parsed from public __NEXT_DATA__ property objects",
    totalAvailable: savillsTotalItems(html, listings.length + nonUsFiltered),
    listings,
    note: nonUsFiltered
      ? `${nonUsFiltered} non-US or non-US-office commercial lease row(s) filtered out`
      : "Commercial sale route was checked separately; the only public commercial sale object observed was Toronto, Canada.",
  };
}

export async function srcSavills(tx: Tx, max: number, _monitor: boolean): Promise<SourceResult> {
  // Enumeration-only source: both the sale list pages and the lease __NEXT_DATA__
  // parse extract every field from the list page (no per-listing detail render),
  // so monitor output == full output.
  if (tx === "lease") return srcSavillsCommercialLease(max);

  const base = "https://search.savills.com/com/en/list/property-for-sale/united-states-of-america";
  const listings: any[] = [];
  let total: number | null = null;
  let nonUsFiltered = 0;
  let emptyStreak = 0;
  for (let page = 1; listings.length < max && page <= Math.max(PAGE_CAP, 10); page++) {
    const before = listings.length;
    const url = page === 1 ? base : `${base}/page/${page}`;
    const html = await scrapeRaw(url, { waitFor: 6000 });
    const $ = cheerio.load(html);
    total =
      total ??
      (Number(
        (html.match(/([0-9][0-9,]*)\s+Properties for (?:sale|rent)/i) ?? [])[1]?.replace(/,/g, "")
      ) || null);
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
      const priceText =
        (priceBlock?.match(/(?:US\$|\$|€|£)\s?[0-9][0-9,.]*(?:\s?million)?/i) ?? [])[0] ??
        priceBlock ??
        null;
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
      const location = parseSavillsUsLocation(address2);
      // When the US filter has no inventory (lease), Savills renders foreign
      // fallback cards (e.g. Cyprus, EUR-priced). US-only feed: drop them.
      if (!location) {
        nonUsFiltered++;
        return;
      }
      const img = card.find("img").attr("src") ?? card.find("img").attr("data-src") ?? null;
      listings.push({
        id: abs.split("/property-detail/")[1] ?? null,
        name,
        transactionType: "Sale",
        city: location.city,
        state: location.state,
        postalCode: location.postalCode,
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
    // Savills shuffles sort order between requests, so a page can be all
    // duplicates without meaning the end of the result set. Stop only after
    // several consecutive pages contribute nothing new.
    if (listings.length === before) {
      if (++emptyStreak >= 3) break;
    } else {
      emptyStreak = 0;
    }
    console.error(`  savills/sale: page ${page}, ${listings.length} collected (total ${total ?? "?"})`);
  }
  if (!listings.length && !nonUsFiltered) {
    throw new Error("no property-detail links found on Savills list page");
  }
  return {
    company: "Savills",
    sourceUrl: base,
    method: "Server-rendered list pages parsed (cards), paginated via /page/N",
    totalAvailable: total,
    listings,
    note: nonUsFiltered
      ? `${nonUsFiltered} non-US fallback card(s) filtered out`
      : undefined,
  };
}
