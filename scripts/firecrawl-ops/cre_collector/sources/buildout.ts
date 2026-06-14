// sources/buildout.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { parseJsonBody, scrapeJson } from "../lib/scrape.js";
import { SourceResult, Tx } from "../types.js";
import { clean, isPerSfPriceText, moneyToNumber, num, pmap } from "../lib/util.js";


// --- Buildout platform (SVN, Lee & Associates): inventory JSON API, paginated ---
// The inventory feed has no server-side sale/lease filter; items carry a `sale`
// boolean (false = lease availability). Fetch the full inventory once per
// brokerage (cached across the sale and lease passes) and partition client-side.

export const buildoutCache = new Map<string, { items: any[]; total: number | null }>();
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

export function envBool(name: string): boolean {
  return ["1", "true", "yes", "on"].includes((process.env[name] ?? "").toLowerCase());
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
    const data = cached.data;
    if (!data || !Array.isArray(data.inventory)) return null;
    return data;
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
  writeFileSync(
    tmp,
    JSON.stringify(
      {
        pluginKey,
        page,
        cachedAt: new Date().toISOString(),
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
  opts: BuildoutInventoryOpts
): Promise<any> {
  const url = buildoutInventoryUrl(pluginKey, page);
  const useCache = opts.usePageCache || envBool("BUILDOUT_USE_PAGE_CACHE") || envBool("BUILDOUT_CACHE_ONLY") || envBool("BUILDOUT_ASSEMBLE_FROM_CACHE");
  if (useCache) {
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
  });
  if (useCache) writeBuildoutPageCache(company, pluginKey, page, opts, data);
  return data;
}

export async function buildoutInventory(
  company: string,
  pluginKey: string,
  opts: BuildoutInventoryOpts = {}
): Promise<{ items: any[]; total: number | null }> {
  const cached = buildoutCache.get(pluginKey);
  if (cached) return cached;
  const cachedFailure = buildoutFailureCache.get(pluginKey);
  if (cachedFailure) throw cachedFailure;
  const first = await fetchBuildoutInventoryPage(company, pluginKey, 0, opts);
  const total: number | null = first.meta?.total ?? null;
  const limit: number = first.meta?.limit ?? 30;
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
  inventoryByPage.set(0, first.inventory ?? []);
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
        const d = await fetchBuildoutInventoryPage(company, pluginKey, p, opts);
        inventoryByPage.set(p, d.inventory ?? []);
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
              d = await fetchBuildoutInventoryPage(company, pluginKey, p, {
                ...opts,
                jsonAttempts: opts.jsonAttempts ?? 4,
                jsonBackoffMs: opts.jsonBackoffMs ?? 12000,
              });
            }
          } else {
            d = await fetchBuildoutInventoryPage(company, pluginKey, p, {
              ...opts,
              jsonAttempts: opts.jsonAttempts ?? 4,
              jsonBackoffMs: opts.jsonBackoffMs ?? 12000,
            });
          }
          inventoryByPage.set(p, d.inventory ?? []);
          if (opts.usePageCache || envBool("BUILDOUT_USE_PAGE_CACHE") || envBool("BUILDOUT_CACHE_ONLY")) {
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
  const result = { items, total };
  buildoutCache.set(pluginKey, result);
  console.error(
    `  ${company}: full inventory cached (${items.length} items, total ${total ?? "?"}${failedPages.size ? `, ${failedPages.size} pages skipped` : ""})`
  );
  return result;
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
  const listings: any[] = [];
  for (const x of items) {
    if (listings.length >= max) break;
    if (x.closed === true) continue;
    const isSale = x.sale === true;
    const isLease = x.sale !== true; // inventory rows are active availabilities: not-for-sale = for-lease
    if (tx === "sale" && !isSale) continue;
    if (tx === "lease" && !isLease) continue;
    {
      const attrs = new Map<string, string>(
        (x.index_attributes ?? []).map((p: any) => [String(p[0]), String(p[1])])
      );
      const priceText = attrs.get("Price") ?? null;
      const leaseRateText = attrs.get("Lease Rate") ?? attrs.get("Rate") ?? null;
      const sizeText =
        attrs.get("Building Size") ?? attrs.get("Lot Size") ?? clean(x.size_summary);
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
        salePriceUsd: tx === "sale" && !isPerSfPriceText(priceText) ? moneyToNumber(priceText) : null,
        salePricePerSf: tx === "sale" && isPerSfPriceText(priceText) ? moneyToNumber(priceText) : null,
        salePriceText: tx === "sale" ? priceText : null,
        leaseRateText,
        sizeText,
        brokerIds,
        brochures: x.pdf_url ? [{ name: "Listing brochure (PDF)", url: x.pdf_url }] : [],
        photos: [x.photo_url, x.large_thumbnail_url].filter(Boolean).slice(0, 1),
        url: clean(x.show_link),
        underContract: x.under_contract === true,
      });
    }
  }
  return {
    company,
    sourceUrl: listingsPage,
    method: "Buildout plugin inventory API (JSON, paginated)",
    totalAvailable: total,
    listings,
  };
}
