import * as cheerio from "cheerio";
import { detailObservation, refreshGenerationId, requireFreshDetails } from "../lib/freshness.js";
import { parseMoney } from "../lib/parse.js";
import { clean, prune } from "../lib/util.js";
import type { SourceResult, Tx } from "../types.js";

export const INTERRA_LISTINGS_URL = "https://interrarealty.com/listings/";
export const INTERRA_INVENTORY_URL =
  "https://interrarealty.com/wp-json/filtered-loop/v1/posts?post_type=torque_listing&posts_per_page=1000&paged=1";
export const INTERRA_MAX_RESPONSE_BYTES = 32 * 1024 * 1024;
export const INTERRA_TIMEOUT_MS = 60_000;

export type InterraLifecycle = "available" | "under_contract" | "closed";

export type InterraInventory = {
  rows: any[];
  total: number;
  lifecycleCounts: Record<InterraLifecycle, number>;
};

function directUrl(value: unknown, expectedHost: string): string | null {
  const raw = clean(value);
  if (!raw) return null;
  try {
    const url = new URL(raw);
    if (
      url.protocol !== "https:"
      || url.hostname.toLowerCase() !== expectedHost
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

export async function boundedText(
  url: string,
  fetchImpl: typeof fetch = fetch,
  timeoutMs = INTERRA_TIMEOUT_MS,
  maxBytes = INTERRA_MAX_RESPONSE_BYTES
): Promise<string> {
  const safeUrl = directUrl(url, "interrarealty.com");
  if (!safeUrl) throw new Error("Interra direct transport rejected URL");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(safeUrl, {
      headers: {
        Accept: "application/json",
        "User-Agent": "Mozilla/5.0 CRE collector",
        "Cache-Control": "no-cache",
      },
      cache: "no-store",
      redirect: "error",
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`Interra inventory HTTP ${response.status}`);
    const declaredLength = Number(response.headers.get("content-length"));
    if (Number.isFinite(declaredLength) && declaredLength > maxBytes) {
      throw new Error(`Interra inventory exceeds ${maxBytes} bytes`);
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
          throw new Error(`Interra inventory exceeds ${maxBytes} bytes`);
        }
        text += decoder.decode(value, { stream: true });
      }
      text += decoder.decode();
      return text;
    } finally {
      reader.releaseLock();
    }
  } finally {
    clearTimeout(timer);
  }
}

export function parseInterraInventory(payload: unknown): InterraInventory {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("Interra inventory response must be an object");
  }
  const root = payload as Record<string, any>;
  if (root.success !== true || !Array.isArray(root.posts)) {
    throw new Error("Interra inventory requires success=true and a posts array");
  }
  if (root.has_next_page !== false) {
    throw new Error("Interra inventory must prove has_next_page=false");
  }
  if (root.posts.length === 0) {
    throw new Error("Interra inventory is unexpectedly empty");
  }

  const lifecycleCounts: Record<InterraLifecycle, number> = {
    available: 0,
    under_contract: 0,
    closed: 0,
  };
  const identities = new Set<string>();
  for (const [index, row] of root.posts.entries()) {
    if (!row || typeof row !== "object" || Array.isArray(row)) {
      throw new Error(`Interra inventory row ${index} is not an object`);
    }
    const id =
      typeof row.ID === "number" && Number.isSafeInteger(row.ID) && row.ID > 0
        ? String(row.ID)
        : null;
    if (!id) throw new Error(`Interra inventory row ${index} requires a positive integer ID`);
    if (identities.has(id)) throw new Error(`Interra inventory duplicate ID ${id}`);
    identities.add(id);

    const status = clean(row.meta?.listing_status)?.toLowerCase();
    if (
      status !== "available"
      && status !== "under_contract"
      && status !== "closed"
    ) {
      throw new Error(`Interra inventory row ${id} has unknown lifecycle ${status ?? "missing"}`);
    }
    lifecycleCounts[status]++;

    const permalink = directUrl(row.permalink, "interrarealty.com");
    if (!permalink || !new URL(permalink).pathname.startsWith("/listing/")) {
      throw new Error(`Interra inventory row ${id} has an invalid permalink`);
    }
    if (!clean(row.post_title)) {
      throw new Error(`Interra inventory row ${id} has no title`);
    }
  }
  const reconciled =
    lifecycleCounts.available
    + lifecycleCounts.under_contract
    + lifecycleCounts.closed;
  if (reconciled !== root.posts.length) {
    throw new Error(
      `Interra lifecycle reconciliation failed (${reconciled} != ${root.posts.length})`
    );
  }
  return { rows: root.posts, total: root.posts.length, lifecycleCounts };
}

export async function fetchInterraInventory(
  fetchImpl: typeof fetch = fetch
): Promise<InterraInventory> {
  const text = await boundedText(INTERRA_INVENTORY_URL, fetchImpl);
  let payload: unknown;
  try {
    payload = JSON.parse(text);
  } catch {
    throw new Error("Interra inventory returned malformed JSON");
  }
  return parseInterraInventory(payload);
}

function htmlText(value: unknown): string | null {
  const html = typeof value === "string" ? value : "";
  return clean(cheerio.load(`<div>${html}</div>`)("div").first().text());
}

function htmlList(value: unknown): string[] {
  const html = typeof value === "string" ? value : "";
  const $ = cheerio.load(`<div>${html}</div>`);
  const out: string[] = [];
  $("li").each((_, element) => {
    const item = clean($(element).text());
    if (item) out.push(item);
  });
  return [...new Set(out)];
}

function interraKeyDetails(meta: any): Map<string, string> {
  const count = Number(meta?.key_details);
  if (!Number.isInteger(count) || count < 0 || count > 100) {
    throw new Error("Interra listing has malformed key_details count");
  }
  const details = new Map<string, string>();
  for (let index = 0; index < count; index++) {
    const key = clean(meta?.[`key_details_${index}_name`])?.toLowerCase();
    const value = clean(meta?.[`key_details_${index}_value`]);
    if (!key && !value) continue;
    if (!key || !value) {
      throw new Error(`Interra listing has incomplete key detail ${index}`);
    }
    if (details.has(key)) throw new Error(`Interra listing has duplicate key detail ${key}`);
    details.set(key, value);
  }
  return details;
}

function parenthesizedInteger(value: string | undefined): number | null {
  if (!value) return null;
  const candidate = value.match(/\(([\d,]+)\)/)?.[1] ?? value.match(/\b([\d,]+)\b/)?.[1];
  if (!candidate) return null;
  const parsed = Number(candidate.replace(/,/g, ""));
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function interraLocation(value: unknown): {
  city: string | null;
  state: string | null;
  postalCode: string | null;
} {
  const location = clean(value);
  const match = location?.match(/^(.+?),\s*([A-Za-z]{2})(?:\s+(\d{5}(?:-\d{4})?))?$/);
  return {
    city: clean(match?.[1]),
    state: match?.[2]?.toUpperCase() ?? null,
    postalCode: match?.[3] ?? null,
  };
}

export function mapInterraListing(
  row: any,
  observedAt: string,
  generationId = refreshGenerationId()
): any {
  const inventory = parseInterraInventory({
    success: true,
    posts: [row],
    has_next_page: false,
  });
  const validated = inventory.rows[0];
  const details = interraKeyDetails(validated.meta);
  const location = interraLocation(validated.meta?.listing_city);
  const status = clean(validated.meta?.listing_status)?.toLowerCase() as InterraLifecycle;
  const permalink = directUrl(validated.permalink, "interrarealty.com")!;
  const propertyTypes = Array.isArray(validated.terms)
    ? validated.terms
        .filter((term: any) => term?.taxonomy === "interra_listing_property_type")
        .map((term: any) => clean(term?.name))
        .filter(Boolean)
    : [];
  const observation = detailObservation(
    "interra_filtered_loop_inventory",
    "live",
    observedAt,
    { generationId }
  );
  return prune({
    id: String(validated.ID),
    name: clean(validated.post_title),
    transactionType: "Sale",
    assetType: propertyTypes.join(", "),
    description: htmlText(validated.post_content),
    street: clean(validated.meta?.listing_address),
    ...location,
    country: "US",
    salePriceUsd: parseMoney(details.get("sale price") ?? null),
    salePriceText: details.get("sale price"),
    units: parenthesizedInteger(details.get("number of units")),
    submarket: details.get("submarket"),
    highlights: htmlList(validated.meta?.listing_highlights),
    photos: directUrl(validated.thumbnail, "interrarealty.com")
      ? [directUrl(validated.thumbnail, "interrarealty.com")]
      : [],
    url: permalink,
    canonicalUrl: permalink,
    lastUpdated: clean(validated.post_modified_gmt)
      ? `${clean(validated.post_modified_gmt)!.replace(" ", "T")}Z`
      : null,
    statusBadge: status === "under_contract" ? "Under Contract" : "Available",
    inventoryObservedAt: observedAt,
    detailObservedAt: observedAt,
    freshnessProvenance: {
      ...observation,
      detailScope: "authoritative_inventory_feed",
    },
    preserveChildCollections: true,
  });
}

export async function srcInterraRealty(
  tx: Tx,
  max: number,
  _monitor: boolean,
  fetchImpl: typeof fetch = fetch
): Promise<SourceResult> {
  if (tx === "lease") {
    return {
      company: "Interra Realty",
      sourceUrl: INTERRA_LISTINGS_URL,
      method: "Sale-only source",
      totalAvailable: 0,
      listings: [],
    };
  }
  const strict = requireFreshDetails();
  const generationId = refreshGenerationId();
  if (strict && !generationId) {
    throw new Error("Interra strict refresh requires CRE_REFRESH_GENERATION");
  }
  const inventory = await fetchInterraInventory(fetchImpl);
  const observedAt = new Date().toISOString();
  const active = inventory.rows.filter((row) =>
    ["available", "under_contract"].includes(
      clean(row.meta?.listing_status)?.toLowerCase() ?? ""
    )
  );
  const selected = active.slice(0, Math.max(0, max));
  const listings = selected.map((row) =>
    mapInterraListing(row, observedAt, generationId)
  );
  return {
    company: "Interra Realty",
    sourceUrl: INTERRA_LISTINGS_URL,
    method: "Direct complete filtered-loop JSON inventory (no LLM)",
    totalAvailable: active.length,
    listings,
    truncated: listings.length < active.length,
    note:
      `Lifecycle reconciliation: ${inventory.lifecycleCounts.available} available + `
      + `${inventory.lifecycleCounts.under_contract} under contract + `
      + `${inventory.lifecycleCounts.closed} closed = ${inventory.total}`,
  };
}
