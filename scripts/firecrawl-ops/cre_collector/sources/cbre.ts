// sources/cbre.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import { createHash, randomUUID } from "node:crypto";

import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { harvestDetail } from "../lib/harvest.js";
import { parseLeaseRate } from "../lib/parse.js";
import { scrapeJson } from "../lib/scrape.js";
import { DocItem, ScrapedDoc, SourceResult, Tx } from "../types.js";
import { clean, num, pmap, prune } from "../lib/util.js";
import {
  detailObservation,
  refreshGenerationId,
  requireFreshDetails,
} from "../lib/freshness.js";

// --- CBRE: internal listings JSON API, paginated, behind Cloudflare (stealth) ---

export function cbreAspect(tx: Tx): string {
  return tx === "sale" ? "isSale" : "isLetting";
}

export function cbreListingSlug(parts: {
  name: string | null;
  street: string | null;
  city: string | null;
  state: string | null;
  zip: string | null;
}): string {
  return [parts.name, parts.street, parts.city, parts.state, parts.zip]
    .filter(Boolean)
    .join(" ")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export function cbreListingUrl(primaryKey: string, slug: string): string {
  return `https://www.cbre.com/properties/properties-for-lease/commercial-space/details/${primaryKey}/${slug}`;
}

export function cbreBrochureUrl(uri: string | null): string {
  const u = clean(uri);
  return u?.startsWith("http") ? u : `https://www.cbre.com${u ?? ""}`;
}

export function cbrePhotoUrl(resourceUri: string | null): string | null {
  const u = clean(resourceUri);
  if (!u) return null;
  return u.startsWith("http") ? u : `https://www.cbre.com${u}`;
}

export function cbreTransactionType(aspects: string[]): string {
  const isSale = aspects.includes("isSale");
  const isLet = aspects.includes("isLetting");
  return isSale && isLet ? "Sale/Lease" : isLet ? "Lease" : "Sale";
}

// Lift stranded structured fields the CBRE listings-api doc exposes but the
// adapter previously dropped, onto the listing keys cre_ingest.to_row maps. CBRE
// is enumeration-only (no detail render), so this reads the JSON doc `d` directly.
// Cap rate, when present, arrives as a Charges entry of kind 'CapRate'/'Yield' or
// a Dynamic field; year built / floors / units arrive as Dynamic.* numerics. Only
// clearly-present values are lifted (prune drops the rest), so a sparse doc never
// clobbers good data. Never throws.
export function cbreStrandedStructured(d: any): Record<string, any> {
  const charges: any[] = Array.isArray(d?.["Common.Charges"])
    ? d["Common.Charges"]
    : [];
  const capCharge = charges.find(
    (c: any) =>
      /cap\s*rate|yield/i.test(String(c?.["Common.ChargeKind"] ?? "")) &&
      num(c?.["Common.Amount"]),
  );
  const capRatePct =
    num(capCharge?.["Common.Amount"]) ??
    num(d?.["Dynamic.CapRate"]) ??
    num(Number(d?.["Dynamic.CapRate"]));
  return (
    prune({
      capRatePct,
      yearBuilt:
        num(d?.["Dynamic.YearBuilt"]) ?? num(Number(d?.["Dynamic.YearBuilt"])),
      floors:
        num(d?.["Dynamic.NumberOfFloors"]) ??
        num(Number(d?.["Dynamic.NumberOfFloors"])),
      units:
        num(d?.["Dynamic.NumberOfUnits"]) ??
        num(Number(d?.["Dynamic.NumberOfUnits"])),
      occupancyRate:
        num(d?.["Dynamic.OccupancyRate"]) ??
        num(Number(d?.["Dynamic.OccupancyRate"])),
      zoning: clean(d?.["Dynamic.Zoning"]),
    }) ?? {}
  );
}

// Classify CBRE brochures by their human BrochureName into typed DocItems
// (an "Offering Memorandum" / "Financials" brochure promotes to docType
// om/financials instead of a flat 'brochure'), then run them through harvestDetail
// so the classification + dedup logic is shared with every other source. CBRE is
// enumeration-only (no detail-page render), so there is no markdown / gallery /
// iframe surface and media/links are always empty. Classified docs ride the
// `documents` channel and `brochures` is left empty, so the same url is never
// inserted into cre_listing_documents twice (that table has no (listing_id,url)
// unique key). One code path => monitor and full are byte-identical for CBRE.
function cbreHarvestDocs(
  d: any,
  brochureItems: Array<{ name: string | null; url: string }>,
): DocItem[] {
  // Pre-classify each brochure by its NAME (the url is an opaque CDN path that
  // rarely carries a keyword). A name-derived docType is passed as a typed
  // DocItem; harvestDetail trusts the given docType and dedups by url.
  const baseUrl = cbreListingUrl(String(d?.["Common.PrimaryKey"] ?? ""), "");
  const extraDocs: DocItem[] = brochureItems
    .filter((b) => /^https?:\/\//i.test(b.url))
    .map((b) => ({
      url: b.url,
      title: b.name,
      docType: cbreDocTypeFromName(b.name),
    }));
  return harvestDetail({} as ScrapedDoc, { baseUrl, extraDocs }).documents;
}

// Extract the WS1 additive scalar fields from a stored CBRE raw_data blob.
// The blob is the JSON the adapter emits (leaseRateText, headline/url already
// present); this function re-derives the NEW camelCase fields from them so tests
// can assert the parse without a network call. Pure: no side effects, never throws.
export function cbreNewFieldsFromRawData(raw: any): {
  canonicalUrl: string | null;
  highlights: string | null;
  leaseRateMin: number | null;
  leaseRateMax: number | null;
  leaseRateType: string | null;
} {
  const url = clean(raw?.url) ?? null;
  const headline = clean(raw?.headline) ?? null;
  const lrt = clean(raw?.leaseRateText) ?? null;
  const lr = parseLeaseRate(lrt);
  return {
    canonicalUrl: url,
    highlights: headline,
    leaseRateMin: lr.min,
    leaseRateMax: lr.max,
    leaseRateType: lr.type,
  };
}

// Map a CBRE brochure display name to a DocItem docType. Mirrors the harvester's
// keyword buckets (most-specific first); defaults to 'brochure' (the prior CBRE
// behavior) when no documentary keyword is present.
export function cbreDocTypeFromName(name: string | null): DocItem["docType"] {
  const hay = (name ?? "").toLowerCase();
  if (/rent[-_ ]?roll/.test(hay)) return "rent_roll";
  if (/financ|pro[-_ ]?forma|proforma|\bt-?12\b/.test(hay)) return "financials";
  if (/floor[-_ ]?plan|site[-_ ]?plan/.test(hay)) return "floor_plan";
  if (/offering|memorandum|\bom\b|teaser/.test(hay)) return "om";
  if (/flyer/.test(hay)) return "flyer";
  return "brochure";
}

export type CbreValidatedPage = {
  total: number;
  documents: any[];
};

export type CbreSnapshot = {
  total: number;
  documents: any[];
  reportedTotals: number[];
  observedAt: string;
  contentDifference?: string;
};

export type CbreSnapshotOptions = {
  pageSize?: number;
  maxPasses?: number;
  maxPages?: number;
  concurrency?: number;
};

type CbrePageFetcher = (page: number, pass: number) => Promise<any>;

const CBRE_PAGE_SIZE = 200;
const CBRE_MAX_SNAPSHOT_PAGES = 1200;

function cbreCacheVariant(
  value: string,
  seed: string,
  distinctPass: number | null = null,
): string {
  // Vary only the raw spelling of already-recognized query values. Percent-
  // encoded ASCII decodes to the exact same site/aspect/page semantics while a
  // generation-and-pass seed avoids reusing one persistent provider-edge key.
  // No cache-buster parameter is added to the provider contract.
  const digest = createHash("sha256").update(seed).digest();
  const chars = [...value];
  let encoded = 0;
  const variant = chars.map((char, index) => {
    // Encode the pass number into the first three raw characters of the site
    // value. Because convergence is capped at five passes, this guarantees a
    // distinct raw URL for every pass even if two hash masks collide. URL
    // parsing still restores the exact same recognized `us-comm` value.
    if (distinctPass !== null && index < 3) {
      if ((distinctPass & (1 << index)) === 0) return char;
      encoded++;
      return `%${char.charCodeAt(0).toString(16).padStart(2, "0")}`;
    }
    if ((digest[index % digest.length] & 1) === 0) return char;
    encoded++;
    const hex = char.charCodeAt(0).toString(16).padStart(2, "0");
    return `%${(digest[(index + 1) % digest.length] & 1) === 0 ? hex : hex.toUpperCase()}`;
  });
  if (encoded === 0 && chars.length > 0) {
    const index = digest[digest.length - 1] % chars.length;
    variant[index] =
      `%${chars[index].charCodeAt(0).toString(16).padStart(2, "0")}`;
  }
  return variant.join("");
}

export function cbreInventoryUrl(
  aspect: string,
  page: number,
  pageSize = CBRE_PAGE_SIZE,
  cacheSeed: string | null = null,
  pass = 1,
): string {
  if (
    !Number.isInteger(page) ||
    page < 1 ||
    !Number.isInteger(pageSize) ||
    pageSize < 1
  ) {
    throw new Error(
      "CBRE inventory URL requires positive integer page metadata",
    );
  }
  if (!Number.isInteger(pass) || pass < 1) {
    throw new Error("CBRE inventory URL requires a positive integer pass");
  }
  const encoded = (name: string, value: string) =>
    cacheSeed
      ? cbreCacheVariant(
          value,
          `${cacheSeed}\u0000${pass}\u0000${name}`,
          name === "site" ? pass : null,
        )
      : encodeURIComponent(value);
  return (
    "https://www.cbre.com/listings-api/propertylistings/query" +
    `?site=${encoded("site", "us-comm")}` +
    `&Common.Aspects=${encoded("Common.Aspects", aspect)}` +
    `&PageSize=${encoded("PageSize", String(pageSize))}` +
    `&Page=${encoded("Page", String(page))}`
  );
}

export function assertCbrePage(
  response: any,
  page: number,
  pageSize: number,
  expectedTotal: number | null,
  strict = requireFreshDetails(),
): CbreValidatedPage {
  if (
    !Number.isInteger(page) ||
    page < 1 ||
    !Number.isInteger(pageSize) ||
    pageSize < 1
  ) {
    throw new Error(
      "CBRE listings API requires positive integer page metadata",
    );
  }
  if (
    !response ||
    typeof response !== "object" ||
    Array.isArray(response) ||
    !Array.isArray(response.Documents)
  ) {
    throw new Error(
      `CBRE listings API page ${page} is missing a Documents array`,
    );
  }
  const total = response.DocumentCount;
  if (!Number.isInteger(total) || total < 0) {
    throw new Error(
      `CBRE listings API page ${page} requires a nonnegative integer DocumentCount`,
    );
  }
  if (strict && expectedTotal !== null && total !== expectedTotal) {
    throw new Error(
      `CBRE listings API DocumentCount changed from ${expectedTotal} to ${total} on page ${page}`,
    );
  }
  if (strict) {
    const providerPage = [
      response.Page,
      response.PageNumber,
      response.CurrentPage,
    ].find((value) => value !== undefined);
    if (
      providerPage !== undefined &&
      (!Number.isInteger(providerPage) || providerPage !== page)
    ) {
      throw new Error(
        `CBRE listings API page metadata expected page ${page}, received ${String(providerPage)}`,
      );
    }
    if (
      response.PageSize !== undefined &&
      (!Number.isInteger(response.PageSize) || response.PageSize !== pageSize)
    ) {
      throw new Error(
        `CBRE listings API page metadata expected PageSize ${pageSize}, received ${String(response.PageSize)}`,
      );
    }
  }

  const documents = response.Documents.flat();
  if (
    strict &&
    documents.some(
      (document: any) =>
        !document || typeof document !== "object" || Array.isArray(document),
    )
  ) {
    throw new Error(
      `CBRE listings API page ${page} contains a malformed document`,
    );
  }
  if (strict) {
    const expectedCount = Math.max(
      0,
      Math.min(pageSize, total - (page - 1) * pageSize),
    );
    if (documents.length !== expectedCount) {
      throw new Error(
        `CBRE listings API page ${page} expected ${expectedCount} documents, received ${documents.length}`,
      );
    }
    const seen = new Set<string>();
    for (const document of documents) {
      const primaryKey = clean(document?.["Common.PrimaryKey"]);
      if (!primaryKey) {
        throw new Error(
          `CBRE listings API page ${page} document lacks a nonempty Common.PrimaryKey`,
        );
      }
      if (seen.has(primaryKey)) {
        throw new Error(
          `CBRE listings API page ${page} has duplicate Common.PrimaryKey ${primaryKey}`,
        );
      }
      seen.add(primaryKey);
    }
  }
  return { total, documents };
}

export function assertCbreAggregate(
  documents: any[],
  total: number,
  fetchedPages: number,
  strict = requireFreshDetails(),
  pageSize = 200,
): void {
  if (!strict) return;
  const expectedCount = Math.min(total, Math.max(1, fetchedPages) * pageSize);
  const seen = new Set<string>();
  for (const document of documents) {
    const primaryKey = clean(document?.["Common.PrimaryKey"]);
    if (!primaryKey) {
      throw new Error(
        "CBRE listings API aggregate contains a document without a nonempty Common.PrimaryKey",
      );
    }
    if (seen.has(primaryKey)) {
      throw new Error(
        `CBRE listings API aggregate has duplicate Common.PrimaryKey ${primaryKey}`,
      );
    }
    seen.add(primaryKey);
  }
  if (documents.length !== expectedCount || seen.size !== expectedCount) {
    throw new Error(
      `CBRE listings API aggregate expected ${expectedCount} unique documents, received ${seen.size}`,
    );
  }
}

function stableCbreJsonValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableCbreJsonValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [
          key,
          stableCbreJsonValue((value as Record<string, unknown>)[key]),
        ]),
    );
  }
  return value;
}

export function cbreSnapshotFingerprint(documents: any[]): string {
  return createHash("sha256")
    .update(JSON.stringify(stableCbreJsonValue(documents)))
    .digest("hex");
}

export function cbreIdentityFingerprint(documents: any[]): string {
  const identities = documents
    .map((document) => clean(document?.["Common.PrimaryKey"]) ?? "<missing>")
    .sort();
  return createHash("sha256").update(JSON.stringify(identities)).digest("hex");
}

export function cbreSnapshotDifference(left: any[], right: any[]): string {
  const byId = (documents: any[]) =>
    new Map(
      documents.map((document) => [
        clean(document?.["Common.PrimaryKey"]) ?? "<missing>",
        document,
      ]),
    );
  const leftById = byId(left);
  const rightById = byId(right);
  const removed = [...leftById.keys()].filter((id) => !rightById.has(id));
  const added = [...rightById.keys()].filter((id) => !leftById.has(id));
  let changedRows = 0;
  const changedFields = new Map<string, number>();
  for (const [id, leftDocument] of leftById) {
    const rightDocument = rightById.get(id);
    if (!rightDocument) continue;
    if (
      cbreSnapshotFingerprint([leftDocument]) ===
      cbreSnapshotFingerprint([rightDocument])
    ) {
      continue;
    }
    changedRows++;
    for (const field of new Set([
      ...Object.keys(leftDocument ?? {}),
      ...Object.keys(rightDocument ?? {}),
    ])) {
      if (
        JSON.stringify(stableCbreJsonValue(leftDocument?.[field])) !==
        JSON.stringify(stableCbreJsonValue(rightDocument?.[field]))
      ) {
        changedFields.set(field, (changedFields.get(field) ?? 0) + 1);
      }
    }
  }
  const leftOrder = [...leftById.keys()];
  const rightOrder = [...rightById.keys()];
  const sameOrder =
    leftOrder.length === rightOrder.length &&
    leftOrder.every((id, index) => id === rightOrder[index]);
  const fieldSummary = [...changedFields.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 8)
    .map(([field, count]) => `${field}:${count}`)
    .join(",");
  return (
    `added=${added.length},removed=${removed.length},changed_rows=${changedRows},` +
    `same_order=${sameOrder}` +
    (fieldSummary ? `,changed_fields=${fieldSummary}` : "")
  );
}

async function fetchCbreSnapshotPass(
  fetchPage: CbrePageFetcher,
  pass: number,
  pageSize: number,
  maxPages: number,
  concurrency: number,
): Promise<
  CbreSnapshot & { fingerprint: string; identityFingerprint: string }
> {
  const pages = new Map<number, CbreValidatedPage>();
  const first = assertCbrePage(
    await fetchPage(1, pass),
    1,
    pageSize,
    null,
    true,
  );
  pages.set(1, first);
  console.error(
    `  cbre: convergence pass ${pass} page 1 ` +
      `(${first.documents.length} docs, reported total ${first.total})`,
  );

  let maxDataPages = Math.ceil(first.total / pageSize);
  while (true) {
    if (maxDataPages > maxPages) {
      throw new Error(
        `CBRE listings API declared ${maxDataPages} pages, exceeding the ${maxPages}-page safety cap`,
      );
    }
    const fetchThrough = Math.max(1, maxDataPages + 1);
    const missingPages = Array.from(
      { length: fetchThrough },
      (_, index) => index + 1,
    ).filter((page) => !pages.has(page));
    if (missingPages.length === 0) break;
    const fetched = await pmap(missingPages, concurrency, async (page) => {
      const value = assertCbrePage(
        await fetchPage(page, pass),
        page,
        pageSize,
        null,
        true,
      );
      console.error(
        `  cbre: convergence pass ${pass} page ${page} ` +
          `(${value.documents.length} docs, reported total ${value.total})`,
      );
      return { page, value };
    });
    for (const entry of fetched) pages.set(entry.page, entry.value);
    maxDataPages = Math.max(
      maxDataPages,
      ...fetched.map((entry) => Math.ceil(entry.value.total / pageSize)),
    );
  }

  const sentinelPage = Math.max(1, maxDataPages + 1);
  const orderedPages = Array.from({ length: sentinelPage }, (_, index) =>
    pages.get(index + 1),
  );
  if (orderedPages.some((page) => !page)) {
    throw new Error("CBRE listings API snapshot is missing a requested page");
  }

  let terminalSeen = false;
  let lastNonemptyPage = 0;
  const documents: any[] = [];
  const reportedTotals: number[] = [];
  for (let index = 0; index < orderedPages.length; index++) {
    const page = orderedPages[index]!;
    const pageNumber = index + 1;
    reportedTotals.push(page.total);
    if (page.documents.length === 0) {
      terminalSeen = true;
      continue;
    }
    if (terminalSeen) {
      throw new Error(
        `CBRE listings API snapshot has a nonempty page ${pageNumber} after a terminal empty page`,
      );
    }
    if (lastNonemptyPage > 0 && documents.length % pageSize !== 0) {
      throw new Error(
        `CBRE listings API snapshot has a short interior page ${lastNonemptyPage}`,
      );
    }
    lastNonemptyPage = pageNumber;
    documents.push(...page.documents);
  }
  if (!terminalSeen) {
    throw new Error(
      "CBRE listings API snapshot is missing an empty sentinel page",
    );
  }

  const seen = new Set<string>();
  for (const document of documents) {
    const primaryKey = clean(document?.["Common.PrimaryKey"]);
    if (!primaryKey) {
      throw new Error(
        "CBRE listings API snapshot contains a document without a nonempty Common.PrimaryKey",
      );
    }
    if (seen.has(primaryKey)) {
      throw new Error(
        `CBRE listings API snapshot has duplicate Common.PrimaryKey ${primaryKey}`,
      );
    }
    seen.add(primaryKey);
  }
  if (seen.size !== documents.length) {
    throw new Error(
      `CBRE listings API snapshot expected ${documents.length} unique documents, received ${seen.size}`,
    );
  }
  if (
    reportedTotals.some((reportedTotal) => reportedTotal !== documents.length)
  ) {
    throw new Error(
      `CBRE listings API snapshot collected ${documents.length} unique documents, ` +
        `but its pages did not all report that total (${[...new Set(reportedTotals)].join(",")})`,
    );
  }
  console.error(
    `  cbre: convergence pass ${pass} assembled ${documents.length} unique records ` +
      `(reported totals ${[...new Set(reportedTotals)].join(",")})`,
  );
  return {
    total: documents.length,
    documents,
    reportedTotals,
    observedAt: new Date().toISOString(),
    fingerprint: cbreSnapshotFingerprint(documents),
    identityFingerprint: cbreIdentityFingerprint(documents),
  };
}

export async function fetchCbreSnapshot(
  fetchPage: CbrePageFetcher,
  options: CbreSnapshotOptions = {},
): Promise<CbreSnapshot> {
  const pageSize = options.pageSize ?? CBRE_PAGE_SIZE;
  const maxPasses = options.maxPasses ?? 3;
  const maxPages = options.maxPages ?? CBRE_MAX_SNAPSHOT_PAGES;
  const concurrency = options.concurrency ?? CONCURRENCY;
  if (!Number.isInteger(pageSize) || pageSize < 1) {
    throw new Error("CBRE snapshot pageSize must be a positive integer");
  }
  if (!Number.isInteger(maxPasses) || maxPasses < 2 || maxPasses > 5) {
    throw new Error("CBRE convergence passes must be an integer from 2 to 5");
  }
  if (
    !Number.isInteger(maxPages) ||
    maxPages < 1 ||
    maxPages > CBRE_MAX_SNAPSHOT_PAGES
  ) {
    throw new Error(
      `CBRE snapshot maxPages must be an integer from 1 to ${CBRE_MAX_SNAPSHOT_PAGES}`,
    );
  }
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 6) {
    throw new Error("CBRE snapshot concurrency must be an integer from 1 to 6");
  }

  let previous: Awaited<ReturnType<typeof fetchCbreSnapshotPass>> | null = null;
  const failures: string[] = [];
  for (let pass = 1; pass <= maxPasses; pass++) {
    let current: Awaited<ReturnType<typeof fetchCbreSnapshotPass>>;
    try {
      current = await fetchCbreSnapshotPass(
        fetchPage,
        pass,
        pageSize,
        maxPages,
        concurrency,
      );
    } catch (error) {
      failures.push(`pass ${pass}: ${String(error)}`);
      previous = null;
      continue;
    }
    if (
      previous &&
      current.total === previous.total &&
      current.identityFingerprint === previous.identityFingerprint
    ) {
      const contentDifference =
        current.fingerprint === previous.fingerprint
          ? undefined
          : cbreSnapshotDifference(previous.documents, current.documents);
      return {
        total: current.total,
        documents: current.documents,
        reportedTotals: current.reportedTotals,
        observedAt: current.observedAt,
        ...(contentDifference ? { contentDifference } : {}),
      };
    }
    if (previous) {
      const difference = cbreSnapshotDifference(
        previous.documents,
        current.documents,
      );
      failures.push(
        `pass ${pass}: inventory membership changed (${previous.total} -> ${current.total}; ${difference})`,
      );
    }
    previous = current;
  }
  throw new Error(
    `CBRE inventory did not converge across ${maxPasses} complete cache-bypassed passes` +
      (failures.length ? ` (${failures.join("; ")})` : ""),
  );
}

export function cbreResultTruncated(
  max: number,
  total: number,
  collected: number,
): boolean {
  const selectedTarget = Math.min(max, total);
  return (
    collected < selectedTarget ||
    (Number.isFinite(max) && selectedTarget < total)
  );
}

export async function srcCbre(
  tx: Tx,
  max: number,
  _monitor: boolean,
): Promise<SourceResult> {
  // Enumeration-only source: the listings-api JSON already returns fully mapped
  // rows with no per-listing detail render, so monitor output == full output.
  const strict = requireFreshDetails();
  if (strict && !refreshGenerationId()) {
    throw new Error("CBRE strict freshness requires CRE_REFRESH_GENERATION");
  }
  const aspect = cbreAspect(tx);
  const opts = {
    proxy: "stealth" as const,
    waitFor: 4000,
    timeout: 120000,
    ...(strict ? { maxAge: 0 } : {}),
  };
  let total: number;
  let collectedDocs: any[];
  let truncated: boolean;
  let snapshotObservedAt: string | null = null;
  let note: string | undefined;
  if (strict && !Number.isFinite(max)) {
    const cacheSeed = `${refreshGenerationId()}:${Date.now()}:${randomUUID()}`;
    const snapshot = await fetchCbreSnapshot((page, pass) =>
      scrapeJson(
        cbreInventoryUrl(aspect, page, CBRE_PAGE_SIZE, cacheSeed, pass),
        opts,
      ),
    );
    total = snapshot.total;
    collectedDocs = snapshot.documents;
    truncated = false;
    snapshotObservedAt = snapshot.observedAt;
    if (snapshot.contentDifference) {
      note =
        `membership-converged with later-pass content churn ` +
        `(${snapshot.contentDifference})`;
    }
    console.error(
      `  cbre/${tx}: membership-converged ${total} unique records across two complete passes`,
    );
  } else {
    // Finite probes and non-strict development runs retain the existing
    // page-1-declared cap behavior. They remain explicitly truncated when a
    // finite max selects less than the provider total and are never lifecycle
    // authority for a strict unlimited refresh.
    const first = assertCbrePage(
      await scrapeJson(cbreInventoryUrl(aspect, 1), opts),
      1,
      CBRE_PAGE_SIZE,
      null,
      strict,
    );
    total = first.total;
    const want = Math.min(max, total);
    const pages = Math.ceil(want / CBRE_PAGE_SIZE);
    console.error(`  cbre/${tx}: ${total} total, fetching ${pages} page(s)`);
    const docsArr: any[][] = [first.documents];
    if (pages > 1) {
      const pageNums = Array.from({ length: pages - 1 }, (_, i) => i + 2);
      const rest = await pmap(pageNums, CONCURRENCY, async (p) => {
        const raw = await scrapeJson(cbreInventoryUrl(aspect, p), opts);
        if (!strict && !Array.isArray(raw?.Documents)) {
          console.error(
            `  cbre/${tx}: page ${p}/${pages} returned no Documents array`,
          );
          return [];
        }
        const page = assertCbrePage(raw, p, CBRE_PAGE_SIZE, total, strict);
        console.error(
          `  cbre/${tx}: page ${p}/${pages} (${page.documents.length} docs)`,
        );
        return page.documents;
      });
      docsArr.push(...rest);
    }
    collectedDocs = docsArr.flat();
    assertCbreAggregate(collectedDocs, total, pages, strict);
    truncated = cbreResultTruncated(max, total, collectedDocs.length);
  }
  const want = Math.min(max, total);
  const docs = collectedDocs.slice(0, want);
  const observed = detailObservation(
    "cbre_listings_api",
    strict ? "live" : "generation_cache",
    snapshotObservedAt ?? new Date().toISOString(),
  );
  const text = (loc: any) =>
    Array.isArray(loc) && loc.length ? clean(loc[0]["Common.Text"]) : null;
  const listings = docs.map((d: any) => {
    const addr = d["Common.ActualAddress"] ?? {};
    const charges: any[] = Array.isArray(d["Common.Charges"])
      ? d["Common.Charges"]
      : [];
    const sale = charges.find(
      (c: any) =>
        c["Common.ChargeKind"] === "SalePrice" && num(c["Common.Amount"]),
    );
    const rent = charges.find(
      (c: any) => c["Common.ChargeKind"] === "Rent" && num(c["Common.Amount"]),
    );
    const coord = d["Common.Coordinate"] ?? {};
    const aspects: string[] = Array.isArray(d["Common.Aspects"])
      ? d["Common.Aspects"]
      : [];
    const name = clean(addr["Common.Line1"]);
    const street = clean(addr["Common.Line2"]);
    const city = clean(addr["Common.Locallity"]);
    const state = clean(addr["Common.Region"]);
    const zip = clean(addr["Common.PostCode"]);
    const slug = cbreListingSlug({ name, street, city, state, zip });
    const brokerIds = (
      Array.isArray(d["Common.Agents"]) ? d["Common.Agents"] : []
    )
      .map((a: any) =>
        brokerRef({
          name: clean(a["Common.AgentName"]),
          email: clean(a["Common.EmailAddress"]),
          phone: clean(a["Common.TelephoneNumber"]),
          office: clean(a["Common.AgentOffice"]),
          company: "CBRE",
        }),
      )
      .filter((x: number | null): x is number => x !== null);
    const brochureItems = (
      Array.isArray(d["Common.Brochures"]) ? d["Common.Brochures"] : []
    ).map((b: any) => ({
      name: clean(b["Common.BrochureName"]),
      url: cbreBrochureUrl(clean(b["Common.Uri"])),
    }));
    const photoUrls = (
      Array.isArray(d["Common.Photos"]) ? d["Common.Photos"] : []
    )
      .map((p: any) => {
        const r =
          (p["Common.ImageResources"] ?? []).find(
            (x: any) => x["Common.Breakpoint"] === "original",
          ) ?? (p["Common.ImageResources"] ?? [])[0];
        return r ? cbrePhotoUrl(clean(r["Common.Resource.Uri"])) : null;
      })
      .filter((u: string | null): u is string => Boolean(u));
    const listingUrl = cbreListingUrl(d["Common.PrimaryKey"], slug);
    const leaseRateText = rent
      ? `${rent["Common.Amount"]} ${clean(rent["Common.ChargeCurrency"]) ?? "USD"}/${clean(rent["Common.ChargeInterval"]) ?? ""} ${clean(rent["Common.ChargeBasis"]) ?? ""}`.trim()
      : null;
    const lr = parseLeaseRate(leaseRateText);
    return {
      id: d["Common.PrimaryKey"],
      name,
      headline: text(d["Common.Strapline"]),
      // WS1: lift highlights from the CBRE strapline/headline field
      highlights: text(d["Common.Strapline"]) ?? undefined,
      transactionType: cbreTransactionType(aspects),
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
      salePriceText:
        sale || tx === "lease" ? null : "Contact broker for pricing",
      leaseRateText,
      // WS1: parse lease rate into typed camelCase fields via parseLeaseRate
      leaseRateMin: lr.min ?? undefined,
      leaseRateMax: lr.max ?? undefined,
      leaseRateType: lr.type ?? undefined,
      buildingSizeSqft: num(d["Dynamic.TotalArea"]),
      ...cbreStrandedStructured(d),
      brokerIds,
      // Brochures are classified by name into typed DocItems on the `documents`
      // channel; `brochures` left empty to avoid a double-insert (no unique key
      // on cre_listing_documents). CBRE has no detail page, so no media/links.
      brochures: [],
      documents: cbreHarvestDocs(d, brochureItems),
      photos: photoUrls,
      url: listingUrl,
      // WS1: canonicalUrl from the listing URL (col currently ~0%; raw_data->>'url' ~92%)
      canonicalUrl: listingUrl,
      lastUpdated: clean(d["Common.LastUpdated"])?.slice(0, 10) ?? null,
      created: clean(d["Common.Created"])?.slice(0, 10) ?? null,
      inventoryObservedAt: observed.observedAt,
      detailObservedAt: observed.observedAt,
      freshnessProvenance: {
        detailScope: "authoritative_inventory_feed",
        generationId: observed.generationId,
        method: observed.method,
        cacheDisposition: observed.cacheDisposition,
        identityMethod: "Common.PrimaryKey",
      },
    };
  });
  return {
    company: "CBRE",
    sourceUrl: `https://www.cbre.com/properties (${aspect})`,
    method: "CBRE public listings API (JSON, paginated, stealth proxy)",
    totalAvailable: total,
    listings,
    truncated,
    ...(note ? { note } : {}),
  };
}
