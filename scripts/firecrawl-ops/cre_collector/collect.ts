import { NativeEvidenceTerminalError } from "./lib/source-evidence/terminal-error.js";
import { observationNow } from "./lib/observation-clock.js";
import { reservePrivateCollectorDirectory, writeCollectorArtifact, writeTerminalCollectorArtifact } from "./lib/source-evidence/artifact-output.js";
import { join } from "node:path";
import { observePages, readObservationInput } from "./lib/page-observations/index.js";
import { captureBuildoutObservationForCaller } from "./sources/pure/buildout-property-observations/index.js";
// collect.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import {
  AVISON_YOUNG_TERMINAL_404_EXIT_CODE,
  isAvisonYoungTerminalSourceError,
  isExclusiveAvisonYoungTerminal404Failure,
  srcAvisonYoung,
} from "./sources/avison-young.js";
import { brokers } from "./lib/broker.js";
import {
  BUILDOUT_SOURCE_INVENTORY_OPTS,
  srcBuildout,
} from "./sources/buildout.js";
import { registeredBuildoutFirmFor } from "./sources/buildout-registry.js";
import { allBuildoutFeeds, buildoutFeedFor } from "./sources/buildout-feeds/index.js";
import { srcCbre } from "./sources/cbre.js";
import { srcCbreDealflow } from "./sources/cbre-dealflow.js";
import { srcColliers } from "./sources/colliers.js";
import {
  COLLIERS_MAIN_BOOTSTRAP_DENIED_EXIT_CODE,
  ColliersMainBootstrapDeniedError, srcColliersMain,
} from "./sources/colliers-main.js";
import { API_URL, OUT_PATH, PAGE_CAP, flags } from "./lib/config.js";
import { srcCushman } from "./sources/cushman-wakefield.js";
import { srcDaumCommercial } from "./sources/daum-commercial.js";
import { srcEssexRealty } from "./sources/essex-realty.js";
import { srcFoundryCommercial } from "./sources/foundry-commercial.js";
import { srcFranklinStreet } from "./sources/franklin-street.js";
import { srcHanley } from "./sources/hanley.js";
import { srcInterraRealty } from "./sources/interra-realty.js";
import { preserveJllSubleaseFact, srcJll } from "./sources/jll.js";
import { srcJllInvestor } from "./sources/jll-investor.js";
import { srcKidderMathews } from "./sources/kidder-mathews.js";
import { preserveLyonStahlOffers, srcLyonStahl } from "./sources/lyon-stahl.js";
import { srcMarcusMillichap } from "./sources/marcus-millichap.js";
import { srcMatthews, validateMatthewsTargetedRefresh } from "./sources/matthews.js";
import { srcNaiGlobal } from "./sources/nai-global.js";
import { srcNewmark } from "./sources/newmark.js";
import { srcPyramidBrokerage } from "./sources/pyramid-brokerage.js";
import { srcSavills } from "./sources/savills.js";
import { srcSrs } from "./sources/srs.js";
import { preserveSrsRaw } from "./sources/pure/srs-raw-retention.js";
import { preserveBuildoutRaw } from "./sources/pure/buildout-raw-retention.js";
import { srcTranswestern } from "./sources/transwestern.js";
import { SOURCE_KEYS, SourceKey, SourceResult, Tx } from "./types.js";
import { prune } from "./lib/util.js";
import { RetainedSourceError } from "./lib/source-observations.js";
import { readFileSync } from "node:fs";
import { EnrichItem, groupEnrichItems, resolveEnricher, runEnrichGroups } from "./lib/enrich.js";
import {
  refreshGenerationId,
  refreshStartedAt,
  requireFreshDetails,
  requireFreshPropertyDetails,
  summarizeListingFreshness,
} from "./lib/freshness.js";
import {
  flushPerformance,
  recordSourceCompleted,
  recordSourceStarted,
  withPerformanceSource,
} from "./lib/performance.js";


// ---------- CLI ----------

if (flags["observe-input"] !== undefined && (!flags["observe-input"] || !OUT_PATH ||
    ["source", "transaction", "max-items", "page-cap", "concurrency", "monitor", "enrich-input", "matthews-targeted-refresh"].some(key => flags[key as keyof typeof flags] !== undefined))) {
  throw new Error("observe mode requires --out and forbids other collection mode flags");
}
const sourceArg = (flags.source ?? "all").toLowerCase();
const requestedSources: SourceKey[] =
  sourceArg === "all"
    ? [...SOURCE_KEYS]
    : (sourceArg.split(",").map((s) => s.trim()) as SourceKey[]);
for (const s of requestedSources) {
  if (!SOURCE_KEYS.includes(s)) {
    console.error(`unknown source '${s}'. Valid: all, ${SOURCE_KEYS.join(", ")}`);
    process.exit(1);
  }
}
const txArg = (flags.transaction ?? "both").toLowerCase();
if (!["sale", "lease", "both"].includes(txArg)) {
  console.error(`--transaction must be sale|lease|both, got '${txArg}'`);
  process.exit(1);
}
const TRANSACTIONS: Tx[] = txArg === "both" ? ["sale", "lease"] : [txArg as Tx];
const rawMax = Number(flags["max-items"] ?? "0");
const MAX_ITEMS = rawMax <= 0 ? Number.POSITIVE_INFINITY : rawMax;
// Monitor mode: run only each source's cheap enumeration step (list/search/API/
// sitemap) and emit the freely-available enumeration fields, skipping the
// detail-page render/enrichment. Additive and gated entirely on this flag; the
// default (non-monitor) full-collection path is byte-identical when absent.
const MONITOR = flags.monitor === true;

const UNSUPPORTED: Record<string, string> = {};

// ---------- main ----------

async function runSource(key: SourceKey, tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  const registeredBuildoutFirm = registeredBuildoutFirmFor(key);
  if (registeredBuildoutFirm) {
    return srcBuildout(
      registeredBuildoutFirm.company,
      registeredBuildoutFirm.pluginKey,
      registeredBuildoutFirm.listingsPage,
      tx,
      max,
      monitor,
      registeredBuildoutFirm.inventoryOpts
    );
  }
  switch (key) {
    case "cbre":
      return srcCbre(tx, max, monitor);
    case "cbre-dealflow":
      return srcCbreDealflow(tx, max, monitor);
    case "jll":
      return srcJll(tx, max, monitor);
    case "jll-investor":
      return srcJllInvestor(tx, max, monitor);
    case "cushman-wakefield":
      return srcCushman(tx, max, monitor);
    case "colliers":
      return srcColliers(tx, max, monitor);
    case "colliers-main":
      return srcColliersMain(tx, max, monitor);
    case "newmark":
      return srcNewmark(tx, max, monitor);
    case "marcus-millichap":
      return srcMarcusMillichap(tx, max, monitor);
    case "avison-young":
      return srcAvisonYoung(tx, max, monitor);
    case "savills":
      return srcSavills(tx, max, monitor);
    case "svn":
    case "lee-associates": {
      const feed = buildoutFeedFor(key);
      if (!feed) throw new Error(`missing Buildout coordinates for ${key}`);
      return srcBuildout(
        feed.company,
        feed.pluginKey,
        feed.listingsPage,
        tx,
        max,
        monitor,
        BUILDOUT_SOURCE_INVENTORY_OPTS[key]
      );
    }
    case "nai-global":
      return srcNaiGlobal(tx, max, monitor);
    case "transwestern":
      return srcTranswestern(tx, max, monitor);
    case "matthews":
      return srcMatthews(tx, max, monitor);
    case "franklin-street":
      return srcFranklinStreet(tx, max, monitor);
    case "srs":
      return srcSrs(tx, max, monitor);
    case "hanley":
      return srcHanley(tx, max, monitor);
    case "kidder-mathews":
      return srcKidderMathews(tx, max, monitor);
    case "interra-realty":
      return srcInterraRealty(tx, max, monitor);
    case "essex-realty":
      return srcEssexRealty(tx, max, monitor);
    case "pyramid-brokerage":
      return srcPyramidBrokerage(tx, max, monitor);
    case "daum-commercial":
      return srcDaumCommercial(tx, max, monitor);
    case "foundry-commercial":
      return srcFoundryCommercial(tx, max, monitor);
    case "lyon-stahl":
      return srcLyonStahl(tx, max, monitor);
    default:
      throw new Error(`unhandled source ${key}`);
  }
}

async function main() {
  const startedAt = observationNow();
  const sources: any[] = [];
  const listings: any[] = [];
  const avisonTerminalFailures: Array<{
    transaction: Tx;
    failures: Array<{ listingId: string; role: string; status: 404 }>;
  }> = [];
  let avisonOtherFailures = 0;
  let terminalCaptureFailure = false;

  sourceLoop: for (const key of requestedSources) {
    if (UNSUPPORTED[key]) {
      console.error(`skipping unsupported source ${key}`);
      sources.push({ sourceKey: key, supported: false, note: UNSUPPORTED[key] });
      continue;
    }
    for (const tx of TRANSACTIONS) {
      console.error(
        `collecting ${key}/${tx} (max ${Number.isFinite(MAX_ITEMS) ? MAX_ITEMS : "unlimited"})...`
      );
      recordSourceStarted(key, tx);
      try {
        const res = await withPerformanceSource(key, tx, () =>
          runSource(key, tx, MAX_ITEMS, MONITOR)
        );
        const freshness = summarizeListingFreshness(res.listings);
        sources.push({
          sourceKey: key,
          transaction: tx,
          supported: true,
          company: res.company,
          sourceUrl: res.sourceUrl,
          method: res.method,
          totalAvailableOnSource: res.totalAvailable,
          listingsCollected: res.listings.length,
          truncated: res.truncated === true,
          note: res.note ?? null,
          ...(res.currentInventoryProof ? { currentInventoryProof: res.currentInventoryProof } : {}),
          ...(res.retainedObservations !== undefined ? { retainedObservations: res.retainedObservations } : {}),
          freshness,
        });
        for (const l of res.listings) {
          const listing = prune({ sourceKey: key, sourceCompany: res.company, transactionMode: tx, ...l });
          if (l.buildoutRawRetention) preserveBuildoutRaw(listing, l.rawBuildout, l.buildoutRawRetention, key);
          if (key === "srs") preserveSrsRaw(listing, l.rawSrs, l.srsRawRetention);
          if (key === "lyon-stahl") preserveLyonStahlOffers(listing, l.extraFacts?.providerOffers);
          listings.push(key === "jll"
            ? preserveJllSubleaseFact(listing, l.jllDetail?.isSubleaseOnly)
            : listing);
        }
        console.error(
          `  ${key}/${tx}: ${res.listings.length} listings (source total: ${res.totalAvailable ?? "unknown"})`
        );
        recordSourceCompleted(key, tx, {
          outcome: "succeeded",
          listingsEmitted: res.listings.length,
        });
      } catch (err) {
        recordSourceCompleted(key, tx, { outcome: "failed" });
        if (err instanceof NativeEvidenceTerminalError) {
          sources.push({ sourceKey: key, transaction: tx, supported: true, error: "native_capture_failed" });
          console.error(`  ${key}/${tx} FAILED: native_capture_failed`);
          terminalCaptureFailure = true;
          break sourceLoop;
        }
        console.error(`  ${key}/${tx} FAILED: ${err}`);
        if (
          !MONITOR && key === "colliers-main" && requestedSources.length === 1
          && err instanceof ColliersMainBootstrapDeniedError
        ) {
          console.error(JSON.stringify({
            event: "colliers_main_terminal_bootstrap_denial", code: err.code, http_status: 403,
          }));
          process.exitCode = COLLIERS_MAIN_BOOTSTRAP_DENIED_EXIT_CODE;
          return;
        }
        if (key === "avison-young") {
          if (isAvisonYoungTerminalSourceError(err)) {
            avisonTerminalFailures.push({ transaction: tx, failures: err.failures });
          } else {
            avisonOtherFailures += 1;
          }
        }
        sources.push({
          sourceKey: key,
          transaction: tx,
          supported: true,
          error: String(err).slice(0, 300),
          ...(err instanceof RetainedSourceError ? { retainedObservations: err.retainedObservations } : {}),
        });
      }
    }
  }

  const succeeded = new Set(
    sources.filter((s) => s.listingsCollected > 0).map((s) => s.sourceKey)
  ).size;
  const evidenceOnly = !MONITOR && listings.length === 0 &&
    sources.some((source) => Array.isArray(source.retainedObservations) && source.retainedObservations.length > 0);
  if (listings.length === 0) {
    if (
      !MONITOR
      && isExclusiveAvisonYoungTerminal404Failure({
        requestedSources,
        transactions: TRANSACTIONS,
        listingsCollected: listings.length,
        terminalFailureTransactions: avisonTerminalFailures.length,
        otherFailureCount: avisonOtherFailures,
      })
    ) {
      // This exit is reserved for the typed direct-HTTP 404 path. It carries no
      // artifact and is only used when every selected transaction failed with
      // that terminal response, so the refresh coordinator can skip a duplicate
      // whole-source attempt without accepting or ingesting partial data.
      console.error(JSON.stringify({
        event: "avison_young_terminal_required_detail_failure",
        code: "AVISON_YOUNG_REQUIRED_DETAIL_HTTP_404",
        failures: avisonTerminalFailures.flatMap(({ transaction, failures }) =>
          failures.map(({ listingId, role, status }) => ({
            transaction,
            listingId,
            role,
            status,
          }))
        ),
      }));
      process.exitCode = AVISON_YOUNG_TERMINAL_404_EXIT_CODE;
      return;
    }
    // Full mode: zero listings means collection failed -> hard error.
    // Monitor mode: zero listings is legitimate (e.g. monitoring only sources that
    // are excluded from monitor: jll, jll-investor, cbre-dealflow, colliers), so
    // write an empty artifact instead of failing the pipeline. Per-source errors are
    // still recorded in `sources[].error` (enforced by the downstream coverage gate),
    // and a source emitting 0 rows is never evaluated for disappearance because its
    // key is absent from run_source_keys.
    if (!MONITOR && !evidenceOnly && !terminalCaptureFailure) {
      throw new Error("no listings collected from any source");
    }
    if (MONITOR) {
      console.error(
        "monitor: 0 listings enumerated (all targeted sources are monitor-excluded or empty); writing empty artifact"
      );
    }
  }
  if (!evidenceOnly && !terminalCaptureFailure) {
    console.error(
      `done: ${listings.length} listings from ${succeeded} sources, ${brokers.length} unique brokers`
    );
  }

  const out = {
    description:
      "Commercial real estate listings (for sale and for lease) collected from major brokerage websites via local self-hosted Firecrawl, normalized to a common structure.",
    runMeta: {
      apiUrl: API_URL,
      transactions: TRANSACTIONS,
      maxItemsPerSource: Number.isFinite(MAX_ITEMS) ? MAX_ITEMS : null,
      pageCap: PAGE_CAP,
      mode: MONITOR ? "monitor" : "full",
      ...(terminalCaptureFailure ? { terminalCaptureFailure: true } : {}),
      ...(evidenceOnly ? { evidenceOnly: true, evidencePurpose: "private_unqualified_source_observations" } : {}),
      startedAt,
      finishedAt: observationNow(),
      freshness: {
        generationId: refreshGenerationId(),
        generationStartedAt: refreshStartedAt() ?? startedAt,
        requireFreshDetails: requireFreshDetails(),
        requireFreshPropertyDetails: requireFreshPropertyDetails(),
      },
    },
    sources,
    listings,
    brokers: brokers.map((b) => prune(b) ?? {}),
    totalListings: listings.length,
  };
  const json = JSON.stringify(out);
  const privateEvidence = sources.some((source) => Array.isArray(source.retainedObservations) && source.retainedObservations.length > 0) ||
    listings.some((listing) => listing.srsRawRetention || listing.buildoutRawRetention);
  if (terminalCaptureFailure) {
    const path = writeTerminalCollectorArtifact(OUT_PATH, json);
    console.error(`terminal partial artifact: ${path}`);
  } else writeCollectorArtifact(OUT_PATH, json, privateEvidence);
  if (OUT_PATH) {
    console.error(`wrote ${OUT_PATH} (${(json.length / 1e6).toFixed(1)} MB)`);
  }
  if (terminalCaptureFailure) process.exitCode = 1;
  if (evidenceOnly) {
    console.error("no listings collected from any source");
    process.exitCode = 1;
  }
}

// ---------- enrich mode ----------

// Display company per source key, mirroring the `res.company` the full path
// attaches as `sourceCompany`. The enrich path's per-listing rows do not carry a
// company field (the enrichers return parsed listing fields only), so it is
// supplied here. `sourceCompany` is metadata; `sourceKey` is what cre_ingest
// maps to a brokerage, so an absent/loose company never affects ingest keys.
const ENRICH_COMPANY: Partial<Record<SourceKey, string>> = {
  "colliers-main": "Colliers",
  "jll-investor": "JLL Investor Center",
  cbre: "CBRE",
  "cbre-dealflow": "CBRE",
  jll: "JLL",
  "cushman-wakefield": "Cushman & Wakefield",
  colliers: "Colliers",
  newmark: "Newmark",
  "marcus-millichap": "Marcus & Millichap",
  "avison-young": "Avison Young",
  savills: "Savills",
  svn: "SVN",
  "nai-global": "NAI Global",
  "lee-associates": "Lee & Associates",
  transwestern: "Transwestern",
  matthews: "Matthews",
  "franklin-street": "Franklin Street",
  srs: "SRS Real Estate Partners",
  hanley: "Hanley Investment Group",
  "kidder-mathews": "Kidder Mathews",
  "interra-realty": "Interra Realty",
  "essex-realty": "Essex Realty Group",
  "pyramid-brokerage": "Cushman & Wakefield | Pyramid Brokerage Company",
  "daum-commercial": "DAUM Commercial Real Estate Services",
  "foundry-commercial": "Foundry Commercial",
  "lyon-stahl": "Lyon Stahl",
};

// Targeted-detail (enrich) mode: read a worker claim batch, group by sourceKey,
// dispatch each group to its registered enricher (generic fallback when none),
// and emit the standard artifact with runMeta.mode="enrich". cre_ingest.py
// consumes this artifact unchanged.
async function enrichMain(claimPath: string): Promise<void> {
  const startedAt = observationNow();
  const raw = readFileSync(claimPath, "utf8");
  const parsed = JSON.parse(raw) as { items?: EnrichItem[]; matthewsTargetedRefresh?: unknown };
  const items = Array.isArray(parsed.items) ? parsed.items : [];
  const optedIn = flags["matthews-targeted-refresh"] === true;
  const hasContext = Object.hasOwn(parsed, "matthewsTargetedRefresh");
  if (optedIn !== hasContext) throw new Error("Matthews targeted: CLI opt-in and input context must agree");
  const context = optedIn ? validateMatthewsTargetedRefresh(parsed.matthewsTargetedRefresh) : undefined;
  const groups = groupEnrichItems(items);
  if (context && (MONITOR || (flags.source !== undefined && sourceArg !== "matthews") || groups.size !== 1 || !groups.has("matthews") || groups.get("matthews")?.length !== items.length)) {
    throw new Error("Matthews targeted: requires an exact Matthews-only enrichment input");
  }
  const companyFor = (key: string) =>
    ENRICH_COMPANY[key as SourceKey] ??
    registeredBuildoutFirmFor(key as SourceKey)?.company ??
    key;
  const { sources, listings, terminalCaptureFailure } = await runEnrichGroups(groups, resolveEnricher, companyFor, context);
  if (context) validateMatthewsTargetedRefresh(context);
  const privateEvidence = sources.some((source) => Array.isArray(source.retainedObservations) && source.retainedObservations.length > 0) ||
    listings.some((listing) => listing.srsRawRetention || listing.buildoutRawRetention);
  const evidenceOnly = listings.length === 0 && privateEvidence;

  console.error(
    `enrich done: ${listings.length} listing(s) from ${groups.size} source group(s), ${brokers.length} unique brokers`
  );

  const out = {
    description:
      "Commercial real estate listings (for sale and for lease) collected from major brokerage websites via local self-hosted Firecrawl, normalized to a common structure.",
    runMeta: {
      apiUrl: API_URL,
      mode: "enrich",
      ...(terminalCaptureFailure ? { terminalCaptureFailure: true } : {}),
      enrichInput: claimPath,
      ...(evidenceOnly ? { evidenceOnly: true, evidencePurpose: "private_unqualified_source_observations" } : {}),
      ...(context ? {
        matthewsTargetedRefresh: context,
        freshness: {
          generationId: context.generationId,
          generationStartedAt: context.generationStartedAt,
          requireFreshDetails: true,
          requireFreshPropertyDetails: requireFreshPropertyDetails(),
        },
      } : {}),
      startedAt,
      finishedAt: observationNow(),
    },
    sources,
    listings,
    brokers: brokers.map((b) => prune(b) ?? {}),
    totalListings: listings.length,
  };
  const json = JSON.stringify(out);
  // All targeted artifacts belong to the worker's private custody, including
  // empty and bespoke-only results without retained observation arrays.
  if (terminalCaptureFailure) {
    const path = writeTerminalCollectorArtifact(OUT_PATH, json);
    console.error(`terminal partial artifact: ${path}`);
    process.exitCode = 1;
  } else writeCollectorArtifact(OUT_PATH, json, true);
  if (OUT_PATH) {
    console.error(`wrote ${OUT_PATH} (${(json.length / 1e6).toFixed(1)} MB)`);
  }
}

async function dispatch(): Promise<void> {
  flushPerformance({ terminal: false });
  try {
    const enrichInput = flags["enrich-input"];
    if (flags["observe-input"] === undefined && !enrichInput && !OUT_PATH &&
        requestedSources.some((key) => key === "srs" || allBuildoutFeeds.some((feed) => feed.sourceKey === key))) {
      throw new Error("SRS and Buildout collection require a private --out file");
    }
    if (enrichInput && process.env.CRE_NEWMARK_PUBLISHED_DETAIL === "1" && !OUT_PATH)
      throw new Error("Newmark published-detail evidence requires a private --out file");
    if (flags["observe-input"] !== undefined) {
      if (!OUT_PATH) throw new Error("observe mode requires --out");
      const items = readObservationInput(flags["observe-input"]);
      try { reservePrivateCollectorDirectory(OUT_PATH); }
      catch { throw new Error("unable to reserve private observation output"); }
      try {
        const result = await observePages(items, undefined, captureBuildoutObservationForCaller);
        const names = result.parts.map((_, index) => `part-${String(index + 1).padStart(4, "0")}.json`);
        for (let i = 0; i < result.parts.length; i++)
          writeCollectorArtifact(join(OUT_PATH, names[i]), JSON.stringify(result.parts[i]), true);
        writeCollectorArtifact(join(OUT_PATH, "run-summary.json"), JSON.stringify({ ...result.summary, parts: names }), true);
      } catch {
        try { writeCollectorArtifact(join(OUT_PATH, "run-summary.json"), JSON.stringify({kind: "observation_run_summary", runMeta: { mode: "observe", evidenceOnly: true }, error: "observation_incomplete"}), true); }
        catch { /* A previously written part remains available for inspection. */ }
        process.exitCode = 1;
      }
      return;
    }
    if (flags["matthews-targeted-refresh"] && !enrichInput) {
      throw new Error("Matthews targeted: requires --enrich-input");
    }
    if (enrichInput) {
      await enrichMain(enrichInput);
    } else {
      await main();
    }
  } finally {
    flushPerformance({ terminal: true });
  }
}

dispatch().catch((err) => {
  console.error(err);
  process.exit(1);
});
