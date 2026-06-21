// sources/nai-global.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import { CONCURRENCY, PAGE_CAP } from "../lib/config.js";
import { stripHtmlText, titleFromFilename } from "../lib/html.js";
import { harvestDetail } from "../lib/harvest.js";
import { parseAmountIgnoringCurrencyLabel, normBuildingClass } from "../lib/parse.js";
import { LinkItem, ScrapedDoc, SourceResult, Tx } from "../types.js";
import { clean, num, pmap } from "../lib/util.js";


// --- NAI Global: Infabode public GraphQL feed ---

export const NAI_WIDGET_URL = "https://ab.infabode.com/nai-global/listings3";
export const NAI_PUBLIC_API_URL = "https://infabode.com/public_api";
export const NAI_PUBLIC_POST_URL = "https://infabode.com/graphql";
export const NAI_LISTING_URL_BASE = "https://infabode.com/services/listings";
export const NAI_PAGE_SIZE = 18;
export const NAI_DETAIL_CONCURRENCY = Math.min(CONCURRENCY, 2);
export const NAI_CONTENT_TYPE_BY_TX: Record<Tx, number> = { sale: 4, lease: 10 };
export const NAI_SOURCE_IDS = [
  99487, 99571, 99491, 99492, 84593, 99494, 99495, 84587, 99573, 161338, 84617, 268182,
  84557, 99574, 99499, 268184, 99500, 85394, 99501, 99502, 99503, 99577, 84594, 209408,
  99505, 99506, 77674, 99507, 99508, 85523, 99509, 85516, 99510, 77668, 99511, 99513,
  99514, 99516, 99517, 99518, 99519, 84585, 92844, 99520, 99581, 99521, 99522, 84591,
  99523, 77643, 99524, 99525, 77682, 85417, 99526, 77670, 99527, 99530, 99532, 200927,
  99533, 99534, 87675, 194245, 99536, 99537, 87673, 84622, 99538, 99540, 210201, 194610,
  99543, 77675, 86241, 87997, 149117, 234516, 99545, 99546, 92845, 99548, 99549, 99550,
  99583, 182876, 99551, 99531, 99552, 84621, 99486, 99554, 99555, 99556, 83286, 294858,
  268194, 99557, 92846, 77680, 99558, 99559, 99560, 268195, 99561, 99535, 99584, 99562,
  99563, 109852, 99498, 99566, 99567, 99569, 99585, 92843,
];

export const NAI_FEED_QUERY =
  "query GET_LISTINGS_POSTS($filter: PostFilter, $offset: Int, $limit: Int) { posts(filter: $filter, offset: $offset, limit: $limit) { id title summary publishedAt locations { id path } contentType { id name } source { id name logoS3(format: LOGO_300X300) bannerS3 } postImages { id url } } }";
export const NAI_DETAIL_QUERY =
  "query publicPost($id: Int!) { publicPost(id: $id) { id title summary content tags currency listingStatus price landSize sizeTotal sizeRangeH sizeRangeL urlOriginal contactEmail urlDocument documentPreview contentType { id name } postImages { id url index } locations { id name geometry path } source { id socialLinks name bannerS3 logoS3(format: LOGO_100X100) } } }";

export const naiFeedPageCache = new Map<number, any[]>();

export async function naiGraphqlPost(url: string, body: any, referer: string): Promise<any> {
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      origin: new URL(referer).origin,
      referer,
      "user-agent": "Mozilla/5.0 CRE collector",
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(30000),
  });
  const text = await res.text();
  let parsed: any = null;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error(`Infabode GraphQL returned non-JSON HTTP ${res.status}`);
  }
  if (!res.ok || parsed.errors?.length) {
    const msg = parsed.errors?.map((e: any) => e?.message).filter(Boolean).join("; ");
    throw new Error(`Infabode GraphQL HTTP ${res.status}${msg ? `: ${msg}` : ""}`);
  }
  return parsed.data;
}

export async function fetchNaiFeedPage(offset: number): Promise<any[]> {
  const cached = naiFeedPageCache.get(offset);
  if (cached) return cached;
  const data = await naiGraphqlPost(
    NAI_PUBLIC_API_URL,
    {
      query: NAI_FEED_QUERY,
      variables: {
        offset,
        limit: NAI_PAGE_SIZE,
        filter: {
          content_types_ids: [NAI_CONTENT_TYPE_BY_TX.sale, NAI_CONTENT_TYPE_BY_TX.lease],
          indSectorsIds: [],
          sourcesIds: NAI_SOURCE_IDS,
          locationsIds: [],
          title: "",
        },
      },
    },
    NAI_WIDGET_URL
  );
  const rows = Array.isArray(data?.posts) ? data.posts : [];
  naiFeedPageCache.set(offset, rows);
  return rows;
}

export async function fetchNaiPublicPost(id: number): Promise<any> {
  const data = await naiGraphqlPost(
    NAI_PUBLIC_POST_URL,
    { query: NAI_DETAIL_QUERY, variables: { id } },
    `${NAI_LISTING_URL_BASE}/${id}`
  );
  return data?.publicPost ?? null;
}

export function naiLocation(partsSource: any[]): { city: string | null; state: string | null; country: string | null } {
  const path = clean(partsSource[0]?.path ?? partsSource[0]?.name);
  const parts = path ? path.split(",").map((p) => clean(p)).filter(Boolean) : [];
  return {
    city: parts[0] ?? null,
    state: parts.find((p) => /^[A-Z]{2}$/.test(p ?? "")) ?? null,
    country: parts.find((p) => /^United States$/i.test(p ?? "")) ?? "US",
  };
}

export function naiImageUrls(row: any, detail: any): string[] {
  const urls = [...(detail?.postImages ?? []), ...(row?.postImages ?? [])]
    .map((img: any) => clean(img?.url))
    .filter((url: string | null): url is string => !!url && /^https?:\/\//i.test(url));
  return [...new Set(urls)];
}

export function naiDocumentUrls(detail: any): string[] {
  const urls = [detail?.urlDocument, detail?.documentPreview]
    .map((url) => clean(url))
    .filter((url: string | null): url is string => !!url && /^https?:\/\//i.test(url));
  return [...new Set(urls)];
}

export function naiPriceText(detail: any): string | null {
  if (detail?.price === null || detail?.price === undefined) return null;
  const value = String(detail.price);
  const currency = clean(detail.currency);
  return currency ? `${currency} ${value}` : value;
}

export function naiSizeText(detail: any): string | null {
  const pieces = [
    num(detail?.sizeTotal) ? `${detail.sizeTotal} SF` : null,
    num(detail?.sizeRangeL) || num(detail?.sizeRangeH)
      ? `${detail.sizeRangeL ?? "?"}-${detail.sizeRangeH ?? "?"} SF`
      : null,
    num(detail?.landSize) ? `${detail.landSize} acres land` : null,
  ].filter(Boolean);
  return pieces.length ? pieces.join("; ") : null;
}

/**
 * Derive building class from the NAI tags array.
 * Infabode tags carry explicit "BuildingClassA", "BuildingClassB", "BuildingClassC"
 * tokens. Returns the first matched class letter via normBuildingClass, or null.
 */
export function naiBuildingClassFromTags(tags: any): "A" | "B" | "C" | "D" | null {
  if (!Array.isArray(tags)) return null;
  for (const tag of tags) {
    const s = typeof tag === "string" ? tag.trim() : null;
    if (!s) continue;
    // Match "BuildingClassA", "BuildingClassB", "BuildingClassC" (case-insensitive).
    const m = s.match(/^BuildingClass([A-Da-d])$/i);
    if (m) return normBuildingClass(m[1]) as "A" | "B" | "C" | "D" | null;
  }
  return null;
}

export function naiListingStatus(detail: any): string | null {
  const value = detail?.listingStatus;
  if (Array.isArray(value)) {
    const statuses = value.map((status) => clean(status)).filter(Boolean);
    return statuses.length ? statuses.join(",") : null;
  }
  return clean(value);
}

// Harvest media/links/documents from the Infabode publicPost detail payload.
// There is no rendered detail page (the source is a GraphQL JSON API), so a
// minimal ScrapedDoc is synthesized from detail.content (HTML that may embed a
// video/tour iframe) and detail.postImages, then the stranded structured fields
// are promoted via ctx.extra*: urlDocument/documentPreview -> documents,
// urlOriginal -> external_listing link, source.socialLinks -> social links.
// Pure (no network); never throws (harvestDetail is guarded). Returns nothing
// useful when detailError is set or detail is absent.
export function harvestNai(row: any, detail: any): {
  media: any[];
  links: any[];
  documents: any[];
  images: string[];
} {
  if (!detail || typeof detail !== "object") return { media: [], links: [], documents: [], images: [] };
  const content = typeof detail.content === "string" ? detail.content : "";
  const images = naiImageUrls(row, detail);
  const docUrls = naiDocumentUrls(detail);
  const extraLinks: (LinkItem | string)[] = [];
  const original = clean(detail.urlOriginal);
  if (original) extraLinks.push({ url: original, rel: null, linkType: "external_listing" });
  for (const s of Array.isArray(detail?.source?.socialLinks) ? detail.source.socialLinks : []) {
    const u =
      typeof s === "string" ? clean(s) : clean(s?.url) ?? clean(s?.href) ?? clean(s?.link);
    if (u) extraLinks.push(u); // re-classified (facebook/linkedin/... -> social)
  }
  const synthetic: ScrapedDoc = { rawHtml: content, markdown: "", links: [], images };
  const r = harvestDetail(synthetic, {
    extraDocs: docUrls,
    extraImages: images,
    extraLinks,
  });
  return { media: r.media, links: r.links, documents: r.documents, images: r.images };
}

export function naiListingFromFeed(row: any, tx: Tx, detail: any, detailError: string | null): any {
  const id = Number(row?.id);
  const sourceLocations = Array.isArray(detail?.locations) && detail.locations.length ? detail.locations : row?.locations;
  const loc = naiLocation(Array.isArray(sourceLocations) ? sourceLocations : []);
  const coords = detail?.locations?.[0]?.geometry?.coordinates;
  const priceText = naiPriceText(detail);
  const docs = detailError ? [] : naiDocumentUrls(detail);
  // Capture-everything harvest runs ONLY on a clean detail touch (a real
  // publicPost payload with no detailError). In monitor mode detail is null and
  // in the full path a failed detail carries detailError, so harvest is skipped
  // and the emitted object stays byte-identical to the prior shape (the spread
  // below adds nothing). The unified `documents` channel supersedes `brochures`
  // when harvest fires, so the same urlDocument is not inserted twice.
  const harvested = !detailError && detail ? harvestNai(row, detail) : null;
  const contacts =
    !detailError && clean(detail?.contactEmail)
      ? [
          {
            name: null,
            email: clean(detail.contactEmail),
            company: clean(detail?.source?.name ?? row?.source?.name) ?? "NAI Global",
            isPrimary: true,
          },
        ]
      : [];
  // Phase-2 scalar lift (detail-gated so monitor/failed-detail output stays byte-identical).
  // canonicalUrl: sourceWebsiteUrl == publicPost.urlOriginal.
  // salePrice / leaseRate: publicPost.price keyed by transactionMode using
  //   parseAmountIgnoringCurrencyLabel (provider sends 'POUND' label on USD values).
  // highlights: tags[] array passed through as-is.
  // minDivisibleSf / maxDivisibleSf: publicPost.sizeRangeL / sizeRangeH (non-zero).
  // buildingClass: tags BuildingClassA/B/C via naiBuildingClassFromTags.
  // extraFacts: listingOffice and sourceOrganization.name for long-tail capture.
  // statusBadge: NOT emitted — publicPost.listingStatus is contaminated (lease rows
  //   carry ['FOR_SALE_ON_MARKET']); skip entirely per the gap doc DQ guard.
  const tags = Array.isArray(detail?.tags) ? detail.tags : [];
  const detailScalars = detail
    ? {
        canonicalUrl: clean(detail?.urlOriginal),
        ...(tx === "sale"
          ? { salePriceUsd: parseAmountIgnoringCurrencyLabel(priceText) }
          : { leaseRateMin: parseAmountIgnoringCurrencyLabel(priceText) }),
        highlights: tags.length ? [...tags] : undefined,
        minDivisibleSf: num(detail?.sizeRangeL) || undefined,
        maxDivisibleSf: num(detail?.sizeRangeH) || undefined,
        buildingClass: naiBuildingClassFromTags(tags) ?? undefined,
        extraFacts: (() => {
          const facts: Record<string, string> = {};
          // listing_office: the NAI member organization name (e.g. 'NAI Excel').
          const office = clean(detail?.source?.name ?? row?.source?.name);
          if (office) facts["listing_office"] = office;
          // source_organization_name: only distinct from listing_office when the
          // sourceOrganization has a different name than the feed source.
          const sourceOrg = clean((detail?.source ?? row?.source)?.name);
          if (sourceOrg && sourceOrg !== office) facts["source_organization_name"] = sourceOrg;
          return Object.keys(facts).length ? facts : undefined;
        })(),
      }
    : {};

  return {
    id: Number.isFinite(id) ? `infabode:${id}` : null,
    name: clean(detail?.title ?? row?.title),
    transactionType: tx === "sale" ? "Sale" : "Lease",
    assetType: clean(detail?.contentType?.name ?? row?.contentType?.name),
    description: stripHtmlText(detail?.content) ?? clean(detail?.summary ?? row?.summary),
    city: loc.city,
    state: loc.state,
    country: loc.country,
    latitude: Array.isArray(coords) ? num(coords[1]) : null,
    longitude: Array.isArray(coords) ? num(coords[0]) : null,
    salePriceText: tx === "sale" ? priceText : null,
    leaseRateText: tx === "lease" ? priceText : null,
    sizeText: naiSizeText(detail),
    buildingSizeSqft: num(detail?.sizeTotal),
    lotSizeAcres: num(detail?.landSize),
    // Stranded structured-field lift: the publicPost size range maps to the
    // available / divisible square-foot columns cre_ingest.to_row carries. Gated
    // on a present detail so a monitor pass (detail=null) emits no new keys and
    // stays byte-identical. minDivisibleSf/maxDivisibleSf are set by detailScalars
    // below (Phase-2); availableSf remains here as the existing mapping.
    ...(detail
      ? {
          availableSf: num(detail?.sizeRangeL) ?? num(detail?.sizeRangeH) ?? num(detail?.sizeTotal),
        }
      : {}),
    listingOffice: clean(detail?.source?.name ?? row?.source?.name),
    sourceCompany: clean(detail?.source?.name ?? row?.source?.name),
    brokerIds: [],
    contactsDetailed: contacts,
    // When harvest fired (clean detail), documents ride the unified channel and
    // brochures is dropped to avoid a duplicate insert; otherwise keep the legacy
    // brochures so monitor/failed-detail output is byte-identical.
    ...(harvested
      ? {
          brochures: undefined,
          documents: harvested.documents,
          media: harvested.media,
          links: harvested.links,
          markdown:
            typeof detail?.content === "string" && stripHtmlText(detail.content)
              ? stripHtmlText(detail.content)
              : undefined,
        }
      : { brochures: docs.map((url) => ({ name: titleFromFilename(url), url })) }),
    photos: harvested && harvested.images.length ? harvested.images : naiImageUrls(row, detail),
    url: Number.isFinite(id) ? `${NAI_LISTING_URL_BASE}/${id}` : NAI_WIDGET_URL,
    lastUpdated: clean(row?.publishedAt),
    feedRow: row,
    publicPost: detail ?? undefined,
    detailError: detailError ?? undefined,
    sourceOrganization: detail?.source ?? row?.source,
    sourceWebsiteUrl: clean(detail?.urlOriginal),
    sourceSocialLinks: Array.isArray(detail?.source?.socialLinks) ? detail.source.socialLinks : undefined,
    listingStatus: naiListingStatus(detail),
    tags: tags.length ? tags : undefined,
    providerCurrency: clean(detail?.currency),
    // Phase-2 scalar fields (detail-gated, additive):
    ...detailScalars,
  };
}

export async function srcNaiGlobal(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  const targetContentType = NAI_CONTENT_TYPE_BY_TX[tx];
  const rows: any[] = [];
  let stoppedOnShortPage = false;
  for (let offset = 0; offset < PAGE_CAP * NAI_PAGE_SIZE && rows.length < max; offset += NAI_PAGE_SIZE) {
    const page = await fetchNaiFeedPage(offset);
    const matching = page.filter((row: any) => Number(row?.contentType?.id) === targetContentType);
    rows.push(...matching.slice(0, Math.max(0, max - rows.length)));
    console.error(
      `  nai-global/${tx}: API offset ${offset}, ${page.length} feed rows, ${rows.length} ${tx} collected`
    );
    if (page.length < NAI_PAGE_SIZE) {
      stoppedOnShortPage = true;
      break;
    }
  }
  if (!rows.length) throw new Error(`no ${tx} listing rows found in NAI Global Infabode feed`);
  // The loop ends for exactly one of three reasons: a short page (real feed
  // end, the normal termination), rows.length >= max (intentional --max-items),
  // or offset reaching the PAGE_CAP ceiling. Only the last is truncation: the
  // last fetched page was full (no short-page break) yet more feed remained, so
  // this pass KNOWINGLY left rows unread. Not flagged on natural exhaustion or
  // an intentional max cap, so it is true only on a real PAGE_CAP clip.
  const truncated = !stoppedOnShortPage && rows.length < max;
  if (monitor) {
    // Monitor mode: emit feed rows only (detail=null) and skip both the per-row
    // publicPost detail GraphQL fetch and the detail-dependent
    // FOR_SALE_ON_MARKET active filter. id (infabode:<id>) matches the full-path
    // external id; status and price are detail-only and thus absent here.
    const listings = rows.map((row) => naiListingFromFeed(row, tx, null, null));
    return {
      company: "NAI Global",
      sourceUrl: NAI_WIDGET_URL,
      method: "Infabode public GraphQL feed enumeration only (monitor mode; publicPost detail enrichment and FOR_SALE_ON_MARKET filter skipped)",
      totalAvailable: stoppedOnShortPage ? listings.length : null,
      listings,
      truncated,
      note: "Monitor mode: feed fields only (id, name, location, lastUpdated/publishedAt, photos, url). listingStatus and price are detail-only, so the FOR_SALE_ON_MARKET filter is deferred and off-market rows may be emitted (resolved downstream on render).",
    };
  }
  let detailFailures = 0;
  const listings = await pmap(rows, NAI_DETAIL_CONCURRENCY, async (row) => {
    const id = Number(row?.id);
    if (!Number.isFinite(id)) {
      detailFailures++;
      return naiListingFromFeed(row, tx, null, "missing numeric Infabode post id");
    }
    try {
      const detail = await fetchNaiPublicPost(id);
      return naiListingFromFeed(row, tx, detail, null);
    } catch (err) {
      detailFailures++;
      return naiListingFromFeed(row, tx, null, String(err));
    }
  });
  const activeListings = listings.filter((listing) => listing.listingStatus === "FOR_SALE_ON_MARKET");
  const skippedInactiveOrUnknown = listings.length - activeListings.length;
  return {
    company: "NAI Global",
    sourceUrl: NAI_WIDGET_URL,
    method: "Infabode public GraphQL feed plus publicPost detail enrichment, offset paginated, filtered to FOR_SALE_ON_MARKET",
    totalAvailable: stoppedOnShortPage ? activeListings.length : null,
    listings: activeListings,
    truncated,
    note:
      `${NAI_SOURCE_IDS.length} documented NAI source organization ids; stable Infabode IDs and detail URLs captured. ` +
      `Documents and contacts remain URL-only when public fields exist. ` +
      `Scanned ${rows.length} public feed rows for ${tx}; retained ${activeListings.length} on-market rows, ` +
      `skipped ${skippedInactiveOrUnknown} inactive/unknown-status rows, detail failures skipped: ${detailFailures}.`,
  };
}
