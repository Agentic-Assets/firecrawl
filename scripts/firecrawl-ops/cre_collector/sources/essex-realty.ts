import * as cheerio from "cheerio";
import { detailObservation, refreshGenerationId, requireFreshDetails } from "../lib/freshness.js";
import { parseMoney } from "../lib/parse.js";
import { clean, prune } from "../lib/util.js";
import type { SourceResult, Tx } from "../types.js";

export const ESSEX_LISTINGS_URL = "https://essexrealtygroup.com/properties/";
export const ESSEX_CLOSED_URL =
  "https://essexrealtygroup.com/properties/?sale_deal_status_id=3";
export const ESSEX_SITEMAP_URL =
  "https://essexrealtygroup.com/properties-sitemap.xml";
export const ESSEX_CRAWL_DELAY_MS = 10_000;
export const ESSEX_TIMEOUT_MS = 60_000;
export const ESSEX_MAX_ARCHIVE_BYTES = 32 * 1024 * 1024;
export const ESSEX_MAX_DETAIL_BYTES = 16 * 1024 * 1024;

export type EssexCurrentLifecycle = "on_market" | "under_contract";

export type EssexArchiveRow = {
  url: string;
  lifecycle: EssexCurrentLifecycle | "closed";
  name: string;
  location: string | null;
  units: number | null;
};

export type EssexInventory = {
  current: EssexArchiveRow[];
  closed: EssexArchiveRow[];
  sitemapUrls: string[];
  lifecycleCounts: Record<EssexCurrentLifecycle | "closed", number>;
};

export type EssexTransport = {
  getText(url: string, maxBytes?: number): Promise<string>;
};

const ESSEX_ASSET_QUERY_KEYS = new Set(["ver", "w"]);

function essexAssetQueryIsBenign(url: URL): boolean {
  const seen = new Set<string>();
  for (const [key, value] of url.searchParams) {
    if (!ESSEX_ASSET_QUERY_KEYS.has(key) || seen.has(key)) return false;
    seen.add(key);
    if (key === "w" && !/^[1-9]\d{0,4}$/.test(value)) return false;
    if (key === "ver" && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(value)) return false;
  }
  return true;
}

function essexUrl(value: unknown): string | null {
  const raw = clean(value);
  if (!raw) return null;
  try {
    const url = new URL(raw, ESSEX_LISTINGS_URL);
    if (
      url.protocol !== "https:"
      || url.hostname.toLowerCase() !== "essexrealtygroup.com"
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

function sameEssexPage(left: string, right: string): boolean {
  const normalize = (value: string): string | null => {
    const url = essexUrl(value);
    if (!url) return null;
    const parsed = new URL(url);
    return parsed.pathname.replace(/\/+$/, "").toLowerCase();
  };
  return normalize(left) !== null && normalize(left) === normalize(right);
}

export function essexAssetUrl(value: unknown): string | null {
  const raw = clean(value);
  if (!raw) return null;
  try {
    const url = new URL(raw);
    if (
      url.protocol !== "https:"
      || url.username
      || url.password
      || url.port
      || Boolean(url.hash)
      || !essexAssetQueryIsBenign(url)
    ) {
      return null;
    }
    return url.toString();
  } catch {
    return null;
  }
}

async function essexBoundedResponseText(
  response: Response,
  maxBytes: number
): Promise<string> {
  const declaredLength = Number(response.headers.get("content-length"));
  if (Number.isFinite(declaredLength) && declaredLength > maxBytes) {
    throw new Error(`Essex response exceeds ${maxBytes} bytes`);
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
        throw new Error(`Essex response exceeds ${maxBytes} bytes`);
      }
      text += decoder.decode(value, { stream: true });
    }
    text += decoder.decode();
    return text;
  } finally {
    reader.releaseLock();
  }
}

export function createEssexTransport(
  fetchImpl: typeof fetch = fetch,
  sleep: (ms: number) => Promise<void> = (ms) =>
    new Promise((resolve) => setTimeout(resolve, ms)),
  crawlDelayMs = ESSEX_CRAWL_DELAY_MS
): EssexTransport {
  let lastStartedAt = 0;
  return {
    async getText(
      requestedUrl: string,
      maxBytes = ESSEX_MAX_ARCHIVE_BYTES
    ): Promise<string> {
      const url = essexUrl(requestedUrl);
      if (!url) throw new Error("Essex direct transport rejected URL");
      const remaining = lastStartedAt + crawlDelayMs - Date.now();
      if (remaining > 0) await sleep(remaining);
      lastStartedAt = Date.now();
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), ESSEX_TIMEOUT_MS);
      try {
        const response = await fetchImpl(url, {
          headers: {
            Accept: "text/html,application/xhtml+xml,application/xml",
            "User-Agent": "Mozilla/5.0 CRE collector",
            "Cache-Control": "no-cache",
          },
          cache: "no-store",
          redirect: "error",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`Essex direct transport HTTP ${response.status}`);
        return await essexBoundedResponseText(response, maxBytes);
      } finally {
        clearTimeout(timer);
      }
    },
  };
}

function parsePositiveInteger(value: unknown): number | null {
  const match = clean(value)?.match(/\b([\d,]+)\b/);
  if (!match) return null;
  const parsed = Number(match[1].replace(/,/g, ""));
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function archiveLifecycle(value: unknown): EssexArchiveRow["lifecycle"] | null {
  const normalized = clean(value)?.toLowerCase().replace(/\s+/g, "_");
  if (normalized === "on_market") return "on_market";
  if (normalized === "under_contract") return "under_contract";
  if (normalized === "closed") return "closed";
  return null;
}

export function parseEssexArchive(
  html: string,
  expectedScope: "current" | "closed"
): EssexArchiveRow[] {
  if (
    !html.trim()
    || /verify you are human|attention required|cf-chl-|access denied|captcha challenge/i.test(
      html
    )
  ) {
    throw new Error(`Essex ${expectedScope} archive returned an error shell`);
  }
  const declaredMatches = [...html.matchAll(/\bALL\s*\[\s*(\d+)\s*\]/gi)];
  if (declaredMatches.length !== 1) {
    throw new Error(
      `Essex ${expectedScope} archive requires exactly one declared ALL count`
    );
  }
  const declared = Number(declaredMatches[0][1]);
  if (!Number.isSafeInteger(declared) || declared <= 0) {
    throw new Error(`Essex ${expectedScope} archive has invalid declared count`);
  }

  const $ = cheerio.load(html);
  const rows: EssexArchiveRow[] = [];
  const identities = new Set<string>();
  $("a.property-card").each((index, element) => {
    const card = $(element);
    const url = essexUrl(card.attr("href"));
    if (!url || !new URL(url).pathname.startsWith("/properties/")) {
      throw new Error(`Essex ${expectedScope} archive card ${index} has invalid URL`);
    }
    if (identities.has(url)) {
      throw new Error(`Essex ${expectedScope} archive has duplicate URL ${url}`);
    }
    identities.add(url);
    const lifecycle = archiveLifecycle(card.find(".property-card__label").first().text());
    const allowed =
      expectedScope === "closed"
        ? lifecycle === "closed"
        : lifecycle === "on_market" || lifecycle === "under_contract";
    if (!allowed) {
      throw new Error(
        `Essex ${expectedScope} archive card ${url} has unknown or contradictory lifecycle`
      );
    }
    const name = clean(card.find(".property-info__address--address-1").first().text());
    if (!name) throw new Error(`Essex ${expectedScope} archive card ${url} has no name`);
    rows.push({
      url,
      lifecycle: lifecycle!,
      name,
      location: clean(card.find(".property-info__address--address-2").first().text()),
      units: parsePositiveInteger(card.find(".property-info__units").first().text()),
    });
  });
  if (rows.length !== declared) {
    throw new Error(
      `Essex ${expectedScope} archive completeness failed (${rows.length} != ${declared})`
    );
  }
  return rows;
}

export function parseEssexSitemap(xml: string): string[] {
  if (!xml.trim()) throw new Error("Essex sitemap is empty");
  const $ = cheerio.load(xml, { xmlMode: true });
  const urls: string[] = [];
  const seen = new Set<string>();
  $("url > loc").each((index, element) => {
    const url = essexUrl($(element).text());
    if (!url) throw new Error(`Essex sitemap loc ${index} is invalid`);
    const path = new URL(url).pathname.replace(/\/+$/, "/");
    if (path === "/properties/") return;
    if (!/^\/properties\/[^/]+\/$/.test(path)) {
      throw new Error(`Essex sitemap contains unexpected property URL ${url}`);
    }
    if (seen.has(url)) throw new Error(`Essex sitemap duplicate URL ${url}`);
    seen.add(url);
    urls.push(url);
  });
  if (urls.length === 0) throw new Error("Essex sitemap has no property detail URLs");
  return urls;
}

export function reconcileEssexInventory(
  current: EssexArchiveRow[],
  closed: EssexArchiveRow[],
  sitemapUrls: string[]
): EssexInventory {
  const currentUrls = new Set(current.map((row) => row.url));
  const closedUrls = new Set(closed.map((row) => row.url));
  for (const url of currentUrls) {
    if (closedUrls.has(url)) {
      throw new Error(`Essex lifecycle overlap for ${url}`);
    }
  }
  const union = new Set([...currentUrls, ...closedUrls]);
  const sitemap = new Set(sitemapUrls);
  const missingFromSitemap = [...union].filter((url) => !sitemap.has(url));
  const missingFromArchives = [...sitemap].filter((url) => !union.has(url));
  if (
    union.size !== sitemap.size
    || missingFromSitemap.length
    || missingFromArchives.length
  ) {
    throw new Error(
      `Essex archive/sitemap reconciliation failed `
      + `(archives=${union.size}, sitemap=${sitemap.size}, `
      + `archive_only=${missingFromSitemap.length}, sitemap_only=${missingFromArchives.length})`
    );
  }
  return {
    current,
    closed,
    sitemapUrls,
    lifecycleCounts: {
      on_market: current.filter((row) => row.lifecycle === "on_market").length,
      under_contract: current.filter((row) => row.lifecycle === "under_contract").length,
      closed: closed.length,
    },
  };
}

export async function fetchEssexInventory(
  transport = createEssexTransport()
): Promise<EssexInventory> {
  const currentHtml = await transport.getText(ESSEX_LISTINGS_URL);
  const closedHtml = await transport.getText(ESSEX_CLOSED_URL);
  const sitemapXml = await transport.getText(ESSEX_SITEMAP_URL);
  return reconcileEssexInventory(
    parseEssexArchive(currentHtml, "current"),
    parseEssexArchive(closedHtml, "closed"),
    parseEssexSitemap(sitemapXml)
  );
}

function labeledDetailValue($: cheerio.CheerioAPI, label: RegExp): string | null {
  let value: string | null = null;
  $(".property-info__label").each((_, element) => {
    if (value || !label.test(clean($(element).text()) ?? "")) return;
    value = clean($(element).next(".property-info__info").text());
  });
  return value;
}

function detailLocation(value: unknown): {
  city: string | null;
  state: string | null;
  postalCode: string | null;
} {
  const normalized = clean(value);
  const match = normalized?.match(/^(.+?),\s*([A-Za-z]{2})(?:\s+(\d{5}(?:-\d{4})?))?$/);
  return {
    city: clean(match?.[1]),
    state: match?.[2]?.toUpperCase() ?? null,
    postalCode: match?.[3] ?? null,
  };
}

export function parseEssexDetail(
  html: string,
  requestedUrl: string,
  expectedLifecycle: EssexCurrentLifecycle
): any {
  const canonicalUrl = essexUrl(requestedUrl);
  if (!canonicalUrl) throw new Error("Essex detail requested URL is invalid");
  if (
    !html.trim()
    || /verify you are human|attention required|cf-chl-|access denied|page not found|404 error/i.test(
      html
    )
  ) {
    throw new Error("Essex detail returned an error shell");
  }
  const idMatches = [...html.matchAll(/\bvar\s+currentPropertyId\s*=\s*(\d+)\s*;/g)];
  if (idMatches.length !== 1) {
    throw new Error("Essex detail requires exactly one currentPropertyId");
  }
  const id = idMatches[0][1];
  if (!Number.isSafeInteger(Number(id)) || Number(id) <= 0) {
    throw new Error("Essex detail currentPropertyId is invalid");
  }
  const $ = cheerio.load(html);
  const identityUrls = [
    clean($("link[rel='canonical']").first().attr("href")),
    clean($("meta[property='og:url']").first().attr("content")),
  ].filter((value): value is string => Boolean(value));
  if (
    identityUrls.length === 0
    || identityUrls.some((value) => !sameEssexPage(value, canonicalUrl))
  ) {
    throw new Error("Essex detail canonical identity does not match requested archive URL");
  }
  if ($(".property-detail-wrapper").length !== 1) {
    throw new Error("Essex detail requires one property detail wrapper");
  }
  const heading = clean($(".property-info__label.label").first().text()) ?? "";
  const lifecycle = archiveLifecycle(heading.split("-")[0]);
  if (lifecycle !== expectedLifecycle) {
    throw new Error(
      `Essex detail lifecycle ${lifecycle ?? "unknown"} does not match archive ${expectedLifecycle}`
    );
  }
  const name = clean($(".property-info__address--address-1").first().text());
  if (!name) throw new Error("Essex detail has no property name");
  const location = detailLocation(
    $(".property-info__address--address-2").first().text()
  );
  const assetType = clean(heading.split("-").slice(1).join("-"));
  const photos = new Set<string>();
  $(".property-photos-carousel__photo").each((_, element) => {
    const style = $(element).attr("style") ?? "";
    const raw = style.match(/background-image\s*:\s*url\((['"]?)(.*?)\1\)/i)?.[2];
    const url = essexAssetUrl(raw);
    if (url) photos.add(url);
  });
  const highlights: string[] = [];
  $(".body-text__subhead").each((_, element) => {
    if (!/^highlights$/i.test(clean($(element).text()) ?? "")) return;
    $(element).next("ul").find("li").each((__, item) => {
      const text = clean($(item).text());
      if (text) highlights.push(text);
    });
  });
  const description = clean(
    $(".body-text__subhead")
      .filter((_, element) => /^property listed$/i.test(clean($(element).text()) ?? ""))
      .first()
      .next("p")
      .text()
  );
  const contactsDetailed: any[] = [];
  $(".small-broker-card").each((_, element) => {
    const card = $(element);
    const profileUrl = essexUrl(card.find(".small-broker-card__name a").attr("href"));
    const name = clean(card.find(".small-broker-card__name").text());
    if (!name || !profileUrl) {
      throw new Error("Essex detail contains an incomplete broker card");
    }
    contactsDetailed.push(prune({
      name,
      title: clean(card.find(".small-broker-card__job-title").text()),
      email: clean(card.find('a[href^="mailto:"]').attr("href"))?.replace(/^mailto:/i, ""),
      phone: clean(
        card
          .find(".small-broker-card__contact")
          .filter((__, node) => !$(node).find("a").length)
          .first()
          .text()
      ),
      company: "Essex Realty Group",
      profileUrl,
    }));
  });
  const documents: any[] = [];
  $('a[href$=".pdf"], a[href*=".pdf?"]').each((_, element) => {
    const url = essexAssetUrl($(element).attr("href"));
    if (!url || !/\.pdf$/i.test(new URL(url).pathname)) return;
    documents.push({
      url,
      title: clean($(element).text()),
      docType: /offering|memorandum|\bom\b/i.test(clean($(element).text()) ?? "")
        ? "om"
        : "brochure",
    });
  });
  return {
    id,
    name,
    lifecycle,
    statusBadge: lifecycle === "under_contract" ? "Under Contract" : "On Market",
    transactionType: "Sale",
    assetType,
    description,
    street: name,
    ...location,
    country: "US",
    salePriceUsd: parseMoney(labeledDetailValue($, /^price$/i)),
    salePriceText: labeledDetailValue($, /^price$/i),
    units: parsePositiveInteger(labeledDetailValue($, /^number of units$/i)),
    capRatePct: Number(
      labeledDetailValue($, /^cap rate$/i)?.replace("%", "")
    ) || null,
    highlights: [...new Set(highlights)],
    photos: [...photos],
    contactsDetailed,
    documents,
    url: canonicalUrl,
    canonicalUrl,
  };
}

export function mapEssexDetail(
  parsed: any,
  inventoryObservedAt: string,
  detailObservedAt: string,
  generationId = refreshGenerationId(),
  monitor = false
): any {
  const observation = detailObservation(
    "essex_direct_detail_html",
    "live",
    detailObservedAt,
    { generationId }
  );
  return prune({
    ...parsed,
    inventoryObservedAt,
    detailObservedAt,
    freshnessProvenance: {
      ...observation,
      detailScope: "detail_page",
    },
    preserveChildCollections: monitor ? true : undefined,
  });
}

export async function srcEssexRealty(
  tx: Tx,
  max: number,
  monitor: boolean,
  transport = createEssexTransport()
): Promise<SourceResult> {
  if (tx === "lease") {
    return {
      company: "Essex Realty Group",
      sourceUrl: ESSEX_LISTINGS_URL,
      method: "Sale-only source",
      totalAvailable: 0,
      listings: [],
    };
  }
  const strict = requireFreshDetails();
  const generationId = refreshGenerationId();
  if (strict && !generationId) {
    throw new Error("Essex strict refresh requires CRE_REFRESH_GENERATION");
  }
  const inventory = await fetchEssexInventory(transport);
  const inventoryObservedAt = new Date().toISOString();
  const selected = inventory.current.slice(0, Math.max(0, max));
  const listings: any[] = [];
  for (const row of selected) {
    const html = await transport.getText(row.url, ESSEX_MAX_DETAIL_BYTES);
    const detailObservedAt = new Date().toISOString();
    listings.push(
      mapEssexDetail(
        parseEssexDetail(html, row.url, row.lifecycle as EssexCurrentLifecycle),
        inventoryObservedAt,
        detailObservedAt,
        generationId,
        monitor
      )
    );
  }
  const ids = new Set(listings.map((row) => row.id));
  if (ids.size !== listings.length) {
    throw new Error("Essex selected details contain duplicate currentPropertyId values");
  }
  return {
    company: "Essex Realty Group",
    sourceUrl: ESSEX_LISTINGS_URL,
    method: "Direct archive, sitemap, and rate-limited detail HTML (no LLM)",
    totalAvailable: inventory.current.length,
    listings,
    truncated: listings.length < inventory.current.length,
    note:
      `Lifecycle reconciliation: ${inventory.lifecycleCounts.on_market} on market + `
      + `${inventory.lifecycleCounts.under_contract} under contract + `
      + `${inventory.lifecycleCounts.closed} closed = ${inventory.sitemapUrls.length} sitemap properties`,
  };
}
