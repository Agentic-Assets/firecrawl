import { publishPyramidPage } from "../lib/source-evidence/pyramid-page.js";
import { recordDirectProviderAttemptStarted, recordDirectProviderAttemptCompleted } from "../lib/performance.js";
import { observationNow } from "../lib/observation-clock.js";
import * as cheerio from "cheerio";
import { createHash } from "node:crypto";
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { detailObservation, refreshGenerationId, refreshStartedAt, requireFreshDetails, requireFreshPropertyDetails } from "../lib/freshness.js";
import { dedupeStrings, stripHtmlText, titleFromFilename } from "../lib/html.js";
import { PyramidCurrentInventoryProof, PyramidInventoryPass, SourceResult, Tx } from "../types.js";
import { parseMoney, parseSizeText } from "../lib/parse.js";
import { clean, pmap, prune } from "../lib/util.js";

export const PYRAMID_HOST = "https://www.pyramidbrokerage.com";
export const PYRAMID_INVENTORY_URL =
  `${PYRAMID_HOST}/wp-json/wp/v2/pbc-listings`;
export const PYRAMID_LISTINGS_PAGE = PYRAMID_INVENTORY_URL;
export const PYRAMID_PAGE_SIZE = 100;
export const PYRAMID_SALE_TERM_ID = 246;
export const PYRAMID_LEASE_TERM_ID = 249;
export const PYRAMID_MAX_RESPONSE_BYTES = 32 * 1024 * 1024;
const PYRAMID_USER_AGENT = "Mozilla/5.0 (compatible; AgenticAssetsCRE/1.0)";

type FetchLike = (
  input: string | URL | Request,
  init?: RequestInit
) => Promise<Response>;

export type PyramidSnapshot = {
  total: number;
  totalPages: number;
  observedAt: string;
  rows: any[];
  passes: PyramidInventoryPass[];
};

type PyramidInventoryPageBinding = [number, string, string];
type PyramidTransactionEvidence =
  | { kind: "html_property_fact"; transaction: Tx; value: string }
  | { kind: "wordpress_sale_type"; transaction: Tx; inventoryPage: PyramidInventoryPageBinding };

export type PyramidDetail = {
  postId: number;
  canonicalUrl: string | null;
  street: string | null;
  mapQueries: string[];
  facts: Record<string, string>;
  transactionFacts: string[];
  description: string | null;
  brochures: Array<{ name: string | null; url: string }>;
  photos: string[];
  contacts: Array<{
    name: string | null;
    title: string | null;
    email: string | null;
    phone: string | null;
    office: string | null;
    profileUrl: string | null;
    avatarUrl: string | null;
  }>;
};

function pyramidAbsoluteUrl(value: unknown): string | null {
  const raw = clean(value);
  if (!raw || /^(?:javascript|mailto|tel):/i.test(raw)) return null;
  try {
    const url = new URL(raw, PYRAMID_HOST);
    if (
      url.protocol !== "https:"
      || url.hostname !== "www.pyramidbrokerage.com"
      || url.username
      || url.password
      || url.port
    ) {
      return null;
    }
    return url.toString();
  } catch {
    return null;
  }
}

function pyramidListingKey(value: unknown): string {
  const owned = pyramidAbsoluteUrl(value);
  const path = owned ? new URL(owned).pathname : "";
  if (!path.startsWith("/listings/") || !path.slice("/listings/".length).replace(/\/+$/g, "")) {
    throw new Error("Pyramid listing URL has no owned property path");
  }
  // Historical keys are the literal URL path: do not decode or fold case.
  return path.replace(/^\/+|\/+$/g, "");
}

function pyramidPropertyImageUrl(value: unknown): string | null {
  const raw = clean(value);
  if (!raw) return null;
  try {
    const url = new URL(raw, PYRAMID_HOST);
    if (url.protocol !== "https:" || url.username || url.password || url.port
        || !["www.pyramidbrokerage.com", "cdn.photos.sparkplatform.com"].includes(url.hostname)) return null;
    return url.toString();
  } catch {
    return null;
  }
}

export function pyramidInventoryUrl(page: number): string {
  if (!Number.isInteger(page) || page < 1) {
    throw new Error(`Pyramid inventory page must be a positive integer, got ${page}`);
  }
  const url = new URL(PYRAMID_INVENTORY_URL);
  url.searchParams.set("per_page", String(PYRAMID_PAGE_SIZE));
  url.searchParams.set("page", String(page));
  url.searchParams.set("orderby", "id");
  url.searchParams.set("order", "asc");
  url.searchParams.set("_embed", "wp:term");
  return url.toString();
}

async function pyramidBoundedResponseText(
  response: Response,
  maxBytes: number
): Promise<string> {
  const declaredLength = Number(response.headers.get("content-length"));
  if (Number.isFinite(declaredLength) && declaredLength > maxBytes) {
    throw new Error(`Pyramid response exceeds ${maxBytes} bytes`);
  }
  if (!response.body) return "";
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let total = 0;
  let text = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maxBytes) {
        await reader.cancel();
        throw new Error(`Pyramid response exceeds ${maxBytes} bytes`);
      }
      text += decoder.decode(value, { stream: true });
    }
    text += decoder.decode();
    return text;
  } finally {
    reader.releaseLock();
  }
}

export const PYRAMID_DENIED_EXIT = { 401: 76, 403: 77 } as const;
export class PyramidAccessDenied extends Error {
  constructor(readonly status: 401 | 403) {
    super(`Pyramid access denied: HTTP ${status}`);
  }
}
type PyramidAccess = { denial?: PyramidAccessDenied };
function checkAccess(access: PyramidAccess): void {
  if (access.denial) throw access.denial;
}

export async function pyramidFetch(
  url: string,
  fetchImpl: FetchLike,
  timeoutMs = 30_000,
  attempts = 2,
  maxBytes = PYRAMID_MAX_RESPONSE_BYTES,
  access: PyramidAccess = {}
): Promise<{ headers: Headers; body: string; status: number }> {
  const initialUrl = pyramidAbsoluteUrl(url);
  if (!initialUrl) throw new Error(`Pyramid refused unsafe URL ${url}`);
  const role = new URL(initialUrl).pathname === new URL(PYRAMID_INVENTORY_URL).pathname
    ? "current_archive" : "detail";
  let lastError: unknown;
  for (let attempt = 1; attempt <= attempts; attempt++) {
    checkAccess(access);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    const telemetry = recordDirectProviderAttemptStarted(role, "pyramid-brokerage");
    let status: number | undefined;
    try {
      let current = initialUrl;
      for (let redirect = 0; redirect <= 3; redirect++) {
        checkAccess(access);
        const safeUrl = pyramidAbsoluteUrl(current);
        if (!safeUrl) throw new Error(`Pyramid refused unsafe redirect URL ${current}`);
        status = undefined;
        const response = await fetchImpl(safeUrl, {
          headers: { "User-Agent": PYRAMID_USER_AGENT, Accept: "application/json,text/html" },
          redirect: "manual",
          signal: controller.signal,
        });
        status = response.status;
        if (status === 401 || status === 403) {
          access.denial ??= new PyramidAccessDenied(status);
          controller.abort();
          throw access.denial;
        }
        checkAccess(access);
        if (response.status >= 300 && response.status < 400) {
          const location = response.headers.get("location");
          if (!location) throw new Error(`Pyramid redirect ${response.status} lacks Location`);
          const target = pyramidAbsoluteUrl(new URL(location, safeUrl).toString());
          if (!target) throw new Error(`Pyramid refused unsafe redirect target ${location}`);
          current = target;
          continue;
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const body = await pyramidBoundedResponseText(response, maxBytes);
        recordDirectProviderAttemptCompleted(telemetry, { outcome: "succeeded", status });
        return { headers: response.headers, body, status: response.status };
      }
      throw new Error("Pyramid exceeded redirect cap");
    } catch (error) {
      recordDirectProviderAttemptCompleted(telemetry, {
        outcome: "failed", status, error,
      });
      checkAccess(access);
      lastError = error;
      if (attempt === attempts) break;
      await new Promise((resolve) => setTimeout(resolve, 500 * attempt));
    } finally {
      clearTimeout(timeout);
    }
  }
  throw new Error(`Pyramid request failed for ${url}: ${String(lastError)}`);
}

function requiredPositiveHeader(headers: Headers, name: string): number {
  const value = Number(headers.get(name));
  if (!Number.isInteger(value) || value < 1) {
    throw new Error(`Pyramid inventory response omitted valid ${name}`);
  }
  return value;
}

export function assertPyramidSnapshot(
  rows: any[],
  total: number,
  totalPages: number
): void {
  if (rows.length !== total) {
    throw new Error(`Pyramid inventory incomplete: collected ${rows.length}/${total}`);
  }
  if (totalPages !== Math.ceil(total / PYRAMID_PAGE_SIZE)) {
    throw new Error(
      `Pyramid page contract changed: ${total} rows require ` +
        `${Math.ceil(total / PYRAMID_PAGE_SIZE)} pages, provider reported ${totalPages}`
    );
  }

  const ids = new Set<number>();
  const canonicalPaths = new Set<string>();
  let sale = 0;
  let lease = 0;
  for (const row of rows) {
    const id = Number(row?.id);
    if (!Number.isInteger(id) || id < 1) {
      throw new Error("Pyramid inventory contains a row without a stable WordPress ID");
    }
    if (ids.has(id)) throw new Error(`Pyramid inventory contains duplicate WordPress ID ${id}`);
    ids.add(id);
    if (row?.status !== "publish" || row?.type !== "pbc-listings") {
      throw new Error(`Pyramid WordPress row ${id} is not a published pbc-listings record`);
    }
    const link = pyramidAbsoluteUrl(row?.link);
    if (!link || !new URL(link).pathname.startsWith("/listings/")) {
      throw new Error(`Pyramid WordPress row ${id} has an invalid detail URL`);
    }
    const canonicalPath = pyramidListingKey(link);
    if (canonicalPaths.has(canonicalPath)) {
      throw new Error(`Pyramid inventory contains duplicate canonical listing path ${canonicalPath}`);
    }
    canonicalPaths.add(canonicalPath);
    const saleTypes = Array.isArray(row?.["sale-type"]) ? row["sale-type"].map(Number) : [];
    if (saleTypes.length !== 1) {
      throw new Error(`Pyramid WordPress row ${id} has ambiguous sale-type taxonomy`);
    }
    if (saleTypes[0] === PYRAMID_SALE_TERM_ID) sale++;
    else if (saleTypes[0] === PYRAMID_LEASE_TERM_ID) lease++;
    else throw new Error(`Pyramid WordPress row ${id} has unknown sale-type ${saleTypes[0]}`);
  }
  if (sale + lease !== total) {
    throw new Error(`Pyramid sale/lease taxonomy partition is incomplete (${sale}+${lease} != ${total})`);
  }
}

function stableJsonValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableJsonValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, stableJsonValue((value as Record<string, unknown>)[key])])
    );
  }
  return value;
}

function pyramidSnapshotFingerprint(rows: any[]): string {
  return JSON.stringify(stableJsonValue(rows));
}

function textSha256(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

async function fetchPyramidSnapshotPass(
  fetchImpl: FetchLike = fetch, pass = 1, access: PyramidAccess = {}
): Promise<PyramidSnapshot & { fingerprint: string }> {
  const first = await pyramidFetch(pyramidInventoryUrl(1), fetchImpl, 30_000, 2, PYRAMID_MAX_RESPONSE_BYTES, access);
  const firstObservedAt = observationNow();
  publishPyramidPage(first.body, pyramidInventoryUrl(1), 1, pass, first.status, first.headers);
  const total = requiredPositiveHeader(first.headers, "X-WP-Total");
  const totalPages = requiredPositiveHeader(first.headers, "X-WP-TotalPages");
  let firstRows: any;
  try {
    firstRows = JSON.parse(first.body);
  } catch (error) {
    throw new Error(`Pyramid first inventory page returned malformed JSON: ${String(error)}`);
  }
  if (!Array.isArray(firstRows) || firstRows.length === 0) {
    throw new Error("Pyramid first inventory page returned no rows");
  }
  const pages = await pmap(
    Array.from({ length: Math.max(0, totalPages - 1) }, (_, index) => index + 2),
    Math.min(CONCURRENCY, 2),
    async (page) => {
      const response = await pyramidFetch(pyramidInventoryUrl(page), fetchImpl, 30_000, 2, PYRAMID_MAX_RESPONSE_BYTES, access);
      publishPyramidPage(response.body, pyramidInventoryUrl(page), page, pass, response.status, response.headers);
      const pageTotal = requiredPositiveHeader(response.headers, "X-WP-Total");
      const pageCount = requiredPositiveHeader(response.headers, "X-WP-TotalPages");
      if (pageTotal !== total || pageCount !== totalPages) {
        throw new Error(
          `Pyramid inventory changed during pagination on page ${page} ` +
            `(${pageTotal}/${pageCount} vs ${total}/${totalPages})`
        );
      }
      let value: any;
      try {
        value = JSON.parse(response.body);
      } catch (error) {
        throw new Error(`Pyramid inventory page ${page} returned malformed JSON: ${String(error)}`);
      }
      if (!Array.isArray(value) || value.length === 0) {
        throw new Error(`Pyramid inventory page ${page}/${totalPages} returned no rows`);
      }
      return { rows: value, body: response.body, observedAt: observationNow() };
    }
  );
  const captured = [{ rows: firstRows, body: first.body, observedAt: firstObservedAt }, ...pages];
  const rows = captured.flatMap((page) => page.rows);
  assertPyramidSnapshot(rows, total, totalPages);
  rows.sort((left, right) => Number(left.id) - Number(right.id));
  return {
    total,
    totalPages,
    observedAt: observationNow(),
    rows,
    fingerprint: pyramidSnapshotFingerprint(rows),
    passes: [{
      fingerprint: textSha256(pyramidSnapshotFingerprint(rows)),
      pages: captured.map((page, index) => [index + 1, pyramidInventoryUrl(index + 1), page.observedAt,
        textSha256(page.body), total, totalPages, page.rows.map((row: any) => Number(row.id))]),
    }],
  };
}

export async function fetchPyramidSnapshot(
  fetchImpl: FetchLike = fetch,
  maxPasses = 3,
  access: PyramidAccess = {}
): Promise<PyramidSnapshot> {
  if (!Number.isInteger(maxPasses) || maxPasses < 2 || maxPasses > 5) {
    throw new Error("Pyramid convergence passes must be an integer from 2 to 5");
  }
  let previous: Awaited<ReturnType<typeof fetchPyramidSnapshotPass>> | null = null;
  for (let pass = 1; pass <= maxPasses; pass++) {
    let current: Awaited<ReturnType<typeof fetchPyramidSnapshotPass>>;
    try {
      current = await fetchPyramidSnapshotPass(fetchImpl, pass, access);
    } catch (error) {
      checkAccess(access);
      throw error;
    }
    if (
      previous
      && current.total === previous.total
      && current.totalPages === previous.totalPages
      && current.fingerprint === previous.fingerprint
    ) {
      return {
        total: current.total,
        totalPages: current.totalPages,
        observedAt: current.observedAt,
        rows: current.rows,
        passes: [...previous.passes, ...current.passes],
      };
    }
    previous = current;
  }
  throw new Error(
    `Pyramid inventory did not converge across ${maxPasses} complete ordered passes`
  );
}

export function pyramidDetailTransaction(value: unknown, postId: number, expected?: Tx): Tx {
  const normalized = typeof value === "string" ? value.trim().toLowerCase() : "";
  const transaction = normalized === "sale" ? "sale"
    : normalized === "lease" || normalized === "sublease" ? "lease" : null;
  const literal = JSON.stringify(String(value).slice(0, 80));
  if (!transaction) {
    throw new Error(`Pyramid detail ${postId} has unsupported Sale/Lease fact ${literal}`);
  }
  if (expected && transaction !== expected) {
    throw new Error(`Pyramid detail ${postId} Sale/Lease fact ${literal} disagrees with its WordPress taxonomy (${expected})`);
  }
  return transaction;
}

function pyramidTenureLabel(value: string): boolean {
  return value.trim().replace(/:\s*$/, "").replace(/\s+/g, "").toLowerCase() === "sale/lease";
}

function pyramidSaleTypeTerms(row: any): any[] {
  const groups = row?._embedded?.["wp:term"];
  return Array.isArray(groups) ? groups.flat().filter((term: any) => term?.taxonomy === "sale-type")
    .map(({ id, taxonomy, name, slug }: any) => ({ id, taxonomy, name, slug })) : [];
}

function pyramidTransactionEvidence(row: any, detail: PyramidDetail, tx: Tx,
  inventoryPage?: PyramidInventoryPageBinding): PyramidTransactionEvidence {
  const id = Number(row.id);
  if (detail.transactionFacts.length) {
    if (detail.transactionFacts.length !== 1) throw new Error(`Pyramid detail ${id} has duplicate Sale/Lease facts`);
    const value = detail.transactionFacts[0];
    return { kind: "html_property_fact", transaction: pyramidDetailTransaction(value, id, tx), value };
  }
  const terms = pyramidSaleTypeTerms(row);
  const term = terms[0];
  const expectedId = tx === "sale" ? PYRAMID_SALE_TERM_ID : PYRAMID_LEASE_TERM_ID;
  if (!inventoryPage || row.status !== "publish" || row.type !== "pbc-listings"
      || detail.postId !== id || detail.canonicalUrl !== pyramidAbsoluteUrl(row.link)
      || !Array.isArray(row["sale-type"]) || row["sale-type"].length !== 1
      || row["sale-type"][0] !== expectedId || terms.length !== 1 || term.id !== expectedId
      || typeof term.name !== "string" || term.name.trim().toLowerCase() !== tx
      || typeof term.slug !== "string" || term.slug.trim().toLowerCase() !== tx) {
    throw new Error(`Pyramid detail ${id} absent Sale/Lease fact lacks bound literal WordPress tenure evidence`);
  }
  return { kind: "wordpress_sale_type", transaction: tx, inventoryPage };
}

function pyramidFactMap($: cheerio.CheerioAPI): Record<string, string> {
  const facts: Record<string, string> = {};
  $(".property-details li").each((_, element) => {
    const label = clean($(element).find(".name").first().text().replace(/:\s*$/, ""));
    const value = clean($(element).find(".value").first().text());
    if (label && value) facts[label] = value;
  });
  return facts;
}

function pyramidMapQueries($: cheerio.CheerioAPI): string[] {
  const queries = new Set<string>();
  $("#content .listings-map iframe[src]").each((_, element) => {
    try {
      const url = new URL($(element).attr("src") ?? "");
      if (url.origin !== "https://www.google.com" || url.pathname !== "/maps/embed/v1/place"
          || url.username || url.password || url.port || url.hash || url.searchParams.getAll("q").length !== 1) return;
      const query = url.searchParams.get("q")?.trim();
      if (query) queries.add(query);
    } catch {
      // An absent/unsupported map does not authorize inferred geography.
    }
  });
  return [...queries];
}

function pyramidPropertyStreet(queries: string[]): string | null {
  const addresses = queries.filter((query) => !/^-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?$/.test(query));
  if (addresses.length > 1) throw new Error("Pyramid detail has conflicting property map addresses");
  return addresses[0] ?? null;
}

export function parsePyramidDetail(html: string, expectedPostId?: number): PyramidDetail {
  const shell = cheerio.load(html);
  const shellTitle = clean(shell("title").text());
  const shortBody = html.length < 2_000 ? clean(shell("body").text()) : null;
  if (
    !html.trim() ||
    /just a moment|captcha|access denied|page not found|404 not found/i.test(
      shellTitle ?? shortBody ?? ""
    )
  ) {
    throw new Error("Pyramid detail returned an empty, challenge, or missing-page shell");
  }
  const $ = shell;
  const shortlink = clean($('link[rel="shortlink"]').attr("href"));
  const postId = pyramidShortlinkPostId(shortlink);
  if (postId === null) {
    throw new Error("Pyramid detail omitted its stable WordPress shortlink ID");
  }
  if (expectedPostId !== undefined && postId !== expectedPostId) {
    throw new Error(`Pyramid detail identity mismatch: expected ${expectedPostId}, observed ${postId}`);
  }
  const facts = pyramidFactMap($);
  const transactionFacts: string[] = [];
  $(".property-details li").each((_, element) => {
    const labels = $(element).find(".name");
    if (!labels.toArray().some((label) => pyramidTenureLabel($(label).text()))) return;
    const values = $(element).find(".value");
    if (labels.length !== 1 || values.length !== 1) {
      throw new Error(`Pyramid detail ${postId} has malformed Sale/Lease property fact`);
    }
    transactionFacts.push(values.text().trim());
  });
  if (!Object.keys(facts).length) throw new Error(`Pyramid detail ${postId} omitted property facts`);
  const description = clean($(".property-content").first().text());
  const brochures = dedupeStrings(
    $(".more-info a[href]")
      .map((_, element) => pyramidAbsoluteUrl($(element).attr("href")) ?? "")
      .get()
      .filter(Boolean)
  ).map((url) => ({ name: titleFromFilename(url), url }));
  const photos = dedupeStrings(
    $("#content .featured-image img, #content .image-gallery [data-full], #content .property-gallery img, #content .property-gallery [data-bg-image], #content .property-slider img")
      .map((_, element) => $(element).attr("data-full") ?? $(element).attr("src")
        ?? $(element).attr("data-src") ?? $(element).attr("data-bg-image"))
      .get()
      .map((value) => {
        const match = clean(value)?.match(/url\(['"]?([^'")]+)['"]?\)/i)?.[1] ?? value;
        return pyramidPropertyImageUrl(match);
      })
      .filter((value): value is string => Boolean(value))
  );
  const contacts = $(".agent-contact-list > li")
    .map((_, element) => {
      const card = $(element);
      const details = card.find(".details").first();
      const names = details
        .find(".name")
        .map((__, item) => clean($(item).text()))
        .get()
        .filter(Boolean);
      const titles = details
        .find(".title")
        .map((__, item) => clean($(item).text()))
        .get()
        .filter(Boolean);
      return {
        name: names[0] ?? null,
        office: names[1] ?? null,
        title: titles.join(", ") || null,
        email:
          clean(details.find('a[href^="mailto:"]').attr("href")?.replace(/^mailto:/i, "").split("?")[0]) ??
          null,
        phone: clean(details.find('a[href^="tel:"]').text()) ?? null,
        profileUrl: pyramidAbsoluteUrl(details.find("a.name[href]").attr("href")),
        avatarUrl: pyramidAbsoluteUrl(card.find("img").attr("src") ?? card.find("img").attr("data-src")),
      };
    })
    .get()
    .filter((contact) => contact.name || contact.email);
  const mapQueries = pyramidMapQueries($);
  return { postId, canonicalUrl: pyramidAbsoluteUrl($('link[rel="canonical"]').attr("href")), street: pyramidPropertyStreet(mapQueries), mapQueries, facts, transactionFacts, description, brochures, photos, contacts };
}

function pyramidShortlinkPostId(value: unknown): number | null {
  const raw = clean(value);
  if (!raw) return null;
  try {
    const url = new URL(raw);
    if (
      url.origin !== PYRAMID_HOST
      || url.pathname !== "/"
      || url.hash
      || url.username
      || url.password
      || url.port
      || url.searchParams.size !== 1
      || url.searchParams.getAll("p").length !== 1
    ) {
      return null;
    }
    const rawId = url.searchParams.get("p");
    const id = rawId && /^[1-9]\d*$/.test(rawId) ? Number(rawId) : NaN;
    return Number.isSafeInteger(id) ? id : null;
  } catch {
    return null;
  }
}

function pyramidTransaction(row: any): Tx {
  const term = Number(Array.isArray(row?.["sale-type"]) ? row["sale-type"][0] : NaN);
  if (term === PYRAMID_SALE_TERM_ID) return "sale";
  if (term === PYRAMID_LEASE_TERM_ID) return "lease";
  throw new Error(`Pyramid row ${row?.id ?? "?"} has unknown sale-type`);
}

function pyramidFeaturedPhoto(row: any): string | null {
  const media = row?._embedded?.["wp:featuredmedia"];
  const yoastImage = Array.isArray(row?.yoast_head_json?.og_image)
    ? row.yoast_head_json.og_image[0]?.url
    : null;
  return pyramidPropertyImageUrl(
    (Array.isArray(media) ? media[0]?.source_url : null)
      ?? (Number(row?.featured_media) > 0 ? yoastImage : null)
  );
}

// Pyramid states building area ("Building Size") apart from available space
// ("Available", "Smallest/Largest Available"), every value as "N +/-SF" or
// "N+/- Acres". Building area alone may reach size_sf: an available-space value
// rides availableSf, and only an acreage-only availability (land offered) is
// passed on as sizeText so the ingest can route it to lot_size_sf.
export function pyramidSizeFacts(facts: Record<string, string> | undefined): {
  sizeText?: string;
  buildingSizeSqft?: number;
  availableSf?: number;
} {
  const building = clean(facts?.["Building Size"]);
  const buildingSize = parseSizeText(building);
  const available = [facts?.Available, facts?.["Largest Available"], facts?.["Smallest Available"]]
    .map((value) => clean(value))
    .filter((value): value is string => Boolean(value) && /\b(?:sf|sq|ac|acres?)\b/i.test(value!))
    .map((text) => ({ text, ...parseSizeText(text) }));
  const availableSf = available.find((value) => value.sizeSf !== null)?.sizeSf;
  const acreageOnly = available.find((value) => value.lotSf !== null && value.sizeSf === null)?.text;
  const sizeText = buildingSize.sizeSf !== null || buildingSize.lotSf !== null ? building : acreageOnly;
  return {
    ...(sizeText ? { sizeText } : {}),
    ...(buildingSize.sizeSf !== null ? { buildingSizeSqft: buildingSize.sizeSf } : {}),
    ...(availableSf != null ? { availableSf } : {}),
  };
}

function pyramidEmbeddedTerms(row: any, taxonomy: string): string[] {
  const groups = Array.isArray(row?._embedded?.["wp:term"]) ? row._embedded["wp:term"] : [];
  return groups
    .flat()
    .filter((term: any) => term?.taxonomy === taxonomy)
    .map((term: any) => clean(stripHtmlText(String(term?.name ?? ""))))
    .filter((value: string | null): value is string => Boolean(value));
}

export function mapPyramidListing(
  row: any,
  tx: Tx,
  inventoryObservedAt: string,
  detail?: PyramidDetail,
  detailObservedAt?: string,
  inventoryPage?: PyramidInventoryPageBinding,
  emitBrokers = true
): any {
  const id = Number(row?.id);
  if (pyramidTransaction(row) !== tx) {
    throw new Error(`Pyramid row ${id} does not belong to requested ${tx} inventory`);
  }
  const transactionEvidence = detail ? pyramidTransactionEvidence(row, detail, tx, inventoryPage) : undefined;
  const title = clean(stripHtmlText(String(row?.title?.rendered ?? "")));
  const description =
    detail?.description ?? clean(stripHtmlText(String(row?.content?.rendered ?? "")));
  const priceText = detail?.facts?.Price ?? null;
  const brokers = (emitBrokers ? detail?.contacts ?? [] : [])
    .map((contact) =>
      brokerRef({
        name: contact.name,
        email: contact.email,
        phone: contact.phone,
        office: contact.office,
        avatarUrl: contact.avatarUrl,
        company: "Cushman & Wakefield | Pyramid Brokerage Company",
      })
    )
    .filter((value): value is number => value !== null);
  const detailObservationValue = detailObservedAt
    ? detailObservation("pyramid_direct_detail_html", "live", detailObservedAt)
    : null;
  return prune({
    id: pyramidListingKey(row?.link),
    name: detail?.facts?.["Property Name"] ?? title,
    headline: title,
    transactionType: tx === "sale" ? "Sale" : "Lease",
    assetType:
      detail?.facts?.Subuse ??
      detail?.facts?.["Major Use"] ??
      pyramidEmbeddedTerms(row, "property-use").join(", "),
    description,
    street: detail?.street,
    mapQueryEvidence: detail?.mapQueries.length ? { values: detail.mapQueries, method: "property_map_embed_query" } : undefined,
    state: pyramidEmbeddedTerms(row, "state")[0],
    city: pyramidEmbeddedTerms(row, "city")[0],
    county: pyramidEmbeddedTerms(row, "county")[0],
    salePriceUsd: tx === "sale" ? parseMoney(priceText) : null,
    salePriceText: tx === "sale" ? priceText : null,
    leaseRateText: tx === "lease" ? priceText : null,
    ...pyramidSizeFacts(detail?.facts),
    brokerIds: brokers,
    contactsDetailed: detail?.contacts,
    brochures: detail?.brochures,
    photos: detail ? detail.photos : dedupeStrings([pyramidFeaturedPhoto(row)].filter(Boolean) as string[]),
    url: pyramidAbsoluteUrl(row?.link),
    canonicalUrl: pyramidAbsoluteUrl(row?.link),
    lastUpdated: clean(row?.modified_gmt ?? row?.modified),
    markdown: description,
    inventoryObservedAt,
    detailObservedAt,
    freshnessProvenance: detailObservationValue
      ? {
          detailScope: "detail_page",
          generationId: detailObservationValue.generationId,
          method: detailObservationValue.method,
          cacheDisposition: detailObservationValue.cacheDisposition,
          identityMethod: "literal_owned_listing_path+wordpress_post_id",
        }
      : {
          detailScope: "inventory_only",
          generationId: refreshGenerationId(),
          method: "pyramid_wordpress_rest_inventory",
          cacheDisposition: "live",
          identityMethod: "literal_owned_listing_path+wordpress_post_id",
        },
    preserveChildCollections: detail ? undefined : true,
    wordpressRecord: {
      id,
      link: row?.link,
      status: row?.status,
      type: row?.type,
      saleTypeTerms: pyramidSaleTypeTerms(row),
      slug: clean(row?.slug),
      modified: clean(row?.modified_gmt ?? row?.modified),
      saleType: row?.["sale-type"],
      propertyUse: row?.["property-use"],
      city: row?.city,
      county: row?.county,
      state: row?.state,
    },
    detailFacts: detail?.facts,
    detailTransactionFacts: detail ? { count: detail.transactionFacts.length, values: detail.transactionFacts } : undefined,
    transactionEvidence,
  });
}

export async function srcPyramidBrokerage(
  tx: Tx,
  max: number,
  monitor: boolean
): Promise<SourceResult> {
  if (monitor && requireFreshPropertyDetails()) {
    throw new Error("Pyramid fresh property details require full mode, not monitor mode");
  }
  const generationId = refreshGenerationId();
  const generationStartedAt = refreshStartedAt();
  const proofEnabled = !monitor && !Number.isFinite(max) && Boolean(generationId && generationStartedAt);
  const subset = proofEnabled && requireFreshDetails();
  const key = proofEnabled ? JSON.stringify([generationId, generationStartedAt, subset]) : null;
  const access: PyramidAccess = {};
  let snapshot: PyramidSnapshot;
  if (key) {
    if (generationSnapshot?.key !== key) {
      generationSnapshot = { key, outcome: fetchPyramidSnapshot(fetch, 3, access).then(
        (snapshot): PyramidSnapshotOutcome => {
          freezeSnapshot(snapshot);
          return { kind: "success", snapshot };
        },
        (error): PyramidSnapshotOutcome => ({ kind: "failure", error })
      ) };
    }
    const outcome = await generationSnapshot.outcome;
    if (outcome.kind === "failure") throw outcome.error;
    snapshot = outcome.snapshot;
  } else {
    snapshot = await fetchPyramidSnapshot(fetch, 3, access);
  }
  const eligible = snapshot.rows.filter((row) => pyramidTransaction(row) === tx);
  const selected = eligible.slice(0, Math.min(max, eligible.length));
  const truncated = selected.length < eligible.length;

  if (monitor) {
    return {
      company: "Cushman & Wakefield | Pyramid Brokerage Company",
      sourceUrl: PYRAMID_LISTINGS_PAGE,
      method: "exact WordPress REST inventory (monitor; detail HTML skipped)",
      totalAvailable: eligible.length,
      truncated,
      listings: selected.map((row) => mapPyramidListing(row, tx, snapshot.observedAt)),
      note:
        `exact ${snapshot.total}-record/${snapshot.totalPages}-page WordPress snapshot; ` +
        "disappearance is eligible only when truncated=false",
    };
  }

  const details: PyramidCurrentInventoryProof["details"] = [];
  const incomplete: NonNullable<PyramidCurrentInventoryProof["incomplete"]> = [];
  let listings: any[];
  try {
    listings = await pmap(selected, Math.min(CONCURRENCY, 4), async (row) => {
    const url = pyramidAbsoluteUrl(row.link);
    if (!url) throw new Error(`Pyramid row ${row.id} has no valid detail URL`);
    const response = await pyramidFetch(url, fetch, 30_000, 2, PYRAMID_MAX_RESPONSE_BYTES, access);
    const html = response.body;
    const observedAt = observationNow();
    const detail = parsePyramidDetail(html, Number(row.id));
    if (proofEnabled && detail.canonicalUrl !== url) throw new Error("Pyramid detail canonical URL disagrees with inventory");
    const page = proofEnabled ? snapshot.passes[1].pages.find((page) => page[6].includes(Number(row.id))) : undefined;
    const mapped = mapPyramidListing(row, tx, snapshot.observedAt, detail, observedAt,
      page ? [page[0], page[2], page[3]] : undefined, true);
    if (proofEnabled) {
      const missing = (["state", "city", "assetType"] as const)
        .filter((field) => typeof mapped[field] !== "string" || !mapped[field].trim());
      if (missing.length && !subset) {
        throw new Error(`Pyramid strict proof lacks named location/property taxonomy for post ${Number(row.id)}: ${missing.join(", ")}`);
      }
      const digest = textSha256(html);
      mapped.wordpressRecord.detailResponseSha256 = digest;
      details.push([Number(row.id), detail.canonicalUrl!, observedAt, digest]);
      if (subset && missing.length) {
        mapped.preserveChildCollections = true;
        mapped.detailObservedWithChildPreservation = true;
        mapped.missingFieldEvidence = { fields: missing, method: "source_detail_and_taxonomy" };
        incomplete.push({ postId: Number(row.id), missingFields: missing });
      }
    }
    return mapped;
    });
  } catch (error) {
    checkAccess(access);
    throw error;
  }
  return {
    company: "Cushman & Wakefield | Pyramid Brokerage Company",
    sourceUrl: PYRAMID_LISTINGS_PAGE,
    method: "exact WordPress REST inventory plus bounded direct detail HTML",
    totalAvailable: eligible.length,
    truncated: truncated || (subset && incomplete.length > 0),
    listings: listings.filter(Boolean),
    ...(proofEnabled ? { currentInventoryProof: {
      schema: incomplete.length ? "pyramid-incomplete-v2" as const : "pyramid-strict-detail-v1" as const, sourceKey: "pyramid-brokerage" as const,
      transaction: tx, generationId: generationId!, generationStartedAt: generationStartedAt!,
      total: snapshot.total, totalPages: snapshot.totalPages, observedAt: snapshot.observedAt,
      passes: snapshot.passes,
      cohort: snapshot.rows.map((row) => [Number(row.id), pyramidAbsoluteUrl(row.link)!, pyramidListingKey(row.link), pyramidTransaction(row)] as [number, string, string, Tx]),
      details: details.sort((a, b) => a[0] - b[0]),
      ...(incomplete.length ? { incomplete: incomplete.sort((a, b) => a.postId - b.postId) } : {}),
    } } : {}),
    note:
      `exact ${snapshot.total}-record/${snapshot.totalPages}-page WordPress snapshot; ` +
      "disappearance is eligible only when truncated=false",
  };
}

type PyramidSnapshotOutcome =
  | { kind: "success"; snapshot: PyramidSnapshot }
  | { kind: "failure"; error: unknown };
let generationSnapshot: { key: string; outcome: Promise<PyramidSnapshotOutcome> } | null = null;

function freezeSnapshot(value: any): void {
  if (!value || typeof value !== "object" || Object.isFrozen(value)) return;
  for (const child of Object.values(value)) freezeSnapshot(child);
  Object.freeze(value);
}
