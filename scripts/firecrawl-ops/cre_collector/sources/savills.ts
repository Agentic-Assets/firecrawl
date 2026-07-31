// sources/savills.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import { brokerRef } from "../lib/broker.js";
import { detailObservation } from "../lib/freshness.js";
import { scrapeRaw } from "../lib/scrape.js";
import { SourceResult, Tx } from "../types.js";
import { clean, moneyToNumber, num } from "../lib/util.js";


// --- Savills: server-rendered list pages ---

/**
 * List pages are enumeration-only. Keep a failed render from serially holding
 * a whole source run at the general 90-second scrape timeout; callers may
 * raise this bounded recovery value when the public site is slow.
 */
export function savillsListTimeoutMs(raw = process.env.SAVILLS_LIST_TIMEOUT_MS): number {
  const value = Number(raw ?? 30000);
  return Number.isFinite(value) ? Math.min(90000, Math.max(10000, Math.floor(value))) : 30000;
}

/**
 * Savills renders the list payload on the server, so it is both faster and
 * more reliable to enumerate it directly than to send it through a browser
 * renderer. Keep a separate, short bound for that public HTTP request. The
 * Firecrawl path remains a fallback for a future response-shape change.
 */
export function savillsDirectListTimeoutMs(raw = process.env.SAVILLS_DIRECT_LIST_TIMEOUT_MS): number {
  const value = Number(raw ?? 25000);
  return Number.isFinite(value) ? Math.min(60000, Math.max(5000, Math.floor(value))) : 25000;
}

const SAVILLS_LIST_USER_AGENT = "Mozilla/5.0 (compatible; AgenticAssetsCRE/1.0)";

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

const US_STATE_ABBRS = new Set([
  ...Object.values(US_STATE_NAME_TO_ABBR),
  "DC",
  "PR",
]);

const CANADIAN_PROVINCE_ABBRS = new Set([
  "AB",
  "BC",
  "MB",
  "NB",
  "NL",
  "NS",
  "NT",
  "NU",
  "ON",
  "PE",
  "QC",
  "SK",
  "YT",
]);

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
  const withoutPostal = address2
    .replace(/\b\d{5}(?:-\d{4})?\b\s*$/, "")
    .trim();
  const terminalAbbreviation = withoutPostal.match(/\b([A-Za-z]{2})\s*$/)?.[1]?.toUpperCase();
  const stateFromAbbreviation =
    terminalAbbreviation && US_STATE_ABBRS.has(terminalAbbreviation)
      ? terminalAbbreviation
      : null;
  const stateNameMatch = Object.entries(US_STATE_NAME_TO_ABBR)
    .sort(([left], [right]) => right.length - left.length)
    .find(([name]) =>
      new RegExp(`\\b${name.replace(/ /g, "\\s+")}\\s*$`, "i").test(withoutPostal)
    );
  const state =
    stateFromAbbreviation ??
    stateNameMatch?.[1] ??
    inferStateFromZip(postalCode);
  if (!state && !postalCode) return null;
  const statePattern = stateFromAbbreviation
    ? stateFromAbbreviation
    : stateNameMatch?.[0] ?? state;
  const withoutState = (
    statePattern
      ? withoutPostal.replace(
          new RegExp(`\\b${statePattern.replace(/ /g, "\\s+")}\\s*$`, "i"),
          ""
        )
      : withoutPostal
  )
    .replace(/,+$/, "")
    .trim();
  const city =
    clean(
      withoutState
        .split(",")
        .map((part) => part.trim())
        .filter(Boolean)
        .at(-1)
    ) ?? null;
  return { city: clean(city), state, postalCode };
}

/**
 * Distinguish a provider-side geographic spillover from an ambiguous U.S.
 * address. Savills currently returns Toronto rows from its nominal U.S. sale
 * URL. A Canadian postal code or province is sufficient to exclude those rows;
 * an otherwise unmappable address remains a hard failure.
 */
export function savillsClearlyNonUsLocation(address2: string | null): boolean {
  if (!address2) return false;
  if (/\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b/i.test(address2)) return true;
  const terminalProvince = address2.match(/\b([A-Za-z]{2})\s*$/)?.[1]?.toUpperCase();
  return terminalProvince ? CANADIAN_PROVINCE_ABBRS.has(terminalProvince) : false;
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

/**
 * Reject challenge pages, redirect shells, and other successful-but-wrong
 * responses before allowing a list pass to emit a sparse source result. A
 * rejected page makes the source fail, which lets the monitor coverage gate
 * suppress disappearance events rather than treating a partial enumeration as
 * an empty current inventory.
 */
export function savillsListHtmlIsUsable(html: string): boolean {
  const state = parseSavillsNextData(html)?.props?.initialReduxState;
  return !!state?.listPage && !!state?.properties && typeof state.properties === "object";
}

async function savillsDirectListHtmlOnce(
  url: string,
  timeoutMs: number,
  fetchImpl: typeof fetch = fetch
): Promise<string> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new Error(`Savills direct list request timed out after ${timeoutMs}ms`));
    }, timeoutMs);
  });
  const request = fetchImpl(url, {
    headers: {
      accept: "text/html,application/xhtml+xml",
      "user-agent": SAVILLS_LIST_USER_AGENT,
    },
    signal: controller.signal,
  }).then(async (response) => {
    if (!response.ok) throw new Error(`Savills direct list HTTP ${response.status}`);
    const html = await response.text();
    if (!savillsListHtmlIsUsable(html)) {
      throw new Error("Savills direct list response did not contain the expected __NEXT_DATA__ list state");
    }
    return html;
  });
  try {
    return await Promise.race([request, deadline]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/**
 * Read a complete server-rendered Savills list page without invoking the local
 * browser service. Two bounded attempts cover transient CDN failures. The
 * legacy Firecrawl read is retained only as a validated fallback so an invalid
 * page cannot become a false "no listings" monitor observation.
 */
export async function savillsListHtml(url: string, fresh = false): Promise<string> {
  const directTimeout = savillsDirectListTimeoutMs();
  let directError: unknown = null;
  for (let attempt = 1; attempt <= 2; attempt++) {
    try {
      return await savillsDirectListHtmlOnce(url, directTimeout);
    } catch (err) {
      directError = err;
      console.error(`Savills direct list attempt ${attempt} failed for ${url}: ${err}`);
      if (attempt < 2) await new Promise((resolve) => setTimeout(resolve, 1000 * attempt));
    }
  }

  try {
    const html = await scrapeRaw(url, {
      waitFor: 6000,
      timeout: savillsListTimeoutMs(),
      ...(fresh ? { maxAge: 0 } : {}),
    });
    if (!savillsListHtmlIsUsable(html)) {
      throw new Error("Savills Firecrawl fallback response did not contain the expected __NEXT_DATA__ list state");
    }
    return html;
  } catch (fallbackError) {
    throw new Error(
      `Savills list page failed via direct fetch (${String(directError)}) and Firecrawl fallback (${String(fallbackError)})`
    );
  }
}

export function savillsTotalItems(html: string, fallback: number): number | null {
  const data = parseSavillsNextData(html);
  const listPage = data?.props?.initialReduxState?.listPage;
  const currentPage = listPage?.currentPage;
  const pageMap = listPage?.pageMap;
  const currentMapPage =
    pageMap?.[String(currentPage)] ??
    (pageMap && typeof pageMap === "object" ? Object.values(pageMap)[0] : null);
  // Savills' top-level totalItems is its page count in current responses. The
  // item count lives on the selected page's paging block.
  const total = currentMapPage?.paging?.totalItems ?? listPage?.totalItems;
  if (typeof total === "number" && total > 0) return Math.max(total, fallback);
  const headingTotal = Number((html.match(/([0-9][0-9,]*)\s+Properties for (?:let|sale|rent)/i) ?? [])[1]?.replace(/,/g, ""));
  return Number.isFinite(headingTotal) && headingTotal > 0 ? Math.max(headingTotal, fallback) : fallback || null;
}

export function savillsVerifiedTotalItems(html: string): number | null {
  const data = parseSavillsNextData(html);
  const listPage = data?.props?.initialReduxState?.listPage;
  const currentPage = listPage?.currentPage;
  const pageMap = listPage?.pageMap;
  const currentMapPage =
    pageMap?.[String(currentPage)] ??
    (pageMap && typeof pageMap === "object" ? Object.values(pageMap)[0] : null);
  const total = currentMapPage?.paging?.totalItems ?? listPage?.totalItems;
  if (Number.isInteger(total) && total > 0) return total;
  const headingTotal = Number(
    (html.match(/([0-9][0-9,]*)\s+Properties for (?:let|sale|rent)/i) ?? [])[1]?.replace(/,/g, "")
  );
  return Number.isInteger(headingTotal) && headingTotal > 0 ? headingTotal : null;
}

export function savillsPageInfo(html: string, fallbackRows: number, requireVerifiedTotal = false): {
  currentPage: number | null;
  totalPages: number | null;
  totalItems: number | null;
  nextUrl: string | null;
} {
  const listPage = parseSavillsNextData(html)?.props?.initialReduxState?.listPage;
  const pageMap = listPage?.pageMap;
  const currentPage = Number(listPage?.currentPage);
  const mapPage =
    pageMap?.[String(currentPage)] ??
    (pageMap && typeof pageMap === "object" ? Object.values(pageMap)[0] : null);
  const rawTotalPages = Number(mapPage?.paging?.total);
  const totalItems = requireVerifiedTotal
    ? savillsVerifiedTotalItems(html)
    : savillsTotalItems(html, fallbackRows);
  const nextUrl = clean(mapPage?.metaData?.NextUrl);
  // The provider currently emits paging.total=0 for a one-page result set
  // while also reporting an exact positive totalItems. Accept that narrow
  // shape only when page one contains every reported row and exposes no
  // continuation. Any other zero/invalid page count remains fail-closed.
  const totalPages =
    Number.isInteger(rawTotalPages) && rawTotalPages > 0
      ? rawTotalPages
      : rawTotalPages === 0 &&
          currentPage === 1 &&
          nextUrl === null &&
          totalItems !== null &&
          totalItems === fallbackRows
        ? 1
        : null;
  return {
    currentPage: Number.isInteger(currentPage) && currentPage > 0 ? currentPage : null,
    totalPages,
    totalItems,
    nextUrl,
  };
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

export function savillsDocumentUrls(
  row: any
): { name: string | null; url: string; docType?: string }[] {
  const docs: { name: string | null; url: string; docType?: string }[] = [];
  const add = (name: string | null, url: string | null, docType?: string) => {
    if (url?.startsWith("http") && /\.pdf(?:$|\?)/i.test(url) && !docs.some((d) => d.url === url)) {
      docs.push(docType ? { name, url, docType } : { name, url });
    }
  };
  // Per-document docType is now honored by the ingest (default 'brochure'); the
  // floor-plan PDF is classified distinctly so it lands as doc_type 'floor_plan'.
  add("Floor plan", clean(row.FloorplanPDFUrl), "floor_plan");
  for (const doc of row.BrochureGallery ?? []) {
    add(clean(doc?.Caption) ?? "Brochure", clean(doc?.ImageUrl), "brochure");
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

// A Savills public sale card is only kept when it is a commercial-surface
// listing. The generic /property-for-sale/ surface is residential luxury
// homes; this guard prevents residential contamination (101 homes were
// ingested and soft-deleted on 2026-06-14) if the sale path ever runs
// additively again. Returns true only for commercial-classified cards.
// Default-deny: when no commercial signal is found the card is dropped,
// because the generic surface is residential and the US commercial-sale feed
// returns 0 rows (verified via a 22-URL probe matrix, 2026-06-12).
export function savillsSaleCardIsCommercial(card: {
  propertyType?: string | null;
  href?: string | null;
  cardText?: string | null;
}): boolean {
  // Commercial keywords that identify a non-residential asset class.
  // Multi-word keywords ("mixed use") use a literal substring match;
  // single-word keywords use a word-boundary regex to avoid false positives
  // (e.g. "warehouse" contains "house", a residential keyword).
  const COMMERCIAL_KEYWORDS = [
    "office", "retail", "industrial", "warehouse",
    "mixed use", "mixed-use", "land", "hospitality",
    "hotel", "leisure", "commercial", "development",
  ];
  // Residential keywords that force a false result. Word-boundary matched so
  // "warehouse" does NOT trigger "house".
  const RESIDENTIAL_KEYWORDS = [
    "house", "apartment", "flat", "bedroom",
    "residential", "villa", "cottage",
  ];

  const href = (card.href ?? "").toLowerCase();
  const propertyType = (card.propertyType ?? "").toLowerCase();
  const cardText = (card.cardText ?? "").toLowerCase();
  const combined = `${href} ${propertyType} ${cardText}`;

  // URL segment /commercial/ is a definitive commercial signal.
  if (href.includes("/commercial/")) return true;

  // Residential markers force false. Use word-boundary regex so substrings
  // inside other words (e.g. "house" inside "warehouse") do not trigger.
  for (const kw of RESIDENTIAL_KEYWORDS) {
    if (new RegExp(`\\b${kw}\\b`).test(combined)) return false;
  }

  // Commercial keyword match in property type, href, or card text.
  // Multi-word keywords use a literal includes; single-word ones use
  // word-boundary matching to be consistent.
  for (const kw of COMMERCIAL_KEYWORDS) {
    if (kw.includes(" ")) {
      if (combined.includes(kw)) return true;
    } else {
      if (new RegExp(`\\b${kw}\\b`).test(combined)) return true;
    }
  }

  // No commercial signal found: default-deny (the generic surface is residential).
  return false;
}

// Maps a single Savills __NEXT_DATA__ property row to a U.S. listing object,
// or returns null when the row has no parseable U.S. location. The server-
// rendered list payload contains the same fields for sale and lease.
export function mapSavillsRow(row: any, transactionType: Tx, sourceUrl: string): any | null {
  const location = parseSavillsUsLocation(clean(row.AddressLine2));
  if (!location) return null;
  const contactsDetailed = [savillsContact(row.PrimaryAgent), savillsContact(row.SecondaryAgent)].filter(Boolean);
  const brokerIds = contactsDetailed
    .map((contact) => brokerRef(contact))
    .filter((id): id is number => id !== null);
  const propertyType = clean(row.PropertyTypes?.[0]?.Caption);
  const detailId = clean(row.ExternalPropertyIDFormatted) ?? clean(row.ExternalPropertyID)?.toLowerCase();
  const url = detailId ? `https://search.savills.com/com/en/property-detail/${detailId}` : sourceUrl;

  // --- Phase-2 scalar lift (additive, nullable) ---
  // canonicalUrl: use the already-computed absolute URL; fall back to resolving
  // MetaInformation.CanonicalUrl (a relative path) against the Savills root.
  const metaCanonical = clean(row.MetaInformation?.CanonicalUrl);
  const canonicalUrl: string | null =
    url !== sourceUrl
      ? url
      : metaCanonical
        ? `https://search.savills.com/com/en/${metaCanonical}`
        : null;

  // highlights: WebFeatureList is an array of plain-text strings.
  const webFeatures: string[] = Array.isArray(row.WebFeatureList)
    ? row.WebFeatureList.map((s: any) => clean(s)).filter((s: string | null): s is string => s !== null)
    : [];
  const highlights: string | null = webFeatures.length ? webFeatures.join("\n") : null;

  // availableSf: AvailableSize.SqFt, suppress zero (zero means "not stated").
  const rawSqFt = row.AvailableSize?.SqFt;
  const availableSf: number | null =
    typeof rawSqFt === "number" && isFinite(rawSqFt) && rawSqFt > 0 ? rawSqFt : null;
  const guidePrice = clean(row.GuidePriceText);
  const displayPrice = clean(row.DisplayPriceText);
  // Sale cards commonly use GuidePriceText="Asking price" and carry the
  // actual amount in DisplayPriceText. Lease cards have historically exposed
  // their rate in GuidePriceText, so retain that preference there.
  const priceText = transactionType === "sale"
    ? displayPrice ?? guidePrice
    : guidePrice ?? displayPrice;

  return {
    id: clean(row.ExternalPropertyID) ?? detailId,
    name: clean(row.AddressLine1) ?? clean(row.PropertyPageTitle),
    transactionType: transactionType === "sale" ? "Sale" : "Lease",
    assetType: propertyType,
    street: clean(row.AddressLine1),
    city: location.city,
    state: location.state,
    postalCode: location.postalCode,
    country: "US",
    latitude: num(row.Latitude),
    longitude: num(row.Longitude),
    salePriceUsd: transactionType === "sale" && /(?:US\$|\$)/.test(priceText ?? "") ? moneyToNumber(priceText) : null,
    salePriceText: transactionType === "sale" ? priceText : null,
    leaseRateText: transactionType === "lease" ? priceText : null,
    sizeText: clean(row.SizeFormatted) ?? clean(row.FooterSizeFormatted),
    buildingSizeSqft: savillsSqft(clean(row.SizeFormatted) ?? clean(row.FooterSizeFormatted)),
    description: clean((row.LongDescription ?? []).map((part: any) => [part.Head, part.Body].filter(Boolean).join("\n")).join("\n\n")),
    brokerIds,
    contactsDetailed,
    brochures: savillsDocumentUrls(row),
    photos: savillsImageUrls(row),
    url,
    rawSavillsProperty: row,
    // Phase-2 scalar fields (nullable; null when source does not provide them).
    canonicalUrl,
    highlights,
    availableSf,
  };
}

// Kept as a focused public helper for existing lease-mapping tests and callers.
export function mapSavillsLeaseRow(row: any, sourceUrl: string): any | null {
  return mapSavillsRow(row, "lease", sourceUrl);
}

async function collectSavillsTransaction(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  const sourceUrl = tx === "lease"
    ? "https://search.savills.com/com/en/list/commercial/property-to-let/united-states-of-america"
    : "https://search.savills.com/com/en/list/commercial/property-for-sale/united-states-of-america";
  const listings: any[] = [];
  let nonUsFiltered = 0;
  let unmappable = 0;
  const seenRawIds = new Set<string>();
  const seenListingIds = new Set<string>();
  const visitedUrls = new Set<string>();
  let total: number | null = null;
  let declaredTotalPages: number | null = null;
  let finalPage: number | null = null;
  let url: string | null = sourceUrl;
  let eligibleCount = 0;

  // Use Savills' actual NextUrl from __NEXT_DATA__. The old synthetic /page/N
  // form now redirects to page one, which silently repeats cards. If Savills
  // reports further pages without an actual next URL, fail closed instead of
  // creating a partial monitor observation.
  while (url) {
    if (visitedUrls.has(url)) throw new Error(`Savills ${tx} pagination looped back to ${url}`);
    visitedUrls.add(url);
    const html = await savillsListHtml(url, !monitor);
    const pageObservation = detailObservation("savills_next_data_public_record", "live");
    const rows = savillsNextDataProperties(html).filter((row) => row?.IsCommercial === true);
    const pageInfo = savillsPageInfo(html, rows.length, !monitor);
    if (pageInfo.currentPage === null || pageInfo.totalItems === null) {
      throw new Error(`Savills ${tx} page did not expose complete pagination metadata`);
    }
    if (finalPage !== null && pageInfo.currentPage !== finalPage + 1) {
      throw new Error(
        `Savills ${tx} page sequence is not contiguous (${finalPage} -> ${pageInfo.currentPage})`
      );
    }
    if (declaredTotalPages !== null && pageInfo.currentPage > declaredTotalPages) {
      throw new Error(
        `Savills ${tx} page ${pageInfo.currentPage} exceeds declared page count ${declaredTotalPages}`
      );
    }
    if (total !== null && pageInfo.totalItems !== total) {
      throw new Error(
        `Savills ${tx} total changed during pagination (${total} -> ${pageInfo.totalItems})`
      );
    }
    if (declaredTotalPages !== null && pageInfo.totalPages !== null && pageInfo.totalPages !== declaredTotalPages) {
      throw new Error(
        `Savills ${tx} page count changed during pagination (${declaredTotalPages} -> ${pageInfo.totalPages})`
      );
    }
    total = pageInfo.totalItems;
    declaredTotalPages ??= pageInfo.totalPages;
    finalPage = pageInfo.currentPage;
    if ((pageInfo.totalItems ?? 0) > 0 && rows.length === 0) {
      throw new Error(`Savills ${tx} page ${pageInfo.currentPage ?? "?"} reported results but exposed no commercial rows`);
    }
    for (const row of rows) {
      const rawId = clean(row.ExternalPropertyID) ?? clean(row.ExternalPropertyIDFormatted);
      if (!rawId || seenRawIds.has(rawId)) continue;
      seenRawIds.add(rawId);
      const mapped = mapSavillsRow(row, tx, sourceUrl);
      if (!mapped) {
        if (savillsClearlyNonUsLocation(clean(row.AddressLine2))) {
          nonUsFiltered++;
        } else {
          unmappable++;
        }
        continue;
      }
      const listingId = mapped.id ?? mapped.url;
      if (seenListingIds.has(listingId)) continue;
      seenListingIds.add(listingId);
      eligibleCount++;
      if (listings.length < max) {
        listings.push({
          ...mapped,
          inventoryObservedAt: pageObservation.observedAt,
          detailObservedAt: pageObservation.observedAt,
          freshnessProvenance: {
            detailScope: "source_native_public_record",
            generationId: pageObservation.generationId,
            method: pageObservation.method,
            cacheDisposition: pageObservation.cacheDisposition,
          },
        });
      }
    }
    const next = pageInfo.nextUrl;
    if (!next) {
      if (declaredTotalPages !== null && pageInfo.currentPage < declaredTotalPages) {
        throw new Error(
          `Savills ${tx} reports ${declaredTotalPages} pages but page ${pageInfo.currentPage} exposes no NextUrl`
        );
      }
      break;
    }
    if (declaredTotalPages !== null && pageInfo.currentPage >= declaredTotalPages) {
      throw new Error(
        `Savills ${tx} final page ${pageInfo.currentPage}/${declaredTotalPages} unexpectedly exposes NextUrl`
      );
    }
    const nextUrl = new URL(next, "https://search.savills.com").toString();
    if (nextUrl === url) throw new Error(`Savills ${tx} page ${pageInfo.currentPage ?? "?"} returned a self-referential NextUrl`);
    url = nextUrl;
    console.error(`  savills/${tx}: ${listings.length} U.S. commercial rows collected (source total ${total ?? "?"})`);
  }
  if (total === null || finalPage === null || (declaredTotalPages !== null && finalPage !== declaredTotalPages)) {
    throw new Error(`Savills ${tx} pagination did not reach a verified final page`);
  }
  if (seenRawIds.size !== total) {
    throw new Error(
      `Savills ${tx} enumerated ${seenRawIds.size} unique commercial rows but provider reported ${total}`
    );
  }
  if (unmappable > 0 || eligibleCount + nonUsFiltered !== seenRawIds.size) {
    throw new Error(
      `Savills ${tx} could not classify ${unmappable} provider row(s) as U.S. or explicitly non-U.S. inventory`
    );
  }
  if (eligibleCount === 0 && nonUsFiltered > 0) {
    throw new Error(
      `Savills ${tx} nominal U.S. endpoint returned only ${nonUsFiltered} explicitly non-U.S. row(s)`
    );
  }
  const truncated =
    Number.isFinite(max) && eligibleCount > max
      ? true
      : undefined;

  return {
    company: "Savills",
    sourceUrl,
    method: "Direct server-rendered Savills __NEXT_DATA__ enumeration using provider NextUrl pagination (validated Firecrawl fallback)",
    totalAvailable: eligibleCount,
    listings,
    truncated,
    note: nonUsFiltered
      ? `${nonUsFiltered} non-U.S. commercial ${tx} row(s) filtered out after complete provider enumeration`
      : undefined,
  };
}

export async function srcSavillsCommercialLease(max: number): Promise<SourceResult> {
  return collectSavillsTransaction("lease", max, false);
}

export async function srcSavills(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  // Enumeration-only source: both the sale list pages and the lease __NEXT_DATA__
  // parse extract every field from the list page (no per-listing detail render),
  // so monitor output == full output.
  return collectSavillsTransaction(tx, max, monitor);
}
