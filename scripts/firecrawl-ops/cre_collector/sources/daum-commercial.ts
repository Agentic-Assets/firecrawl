import * as cheerio from "cheerio";
import { brokerRef } from "../lib/broker.js";
import { detailObservation } from "../lib/freshness.js";
import { dedupeStrings, stripHtmlText, titleFromFilename } from "../lib/html.js";
import { SourceResult, Tx } from "../types.js";
import { clean, moneyToNumber, prune } from "../lib/util.js";

export const DAUM_HOST = "https://daumcommercial.com";
export const DAUM_SEARCH_URL = `${DAUM_HOST}/property-search/`;
export const DAUM_PAGE_SIZE = 90;
export const DAUM_ROBOTS_DELAY_MS = 3000;
export const DAUM_MAX_RESPONSE_BYTES = 5_000_000;
const DAUM_USER_AGENT = "Mozilla/5.0 (compatible; AgenticAssetsCRE/1.0)";

type FetchLike = (
  input: string | URL | Request,
  init?: RequestInit
) => Promise<Response>;

export type DaumTenure = "sale" | "lease" | "sale_or_lease" | "unknown";

export type DaumInventoryRow = {
  url: string;
  title: string;
  tenure: DaumTenure;
  tenureText: string | null;
  city: string | null;
  state: string | null;
  postalCode: string | null;
  sizeText: string | null;
  spaceCount: number | null;
  assetType: string | null;
  salePriceText: string | null;
  leaseRateText: string | null;
  brokerNames: string[];
  imageUrl: string | null;
  latitude: number | null;
  longitude: number | null;
};

export type DaumPage = {
  reportedTotal: number;
  reportedPages: number;
  rows: DaumInventoryRow[];
};

export type DaumDetail = {
  postId: number;
  facts: Record<string, string>;
  title: string;
  tenure: DaumTenure;
  tenureText: string | null;
  city: string | null;
  state: string | null;
  postalCode: string | null;
  salePriceText: string | null;
  leaseRateText: string | null;
  sizeText: string | null;
  description: string | null;
  highlights: string[];
  brochures: Array<{ name: string | null; url: string }>;
  photos: string[];
  contacts: Array<{
    name: string | null;
    email: string | null;
    phone: string | null;
    office: string | null;
    profileUrl: string | null;
    avatarUrl: string | null;
    license: string | null;
  }>;
};

async function daumBoundedResponseText(
  response: Response,
  maxBytes: number
): Promise<string> {
  const contentLength = Number(response.headers.get("content-length"));
  if (Number.isFinite(contentLength) && contentLength > maxBytes) {
    throw new Error(`response Content-Length ${contentLength} exceeds ${maxBytes} bytes`);
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
        throw new Error(`response body exceeds ${maxBytes} bytes`);
      }
      text += decoder.decode(value, { stream: true });
    }
    text += decoder.decode();
    return text;
  } finally {
    reader.releaseLock();
  }
}

export function daumPageUrl(page: number): string {
  if (!Number.isInteger(page) || page < 1) {
    throw new Error(`DAUM page must be a positive integer, got ${page}`);
  }
  return page === 1 ? DAUM_SEARCH_URL : `${DAUM_SEARCH_URL}page/${page}/`;
}

export function canonicalDaumPropertyUrl(value: unknown): string | null {
  const raw = clean(value);
  if (!raw || /^(?:javascript|mailto|tel):/i.test(raw)) return null;
  try {
    const url = new URL(raw, DAUM_HOST);
    if (
      url.protocol !== "https:" ||
      url.hostname !== "daumcommercial.com" ||
      url.username ||
      url.password ||
      url.port ||
      !/^\/property\/[^/]+\/$/.test(url.pathname) ||
      url.search ||
      url.hash
    ) {
      return null;
    }
    return url.toString();
  } catch {
    return null;
  }
}

function daumAbsoluteUrl(value: unknown): string | null {
  const raw = clean(value);
  if (!raw || /^(?:javascript|mailto|tel):/i.test(raw)) return null;
  try {
    const url = new URL(raw, DAUM_HOST);
    if (
      url.protocol !== "https:"
      || url.hostname !== "daumcommercial.com"
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

export function daumTenure(value: unknown): DaumTenure {
  const text = clean(value)?.toLowerCase();
  if (!text) return "unknown";
  if (text === "lease or sale" || text === "sale or lease") return "sale_or_lease";
  if (text === "lease" || text === "sublease") return "lease";
  if (text === "sale- user" || text === "sale- investment" || text === "sale") return "sale";
  throw new Error(`DAUM inventory contains unknown transaction type ${JSON.stringify(value)}`);
}

function parseDaumMapData(html: string): any[] {
  const match = html.match(/\bvar\s+propertySearchData\s*=\s*(\[[\s\S]*?\]);/);
  if (!match) throw new Error("DAUM page omitted or malformed embedded propertySearchData");
  try {
    const value = JSON.parse(match[1]);
    if (!Array.isArray(value)) throw new Error("not an array");
    return value;
  } catch (error) {
    throw new Error(`DAUM propertySearchData is malformed: ${String(error)}`);
  }
}

function daumBackgroundUrl(value: unknown): string | null {
  const raw = clean(value);
  const extracted = raw?.match(/url\(['"]?([^'")]+)['"]?\)/i)?.[1] ?? raw;
  return daumAbsoluteUrl(extracted);
}

function positiveNumber(value: unknown): number | null {
  const match = clean(value)?.replace(/,/g, "").match(/\d+(?:\.\d+)?/);
  const number = match ? Number(match[0]) : NaN;
  return Number.isFinite(number) && number > 0 ? number : null;
}

function signedNumber(value: unknown): number | null {
  const number = Number(value);
  return Number.isFinite(number) && number !== 0 ? number : null;
}

function meaningfulMoney(value: unknown): string | null {
  const text = clean(value);
  const amount = moneyToNumber(text);
  return amount && amount > 0 ? text : null;
}

export function parseDaumSearchPage(html: string): DaumPage {
  const shell = cheerio.load(html);
  const shellTitle = clean(shell("title").text());
  const shortBody = html.length < 2_000 ? clean(shell("body").text()) : null;
  if (
    !html.trim() ||
    /just a moment|captcha|access denied|page not found|404 not found/i.test(
      shellTitle ?? shortBody ?? ""
    )
  ) {
    throw new Error("DAUM search returned an empty, challenge, or missing-page shell");
  }
  const $ = shell;
  const totalText = clean($(".results-list-controls .results").first().text());
  const reportedTotal = Number(totalText?.replace(/,/g, "").match(/\d+/)?.[0]);
  if (!Number.isInteger(reportedTotal) || reportedTotal < 1) {
    throw new Error("DAUM search omitted a positive result total");
  }
  const paginationPages = $(".pagination a[href], .pagination .current")
    .map((_, element) => {
      const href = clean($(element).attr("href"));
      const fromPath = href?.match(/\/property-search\/page\/(\d+)\/$/)?.[1];
      const fromText = clean($(element).text())?.match(/^\d+$/)?.[0];
      return Number(fromPath ?? fromText);
    })
    .get()
    .filter((value) => Number.isInteger(value) && value > 0);
  const reportedPages = Math.max(
    Math.ceil(reportedTotal / DAUM_PAGE_SIZE),
    ...paginationPages
  );
  const mapRows = parseDaumMapData(html);
  const mapByUrl = new Map<string, any>();
  for (const row of mapRows) {
    const url = canonicalDaumPropertyUrl(row?.single_post_link ?? row?.["link-to"]);
    if (!url) throw new Error("DAUM propertySearchData contains an invalid or query-bearing URL");
    if (mapByUrl.has(url)) throw new Error(`DAUM propertySearchData contains duplicate URL ${url}`);
    mapByUrl.set(url, row);
  }

  const rows: DaumInventoryRow[] = [];
  $(".results-list.wrap-row > .item").each((_, element) => {
    const card = $(element);
    const url = canonicalDaumPropertyUrl(
      card.find('a.photo[href], .content a.red-text[href]').first().attr("href")
    );
    if (!url) throw new Error("DAUM result card contains an invalid or query-bearing URL");
    const embedded = mapByUrl.get(url);
    if (!embedded) throw new Error(`DAUM result card ${url} is absent from propertySearchData`);
    const content = card.find(":scope > .content");
    const title =
      clean(content.eq(0).find("a.red-text").first().text()) ??
      clean(stripHtmlText(String(embedded?.title ?? "")));
    if (!title) throw new Error(`DAUM result card ${url} omitted its title`);
    const tenureText = clean(content.eq(0).find(".listing-type").text());
    const locationParts = content
      .eq(0)
      .find(".info-location")
      .text()
      .split("|")
      .map((part) => clean(part))
      .filter((part): part is string => Boolean(part));
    const detailParagraphs = content
      .eq(1)
      .children("p")
      .map((__, paragraph) => clean($(paragraph).text()))
      .get()
      .filter((value): value is string => Boolean(value));
    const sizeText = detailParagraphs.find((value) => /\bSF\b/i.test(value)) ?? null;
    const spaceText = detailParagraphs.find((value) => /\bSpaces?\b/i.test(value)) ?? null;
    const assetType =
      [...detailParagraphs].reverse().find((value) => !/\bSF\b|\bSpaces?\b/i.test(value)) ?? null;
    rows.push({
      url,
      title,
      tenure: daumTenure(tenureText),
      tenureText,
      city: locationParts[0] ?? null,
      state: locationParts[1] ?? null,
      postalCode: locationParts[2] ?? null,
      sizeText,
      spaceCount: positiveNumber(spaceText),
      assetType,
      salePriceText: meaningfulMoney(
        content.eq(1).find(".sale-price, .sale-price-lease").first().text()
      ),
      leaseRateText: meaningfulMoney(content.eq(1).find(".rentpersqft").first().text()),
      brokerNames: dedupeStrings(
        card
          .find(".wrap-links-prop a.red-text")
          .map((__, link) => clean($(link).text()) ?? "")
          .get()
          .filter(Boolean)
      ),
      imageUrl:
        daumAbsoluteUrl(embedded?.image) ??
        daumBackgroundUrl(card.find("[data-bg-image]").first().attr("data-bg-image")),
      latitude: signedNumber(embedded?.lat),
      longitude: signedNumber(embedded?.lng),
    });
  });
  if (rows.length !== mapByUrl.size) {
    throw new Error(
      `DAUM card/map mismatch: ${rows.length} cards vs ${mapByUrl.size} propertySearchData rows`
    );
  }
  const seen = new Set<string>();
  for (const row of rows) {
    if (seen.has(row.url)) throw new Error(`DAUM search page contains duplicate URL ${row.url}`);
    seen.add(row.url);
  }
  return { reportedTotal, reportedPages, rows };
}

export function assertDaumSnapshot(pages: DaumPage[]): DaumInventoryRow[] {
  if (!pages.length) throw new Error("DAUM snapshot has no pages");
  const total = pages[0].reportedTotal;
  const expectedPages = pages[0].reportedPages;
  if (pages.length !== expectedPages) {
    throw new Error(`DAUM snapshot incomplete: collected ${pages.length}/${expectedPages} pages`);
  }
  const rows: DaumInventoryRow[] = [];
  const urls = new Set<string>();
  for (let index = 0; index < pages.length; index++) {
    const page = pages[index];
    const pageNumber = index + 1;
    if (page.reportedTotal !== total || page.reportedPages !== expectedPages) {
      throw new Error(`DAUM result total changed during pagination on page ${pageNumber}`);
    }
    const expectedRows =
      pageNumber < expectedPages ? DAUM_PAGE_SIZE : total - DAUM_PAGE_SIZE * (expectedPages - 1);
    if (page.rows.length !== expectedRows) {
      throw new Error(
        `DAUM page ${pageNumber}/${expectedPages} incomplete: ` +
          `collected ${page.rows.length}/${expectedRows}`
      );
    }
    for (const row of page.rows) {
      if (urls.has(row.url)) throw new Error(`DAUM snapshot contains duplicate URL ${row.url}`);
      urls.add(row.url);
      rows.push(row);
    }
  }
  if (rows.length !== total) {
    throw new Error(`DAUM inventory incomplete: collected ${rows.length}/${total}`);
  }
  return rows;
}

export function assembleDaumSnapshot(pages: DaumPage[]): {
  total: number;
  rows: DaumInventoryRow[];
  lifecycleExact: boolean;
  reportedTotals: number[];
} {
  if (!pages.length) throw new Error("DAUM snapshot has no pages");
  const expectedPages = Math.max(...pages.map((page) => page.reportedPages));
  if (
    pages.length !== expectedPages ||
    pages.some((page) => page.reportedPages !== expectedPages)
  ) {
    throw new Error(`DAUM snapshot page path changed during pagination`);
  }
  const reportedTotals = pages.map((page) => page.reportedTotal);
  const total = Math.max(...reportedTotals);
  if (Math.ceil(total / DAUM_PAGE_SIZE) !== expectedPages) {
    throw new Error(
      `DAUM maximum reported total ${total} disagrees with ${expectedPages}-page path`
    );
  }
  const rows: DaumInventoryRow[] = [];
  const urls = new Set<string>();
  for (let index = 0; index < pages.length; index++) {
    const expectedRows =
      index + 1 < expectedPages
        ? DAUM_PAGE_SIZE
        : total - DAUM_PAGE_SIZE * (expectedPages - 1);
    if (pages[index].rows.length !== expectedRows) {
      throw new Error(
        `DAUM page ${index + 1}/${expectedPages} incomplete: ` +
          `collected ${pages[index].rows.length}/${expectedRows}`
      );
    }
    for (const row of pages[index].rows) {
      if (urls.has(row.url)) throw new Error(`DAUM snapshot contains duplicate URL ${row.url}`);
      urls.add(row.url);
      rows.push(row);
    }
  }
  if (rows.length !== total) {
    throw new Error(`DAUM inventory incomplete: collected ${rows.length}/${total}`);
  }
  return {
    total,
    rows,
    lifecycleExact: new Set(reportedTotals).size === 1,
    reportedTotals,
  };
}

function daumFactMap($: cheerio.CheerioAPI): Record<string, string> {
  const facts: Record<string, string> = {};
  $(".property-summary li").each((_, element) => {
    const label = clean($(element).find(".title").first().text().replace(/:\s*$/, ""));
    const value = clean($(element).find(".info").first().text());
    if (label && value) facts[label] = value;
  });
  return facts;
}

function daumCloudflareEmail(hex: string | undefined): string | null {
  if (!hex || !/^[0-9a-f]+$/i.test(hex) || hex.length < 4 || hex.length % 2 !== 0) return null;
  const key = Number.parseInt(hex.slice(0, 2), 16);
  let value = "";
  for (let index = 2; index < hex.length; index += 2) {
    value += String.fromCharCode(Number.parseInt(hex.slice(index, index + 2), 16) ^ key);
  }
  return clean(value);
}

export function parseDaumDetail(html: string, expectedUrl?: string): DaumDetail {
  const shell = cheerio.load(html);
  const shellTitle = clean(shell("title").text());
  const shortBody = html.length < 2_000 ? clean(shell("body").text()) : null;
  if (
    !html.trim() ||
    /just a moment|captcha|access denied|page not found|404 not found/i.test(
      shellTitle ?? shortBody ?? ""
    )
  ) {
    throw new Error("DAUM detail returned an empty, challenge, or missing-page shell");
  }
  const $ = shell;
  const shortlink = clean($('link[rel="shortlink"]').attr("href"));
  const postId = daumShortlinkPostId(shortlink);
  if (postId === null) {
    throw new Error("DAUM detail omitted its stable WordPress shortlink ID");
  }
  if (expectedUrl) {
    const canonical = canonicalDaumPropertyUrl(
      $('link[rel="canonical"]').attr("href") ?? expectedUrl
    );
    if (!canonical || canonical !== expectedUrl) {
      throw new Error(`DAUM detail identity mismatch for ${expectedUrl}`);
    }
  }
  const title = clean($(".main-section .title-property").first().text());
  if (!title) throw new Error(`DAUM detail ${postId} omitted its property title`);
  const tenureText = clean($(".main-section .title-about").first().text());
  const locationParts = $(".main-section .state-city li")
    .map((_, element) => clean($(element).text()))
    .get()
    .filter((value): value is string => Boolean(value));
  const facts = daumFactMap($);
  const topValues = $(".main-section .info-wrap > .info-units")
    .map((_, element) => clean($(element).text()))
    .get()
    .filter((value): value is string => Boolean(value));
  const leaseRateText =
    topValues.find((value) => /\/\s*SF|PSF|per\s+(?:square|sf)/i.test(value) && meaningfulMoney(value)) ??
    meaningfulMoney(facts["Monthly Rent PSF"]);
  const salePriceText =
    topValues
      .filter((value) => !/\/\s*SF|PSF|per\s+(?:square|sf)/i.test(value))
      .map(meaningfulMoney)
      .find(Boolean) ?? null;
  const sizeText =
    topValues.find((value) => /\bSF\b/i.test(value) && !/\$/.test(value)) ??
    facts["Total Space Available"] ??
    null;
  const description = clean(
    $(".units-info .left-side .content, .property-description").first().text()
  );
  const highlights = $(".units-info .left-side .content li")
    .map((_, element) => clean($(element).text()))
    .get()
    .filter((value): value is string => Boolean(value));
  const brochures = dedupeStrings(
    $(".main-section a[href]")
      .map((_, element) => daumAbsoluteUrl($(element).attr("href")) ?? "")
      .get()
      .filter((url) => /\.pdf(?:$|\?)/i.test(url))
  ).map((url) => ({ name: titleFromFilename(url), url }));
  const photos = dedupeStrings(
    $(".main-section [data-bg-image], .main-section img")
      .map(
        (_, element) =>
          daumBackgroundUrl($(element).attr("data-bg-image")) ??
          daumAbsoluteUrl($(element).attr("src") ?? $(element).attr("data-src"))
      )
      .get()
      .filter((value): value is string => Boolean(value))
  );
  const contacts = $(
    "#agent-contact .item, .agents .item, .property-agents .item, " +
      ".sidebar-single-property .wrap > .item"
  )
    .map((_, element) => {
      const card = $(element);
      const emailLink = card.find("a.email, a[href^='mailto:']").first();
      const office = card
        .find(".content")
        .first()
        .find("p")
        .map((__, paragraph) => clean($(paragraph).text()))
        .get()
        .filter(Boolean)
        .join(", ");
      const emailHref = clean(emailLink.attr("href"));
      return {
        name: clean(card.find("a.name").first().text()),
        email:
          (emailHref && /^mailto:/i.test(emailHref)
            ? clean(emailHref.replace(/^mailto:/i, ""))
            : null) ??
          daumCloudflareEmail(emailLink.find("[data-cfemail]").attr("data-cfemail")),
        phone: clean(card.find("a.phone, a[href^='tel:']").first().text()?.replace(/^P\s*/i, "")),
        office: clean(office),
        profileUrl: daumAbsoluteUrl(card.find("a.name").attr("href")),
        avatarUrl: daumBackgroundUrl(card.find("[data-bg-image]").attr("data-bg-image")),
        license: clean(card.text().match(/License:\s*([A-Z]{2}\s+\d+)/i)?.[1] ?? null),
      };
    })
    .get()
    .filter((contact) => contact.name || contact.email);
  return {
    postId,
    facts,
    title,
    tenure: daumTenure(tenureText),
    tenureText,
    city: locationParts[0] ?? null,
    state: locationParts[1] ?? null,
    postalCode: locationParts[2] ?? null,
    salePriceText,
    leaseRateText,
    sizeText,
    description,
    highlights,
    brochures,
    photos,
    contacts,
  };
}

function daumShortlinkPostId(value: unknown): number | null {
  const raw = clean(value);
  if (!raw) return null;
  try {
    const url = new URL(raw);
    if (
      url.origin !== DAUM_HOST
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

export class DaumPoliteTransport {
  private lastStartedAt = 0;
  private queue: Promise<unknown> = Promise.resolve();

  constructor(
    private readonly fetchImpl: FetchLike = fetch,
    private readonly delayMs = DAUM_ROBOTS_DELAY_MS,
    private readonly timeoutMs = 30_000,
    private readonly maxResponseBytes = DAUM_MAX_RESPONSE_BYTES
  ) {}

  private request(url: string, method: "GET" | "POST"): Promise<string> {
    const safeUrl = daumAbsoluteUrl(url);
    if (!safeUrl) {
      return Promise.reject(new Error(`DAUM refused unsafe URL ${url}`));
    }
    if (new URL(safeUrl).search) {
      return Promise.reject(new Error(`DAUM robots policy forbids query URL ${url}`));
    }
    const task = this.queue.then(async () => {
      const waitMs = Math.max(0, this.delayMs - (Date.now() - this.lastStartedAt));
      if (waitMs) await new Promise((resolve) => setTimeout(resolve, waitMs));
      this.lastStartedAt = Date.now();
      let lastError: unknown;
      for (let attempt = 1; attempt <= 2; attempt++) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
        try {
          const response = await this.fetchImpl(safeUrl, {
            method,
            headers: {
              "User-Agent": DAUM_USER_AGENT,
              Accept: "text/html",
              ...(method === "POST"
                ? { "Content-Type": "application/x-www-form-urlencoded" }
                : {}),
            },
            ...(method === "POST" ? { body: "" } : {}),
            redirect: "manual",
            signal: controller.signal,
          });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          return await daumBoundedResponseText(response, this.maxResponseBytes);
        } catch (error) {
          lastError = error;
          if (attempt === 2) break;
          const retryWait = Math.max(0, this.delayMs - (Date.now() - this.lastStartedAt));
          if (retryWait) await new Promise((resolve) => setTimeout(resolve, retryWait));
          this.lastStartedAt = Date.now();
        } finally {
          clearTimeout(timeout);
        }
      }
      throw new Error(`DAUM request failed for ${safeUrl}: ${String(lastError)}`);
    });
    this.queue = task.catch(() => undefined);
    return task as Promise<string>;
  }

  get(url: string): Promise<string> {
    return this.request(url, "GET");
  }

  post(url: string): Promise<string> {
    return this.request(url, "POST");
  }
}

export function daumSnapshotIdentitySignature(rows: DaumInventoryRow[]): string {
  return rows.map((row) => `${row.url}\t${row.tenure}`).join("\n");
}

export function assertDaumUniqueProviderIds(listings: Array<{ id?: unknown }>): void {
  const ids = new Set<string>();
  for (const listing of listings) {
    const id = clean(String(listing?.id ?? ""));
    if (!id) throw new Error("DAUM emitted a listing without a provider ID");
    if (ids.has(id)) throw new Error(`DAUM emitted duplicate provider ID ${id}`);
    ids.add(id);
  }
}

type DaumSearchTransport = Pick<DaumPoliteTransport, "post">;

async function fetchDaumSnapshotPass(
  transport: DaumSearchTransport
): Promise<{
  total: number;
  rows: DaumInventoryRow[];
  reportedTotals: number[];
  signature: string;
}> {
  // DAUM's edge cache can serve adjacent path pages from different inventory
  // generations. An empty form POST to the same clean, query-free public path
  // bypasses that cache while preserving the site's normal server-rendered
  // search behavior. Every request still goes through DaumPoliteTransport's
  // global three-second crawl delay.
  const first = parseDaumSearchPage(await transport.post(daumPageUrl(1)));
  const pages = [first];
  for (let page = 2; page <= first.reportedPages; page++) {
    pages.push(parseDaumSearchPage(await transport.post(daumPageUrl(page))));
  }
  const rows = assertDaumSnapshot(pages);
  return {
    total: first.reportedTotal,
    rows,
    reportedTotals: pages.map((page) => page.reportedTotal),
    signature: daumSnapshotIdentitySignature(rows),
  };
}

export async function fetchDaumSnapshot(
  transport: DaumSearchTransport = new DaumPoliteTransport(),
  maxPasses = 3
): Promise<{
  total: number;
  observedAt: string;
  rows: DaumInventoryRow[];
  lifecycleExact: boolean;
  reportedTotals: number[];
}> {
  if (!Number.isInteger(maxPasses) || maxPasses < 2 || maxPasses > 5) {
    throw new Error(`DAUM convergence passes must be an integer from 2 to 5`);
  }
  let previous:
    | {
        total: number;
        rows: DaumInventoryRow[];
        reportedTotals: number[];
        signature: string;
      }
    | null = null;
  const failures: string[] = [];
  for (let pass = 1; pass <= maxPasses; pass++) {
    let current;
    try {
      current = await fetchDaumSnapshotPass(transport);
    } catch (error) {
      failures.push(`pass ${pass}: ${String(error)}`);
      previous = null;
      continue;
    }
    if (
      previous &&
      current.total === previous.total &&
      current.signature === previous.signature
    ) {
      return {
        total: current.total,
        rows: current.rows,
        lifecycleExact: true,
        reportedTotals: current.reportedTotals,
        observedAt: new Date().toISOString(),
      };
    }
    if (previous) {
      failures.push(
        `pass ${pass}: identity inventory changed ` +
          `(${previous.total}/${previous.rows.length} -> ${current.total}/${current.rows.length})`
      );
    }
    previous = current;
  }
  throw new Error(
    `DAUM inventory did not converge across ${maxPasses} cache-bypassed full passes` +
      (failures.length ? ` (${failures.join("; ")})` : "")
  );
}

export function daumBelongsToTx(tenure: DaumTenure, tx: Tx): boolean {
  if (tenure === "sale_or_lease") return true;
  if (tenure === "unknown") return false;
  return tenure === tx;
}

export function resolveDaumTenure(
  inventoryTenure: DaumTenure,
  detailTenure: DaumTenure
): DaumTenure {
  if (inventoryTenure === "unknown") return detailTenure;
  if (detailTenure === "unknown" || detailTenure === inventoryTenure) {
    return inventoryTenure;
  }
  throw new Error(
    `DAUM detail tenure ${detailTenure} disagrees with inventory tenure ${inventoryTenure}`
  );
}

export function mapDaumListing(
  inventory: DaumInventoryRow,
  detail: DaumDetail,
  tx: Tx,
  inventoryObservedAt: string,
  detailObservedAt: string
): any {
  const resolvedTenure = resolveDaumTenure(inventory.tenure, detail.tenure);
  if (resolvedTenure === "unknown") {
    throw new Error(`DAUM ${inventory.url} has unresolved transaction tenure`);
  }
  if (!daumBelongsToTx(resolvedTenure, tx)) {
    throw new Error(`DAUM ${inventory.url} does not belong to requested ${tx} inventory`);
  }
  const observed = detailObservation("daum_direct_detail_html", "live", detailObservedAt);
  const contacts = detail.contacts.length
    ? detail.contacts
    : inventory.brokerNames.map((name) => ({
        name,
        email: null,
        phone: null,
        office: null,
        profileUrl: null,
        avatarUrl: null,
        license: null,
      }));
  const brokerIds = contacts
    .map((contact) =>
      brokerRef({
        name: contact.name,
        email: contact.email,
        phone: contact.phone,
        office: contact.office,
        avatarUrl: contact.avatarUrl,
        company: "DAUM Commercial Real Estate Services",
      })
    )
    .filter((value): value is number => value !== null);
  const tenure =
    resolvedTenure === "sale_or_lease"
      ? "Sale/Lease"
      : resolvedTenure === "sale"
        ? "Sale"
        : "Lease";
  const salePriceText = detail.salePriceText ?? inventory.salePriceText;
  return prune({
    id: String(detail.postId),
    name: detail.title ?? inventory.title,
    transactionType: tenure,
    assetType: detail.facts["Property Type"] ?? inventory.assetType,
    propertySubtype: detail.facts["Primary Use"],
    description: detail.description,
    highlights: detail.highlights,
    street: detail.title ?? inventory.title,
    city: detail.city ?? inventory.city,
    state: detail.state ?? inventory.state,
    postalCode: detail.postalCode ?? inventory.postalCode,
    latitude: inventory.latitude,
    longitude: inventory.longitude,
    salePriceUsd: tx === "sale" ? moneyToNumber(salePriceText) : null,
    salePriceText: tx === "sale" ? salePriceText : null,
    leaseRateText: tx === "lease" ? detail.leaseRateText ?? inventory.leaseRateText : null,
    sizeText: detail.sizeText ?? inventory.sizeText,
    availableSf: positiveNumber(
      detail.facts["Total Space Available"] ?? detail.sizeText ?? inventory.sizeText
    ),
    minDivisibleSf: positiveNumber(detail.facts["Smallest Space"]),
    maxDivisibleSf: positiveNumber(
      detail.facts["Maximum Contiguous"] ?? detail.facts["Largest Space"]
    ),
    lotSizeAcres: (() => {
      const sqft = positiveNumber(detail.facts["Lot Size (Sq. Ft.)"]);
      return sqft ? sqft / 43_560 : null;
    })(),
    zoning: detail.facts.Zoning,
    brokerIds,
    contactsDetailed: contacts,
    brochures: detail.brochures,
    photos: dedupeStrings([...detail.photos, inventory.imageUrl].filter(Boolean) as string[]),
    url: inventory.url,
    canonicalUrl: inventory.url,
    markdown: clean([detail.description, ...detail.highlights].filter(Boolean).join("\n\n")),
    inventoryObservedAt,
    detailObservedAt,
    freshnessProvenance: {
      detailScope: "detail_page",
      generationId: observed.generationId,
      method: observed.method,
      cacheDisposition: observed.cacheDisposition,
      identityMethod: "wordpress_shortlink_post_id",
    },
    daumSearchCard: inventory,
    detailFacts: detail.facts,
  });
}

export async function srcDaumCommercial(
  tx: Tx,
  max: number,
  _monitor: boolean
): Promise<SourceResult> {
  const transport = new DaumPoliteTransport();
  const snapshot = await fetchDaumSnapshot(transport);
  // Unknown inventory tenure is not assigned to a transaction pass. Fetch its
  // detail in both passes so a recognized current detail tenure can resolve it;
  // if detail also remains unknown, hold the row entirely and block lifecycle.
  const candidates = snapshot.rows.filter(
    (row) => row.tenure === "unknown" || daumBelongsToTx(row.tenure, tx)
  );
  const selected = candidates.slice(0, Math.min(max, candidates.length));
  const listings = [];
  let unresolvedTenureHolds = 0;
  let resolvedToOtherPass = 0;
  for (const row of selected) {
    const html = await transport.get(row.url);
    const detailObservedAt = new Date().toISOString();
    const detail = parseDaumDetail(html, row.url);
    const resolvedTenure = resolveDaumTenure(row.tenure, detail.tenure);
    if (resolvedTenure === "unknown") {
      unresolvedTenureHolds++;
      continue;
    }
    if (!daumBelongsToTx(resolvedTenure, tx)) {
      resolvedToOtherPass++;
      continue;
    }
    listings.push(
      mapDaumListing(row, detail, tx, snapshot.observedAt, detailObservedAt)
    );
  }
  assertDaumUniqueProviderIds(listings);
  const unknownRows = snapshot.rows.filter((row) => row.tenure === "unknown").length;
  return {
    company: "DAUM Commercial Real Estate Services",
    sourceUrl: DAUM_SEARCH_URL,
    method:
      "complete server-rendered pagination plus direct detail HTML " +
      "(robots crawl-delay 3 seconds; monitor and full share exact identity path)",
    totalAvailable: candidates.length - resolvedToOtherPass - unresolvedTenureHolds,
    truncated:
      selected.length < candidates.length ||
      unresolvedTenureHolds > 0 ||
      !snapshot.lifecycleExact,
    listings,
    note:
      `exact ${snapshot.total}-record/${Math.ceil(snapshot.total / DAUM_PAGE_SIZE)}-page snapshot; ` +
      `${unknownRows} blank-tenure row(s) detail-resolved or held; ` +
      (unresolvedTenureHolds
        ? `${unresolvedTenureHolds} unresolved tenure row(s) held from ingest; `
        : "") +
      (snapshot.lifecycleExact
        ? ""
        : `page-total cache skew (${snapshot.reportedTotals.join(",")}) blocks disappearance; `) +
      "disappearance is eligible only when truncated=false",
  };
}
