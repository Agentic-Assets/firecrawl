import { createHash } from "node:crypto";
import { observationNow } from "../lib/observation-clock.js";
import * as cheerio from "cheerio";
import { brokerRef } from "../lib/broker.js";
import { detailObservation, refreshGenerationId, refreshStartedAt, requireFreshDetails } from "../lib/freshness.js";
import { dedupeStrings, stripHtmlText, titleFromFilename } from "../lib/html.js";
import { SourceResult, Tx, type DaumInventoryPass, type DaumInventoryRow, type DaumDetail, type DaumEmptyTitleObservation } from "../types.js";
import { daumProof } from "./pure/daum-proof.js";
import { clean, moneyToNumber, prune } from "../lib/util.js";
import { canonicalDaumPropertyUrl, daumAbsoluteUrl, daumListingId, DAUM_HOST, DAUM_SEARCH_URL, daumPageUrl, daumTenure, resolveDaumTenure, type DaumTenure } from "./pure/daum-identity.js";
import { DaumPoliteTransport } from "./daum-transport.js";

export { canonicalDaumPropertyUrl, daumListingId, DAUM_HOST, DAUM_SEARCH_URL, daumPageUrl, daumTenure, resolveDaumTenure, type DaumTenure } from "./pure/daum-identity.js";
export { DAUM_MAX_RESPONSE_BYTES, DAUM_ROBOTS_DELAY_MS, DaumPoliteTransport } from "./daum-transport.js";
export type { DaumInventoryRow, DaumDetail, DaumEmptyTitleObservation } from "../types.js";
export const DAUM_PAGE_SIZE = 90;


export type DaumPage = {
  reportedTotal: number;
  reportedPages: number;
  rows: DaumInventoryRow[];
};


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

export function parseDaumDetail(html: string, expectedUrl?: string): DaumDetail;
export function parseDaumDetail(html: string, expectedUrl: string, allowEmptyTitle: true): DaumDetail | DaumEmptyTitleObservation;
export function parseDaumDetail(html: string, expectedUrl?: string, allowEmptyTitle = false): DaumDetail | DaumEmptyTitleObservation {
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
  const tenureText = clean($(".main-section .title-about").first().text());
  if (!title && allowEmptyTitle && $(".main-section .title-property").length === 1) {
    const canonical = canonicalDaumPropertyUrl($('link[rel="canonical"]').attr("href"));
    if ($('link[rel="canonical"]').length !== 1 || $('link[rel="shortlink"]').length !== 1
      || !canonical || canonical !== expectedUrl) throw new Error("DAUM empty-title observation has ambiguous identity");
    return { postId, observedCanonicalUrl: canonical, tenure: daumTenure(tenureText), tenureText, emptyRequiredTitle: true };
  }
  if (!title) throw new Error(`DAUM detail ${postId} omitted its property title`);
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
    observedCanonicalUrl: canonicalDaumPropertyUrl($('link[rel="canonical"]').attr("href")),
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

export function daumSnapshotIdentitySignature(rows: DaumInventoryRow[]): string {
  return rows.map((row) => `${row.url}\t${row.tenure}`).sort().join("\n");
}

export function assertDaumUniqueProviderIds(listings: Array<{ id?: unknown; daumPostId?: unknown }>): void {
  const ids = new Set<string>();
  const keys = new Set<string>();
  for (const listing of listings) {
    const id = clean(String(listing?.daumPostId ?? ""));
    const key = clean(String(listing?.id ?? ""));
    if (!id || !/^[1-9]\d*$/.test(id) || !Number.isSafeInteger(Number(id))) throw new Error("DAUM emitted a listing without a valid provider ID");
    if (!key) throw new Error("DAUM emitted a listing without a listing key");
    if (ids.has(id)) throw new Error(`DAUM emitted duplicate provider ID ${id}`);
    if (keys.has(key)) throw new Error(`DAUM emitted duplicate listing key ${key}`);
    ids.add(id);
    keys.add(key);
  }
}

type DaumSearchTransport = Pick<DaumPoliteTransport, "post">;

async function fetchDaumSnapshotPass(
  transport: DaumSearchTransport, index: number
): Promise<{
  total: number; rows: DaumInventoryRow[]; reportedTotals: number[];
  signature: string; proof: DaumInventoryPass;
}> {
  const receipts: DaumInventoryPass["pages"] = [];
  async function load(pageNumber: number): Promise<DaumPage> {
    const endpoint = daumPageUrl(pageNumber);
    const html = await transport.post(endpoint);
    const observedAt = observationNow();
    const page = parseDaumSearchPage(html);
    receipts.push({
      endpoint, method: "POST", observedAt, responseTextSha256: textSha256(html),
      reportedTotal: page.reportedTotal, reportedPages: page.reportedPages,
      members: page.rows.map(row => [row.url, row.tenure, row.tenureText]),
    });
    return page;
  }
  // Empty-form POST retains the public query-free route and existing pacing.
  const first = await load(1);
  const pages = [first];
  for (let page = 2; page <= first.reportedPages; page++) pages.push(await load(page));
  const rows = assertDaumSnapshot(pages);
  console.error(`  daum-commercial: inventory pass ${index} completed; pages=${pages.length}; members=${rows.length}`);
  return {
    total: first.reportedTotal, rows,
    reportedTotals: pages.map(page => page.reportedTotal),
    signature: daumSnapshotIdentitySignature(rows),
    proof: { index, total: first.reportedTotal, pages: receipts },
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
  passes: DaumInventoryPass[];
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
        proof: DaumInventoryPass;
      }
    | null = null;
  const failures: string[] = [];
  for (let pass = 1; pass <= maxPasses; pass++) {
    let current;
    try {
      current = await fetchDaumSnapshotPass(transport, pass);
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
        observedAt: observationNow(),
        passes: [previous.proof, current.proof],
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
    id: daumListingId(inventory.url),
    daumPostId: String(detail.postId),
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
    daumTenureEvidence: {
      inventory: inventory.tenure, inventoryText: inventory.tenureText,
      detail: detail.tenure, detailText: detail.tenureText, resolved: resolvedTenure,
    },
    daumObservedCanonicalUrl: detail.observedCanonicalUrl,
    daumSearchCard: inventory,
    detailFacts: detail.facts,
  });
}

const textSha256 = (text: string) => createHash("sha256").update(text, "utf8").digest("hex");
export type DaumCompleteCohort = {
  snapshot: Awaited<ReturnType<typeof fetchDaumSnapshot>>;
  details: Array<{ inventory: DaumInventoryRow; detail: DaumDetail | DaumEmptyTitleObservation; observedAt: string; bodySha256: string }>;
};
type DaumProofMode = "complete" | "verified_subset" | "verified_subset_empty_title";
const daumCohortAcquisitions: Partial<Record<DaumProofMode, { key: string; promise: Promise<DaumCompleteCohort> }>> = {};

async function completeDaumCohort(key: string, transport: DaumPoliteTransport, mode: DaumProofMode): Promise<DaumCompleteCohort> {
  let acquisition = daumCohortAcquisitions[mode];
  if (acquisition?.key !== key) {
    // Both transactions share even a rejection; each mode retains its own episode.
    acquisition = { key, promise: Promise.resolve().then(() => fetchCompleteDaumCohort(transport, mode)) };
    daumCohortAcquisitions[mode] = acquisition;
  }
  return structuredClone(await acquisition.promise);
}

async function fetchCompleteDaumCohort(transport: DaumPoliteTransport, mode: DaumProofMode): Promise<DaumCompleteCohort> {
  const snapshot = await fetchDaumSnapshot(transport);
  console.error(`  daum-commercial: inventory matched; starting detail acquisition; members=${snapshot.rows.length}`);
  const work = snapshot.rows.map((inventory, index) => ({ inventory, index }))
    .sort((a, b) => Number(b.inventory.tenure === "unknown") - Number(a.inventory.tenure === "unknown"));
  const acquired: Array<{ index: number; row: DaumCompleteCohort["details"][number] }> = [];
  for (const { inventory, index } of work) {
    const html = await transport.get(inventory.url);
    const observedAt = observationNow();
    try {
      const detail = mode === "verified_subset_empty_title" ? parseDaumDetail(html, inventory.url, true) : parseDaumDetail(html, inventory.url);
      if (detail.observedCanonicalUrl !== inventory.url) throw new Error("DAUM strict proof requires observed canonical identity");
      try {
        if (resolveDaumTenure(inventory.tenure, detail.tenure) === "unknown" && mode === "complete") throw new Error("DAUM strict proof has unresolved tenure");
      } catch (error) {
        if (error instanceof Error) {
          error.message += ` [postId=${detail.postId}; ordinal=${index + 1}/${snapshot.rows.length}; inventoryTenure=${inventory.tenure}; inventoryTextPresent=${Boolean(inventory.tenureText)}; detailTenure=${detail.tenure}; detailTextPresent=${Boolean(detail.tenureText)}]`;
        }
        throw error;
      }
      acquired.push({ index, row: { inventory, detail, observedAt, bodySha256: textSha256(html) } });
      if (acquired.length % 100 === 0 && acquired.length < work.length) {
        console.error(`  daum-commercial: detail acquisition checked=${acquired.length}/${work.length}`);
      }
    } catch (error) {
      if (error instanceof Error) error.message += ` [inventoryUrl=${inventory.url}]`;
      throw error;
    }
  }
  const details = acquired.sort((a, b) => a.index - b.index).map(entry => entry.row);
  assertDaumUniqueProviderIds(details.map(row => ({ id: daumListingId(row.inventory.url), daumPostId: row.detail.postId })));
  console.error(`  daum-commercial: detail acquisition finished; checked=${details.length}/${snapshot.rows.length}`);
  return { snapshot, details };
}


export async function srcDaumCommercial(
  tx: Tx,
  max: number,
  _monitor: boolean
): Promise<SourceResult> {
  const subsetPolicy = process.env.DAUM_SUBSET_POLICY ?? "tenure-only-v1";
  if (!["tenure-only-v1", "tenure-and-empty-title-v2"].includes(subsetPolicy)
    || (subsetPolicy !== "tenure-only-v1" && process.env.DAUM_VERIFIED_SUBSET !== "1")) throw new Error("DAUM subset policy requires its exact explicit subset permission");
  const transport = new DaumPoliteTransport();
  const generationId = refreshGenerationId();
  const generationStartedAt = refreshStartedAt();
  if (requireFreshDetails() && !_monitor && !Number.isFinite(max) && generationId && generationStartedAt) {
    const mode: DaumProofMode = process.env.DAUM_VERIFIED_SUBSET === "1" ? subsetPolicy === "tenure-and-empty-title-v2" ? "verified_subset_empty_title" : "verified_subset" : "complete";
    const cohort = await completeDaumCohort(JSON.stringify([mode, generationId, generationStartedAt, subsetPolicy]), transport, mode);
    const proof = daumProof(cohort, tx, generationId, generationStartedAt, mode);
    const held = new Set(proof.schema === "daum-strict-detail-v1" ? [] : proof.held.map(row => row[0]));
    const listings = cohort.details.filter(row => !held.has(String(row.detail.postId)) && daumBelongsToTx(resolveDaumTenure(row.inventory.tenure, row.detail.tenure), tx))
      .map(row => {
        if ("emptyRequiredTitle" in row.detail) throw new Error("DAUM empty-title observation cannot enter listing mapping");
        return { ...mapDaumListing(row.inventory, row.detail, tx, cohort.snapshot.observedAt, row.observedAt), daumDetailResponseSha256: row.bodySha256 };
      });
    return {
      company: "DAUM Commercial Real Estate Services", sourceUrl: DAUM_SEARCH_URL,
      method: "complete consecutive public POST snapshots plus every fresh canonical detail",
      totalAvailable: listings.length, listings,
      truncated: proof.schema !== "daum-strict-detail-v1" && proof.held.length > 0,
      currentInventoryProof: proof,
    };
  }
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
    const detailObservedAt = observationNow();
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
