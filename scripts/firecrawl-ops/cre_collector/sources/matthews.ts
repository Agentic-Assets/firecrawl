import * as cheerio from "cheerio";
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import {
  refreshGenerationId,
  requireFreshDetails,
} from "../lib/freshness.js";
import { clean, moneyToNumber, pmap, prune } from "../lib/util.js";
import { SourceResult, Tx } from "../types.js";

const MATTHEWS_HOST = "https://www.matthews.com";
const MATTHEWS_SOURCE_URL = `${MATTHEWS_HOST}/listings`;
const MATTHEWS_SITEMAP_URL = `${MATTHEWS_HOST}/sitemap.xml`;
const MATTHEWS_NON_PHOTO = /headshot|web-use|brand-logo|logo|og-default|placeholder|favicon|sprite/i;
export const MATTHEWS_FETCH_TIMEOUT_MS = 30_000;

let matthewsNextSlot = 0;
let matthewsInterval = 1800;

const matthewsSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

async function matthewsGate(): Promise<void> {
  const now = Date.now();
  const slot = Math.max(now, matthewsNextSlot);
  matthewsNextSlot = slot + matthewsInterval;
  const wait = slot - now;
  if (wait > 0) await matthewsSleep(wait);
}

export function matthewsFetchOptions(
  strict = requireFreshDetails(),
  timeoutMs = MATTHEWS_FETCH_TIMEOUT_MS,
  redirect: RequestRedirect = "follow",
  signal: AbortSignal = AbortSignal.timeout(timeoutMs)
): RequestInit {
  const headers: Record<string, string> = {
    "User-Agent":
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
  };
  if (strict) headers["Cache-Control"] = "no-cache";
  return {
    headers,
    signal,
    redirect,
    ...(strict ? { cache: "no-store" as const } : {}),
  };
}

/**
 * Bound the body separately from fetch()'s header phase. Some provider HTTP/2
 * streams send headers and then leave a compressed response body open; relying
 * only on fetch's request signal makes that detail pass wait indefinitely.
 */
export async function matthewsResponseText(
  response: Pick<Response, "text">,
  controller: AbortController,
  timeoutMs = MATTHEWS_FETCH_TIMEOUT_MS
): Promise<string> {
  let timeout: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_, reject) => {
    timeout = setTimeout(() => {
      controller.abort();
      reject(new Error(`Matthews response body timed out after ${timeoutMs}ms`));
    }, timeoutMs);
  });
  try {
    return await Promise.race([response.text(), deadline]);
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

type MatthewsFetchResult = {
  html: string | null;
  status: number;
  location: string | null;
};

/**
 * A response body can time out after a successful HTTP status. Treat that as
 * a transport failure, not as a deterministic HTTP 200 validation failure.
 */
export function matthewsRetryableFetchFailure(status: number, error: unknown): boolean {
  return error != null || status === 0 || status === 429 || status === 403 || status === 503;
}

export async function matthewsFetch(
  url: string,
  manualRedirect = false
): Promise<MatthewsFetchResult> {
  let lastError: unknown = null;
  for (let attempt = 0; attempt < 6; attempt++) {
    await matthewsGate();
    let status = 0;
    let attemptError: unknown = null;
    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(),
      MATTHEWS_FETCH_TIMEOUT_MS
    );
    try {
      const res = await fetch(
        url,
        matthewsFetchOptions(
          requireFreshDetails(),
          MATTHEWS_FETCH_TIMEOUT_MS,
          manualRedirect ? "manual" : "follow",
          controller.signal
        )
      );
      status = res.status;
      if (res.ok) {
        return {
          html: await matthewsResponseText(res, controller),
          status,
          location: null,
        };
      }
      if (manualRedirect && (status === 301 || status === 308)) {
        return { html: null, status, location: res.headers.get("location") };
      }
    } catch (error) {
      // This includes the separately bounded response-body timeout. `status`
      // may already be 200 at this point, so retaining the error is essential
      // to avoid converting a transient body stall into a hard HTTP failure.
      attemptError = error;
    } finally {
      clearTimeout(timeout);
    }
    if (matthewsRetryableFetchFailure(status, attemptError)) {
      lastError = attemptError;
      matthewsInterval = Math.min(matthewsInterval + 700, 7000);
      await matthewsSleep(20000 + attempt * 15000 + Math.random() * 5000);
      continue;
    }
    throw new Error(`Matthews HTTP ${status}`);
  }
  if (lastError instanceof Error) throw lastError;
  throw new Error("Matthews: throttled after retries");
}

function matthewsImages(html: string): string[] {
  const urls: string[] = [];
  const seen = new Set<string>();
  const add = (raw: string | null) => {
    if (!raw) return;
    let url = raw.trim();
    if (url.startsWith("//")) url = "https:" + url;
    if (!/^https:\/\/cms\.matthews\.com\/wp-content\/uploads\//i.test(url)) return;
    if (MATTHEWS_NON_PHOTO.test(url) || seen.has(url)) return;
    seen.add(url);
    urls.push(url);
  };

  const nextRe = /\/_next\/image\?url=([^&"'\\ ]+)/gi;
  let match: RegExpExecArray | null;
  while ((match = nextRe.exec(html))) {
    try {
      add(decodeURIComponent(match[1]));
    } catch {
      /* ignore malformed image proxy URLs */
    }
  }

  const directRe =
    /https?:\/\/cms\.matthews\.com\/wp-content\/uploads\/[^"'\\ )]+?\.(?:jpe?g|png|webp)/gi;
  while ((match = directRe.exec(html))) add(match[0]);
  return urls;
}

function matthewsBrokers($: cheerio.CheerioAPI): {
  name: string | null;
  email: string | null;
  phone: string | null;
  avatarUrl: string | null;
}[] {
  const out: {
    name: string | null;
    email: string | null;
    phone: string | null;
    avatarUrl: string | null;
  }[] = [];

  $('a[id="agentName"]').each((_, el) => {
    const name = clean($(el).text());
    if (!name || out.some((broker) => broker.name === name)) return;

    let card = $(el);
    for (let i = 0; i < 6; i++) {
      const parent = card.parent();
      if (parent.length === 0) break;
      card = parent;
      if (card.find('a[href^="tel:"], a[href^="mailto:"]').length > 0) break;
    }

    const mailHref = card.find('a[href^="mailto:"]').first().attr("href") ?? "";
    const telText = clean(card.find('a[href^="tel:"]').first().text());
    const telHref = card.find('a[href^="tel:"]').first().attr("href") ?? "";
    let avatar = card.find('img[src*="cms.matthews.com"]').first().attr("src") ?? null;
    if (avatar?.startsWith("//")) avatar = "https:" + avatar;

    out.push({
      name,
      email: clean(mailHref.replace(/^mailto:/i, "").split("?")[0]),
      phone: telText || clean(telHref.replace(/^tel:/i, "")),
      avatarUrl: avatar?.startsWith("http") ? avatar : null,
    });
  });

  return out;
}

function parseMatthewsAddress(line: string | null): {
  street: string | null;
  city: string | null;
  state: string | null;
  postalCode: string | null;
} {
  const out = {
    street: null as string | null,
    city: null as string | null,
    state: null as string | null,
    postalCode: null as string | null,
  };
  if (!line) return out;

  const parts = line
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
  const stateZip = parts[parts.length - 1]?.match(/^([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$/);
  if (stateZip) {
    out.state = stateZip[1];
    out.postalCode = stateZip[2];
    parts.pop();
  } else {
    if (parts.length && /^\d{5}(-\d{4})?$/.test(parts[parts.length - 1])) {
      out.postalCode = parts.pop()!;
    }
    if (parts.length && /^[A-Z]{2}$/.test(parts[parts.length - 1])) {
      out.state = parts.pop()!;
    }
  }
  if (parts.length) out.city = parts.pop()!;
  if (parts.length) out.street = parts.join(", ");
  return out;
}

function matthewsDetailUrlsFromSitemap(xml: string): string[] {
  return Array.from(new Set(xml.match(/https:\/\/www\.matthews\.com\/properties\/[^<\s"')]+/gi) ?? []));
}

export function matthewsTenureFromUrl(url: string): Tx {
  return /\/properties\/leasing-/i.test(url) ? "lease" : "sale";
}

function normalizedMatthewsPropertyUrl(raw: string | null, baseUrl: string): string | null {
  const value = clean(raw);
  if (!value) return null;
  try {
    const parsed = new URL(value, baseUrl);
    if (parsed.hostname.toLowerCase().replace(/^www\./, "") !== "matthews.com") {
      return null;
    }
    const path = parsed.pathname.replace(/\/+$/, "");
    if (!/^\/properties\/[^/]+$/i.test(path)) return null;
    return `https://www.matthews.com${path}`;
  } catch {
    return null;
  }
}

/**
 * A permanent first-party redirect can retire a sitemap alias without making
 * the target listing unavailable. The caller must still prove the target is
 * enumerated in the same fresh sitemap before treating the alias as excluded.
 */
export function matthewsPermanentRedirectTarget(
  status: number,
  location: string | null,
  requestedUrl: string,
  tx: Tx
): string | null {
  if (status !== 301 && status !== 308) return null;
  const requested = normalizedMatthewsPropertyUrl(requestedUrl, MATTHEWS_HOST);
  const target = normalizedMatthewsPropertyUrl(location, requestedUrl);
  if (
    !requested
    || !target
    || target === requested
    || matthewsTenureFromUrl(requested) !== tx
    || matthewsTenureFromUrl(target) !== tx
  ) {
    return null;
  }
  return target;
}

export function matthewsProviderIdentity(
  html: string,
  url: string,
  strict = requireFreshDetails()
): string | null {
  const requested = normalizedMatthewsPropertyUrl(url, MATTHEWS_HOST);
  if (!requested) return null;
  const $ = cheerio.load(html);
  const declaredRaw =
    $("link[rel='canonical']").first().attr("href") ??
    $("meta[property='og:url']").first().attr("content") ??
    null;
  const declared = normalizedMatthewsPropertyUrl(declaredRaw, requested);
  if (declaredRaw && declared !== requested) return null;
  if (strict && declared !== requested) return null;
  const identityUrl = declared ?? requested;
  if (matthewsTenureFromUrl(identityUrl) !== matthewsTenureFromUrl(requested)) {
    return null;
  }
  return identityUrl.split("/properties/")[1] ?? null;
}

export type MatthewsParseContext = {
  inventoryObservedAt?: string;
  detailObservedAt?: string;
  strict?: boolean;
};

/**
 * Matthews leaves historical property URLs in its sitemap after the property
 * page has been removed. Those URLs return HTTP 200, self-canonicalize, and
 * carry a Next.js redirect payload instead of a property detail. Treat only
 * this exact provider-originated inactive signal as absent inventory. Every
 * other malformed, blocked, or incomplete detail remains a hard truncation.
 */
export function matthewsProviderNotFound(
  html: string,
  url: string,
  tx: Tx
): boolean {
  if (
    !matthewsProviderIdentity(html, url, true)
    || matthewsTenureFromUrl(url) !== tx
  ) {
    return false;
  }
  const $ = cheerio.load(html);
  const hasPropertyDetailDom = Boolean(
    $("#propertyTitle").length
    || $("#propertyAddress").length
    || $("#propertyPrice").length
    || $(".key-info-title").length
    || $(".key-info-value").length
    || $("#propertyDocumentLink").length
    || $('a[id="agentName"]').length
  );
  return (
    !hasPropertyDetailDom
    && html.includes("NEXT_REDIRECT;replace;/listings;307;")
    && /404\s*-\s*Page Not Found/i.test(html)
  );
}

export function parseMatthewsDetail(
  html: string,
  url: string,
  tx: Tx,
  context: MatthewsParseContext = {}
): any | null {
  const $ = cheerio.load(html);
  const strict = context.strict ?? requireFreshDetails();
  const identity = matthewsProviderIdentity(
    html,
    url,
    strict
  );
  if (!identity || matthewsTenureFromUrl(url) !== tx) return null;
  const propertyTitle = clean($("#propertyTitle").first().text());
  if (strict) {
    const visibleBody = $("body").clone();
    visibleBody.find("script, style, noscript").remove();
    const visibleText = [
      clean($("title").first().text()),
      clean($("h1").first().text()),
      clean(visibleBody.text()),
    ]
      .filter((value): value is string => Boolean(value))
      .join("\n");
    const headingText = [
      clean($("title").first().text()),
      clean($("h1").first().text()),
    ]
      .filter((value): value is string => Boolean(value))
      .join("\n");
    const hasStandalone404Heading = headingText
      .split("\n")
      .some((value) =>
        /^404(?:\s*[-:]\s*(?:page\s*)?not found)?[.!]?\s*$/i.test(value)
      );
    const isErrorShell =
      hasStandalone404Heading
      || /\b(?:not found|error|access denied|forbidden|captcha|just a moment|something went wrong)\b/i.test(
        headingText
      )
      ||
      /\b(?:404(?:\s*[-:]\s*)?(?:page\s*)?not found|page not found|property not found|requested property (?:was )?not found|internal server error|service unavailable|access denied|request blocked|forbidden|captcha|verify you are human|checking (?:your )?browser|just a moment|an? error (?:occurred|has occurred)|something went wrong)\b/i.test(
        visibleText
      )
      || /\b(?:cf-chl-|g-recaptcha|hcaptcha)\b/i.test(html);
    const hasPropertyDetailStructure = Boolean(
      propertyTitle
      && (
        clean($("#propertyAddress").first().text())
        || clean($("#propertyPrice").first().text())
        || (
          $(".key-info-title").length > 0
          && $(".key-info-value").length > 0
        )
        || clean($("#propertyDocumentLink").first().attr("href"))
        || $('a[id="agentName"]').length > 0
      )
    );
    if (isErrorShell || !hasPropertyDetailStructure) return null;
  }
  const title = propertyTitle || clean($("h1").first().text());
  const photos = matthewsImages(html);
  if (!title && photos.length === 0) return null;

  const addr = parseMatthewsAddress(clean($("#propertyAddress").first().text()));
  const priceText = clean($("#propertyPrice").first().text());
  const realPrice =
    priceText && !/call|inquire|contact|request|tbd|offer/i.test(priceText) ? priceText : null;

  const labels: string[] = [];
  const values: string[] = [];
  $(".key-info-title").each((_, el) => {
    const text = clean($(el).text());
    if (text) labels.push(text.replace(/:$/, ""));
  });
  $(".key-info-value").each((_, el) => {
    values.push(clean($(el).text()) ?? "");
  });

  const facts: Record<string, string> = {};
  for (let i = 0; i < Math.min(labels.length, values.length); i++) {
    if (labels[i] && values[i] && !(labels[i] in facts)) facts[labels[i]] = values[i];
  }
  const factGet = (re: RegExp): string | null => {
    const key = Object.keys(facts).find((factKey) => re.test(factKey));
    return key ? facts[key] : null;
  };

  const capText = factGet(/cap\s*rate|^cap\b/i);
  const capRatePct = capText ? Number((capText.match(/([0-9]+(?:\.[0-9]+)?)/) ?? [])[1]) || null : null;
  const assetType = factGet(/^type$|property type/i);
  const leasableText = factGet(/leasable area|building (?:size|sf)|gla|rentable/i);
  const buildingSizeSqft = leasableText ? Number(leasableText.replace(/[^0-9.]/g, "")) || null : null;
  const lotText = factGet(/lot size/i);
  const lotSizeAcres = lotText ? Number((lotText.match(/([0-9.]+)\s*acre/i) ?? [])[1]) || null : null;
  const yearText = factGet(/year built/i);
  const yearBuilt = yearText ? Number((yearText.match(/(\d{4})/) ?? [])[1]) || null : null;

  const highlights: string[] = [];
  $("h3").each((_, el) => {
    if (!/^highlights$/i.test(clean($(el).text()) ?? "")) return;
    const prose = $(el).nextAll(".prose").first();
    const text = prose.length ? prose.text() : "";
    for (const part of text.split(/\u2022|\*/)) {
      const item = clean(part);
      if (item && !highlights.includes(item)) highlights.push(item);
    }
  });

  const docHref = $("#propertyDocumentLink").first().attr("href") ?? null;
  const brochures = docHref
    ? [
        {
          name: "Offering Memorandum",
          url: docHref.startsWith("http") ? docHref : `${MATTHEWS_HOST}${docHref}`,
        },
      ]
    : [];

  const brokerIds = matthewsBrokers($)
    .map((broker) =>
      brokerRef({
        name: broker.name,
        email: broker.email,
        phone: broker.phone,
        avatarUrl: broker.avatarUrl,
        office: null,
        company: "Matthews",
      })
    )
    .filter((id): id is number => id !== null);

  return prune({
    id: identity,
    inventoryObservedAt: context.inventoryObservedAt,
    detailObservedAt: context.detailObservedAt,
    freshnessProvenance:
      context.inventoryObservedAt && context.detailObservedAt
        ? {
            detailScope: "detail_page",
            generationId: refreshGenerationId(),
            method: "matthews_canonical_detail",
            cacheDisposition: "live",
            identityMethod: "canonical_property_url",
          }
        : undefined,
    name: title,
    transactionType: tx === "sale" ? "Sale" : "Lease",
    assetType,
    description: highlights.length ? highlights.join("; ") : null,
    street: addr.street,
    city: addr.city,
    state: addr.state,
    postalCode: addr.postalCode,
    country: "US",
    salePriceUsd: tx === "sale" && realPrice ? moneyToNumber(realPrice) : null,
    salePriceText: tx === "sale" ? realPrice : null,
    capRatePct,
    leaseRateText: tx === "lease" ? realPrice ?? priceText ?? null : null,
    sizeText: leasableText ? `${leasableText} SF` : null,
    buildingSizeSqft,
    lotSizeAcres,
    yearBuilt,
    brokerIds,
    brochures,
    photos,
    url,
    canonicalUrl: url,
    highlights,
  });
}

export function matthewsParsedCoverage(
  parsed: Array<any | null | undefined>,
  providerNotFound = 0,
  permanentRedirectAliases = 0
): {
  listings: any[];
  failures: number;
  providerNotFound: number;
  permanentRedirectAliases: number;
  truncated: boolean;
} {
  const listings = parsed.filter((listing): listing is any => listing != null);
  const failures =
    parsed.length - listings.length - providerNotFound - permanentRedirectAliases;
  if (providerNotFound < 0 || permanentRedirectAliases < 0 || failures < 0) {
    throw new Error("Matthews parsed coverage has an invalid excluded-page count");
  }
  return {
    listings,
    failures,
    providerNotFound,
    permanentRedirectAliases,
    truncated: failures > 0,
  };
}

export async function srcMatthews(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  const sitemap = await matthewsFetch(MATTHEWS_SITEMAP_URL);
  if (!sitemap.html) {
    throw new Error("Matthews: sitemap did not return an XML response body");
  }
  const xml = sitemap.html;
  const inventoryObservedAt = new Date().toISOString();
  const detailUrls = matthewsDetailUrlsFromSitemap(xml);
  if (!detailUrls.length) {
    throw new Error(
      "Matthews: no /properties/ URLs found in sitemap.xml (fetch may have been blocked or transformed)"
    );
  }

  const urls = detailUrls.filter((url) => matthewsTenureFromUrl(url) === tx);
  const urlSet = new Set(urls);
  const take = Number.isFinite(max) ? urls.slice(0, max) : urls;

  if (monitor) {
    const truncated = take.length !== urls.length;
    return {
      company: "Matthews",
      sourceUrl: MATTHEWS_SOURCE_URL,
      method: "Public sitemap.xml enumeration filtered by /properties/leasing-* tenure slug",
      totalAvailable: urls.length,
      truncated,
      note: truncated
        ? `Selected ${take.length}/${urls.length} sitemap detail page(s)`
        : undefined,
      listings: take.map((url) =>
        prune({
          id: (url.split("/properties/")[1] ?? url).replace(/[/?#].*$/, ""),
          url,
          canonicalUrl: url,
          transactionType: tx === "sale" ? "Sale" : "Lease",
        })
      ),
    };
  }

  const parsed = await pmap(take, Math.min(CONCURRENCY, 2), async (url) => {
    try {
      const response = await matthewsFetch(url, true);
      const permanentRedirectTarget = matthewsPermanentRedirectTarget(
        response.status,
        response.location,
        url,
        tx
      );
      if (permanentRedirectTarget) {
        if (urlSet.has(permanentRedirectTarget)) {
          console.warn(
            `  matthews/${tx}: ${url} excluded as permanent redirect alias to ${permanentRedirectTarget}`
          );
          return {
            listing: null,
            providerNotFound: false,
            permanentRedirectAlias: true,
          };
        }
        console.error(
          `  matthews/${tx}: ${url} permanently redirects to a target absent from the fresh sitemap`
        );
        return { listing: null, providerNotFound: false, permanentRedirectAlias: false };
      }
      if (!response.html) {
        console.error(`  matthews/${tx}: ${url} returned HTTP ${response.status}`);
        return { listing: null, providerNotFound: false, permanentRedirectAlias: false };
      }
      const html = response.html;
      if (matthewsProviderNotFound(html, url, tx)) {
        console.warn(`  matthews/${tx}: ${url} excluded as provider 404`);
        return { listing: null, providerNotFound: true, permanentRedirectAlias: false };
      }
      const listing = parseMatthewsDetail(html, url, tx, {
        inventoryObservedAt,
        detailObservedAt: new Date().toISOString(),
      });
      if (!listing) {
        console.error(`  matthews/${tx}: ${url} failed identity or detail validation`);
      }
      return { listing, providerNotFound: false, permanentRedirectAlias: false };
    } catch (err) {
      console.error(`  matthews/${tx}: ${url} failed: ${err}`);
      return { listing: null, providerNotFound: false, permanentRedirectAlias: false };
    }
  });
  const coverage = matthewsParsedCoverage(
    parsed.map((result) => result.listing),
    parsed.filter((result) => result.providerNotFound).length,
    parsed.filter((result) => result.permanentRedirectAlias).length
  );
  const listings = coverage.listings;
  const failures = coverage.failures;
  if (!listings.length) {
    throw new Error("Matthews: sitemap enumerated detail pages but none parsed");
  }
  const incompleteEnumeration = take.length !== urls.length;
  const truncated = coverage.truncated || incompleteEnumeration;
  const verifiedActiveTotal =
    urls.length - coverage.providerNotFound - coverage.permanentRedirectAliases;
  const notes: string[] = [];
  if (coverage.providerNotFound > 0) {
    notes.push(
      `Excluded ${coverage.providerNotFound} sitemap URL(s) with verified Matthews provider 404 responses`
    );
  }
  if (coverage.permanentRedirectAliases > 0) {
    notes.push(
      `Excluded ${coverage.permanentRedirectAliases} sitemap URL(s) with permanent redirects to same-sitemap Matthews property URLs`
    );
  }
  if (failures > 0) {
    notes.push(`${failures} detail page(s) failed to fetch, parse, or validate identity`);
  }
  if (incompleteEnumeration) {
    notes.push(`Selected ${take.length}/${urls.length} sitemap detail page(s)`);
  }

  return {
    company: "Matthews",
    sourceUrl: MATTHEWS_SOURCE_URL,
    method: "Public sitemap.xml enumeration to server-rendered detail pages, DOM parsed via throttled plain fetch",
    totalAvailable: verifiedActiveTotal,
    listings,
    truncated,
    note: notes.length ? notes.join("; ") : undefined,
  };
}
