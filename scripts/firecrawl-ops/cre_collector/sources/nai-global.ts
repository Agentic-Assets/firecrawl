// sources/nai-global.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import { createHash } from "node:crypto";
import { PAGE_CAP } from "../lib/config.js";
import { stripHtmlText, titleFromFilename } from "../lib/html.js";
import { harvestDetail } from "../lib/harvest.js";
import {
  detailObservation,
  generationMatches,
  requireFreshDetails,
} from "../lib/freshness.js";
import { parseAmountIgnoringCurrencyLabel, normBuildingClass } from "../lib/parse.js";
import { DetailObservation, LinkItem, ScrapedDoc, SourceResult, Tx } from "../types.js";
import { boundedInt, clean, num, pmap } from "../lib/util.js";


// --- NAI Global: Infabode public GraphQL feed ---

export const NAI_WIDGET_URL = "https://ab.infabode.com/nai-global/listings3";
export const NAI_PUBLIC_POST_URL = "https://infabode.com/graphql";
export const NAI_LISTING_URL_BASE = "https://infabode.com/services/listings";
// Infabode accepts 100 rows per public GraphQL page. Its widget uses 18, but
// retaining that UI-sized page makes a complete 116-office monitor take dozens
// of slow round trips. A bounded 100-row page preserves offset pagination
// while making the source practical to refresh.
export const NAI_PAGE_SIZE = boundedInt(process.env.NAI_PAGE_SIZE, 100, 18, 100);
// Infabode occasionally accepts a request then stalls while streaming the body.
// This must bound both headers and body consumption: timing out only `fetch()`
// leaves `res.text()` unbounded and can strand the entire enumeration pass.
export const NAI_GRAPHQL_TIMEOUT_MS = boundedInt(
  process.env.NAI_GRAPHQL_TIMEOUT_MS,
  30000,
  5000,
  60000
);
// The public API becomes unreliable when every member-office id is placed in
// one large filter. Enumerating the live widget offices in modest, disjoint
// batches returns the same public feed without making a partial source result
// look complete. Each batch is paginated to its own short page.
export const NAI_SOURCE_BATCH_SIZE = boundedInt(process.env.NAI_SOURCE_BATCH_SIZE, 40, 1, 40);
// Each batch is independently paginated. A small bounded fan-out makes the
// full public-feed pass finish in practical time without turning a source
// timeout into an unbounded concurrency storm.
export const NAI_ENUMERATION_CONCURRENCY = boundedInt(
  process.env.NAI_ENUMERATION_CONCURRENCY,
  2,
  1,
  3
);
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
export const NAI_SOURCE_DISCOVERY_TIMEOUT_MS = boundedInt(
  process.env.NAI_SOURCE_DISCOVERY_TIMEOUT_MS,
  15000,
  3000,
  60000
);
export const NAI_SOURCE_DISCOVERY_MAX_SCRIPTS = 20;
export const NAI_SOURCE_DISCOVERY_MAX_BODY_BYTES = 750_000;
export const NAI_SOURCE_DISCOVERY_MIN_IDS = Math.floor(NAI_SOURCE_IDS.length * 0.75);
export const NAI_SOURCE_DISCOVERY_MAX_IDS = 500;
export const NAI_SOURCE_DISCOVERY_PARSER = "next-static-nai-office-array-v1";

export type NaiSourceIdDiscovery = {
  sourceIds: number[];
  usedFallback: boolean;
  warning: string | null;
  provenance: {
    mode: "live-widget-js" | "documented-fallback";
    widgetUrl: string;
    scriptUrl: string | null;
    parser: string;
    sourceIdCount: number;
    sourceIdsSha256: string;
    configSha256: string | null;
  };
};

type NaiSourceIdDiscoveryOptions = {
  fetchImpl?: typeof fetch;
  strict?: boolean;
  widgetUrl?: string;
  timeoutMs?: number;
};

function naiSha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function naiDiscoveryError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

async function fetchNaiDiscoveryText(
  url: string,
  fetchImpl: typeof fetch,
  timeoutMs: number,
  referer = NAI_WIDGET_URL
): Promise<string> {
  const controller = new AbortController();
  let timeout: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_, reject) => {
    timeout = setTimeout(() => {
      controller.abort();
      reject(new Error(`request timed out after ${timeoutMs}ms`));
    }, timeoutMs);
  });
  try {
    const response = await Promise.race([
      fetchImpl(url, {
        headers: {
          referer,
          "user-agent": "Mozilla/5.0 CRE collector",
        },
        signal: controller.signal,
      }),
      deadline,
    ]);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const contentLength = Number(response.headers.get("content-length"));
    if (Number.isFinite(contentLength) && contentLength > NAI_SOURCE_DISCOVERY_MAX_BODY_BYTES) {
      throw new Error(`body exceeds ${NAI_SOURCE_DISCOVERY_MAX_BODY_BYTES} bytes`);
    }
    if (!response.body) return "";
    const reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let totalBytes = 0;
    while (true) {
      const { done, value } = await Promise.race([reader.read(), deadline]);
      if (done) break;
      totalBytes += value.byteLength;
      if (totalBytes > NAI_SOURCE_DISCOVERY_MAX_BODY_BYTES) {
        controller.abort();
        await reader.cancel();
        throw new Error(`body exceeds ${NAI_SOURCE_DISCOVERY_MAX_BODY_BYTES} bytes`);
      }
      chunks.push(value);
    }
    const body = new Uint8Array(totalBytes);
    let offset = 0;
    for (const chunk of chunks) {
      body.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return new TextDecoder().decode(body);
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

export function naiWidgetScriptUrls(html: string, widgetUrl = NAI_WIDGET_URL): string[] {
  const widget = new URL(widgetUrl);
  const urls: string[] = [];
  const seen = new Set<string>();
  for (const match of html.matchAll(/<script\b[^>]*\bsrc=["']([^"']+)["'][^>]*>/gi)) {
    const url = new URL(match[1]!, widget);
    if (
      url.origin !== widget.origin ||
      !url.pathname.startsWith("/_next/static/chunks/") ||
      !url.pathname.endsWith(".js") ||
      seen.has(url.href)
    ) {
      continue;
    }
    seen.add(url.href);
    urls.push(url.href);
    if (urls.length >= NAI_SOURCE_DISCOVERY_MAX_SCRIPTS) break;
  }
  return urls;
}

/**
 * Parse the widget's domestic NAI-office config without evaluating provider JS.
 * The domestic and international configs are emitted as contiguous arrays of
 * exact `{id,name,country}` objects. Only one current array is large enough to
 * satisfy the domestic-list sanity bound; ambiguity or malformed IDs fails.
 */
export function parseNaiSourceIdsFromScript(
  script: string,
  scriptUrl: string
): NaiSourceIdDiscovery | null {
  const objectPattern =
    String.raw`\{id:(\d+),name:"((?:\\.|[^"\\])*)",country:(?:null|"(?:\\.|[^"\\])*")\}`;
  const arrayPattern = new RegExp(String.raw`\[((?:${objectPattern},?)+)\]`, "g");
  const candidates: Array<{ literal: string; sourceIds: number[] }> = [];
  for (const match of script.matchAll(arrayPattern)) {
    const literal = match[0]!;
    const entries = [...literal.matchAll(new RegExp(objectPattern, "g"))];
    const sourceIds = entries.map((entry) => Number(entry[1]));
    const names = entries.map((entry) => entry[2]!);
    if (
      sourceIds.length >= NAI_SOURCE_DISCOVERY_MIN_IDS &&
      names.every((name) => /^NAI(?: |$)/.test(name)) &&
      names.includes("NAI Global")
    ) {
      candidates.push({ literal, sourceIds });
    }
  }
  if (!candidates.length) return null;
  if (candidates.length !== 1) {
    throw new Error(
      `ambiguous NAI office config in ${scriptUrl}: found ${candidates.length} sane-size arrays`
    );
  }
  const candidate = candidates[0]!;
  if (candidate.sourceIds.length > NAI_SOURCE_DISCOVERY_MAX_IDS) {
    throw new Error(
      `NAI office config in ${scriptUrl} has ${candidate.sourceIds.length} ids, above ${NAI_SOURCE_DISCOVERY_MAX_IDS}`
    );
  }
  if (
    candidate.sourceIds.some((id) => !Number.isSafeInteger(id) || id <= 0) ||
    new Set(candidate.sourceIds).size !== candidate.sourceIds.length
  ) {
    throw new Error(`NAI office config in ${scriptUrl} contains invalid or duplicate ids`);
  }
  return {
    sourceIds: candidate.sourceIds,
    usedFallback: false,
    warning: null,
    provenance: {
      mode: "live-widget-js",
      widgetUrl: NAI_WIDGET_URL,
      scriptUrl,
      parser: NAI_SOURCE_DISCOVERY_PARSER,
      sourceIdCount: candidate.sourceIds.length,
      sourceIdsSha256: naiSha256(candidate.sourceIds.join(",")),
      configSha256: naiSha256(candidate.literal),
    },
  };
}

function naiDocumentedSourceIdFallback(error: unknown, widgetUrl: string): NaiSourceIdDiscovery {
  const warning =
    `live NAI widget source-id discovery failed; using ${NAI_SOURCE_IDS.length} documented ids: ` +
    naiDiscoveryError(error);
  return {
    sourceIds: [...NAI_SOURCE_IDS],
    usedFallback: true,
    warning,
    provenance: {
      mode: "documented-fallback",
      widgetUrl,
      scriptUrl: null,
      parser: NAI_SOURCE_DISCOVERY_PARSER,
      sourceIdCount: NAI_SOURCE_IDS.length,
      sourceIdsSha256: naiSha256(NAI_SOURCE_IDS.join(",")),
      configSha256: null,
    },
  };
}

export async function discoverNaiSourceIds(
  options: NaiSourceIdDiscoveryOptions = {}
): Promise<NaiSourceIdDiscovery> {
  const fetchImpl = options.fetchImpl ?? fetch;
  const strict = options.strict ?? true;
  const widgetUrl = options.widgetUrl ?? NAI_WIDGET_URL;
  const timeoutMs = options.timeoutMs ?? NAI_SOURCE_DISCOVERY_TIMEOUT_MS;
  try {
    const html = await fetchNaiDiscoveryText(widgetUrl, fetchImpl, timeoutMs, widgetUrl);
    const scriptUrls = naiWidgetScriptUrls(html, widgetUrl);
    if (!scriptUrls.length) throw new Error("widget HTML contains no eligible Next.js script chunks");
    const scriptErrors: string[] = [];
    for (const scriptUrl of scriptUrls) {
      try {
        const script = await fetchNaiDiscoveryText(scriptUrl, fetchImpl, timeoutMs, widgetUrl);
        const discovered = parseNaiSourceIdsFromScript(script, scriptUrl);
        if (discovered) {
          discovered.provenance.widgetUrl = widgetUrl;
          const liveIds = new Set(discovered.sourceIds);
          const missingDocumentedIds = NAI_SOURCE_IDS.filter((id) => !liveIds.has(id));
          if (strict && missingDocumentedIds.length) {
            throw new Error(
              `live NAI office config omitted ${missingDocumentedIds.length} documented id(s): ` +
                missingDocumentedIds.join(",")
            );
          }
          return discovered;
        }
      } catch (error) {
        scriptErrors.push(`${scriptUrl}: ${naiDiscoveryError(error)}`);
      }
    }
    const suffix = scriptErrors.length ? `; ${scriptErrors.join("; ")}` : "";
    throw new Error(`no validated domestic NAI office config found in ${scriptUrls.length} chunks${suffix}`);
  } catch (error) {
    const wrapped = new Error(`NAI source organization discovery failed: ${naiDiscoveryError(error)}`);
    if (strict) throw wrapped;
    return naiDocumentedSourceIdFallback(wrapped, widgetUrl);
  }
}

let naiLiveSourceIdDiscovery: Promise<NaiSourceIdDiscovery> | null = null;

async function resolveNaiSourceIds(strict: boolean): Promise<NaiSourceIdDiscovery> {
  if (!naiLiveSourceIdDiscovery) {
    naiLiveSourceIdDiscovery = discoverNaiSourceIds({ strict: true });
  }
  try {
    return await naiLiveSourceIdDiscovery;
  } catch (error) {
    naiLiveSourceIdDiscovery = null;
    if (strict) throw error;
    return naiDocumentedSourceIdFallback(error, NAI_WIDGET_URL);
  }
}

export function naiStrictSourceDiscovery(monitor: boolean): boolean {
  if (!monitor) return true;
  return /^(1|true|yes)$/i.test(clean(process.env.NAI_STRICT_SOURCE_DISCOVERY) ?? "");
}

export function naiSourceDiscoveryNote(discovery: NaiSourceIdDiscovery): string {
  const provenance = discovery.provenance;
  const script = provenance.scriptUrl ? ` from ${provenance.scriptUrl}` : "";
  const configHash = provenance.configSha256 ? `; config sha256 ${provenance.configSha256}` : "";
  return (
    `${provenance.sourceIdCount} NAI source organization ids (${provenance.mode}${script}; ` +
    `${provenance.parser}; ids sha256 ${provenance.sourceIdsSha256}${configHash})` +
    (discovery.warning ? `; WARNING: ${discovery.warning}` : "")
  );
}

export const NAI_FEED_QUERY =
  "query GET_LISTINGS_PUBLIC_POSTS($filter: PostFilter, $offset: Int, $limit: Int) { publicPosts(filter: $filter, offset: $offset, limit: $limit) { id title summary content tags currency listingStatus price landSize sizeTotal sizeRangeH sizeRangeL url urlOriginal contactEmail urlDocument documentPreview updatedAt publishedAt contentType { id name } postImages { id url index } locations { id name geometry path } source { id socialLinks name bannerS3 logoS3(format: LOGO_100X100) } } }";

export const naiFeedPageCache = new Map<string, any[]>();
const naiFeedRowObservations = new WeakMap<object, DetailObservation>();

export function naiSourceIdBatches(sourceIds = NAI_SOURCE_IDS, batchSize = NAI_SOURCE_BATCH_SIZE): number[][] {
  const unique = [...new Set(sourceIds.filter((id) => Number.isInteger(id) && id > 0))];
  const boundedBatchSize = Math.max(1, Math.trunc(batchSize));
  const batches: number[][] = [];
  for (let index = 0; index < unique.length; index += boundedBatchSize) {
    batches.push(unique.slice(index, index + boundedBatchSize));
  }
  return batches;
}

export function naiFeedPageCacheKey(offset: number, sourceIds: number[] = NAI_SOURCE_IDS): string {
  return `${sourceIds.join(",")}:${offset}`;
}

/**
 * Infabode can repeat its final full page at later offsets instead of returning
 * a short page. The ordered post-id signature lets enumeration stop at that
 * verified replay instead of needlessly consuming the configured page cap.
 */
export function naiPageSignature(rows: any[]): string | null {
  const ids = rows
    .map((row) => {
      const id = row?.id;
      return typeof id === "string" || typeof id === "number" ? clean(String(id)) : null;
    })
    .filter((id): id is string => !!id);
  return ids.length === rows.length && ids.length > 0 ? ids.join(",") : null;
}

export function naiResultTruncated(
  max: number,
  selected: number,
  knownEligible: number,
  incompleteSourceBatches: number,
  unenumeratedSourceBatches: number
): boolean {
  return (
    incompleteSourceBatches > 0 ||
    (Number.isFinite(max) &&
      (selected < knownEligible || unenumeratedSourceBatches > 0))
  );
}

export async function naiGraphqlPost(
  url: string,
  body: any,
  referer: string,
  timeoutMs = NAI_GRAPHQL_TIMEOUT_MS
): Promise<any> {
  const controller = new AbortController();
  let timeout: ReturnType<typeof setTimeout> | undefined;
  const request = fetch(url, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      origin: new URL(referer).origin,
      referer,
      "user-agent": "Mozilla/5.0 CRE collector",
    },
    body: JSON.stringify(body),
    signal: controller.signal,
  });
  // Keep the deadline live through both fetch and response-body consumption.
  // `fetch()` can settle as soon as response headers arrive; clearing a timer
  // there would leave a stalled `res.text()` unbounded.
  const deadline = new Promise<never>((_, reject) => {
    timeout = setTimeout(() => {
      controller.abort();
      reject(new Error(`Infabode GraphQL request timed out after ${timeoutMs}ms`));
    }, timeoutMs);
  });
  let res: Response;
  let text: string;
  try {
    ({ res, text } = await Promise.race([
      request.then(async (response) => ({ res: response, text: await response.text() })),
      deadline,
    ]));
  } finally {
    if (timeout) clearTimeout(timeout);
  }
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

export async function fetchNaiFeedPage(offset: number, sourceIds: number[] = NAI_SOURCE_IDS): Promise<any[]> {
  const cacheKey = naiFeedPageCacheKey(offset, sourceIds);
  const cached = naiFeedPageCache.get(cacheKey);
  if (cached) return cached;
  const data = await naiGraphqlPost(
    NAI_PUBLIC_POST_URL,
    {
      operationName: "GET_LISTINGS_PUBLIC_POSTS",
      query: NAI_FEED_QUERY,
      variables: {
        offset,
        limit: NAI_PAGE_SIZE,
        filter: {
          contentTypesIds: [NAI_CONTENT_TYPE_BY_TX.sale, NAI_CONTENT_TYPE_BY_TX.lease],
          sourcesIds: sourceIds,
          locationsIds: [],
          title: "",
        },
      },
    },
    NAI_WIDGET_URL
  );
  if (!Array.isArray(data?.publicPosts)) {
    throw new Error("Infabode GraphQL response lacks a publicPosts array");
  }
  const rows = data.publicPosts;
  const observed = detailObservation("infabode_public_posts_graphql", "live");
  for (const row of rows) {
    if (row && typeof row === "object") naiFeedRowObservations.set(row, observed);
  }
  naiFeedPageCache.set(cacheKey, rows);
  return rows;
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

/**
 * The publicPosts feed includes historical posts. Keep one eligibility rule for
 * both full ingestion and monitoring so the two paths compare like-for-like.
 */
export function naiIsSourceEligible(row: any, tx: Tx): boolean {
  return (
    Number(row?.contentType?.id) === NAI_CONTENT_TYPE_BY_TX[tx] &&
    naiListingStatus(row) === "FOR_SALE_ON_MARKET"
  );
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

export function naiListingFromFeed(
  row: any,
  tx: Tx,
  detail: any,
  detailError: string | null,
  useFeedScalars = false,
  observation: DetailObservation | null = null
): any {
  // The publicPosts feed carries the public listing detail fields, including
  // price, size, contacts, documents, and the provider status label.
  const scalarSource = detail ?? (useFeedScalars ? row : null);
  const id = Number(row?.id);
  const sourceLocations = Array.isArray(detail?.locations) && detail.locations.length ? detail.locations : row?.locations;
  const loc = naiLocation(Array.isArray(sourceLocations) ? sourceLocations : []);
  const coords = detail?.locations?.[0]?.geometry?.coordinates;
  const priceText = naiPriceText(scalarSource);
  const docs = detailError ? [] : naiDocumentUrls(detail);
  // Capture-everything harvest runs only on a clean publicPosts row. The
  // unified `documents` channel supersedes `brochures` when harvest fires, so
  // the same urlDocument is not inserted twice.
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
  // Scalar lift from the publicPosts payload.
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
  const detailScalars = scalarSource
    ? {
        canonicalUrl: clean(scalarSource?.urlOriginal ?? scalarSource?.url),
        ...(tx === "sale"
          ? { salePriceUsd: parseAmountIgnoringCurrencyLabel(priceText) }
          : { leaseRateMin: parseAmountIgnoringCurrencyLabel(priceText) }),
        highlights: tags.length ? [...tags] : undefined,
        minDivisibleSf: num(scalarSource?.sizeRangeL) || undefined,
        maxDivisibleSf: num(scalarSource?.sizeRangeH) || undefined,
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
    sizeText: naiSizeText(scalarSource),
    buildingSizeSqft: num(scalarSource?.sizeTotal),
    lotSizeAcres: num(scalarSource?.landSize),
    // The publicPost size range maps to the available/divisible square-foot
    // columns cre_ingest.to_row carries. minDivisibleSf/maxDivisibleSf are set
    // by detailScalars above; availableSf remains the existing mapping.
    ...(scalarSource
      ? {
          availableSf:
            num(scalarSource?.sizeRangeL) ?? num(scalarSource?.sizeRangeH) ?? num(scalarSource?.sizeTotal),
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
    lastUpdated: clean(row?.updatedAt ?? row?.publishedAt),
    feedRow: row,
    publicPost: detail ?? undefined,
    detailError: detailError ?? undefined,
    // Inventory-only and failed-detail rows may carry useful current card
    // fields, but must not authorize wholesale replacement of last-good child
    // collections in the ingest path.
    preserveChildCollections: !detail || detailError ? true : undefined,
    inventoryObservedAt: observation?.observedAt,
    detailObservedAt: detail && !detailError ? observation?.observedAt : undefined,
    freshnessProvenance: observation
      ? {
          detailScope: detail && !detailError ? "source_native_public_record" : "inventory_only",
          generationId: observation.generationId,
          method: observation.method,
          cacheDisposition: observation.cacheDisposition,
        }
      : undefined,
    sourceOrganization: detail?.source ?? row?.source,
    sourceWebsiteUrl: clean(detail?.urlOriginal ?? detail?.url),
    sourceSocialLinks: Array.isArray(detail?.source?.socialLinks) ? detail.source.socialLinks : undefined,
    listingStatus: naiListingStatus(detail),
    tags: tags.length ? tags : undefined,
    providerCurrency: clean(detail?.currency),
    // Phase-2 scalar fields (detail-gated, additive):
    ...detailScalars,
  };
}

export async function srcNaiGlobal(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  const sourceIdDiscovery = await resolveNaiSourceIds(naiStrictSourceDiscovery(monitor));
  if (sourceIdDiscovery.warning) console.error(`  nai-global/${tx}: ${sourceIdDiscovery.warning}`);
  const sourceBatches = naiSourceIdBatches(sourceIdDiscovery.sourceIds);
  const collectBatch = async (sourceIds: number[], batchIndex: number) => {
    const batchRows: any[] = [];
    const seenPageSignatures = new Set<string>();
    let enumeratedFeedRows = 0;
    let stoppedOnShortPage = false;
    for (let offset = 0; offset < PAGE_CAP * NAI_PAGE_SIZE; offset += NAI_PAGE_SIZE) {
      const page = await fetchNaiFeedPage(offset, sourceIds);
      const signature = naiPageSignature(page);
      if (signature && seenPageSignatures.has(signature)) {
        if (requireFreshDetails()) {
          throw new Error(
            `NAI ${tx} office batch ${batchIndex + 1}/${sourceBatches.length} repeated ` +
              `a full page at offset ${offset} during strict freshness collection`
          );
        }
        console.error(
          `  nai-global/${tx}: office batch ${batchIndex + 1}/${sourceBatches.length}, API offset ${offset}, ` +
            "repeated page; treating the prior page as the end of the public feed"
        );
        stoppedOnShortPage = true;
        break;
      }
      if (signature) seenPageSignatures.add(signature);
      enumeratedFeedRows += page.length;
      // The public feed contains historical and inactive posts. Use the same
      // conservative eligibility rule on full and monitor paths so their
      // inventories cannot diverge and manufacture false monitor changes.
      const matching = page.filter((row: any) => naiIsSourceEligible(row, tx));
      batchRows.push(...matching);
      console.error(
        `  nai-global/${tx}: office batch ${batchIndex + 1}/${sourceBatches.length}, API offset ${offset}, ` +
          `${page.length} feed rows, ${batchRows.length} ${tx} collected`
      );
      if (page.length < NAI_PAGE_SIZE) {
        stoppedOnShortPage = true;
        break;
      }
    }
    return { rows: batchRows, enumeratedFeedRows, complete: stoppedOnShortPage };
  };

  // An unlimited run can enumerate disjoint source batches in a small bounded
  // fan-out. Capped runs stay sequential so a probe stops after enough
  // source-eligible rows have been found.
  const batchResults: Array<{ rows: any[]; enumeratedFeedRows: number; complete: boolean }> = [];
  if (Number.isFinite(max) && !requireFreshDetails()) {
    for (const [batchIndex, sourceIds] of sourceBatches.entries()) {
      const result = await collectBatch(sourceIds, batchIndex);
      batchResults.push(result);
      if (batchResults.reduce((count, batch) => count + batch.rows.length, 0) >= max) break;
    }
  } else {
    batchResults.push(
      ...(await pmap(sourceBatches, NAI_ENUMERATION_CONCURRENCY, (sourceIds, batchIndex) =>
        collectBatch(sourceIds, batchIndex)
      ))
    );
  }

  const eligibleRows: any[] = [];
  const seenPostIds = new Set<string>();
  for (const batch of batchResults) {
    for (const row of batch.rows) {
      const numericId = Number(row?.id);
      const id = Number.isFinite(numericId) ? String(numericId) : clean(row?.id);
      if (id && seenPostIds.has(id)) continue;
      if (id) seenPostIds.add(id);
      eligibleRows.push(row);
    }
  }
  const rows = eligibleRows.slice(0, Math.min(max, Number.MAX_SAFE_INTEGER));
  const incompleteSourceBatches = batchResults.filter((batch) => !batch.complete).length;
  const unenumeratedSourceBatches = sourceBatches.length - batchResults.length;
  const enumeratedFeedRows = batchResults.reduce((count, batch) => count + batch.enumeratedFeedRows, 0);
  const sourceBatchCoverage =
    batchResults.length === sourceBatches.length
      ? String(sourceBatches.length)
      : `${batchResults.length}/${sourceBatches.length}`;
  if (!rows.length) throw new Error(`no ${tx} listing rows found in NAI Global Infabode feed`);
  // A batch is complete only after its short page. Any page-cap hit makes the
  // source artifact explicitly truncated instead of allowing a monitor to
  // infer disappearances from an incomplete office subset. A finite
  // --max-items cap is also truncating whenever it slices known eligible rows
  // or stops before every provider-office batch has been enumerated.
  const truncated = naiResultTruncated(
    max,
    rows.length,
    eligibleRows.length,
    incompleteSourceBatches,
    unenumeratedSourceBatches
  );
  if (monitor) {
    // Monitor uses the same source-eligible inventory as the full path. It
    // neither activates a terminal status nor deactivates a listing; those
    // decisions remain outside the collector/monitor contract.
    const listings = rows.map((row) =>
      naiListingFromFeed(row, tx, row, null, false, naiFeedRowObservations.get(row) ?? null)
    );
    return {
      company: "NAI Global",
      sourceUrl: NAI_WIDGET_URL,
      method: "Infabode publicPosts GraphQL source-eligible enumeration with bulk public detail fields",
      totalAvailable: truncated ? null : listings.length,
      listings,
      truncated,
      note:
        `${naiSourceDiscoveryNote(sourceIdDiscovery)}. ` +
        "Monitor uses the same source-native public detail fields and FOR_SALE_ON_MARKET eligibility as full ingestion, without status activation or listing deactivation.",
    };
  }
  // publicPosts already carries the public detail fields needed for the normal
  // listing shape. A complete source refresh is now bounded by feed pages, not
  // by a potentially hours-long request-per-listing fanout.
  const listings = rows.map((row) => {
    const observation = naiFeedRowObservations.get(row) ?? null;
    if (requireFreshDetails() && (!observation || !generationMatches(observation.generationId))) {
      throw new Error(`NAI ${tx} row ${clean(row?.id) ?? "unknown"} lacks current-generation provenance`);
    }
    return naiListingFromFeed(row, tx, row, null, false, observation);
  });
  const activeListings = listings;
  return {
    company: "NAI Global",
    sourceUrl: NAI_WIDGET_URL,
    method: "Infabode bulk publicPosts GraphQL detail feed, offset paginated, filtered to FOR_SALE_ON_MARKET",
      totalAvailable: truncated ? null : activeListings.length,
    listings: activeListings,
    truncated,
    note:
      `${naiSourceDiscoveryNote(sourceIdDiscovery)}; stable Infabode IDs and detail URLs captured. ` +
      `Documents and contacts remain URL-only when public fields exist. ` +
      `Scanned ${enumeratedFeedRows} bulk public-detail rows across ${sourceBatchCoverage} office batches for ${tx}; ` +
      `retained ${activeListings.length} source-eligible on-market rows.`,
  };
}
