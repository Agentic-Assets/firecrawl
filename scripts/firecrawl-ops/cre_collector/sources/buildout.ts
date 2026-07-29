// sources/buildout.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { parseJsonBody, scrapeDoc, scrapeJson } from "../lib/scrape.js";
import { SourceResult, Tx } from "../types.js";
import { harvestDetail } from "../lib/harvest.js";
import { clean, isPerSfPriceText, moneyToNumber, num, pmap } from "../lib/util.js";
import { isPerSfText, parseLeaseRate, parseSizeText } from "../lib/parse.js";
import {
  generationMatches,
  refreshGenerationId,
  requireFreshDetails,
} from "../lib/freshness.js";


// --- Buildout platform (SVN, Lee & Associates): inventory JSON API, paginated ---
// The inventory feed has no server-side sale/lease filter; items carry a `sale`
// boolean (false = lease availability). Fetch the full inventory once per
// brokerage (cached across the sale and lease passes) and partition client-side.

type BuildoutInventoryResult = {
  items: any[];
  total: number | null;
  strictValidated?: boolean;
  generationId?: string | null;
};

export const buildoutCache = new Map<string, BuildoutInventoryResult>();
export const buildoutFailureCache = new Map<string, Error>();

export type BuildoutInventoryOpts = {
  preferDirectJson?: boolean;
  directReferer?: string;
  pageConcurrency?: number;
  requireCompletePages?: boolean;
  cacheSlug?: string;
  usePageCache?: boolean;
  recoveryPasses?: number;
  recoveryCooldownMs?: number;
  maxRecoveryPages?: number;
  jsonAttempts?: number;
  jsonBackoffMs?: number;
};

export function buildoutInventoryUrl(pluginKey: string, page: number): string {
  return `https://buildout.com/plugins/${pluginKey}/inventory.json?page=${page}`;
}

type BuildoutInventoryPageExpectation = {
  total: number;
  limit: number;
};

export function assertBuildoutInventoryPage(
  data: any,
  page: number,
  expected: BuildoutInventoryPageExpectation | null = null,
  strict = requireFreshDetails()
): BuildoutInventoryPageExpectation {
  if (!strict) {
    return {
      total: Number.isInteger(data?.meta?.total) && data.meta.total >= 0 ? data.meta.total : 0,
      limit: Number.isInteger(data?.meta?.limit) && data.meta.limit > 0 ? data.meta.limit : 30,
    };
  }
  if (!Array.isArray(data?.inventory)) {
    throw new Error(`Buildout page ${page} response lacks an inventory array`);
  }
  const total = data?.meta?.total;
  if (!Number.isInteger(total) || total < 0) {
    throw new Error(`Buildout page ${page} response lacks a valid integer meta.total`);
  }
  const limit = data?.meta?.limit;
  if (!Number.isInteger(limit) || limit <= 0) {
    throw new Error(`Buildout page ${page} response lacks a valid positive integer meta.limit`);
  }
  if (expected && (total !== expected.total || limit !== expected.limit)) {
    throw new Error(
      `Buildout page ${page} metadata changed from total=${expected.total}, limit=${expected.limit} ` +
        `to total=${total}, limit=${limit}`
    );
  }
  const pages = Math.max(1, Math.ceil(total / limit));
  if (!Number.isInteger(page) || page < 0 || page >= pages) {
    throw new Error(`Buildout page ${page} falls outside the declared ${pages}-page inventory`);
  }
  const expectedRows = page < pages - 1 ? limit : total - page * limit;
  if (data.inventory.length !== expectedRows) {
    throw new Error(
      `Buildout page ${page} expected ${expectedRows} inventory rows from total=${total}, ` +
        `limit=${limit}, received ${data.inventory.length}`
    );
  }
  return { total, limit };
}

export function assertBuildoutInventoryReconciled(
  items: any[],
  total: number | null,
  strict = requireFreshDetails()
): void {
  if (!strict) return;
  if (!Number.isInteger(total) || (total as number) < 0) {
    throw new Error("Buildout strict inventory reconciliation requires a valid provider total");
  }
  const identities = new Set<string>();
  for (const item of items) {
    const id =
      typeof item?.id === "string" || typeof item?.id === "number"
        ? clean(String(item.id))
        : null;
    if (!id) {
      throw new Error("Buildout inventory row is missing a stable id");
    }
    if (identities.has(id)) {
      throw new Error(`Buildout duplicate inventory identity ${id}`);
    }
    identities.add(id);
  }
  if (identities.size !== total) {
    throw new Error(
      `Buildout reconciled ${identities.size} unique inventory rows against provider total ${total}`
    );
  }
}

// Parse a Buildout "Available" attribute (e.g. "175 - 2,396 SF" or "4,750 SF")
// into the available / min-divisible / max-divisible square-foot triple that
// cre_ingest.to_row lifts into existing cre_listings columns. Returns an empty
// object when no numeric SF is present. Pure; never throws.
export function buildoutAvailableSf(text: string | null | undefined): {
  availableSf?: number;
  minDivisibleSf?: number;
  maxDivisibleSf?: number;
} {
  const t = clean(text);
  if (!t || !/sf|sq|square/i.test(t)) return {};
  const nums = (t.match(/\d[\d,]*(?:\.\d+)?/g) ?? [])
    .map((n) => Number(n.replace(/,/g, "")))
    .filter((n) => Number.isFinite(n) && n > 0);
  if (!nums.length) return {};
  if (nums.length === 1) return { availableSf: nums[0], minDivisibleSf: nums[0], maxDivisibleSf: nums[0] };
  const lo = Math.min(...nums);
  const hi = Math.max(...nums);
  return { availableSf: lo, minDivisibleSf: lo, maxDivisibleSf: hi };
}

export function envBool(name: string): boolean {
  return ["1", "true", "yes", "on"].includes((process.env[name] ?? "").toLowerCase());
}

/**
 * Opt in to a live Buildout inventory pass when a normal source run would
 * otherwise reuse its durable page cache.  The cache is still written with
 * successfully fetched pages so the recovery path has the newest good copy.
 *
 * This deliberately does not bypass the in-process inventory cache: sale and
 * lease collection share the one live inventory read made by this invocation.
 */
export function buildoutRefreshPageCache(): boolean {
  return envBool("BUILDOUT_REFRESH_PAGE_CACHE");
}

export function buildoutPageCachePolicy(opts: BuildoutInventoryOpts): {
  read: boolean;
  write: boolean;
} {
  const cacheOnly = envBool("BUILDOUT_CACHE_ONLY");
  const assembleFromCache = envBool("BUILDOUT_ASSEMBLE_FROM_CACHE");
  const refresh = buildoutRefreshPageCache();
  if (refresh && (cacheOnly || assembleFromCache)) {
    throw new Error(
      "BUILDOUT_REFRESH_PAGE_CACHE=1 cannot be combined with BUILDOUT_CACHE_ONLY=1 or BUILDOUT_ASSEMBLE_FROM_CACHE=1"
    );
  }
  const enabled =
    refresh ||
    opts.usePageCache ||
    envBool("BUILDOUT_USE_PAGE_CACHE") ||
    cacheOnly ||
    assembleFromCache;
  return { read: enabled && !refresh, write: enabled };
}

export function envInt(name: string): number | null {
  const raw = process.env[name];
  if (raw === undefined || raw.trim() === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) ? Math.max(0, Math.trunc(n)) : null;
}

export function buildoutCacheSlug(company: string, pluginKey: string, opts: BuildoutInventoryOpts): string {
  return (
    opts.cacheSlug ??
    company
      .toLowerCase()
      .replace(/&/g, "and")
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "") ??
    pluginKey.slice(0, 12)
  );
}

export function buildoutCacheDir(): string {
  return process.env.BUILDOUT_CACHE_DIR ?? "out/cache/buildout";
}

export function buildoutPageCachePath(company: string, pluginKey: string, page: number, opts: BuildoutInventoryOpts): string {
  return `${buildoutCacheDir()}/${buildoutCacheSlug(company, pluginKey, opts)}/page-${String(page).padStart(4, "0")}.json`;
}

type BuildoutPageObservation = {
  observedAt: string;
  generationId: string | null;
  cacheDisposition: "live" | "generation_cache";
};

function annotateBuildoutPage(data: any, observation: BuildoutPageObservation): any {
  if (!data || typeof data !== "object") return data;
  Object.defineProperty(data, "__creFreshness", {
    value: observation,
    enumerable: false,
    configurable: true,
  });
  return data;
}

function buildoutInventoryRows(data: any): any[] {
  const observation = data?.__creFreshness as BuildoutPageObservation | undefined;
  return (Array.isArray(data?.inventory) ? data.inventory : []).map((item: any) => ({
    ...item,
    __creFreshness: observation,
  }));
}

export function readBuildoutPageCache(
  company: string,
  pluginKey: string,
  page: number,
  opts: BuildoutInventoryOpts
): any | null {
  const path = buildoutPageCachePath(company, pluginKey, page, opts);
  if (!existsSync(path)) return null;
  try {
    const cached = JSON.parse(readFileSync(path, "utf8"));
    if (cached.pluginKey !== pluginKey || cached.page !== page) return null;
    if (!generationMatches(cached.generationId)) return null;
    const data = cached.data;
    if (!data || !Array.isArray(data.inventory)) return null;
    if (typeof cached.cachedAt !== "string" || !Number.isFinite(Date.parse(cached.cachedAt))) {
      return null;
    }
    return annotateBuildoutPage(data, {
      observedAt: cached.cachedAt,
      generationId: cached.generationId ?? null,
      cacheDisposition: "generation_cache",
    });
  } catch {
    return null;
  }
}

export function writeBuildoutPageCache(
  company: string,
  pluginKey: string,
  page: number,
  opts: BuildoutInventoryOpts,
  data: any
): void {
  if (!data || !Array.isArray(data.inventory)) return;
  const path = buildoutPageCachePath(company, pluginKey, page, opts);
  mkdirSync(dirname(path), { recursive: true });
  const tmp = `${path}.tmp-${process.pid}-${Date.now()}`;
  const observation = data.__creFreshness as BuildoutPageObservation | undefined;
  const cachedAt = observation?.observedAt ?? new Date().toISOString();
  writeFileSync(
    tmp,
    JSON.stringify(
      {
        pluginKey,
        page,
        cachedAt,
        generationId: observation?.generationId ?? refreshGenerationId(),
        data,
      },
      null,
      2
    )
  );
  renameSync(tmp, path);
}

export function buildoutPageWindow(pages: number): { start: number; end: number } | null {
  const start = envInt("BUILDOUT_PAGE_START");
  const end = envInt("BUILDOUT_PAGE_END");
  if (start === null && end === null) return null;
  const lo = Math.max(0, start ?? 0);
  const hi = Math.min(pages - 1, end ?? lo);
  if (hi < lo) throw new Error(`invalid Buildout page window ${lo}-${hi}`);
  return { start: lo, end: hi };
}

export function buildoutJitterMs(): [number, number] | null {
  const raw = process.env.BUILDOUT_PAGE_JITTER_MS;
  if (!raw) return null;
  const parts = raw.split(",").map((p) => Number(p.trim()));
  if (!parts.every(Number.isFinite)) return null;
  const lo = Math.max(0, Math.trunc(parts[0] ?? 0));
  const hi = Math.max(lo, Math.trunc(parts[1] ?? parts[0] ?? 0));
  return [lo, hi];
}

export async function sleepBuildoutJitter(): Promise<void> {
  const jitter = buildoutJitterMs();
  if (!jitter || jitter[1] <= 0) return;
  const [lo, hi] = jitter;
  const ms = lo + Math.floor(Math.random() * (hi - lo + 1));
  await new Promise((r) => setTimeout(r, ms));
}

export async function directBuildoutJson(url: string, referer: string, timeoutMs = 15000): Promise<any> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      signal: controller.signal,
      headers: {
        accept: "application/json,text/plain,*/*",
        "user-agent":
          "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
        referer,
      },
    });
    const body = await res.text();
    const parsed = parseJsonBody(body);
    if (res.ok && parsed !== null) return parsed;
    const contentType = res.headers.get("content-type") ?? "unknown";
    const shape = parsed === null ? "non-json" : "json-error-status";
    throw new Error(`direct GET ${res.status} ${contentType} (${shape})`);
  } finally {
    clearTimeout(timer);
  }
}

export async function fetchBuildoutInventoryPage(
  company: string,
  pluginKey: string,
  page: number,
  opts: BuildoutInventoryOpts,
  expected: BuildoutInventoryPageExpectation | null = null
): Promise<any> {
  const url = buildoutInventoryUrl(pluginKey, page);
  const cachePolicy = buildoutPageCachePolicy(opts);
  if (cachePolicy.read) {
    const cached = readBuildoutPageCache(company, pluginKey, page, opts);
    if (cached) return cached;
    if (envBool("BUILDOUT_ASSEMBLE_FROM_CACHE")) {
      throw new Error(`missing Buildout cache page ${page} for ${buildoutCacheSlug(company, pluginKey, opts)}`);
    }
  }
  await sleepBuildoutJitter();
  let data: any;
  if (opts.preferDirectJson) {
    try {
      data = await directBuildoutJson(url, opts.directReferer ?? "https://buildout.com/");
      if (page === 0) console.error(`  ${company}: direct Buildout JSON available`);
    } catch (err) {
      console.error(`  ${company}: direct Buildout JSON failed for page ${page} (${err}); falling back to Firecrawl`);
    }
  }
  data ??= await scrapeJson(url, {
    timeout: 60000,
    jsonAttempts: opts.jsonAttempts,
    jsonBackoffMs: opts.jsonBackoffMs,
    ...(requireFreshDetails() ? { maxAge: 0 } : {}),
  });
  // Reject malformed, partial, or cross-page-incoherent strict pages before
  // they can poison the generation-scoped recovery cache.
  assertBuildoutInventoryPage(data, page, expected);
  annotateBuildoutPage(data, {
    observedAt: new Date().toISOString(),
    generationId: refreshGenerationId(),
    cacheDisposition: "live",
  });
  if (cachePolicy.write) writeBuildoutPageCache(company, pluginKey, page, opts, data);
  return data;
}

export async function buildoutInventory(
  company: string,
  pluginKey: string,
  opts: BuildoutInventoryOpts = {}
): Promise<{ items: any[]; total: number | null }> {
  const strictFreshness = requireFreshDetails();
  const cached = buildoutCache.get(pluginKey);
  if (
    cached &&
    (!strictFreshness ||
      (cached.strictValidated === true && generationMatches(cached.generationId)))
  ) {
    return cached;
  }
  if (cached) buildoutCache.delete(pluginKey);
  const cachedFailure = buildoutFailureCache.get(pluginKey);
  if (cachedFailure) throw cachedFailure;
  const first = await fetchBuildoutInventoryPage(company, pluginKey, 0, opts);
  const firstPage = assertBuildoutInventoryPage(first, 0, null, strictFreshness);
  const total: number | null = strictFreshness ? firstPage.total : first.meta?.total ?? null;
  const limit: number = strictFreshness ? firstPage.limit : first.meta?.limit ?? 30;
  const pages = total && total > limit ? Math.min(Math.ceil(total / limit), 1200) : 1;
  const pageWindow = buildoutPageWindow(pages);
  const cacheOnly = envBool("BUILDOUT_CACHE_ONLY");
  const assembleFromCache = envBool("BUILDOUT_ASSEMBLE_FROM_CACHE");
  if (pageWindow) {
    console.error(
      `  ${company}: Buildout page window ${pageWindow.start}-${pageWindow.end}/${pages - 1}${
        cacheOnly ? " (cache-only)" : assembleFromCache ? " (assembling from cache)" : ""
      }`
    );
  }
  const inventoryByPage = new Map<number, any[]>();
  inventoryByPage.set(0, buildoutInventoryRows(first));
  const failedPages = new Set<number>();
  const attemptedPages = new Set<number>([0]);
  const unattemptedPages = new Set<number>();
  if (total && total > limit) {
    const failureLimit = Math.max(3, Math.floor(pages * 0.03));
    const allPageNums = Array.from({ length: pages - 1 }, (_, i) => i + 1);
    const pageNums =
      pageWindow && !assembleFromCache
        ? allPageNums.filter((p) => p >= pageWindow.start && p <= pageWindow.end)
        : allPageNums;
    if (pageWindow && !assembleFromCache) {
      for (const p of allPageNums) {
        if (!pageNums.includes(p)) unattemptedPages.add(p);
      }
    }
    const pageConcurrency = Math.max(1, Math.min(opts.pageConcurrency ?? Math.min(CONCURRENCY, 2), pageNums.length));
    let done = 1;
    let aborting = false;
    await pmap(pageNums, pageConcurrency, async (p) => {
      if (aborting) {
        unattemptedPages.add(p);
        return;
      }
      try {
        attemptedPages.add(p);
        const d = await fetchBuildoutInventoryPage(
          company,
          pluginKey,
          p,
          opts,
          strictFreshness ? firstPage : null
        );
        assertBuildoutInventoryPage(d, p, firstPage, strictFreshness);
        inventoryByPage.set(p, buildoutInventoryRows(d));
        done++;
        if (done % 25 === 0) console.error(`  ${company}: inventory page ${done}/${pages}`);
      } catch (err) {
        failedPages.add(p);
        console.error(`  ${company}: inventory page ${p} FAILED after retries: ${err}`);
        if (failedPages.size > failureLimit) {
          aborting = true;
        }
      }
    });

    const recoveryPasses = assembleFromCache ? 0 : opts.recoveryPasses ?? 0;
    const maxRecoveryPages = opts.maxRecoveryPages ?? failureLimit;
    for (let pass = 1; pass <= recoveryPasses && failedPages.size > 0; pass++) {
      if (failedPages.size > maxRecoveryPages) break;
      const retryPages = [...failedPages].sort((a, b) => a - b);
      console.error(
        `  ${company}: retrying ${retryPages.length} failed Buildout page(s), recovery pass ${pass}/${recoveryPasses}`
      );
      await new Promise((r) => setTimeout(r, opts.recoveryCooldownMs ?? 15000));
      for (const p of retryPages) {
        try {
          let d: any;
          if (opts.preferDirectJson) {
            try {
              d = await directBuildoutJson(
                buildoutInventoryUrl(pluginKey, p),
                opts.directReferer ?? "https://buildout.com/"
              );
            } catch {
              d = await fetchBuildoutInventoryPage(
                company,
                pluginKey,
                p,
                {
                  ...opts,
                  jsonAttempts: opts.jsonAttempts ?? 4,
                  jsonBackoffMs: opts.jsonBackoffMs ?? 12000,
                },
                strictFreshness ? firstPage : null
              );
            }
          } else {
            d = await fetchBuildoutInventoryPage(
              company,
              pluginKey,
              p,
              {
                ...opts,
                jsonAttempts: opts.jsonAttempts ?? 4,
                jsonBackoffMs: opts.jsonBackoffMs ?? 12000,
              },
              strictFreshness ? firstPage : null
            );
          }
          assertBuildoutInventoryPage(d, p, firstPage, strictFreshness);
          if (!d?.__creFreshness) {
            annotateBuildoutPage(d, {
              observedAt: new Date().toISOString(),
              generationId: refreshGenerationId(),
              cacheDisposition: "live",
            });
          }
          inventoryByPage.set(p, buildoutInventoryRows(d));
          if (buildoutPageCachePolicy(opts).write) {
            writeBuildoutPageCache(company, pluginKey, p, opts, d);
          }
          failedPages.delete(p);
          console.error(`  ${company}: recovered Buildout inventory page ${p}`);
        } catch (err) {
          console.error(`  ${company}: inventory page ${p} still failed on recovery pass ${pass}: ${err}`);
        }
      }
    }

    if (cacheOnly) {
      const missingAttempted = [...pageNums].filter((p) => !inventoryByPage.has(p));
      const cacheError = new Error(
        `${company}: cache-only Buildout window complete (${pageWindow?.start ?? 0}-${pageWindow?.end ?? pages - 1}); ` +
          `${attemptedPages.size} page(s) attempted, ${missingAttempted.length} selected page(s) missing; not producing listing artifact`
      );
      buildoutFailureCache.set(pluginKey, cacheError);
      throw cacheError;
    }

    if (pageWindow && !assembleFromCache) {
      const windowError = new Error(
        `${company}: Buildout page window was requested without BUILDOUT_CACHE_ONLY=1 or BUILDOUT_ASSEMBLE_FROM_CACHE=1; refusing partial listing artifact`
      );
      buildoutFailureCache.set(pluginKey, windowError);
      throw windowError;
    }

    if (assembleFromCache) {
      for (let p = 0; p < pages; p++) {
        if (!inventoryByPage.has(p)) failedPages.add(p);
      }
    }

    // A few rate-limited pages are tolerable (gap fills on the next run);
    // a large gap means the feed is unusable and must not be cached or
    // ingested (mark-missing on a gappy run would soft-delete live rows).
    if (opts.requireCompletePages ? failedPages.size > 0 : failedPages.size > failureLimit) {
      const failed = [...failedPages].sort((a, b) => a - b);
      const shown = failed.slice(0, 20).join(",");
      const suffix = failed.length > 20 ? `... (+${failed.length - 20} more)` : "";
      const unattempted = unattemptedPages.size ? `, ${unattemptedPages.size} unattempted page(s)` : "";
      const abortError = new Error(
        `${company}: ${failedPages.size}/${pages} inventory pages failed (${shown}${suffix}${unattempted}); aborting this source`
      );
      buildoutFailureCache.set(pluginKey, abortError);
      throw abortError;
    }
  }
  const items: any[] = [];
  for (let p = 0; p < pages; p++) items.push(...(inventoryByPage.get(p) ?? []));
  assertBuildoutInventoryReconciled(items, total, strictFreshness);
  const result: BuildoutInventoryResult = {
    items,
    total,
    strictValidated: strictFreshness,
    generationId: refreshGenerationId(),
  };
  buildoutCache.set(pluginKey, result);
  console.error(
    `  ${company}: full inventory cached (${items.length} items, total ${total ?? "?"}${failedPages.size ? `, ${failedPages.size} pages skipped` : ""})`
  );
  return result;
}

// --- Tier-B (enrich-input only) Buildout detail iframe resolver + enricher ---
//
// The bulk srcBuildout path reads inventory.json ONLY; the per-property
// media / virtual-tour / full image gallery / OM documents live inside the
// Buildout detail IFRAME, which the bulk path never renders. Those are captured
// forward-only via the monitor -> cre_enrichment_queue -> cre_enrich.py Tier-B
// worker, which calls collect.ts --enrich-input. This block is invoked ONLY on
// that enrich path; it does NOT add detail rendering to the daily bulk collect.

// Static per-source Buildout coordinates the EnrichItem does not carry: the
// plugin key and the brokerage host that compose the iframe content URL. The
// host is also recoverable from item.url, but pinning it keeps a malformed
// show_link from yielding a wrong-host iframe URL.
export const BUILDOUT_ENRICH_CONFIG: Record<string, { pluginKey: string; host: string; company: string }> = {
  svn: {
    pluginKey: "b933480474026c41d248b77156c84aef37dcac68",
    host: "svn.com",
    company: "SVN",
  },
  "lee-associates": {
    pluginKey: "9a64a93980aeae8db347e72cdfa8ca61017acc9a",
    host: "www.lee-associates.com",
    company: "Lee & Associates",
  },
};

// Pull the Buildout property slug (the show_link `?propertyId=` value) off a
// listing url, dropping the dual-mode `-sale` / `-lease` suffix so a sale and a
// lease url resolve to the same iframe. Returns null when no propertyId is
// present. Pure; never throws.
export function buildoutSlugFromUrl(url: string | null): string | null {
  const u = clean(url);
  if (!u) return null;
  try {
    const parsed = new URL(u);
    const pid = parsed.searchParams.get("propertyId");
    if (pid) return pid.replace(/-(?:sale|lease)$/i, "") || null;
  } catch {
    const m = u.match(/[?&]propertyId=([^&#]+)/i);
    if (m) return decodeURIComponent(m[1]).replace(/-(?:sale|lease)$/i, "") || null;
  }
  return null;
}

// Compose the Buildout detail iframe content URL for a source. Returns null when
// the slug cannot be derived (so the enricher skips the item, leaving its claim
// queued for the weekly additive backstop). Pure; never throws.
//   buildout.com/plugins/<key>/<host>/inventory/<slug>?pluginId=0&iframe=true&embedded=true
export function buildoutDetailIframeUrl(sourceKey: string, listingUrl: string | null): string | null {
  const cfg = BUILDOUT_ENRICH_CONFIG[sourceKey];
  if (!cfg) return null;
  const slug = buildoutSlugFromUrl(listingUrl);
  if (!slug) return null;
  return (
    `https://buildout.com/plugins/${cfg.pluginKey}/${cfg.host}/inventory/` +
    `${encodeURIComponent(slug)}?pluginId=0&iframe=true&embedded=true`
  );
}

// Tier-B detail enricher for a single Buildout (svn / lee-associates) listing.
// Scrapes the detail iframe URL with the capture-everything format set, runs
// harvestDetail over the rendered doc, and returns an additive row that echoes
// the input url (URL-keyed completion) plus the harvested media/links/documents/
// images and full-page markdown. Returns null on a derivation or scrape failure
// so the worker leaves the claim queued. NOT used by the bulk collect path.
export async function enrichBuildoutDetail(sourceKey: string, listingUrl: string): Promise<any | null> {
  const iframeUrl = buildoutDetailIframeUrl(sourceKey, listingUrl);
  if (!iframeUrl) {
    console.error(`  enrich/buildout(${sourceKey}): no iframe url for ${listingUrl}`);
    return null;
  }
  let doc;
  try {
    doc = await scrapeDoc(iframeUrl, {
      waitFor: 1500,
      timeout: 60000,
      ...(requireFreshDetails() ? { maxAge: 0 } : {}),
    });
  } catch (err) {
    console.error(`  enrich/buildout(${sourceKey}): detail iframe scrape failed for ${listingUrl}: ${err}`);
    return null;
  }
  const harvested = harvestDetail(doc, { baseUrl: iframeUrl });
  // Echo the ORIGINAL listing url (not the iframe url) so cre_ingest.to_row
  // recomputes the same Buildout external_id (?propertyId=...) and the worker
  // marks the claim done by url. The artifact is additive: only the harvested
  // child arrays + markdown are populated; price/status are left to the inventory
  // feed (the iframe has no authoritative status field worth flipping).
  const cfg = BUILDOUT_ENRICH_CONFIG[sourceKey];
  return {
    id: buildoutSlugFromUrl(listingUrl),
    url: listingUrl,
    sourceCompany: cfg?.company,
    documents: harvested.documents,
    media: harvested.media,
    links: harvested.links,
    photos: harvested.images,
    markdown: typeof doc.markdown === "string" && doc.markdown ? doc.markdown : undefined,
    buildoutDetailIframeUrl: iframeUrl,
  };
}

// --- Pure listing builder (exported for unit testing) ---
//
// Extracts the Phase-2 camelCase scalar fields from a raw Buildout inventory
// item (x) plus the transaction type. This is the pure, no-network transform
// that tests can call directly against fixture blobs. `srcBuildout` calls this
// internally via the inline expansion; the function below mirrors that logic
// exactly so the test can reach it without triggering network I/O.
//
// The raw_data blobs stored in the DB are already the transformed listing objects
// (after the adapter runs). For forward-path testing, we test the scalar parsers
// directly against stored raw_data fields (leaseRateText, sizeText, salePriceText,
// underContract, url) that the adapter would have consumed from the inventory item.
export function buildoutScalarFields(raw: {
  url?: string | null;
  leaseRateText?: string | null;
  sizeText?: string | null;
  salePriceText?: string | null;
  underContract?: boolean;
  transactionMode?: string;
}): {
  canonicalUrl: string | null;
  leaseRateMin: number | null;
  leaseRateMax: number | null;
  leaseRateType: string | null;
  lotSf: number | null;
  statusBadge: string | null;
  salePricePsfGuard: boolean;
} {
  const canonicalUrl = clean(raw.url) ?? null;

  const parsedRate = parseLeaseRate(raw.leaseRateText ?? null);
  const leaseRateMin = parsedRate.min ?? null;
  const leaseRateMax = parsedRate.max ?? null;
  const leaseRateType = parsedRate.type ?? null;

  const { lotSf } = parseSizeText(raw.sizeText ?? null);

  const statusBadge = raw.underContract === true ? "under_contract" : null;

  // DQ guard: is the salePriceText a per-SF value?
  const salePricePsfGuard = isPerSfText(raw.salePriceText ?? null);

  return {
    canonicalUrl,
    leaseRateMin,
    leaseRateMax,
    leaseRateType,
    lotSf: lotSf ?? null,
    statusBadge,
    salePricePsfGuard,
  };
}

export async function srcBuildout(
  company: string,
  pluginKey: string,
  listingsPage: string,
  tx: Tx,
  max: number,
  _monitor: boolean,
  inventoryOpts: BuildoutInventoryOpts = {}
): Promise<SourceResult> {
  // Enumeration-only source: the Buildout inventory API has no per-listing detail
  // render, so monitor output == full output (status/price/id are all in-feed).
  const { items, total } = await buildoutInventory(company, pluginKey, inventoryOpts);
  const eligibleItems = items.filter((x) => {
    if (x.closed === true) return false;
    const isSale = x.sale === true;
    return tx === "sale" ? isSale : !isSale;
  });
  const listings: any[] = [];
  for (const x of eligibleItems) {
    if (listings.length >= max) break;
    const isSale = x.sale === true;
    const isLease = x.sale !== true; // inventory rows are active availabilities: not-for-sale = for-lease
    {
      const attrs = new Map<string, string>(
        (x.index_attributes ?? []).map((p: any) => [String(p[0]), String(p[1])])
      );
      const priceText = attrs.get("Price") ?? null;
      const leaseRateText = attrs.get("Lease Rate") ?? attrs.get("Rate") ?? null;
      const sizeText =
        attrs.get("Building Size") ?? attrs.get("Lot Size") ?? clean(x.size_summary);
      // Stranded structured-field lift: the inventory "Available" attribute is a
      // dropped available / divisible square-foot signal. Only non-null parsed
      // values are spread in, so a row without an Available attr is unchanged.
      const availSf = buildoutAvailableSf(attrs.get("Available"));
      const brokerIds = (x.broker_contacts ?? [])
        .map((b: any) =>
          brokerRef({
            name: clean(b.name),
            email: clean(b.email),
            phone: clean(b.phone),
            avatarUrl: clean(b.photo_url),
            company,
          })
        )
        .filter((v: number | null): v is number => v !== null);

      // --- Phase-2 scalar lift (additive; emits nullable camelCase fields) ---

      // canonicalUrl: the listing URL is the canonical identifier for Buildout rows.
      // COALESCE at backfill time (primary -> secondary_pass -> top-level url).
      const canonicalUrl = clean(x.show_link) ?? null;

      // Lease rate: parse the dense Buildout string ('$35 SF/yr (NNN)',
      // '$1.59 - 1.70 SF/month', '$2.50 - 250 SF/month' suite-size mis-range).
      // parseLeaseRate handles annualization, range rejection, and type extraction.
      const parsedRate = parseLeaseRate(leaseRateText);
      const leaseRateMin = parsedRate.min ?? null;
      const leaseRateMax = parsedRate.max ?? null;
      const leaseRateType = parsedRate.type ?? null;

      // lotSf: route acreage sizeText to lot (not building) SF.
      // parseSizeText returns { sizeSf, lotSf }; an "Acres" token fills lotSf.
      const { lotSf } = parseSizeText(sizeText);

      // statusBadge: underContract flag -> feeds the existing OPT-IN activation gate.
      // Never written directly to status; cre_ingest STATUS_SOURCE_PATHS routes it.
      const statusBadge = x.under_contract === true ? "under_contract" : null;

      // DQ guard (Lee & Associates): salePriceUsd in the Buildout feed conflates
      // an absolute price and a per-SF rate ('$6.00/SF' stored as salePriceUsd:6).
      // Use isPerSfText on salePriceText to detect this and suppress the bad absolute
      // while routing the per-SF value to salePricePerSf instead.
      // Note: the adapter already applies isPerSfPriceText (from lib/util.ts) on
      // priceText from the attrs map; this guard applies isPerSfText (from lib/parse.ts,
      // the contract-specified helper) on the top-level salePriceText field.
      const rawSalePriceText = tx === "sale" ? priceText : null;
      const isSalePsf = isPerSfText(rawSalePriceText);

      listings.push({
        id: x.id != null ? String(x.id) : null,
        name: clean(x.display_name) ?? clean(x.name),
        transactionType: x.also_for_sale_or_lease ? "Sale/Lease" : isLease ? "Lease" : "Sale",
        assetType: clean(x.property_sub_type_name) ?? attrs.get("Property Type") ?? null,
        street: clean(x.address),
        city: clean(x.city),
        state: clean(x.state),
        postalCode: clean(x.zip),
        country: "US",
        latitude: num(x.latitude),
        longitude: num(x.longitude),
        // Apply the isPerSfText DQ guard: if salePriceText is a per-SF value,
        // suppress salePriceUsd (do not emit a false absolute price) and place
        // the value in salePricePerSf instead.
        salePriceUsd: tx === "sale" && !isSalePsf && !isPerSfPriceText(priceText) ? moneyToNumber(priceText) : null,
        salePricePerSf: tx === "sale" && (isSalePsf || isPerSfPriceText(priceText)) ? moneyToNumber(priceText) : null,
        salePriceText: rawSalePriceText,
        leaseRateText,
        leaseRateMin: leaseRateMin,
        leaseRateMax: leaseRateMax,
        leaseRateType: leaseRateType,
        sizeText,
        lotSf: lotSf ?? null,
        canonicalUrl,
        statusBadge,
        ...availSf,
        brokerIds,
        brochures: x.pdf_url ? [{ name: "Listing brochure (PDF)", url: x.pdf_url }] : [],
        photos: [x.photo_url, x.large_thumbnail_url].filter(Boolean).slice(0, 1),
        url: clean(x.show_link),
        underContract: x.under_contract === true,
        inventoryObservedAt: x.__creFreshness?.observedAt,
        freshnessProvenance: {
          detailScope: "authoritative_inventory_feed",
          generationId: x.__creFreshness?.generationId ?? refreshGenerationId(),
          method: "buildout_inventory_feed",
          cacheDisposition: x.__creFreshness?.cacheDisposition ?? "live",
        },
      });
    }
  }
  return {
    company,
    sourceUrl: listingsPage,
    method: "Buildout plugin inventory API (JSON, paginated)",
    totalAvailable: total,
    listings,
    truncated: buildoutCapTruncated(max, listings.length, eligibleItems.length),
  };
}

export function buildoutCapTruncated(
  max: number,
  emitted: number,
  knownEligible: number
): boolean {
  return Number.isFinite(max) && emitted < knownEligible;
}
