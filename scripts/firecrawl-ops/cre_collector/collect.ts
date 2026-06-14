// collect.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { srcAvisonYoung } from "./sources/avison-young.js";
import { brokers } from "./lib/broker.js";
import { srcBuildout } from "./sources/buildout.js";
import { srcCbre } from "./sources/cbre.js";
import { srcCbreDealflow } from "./sources/cbre-dealflow.js";
import { srcColliers } from "./sources/colliers.js";
import { srcColliersMain } from "./sources/colliers-main.js";
import { API_URL, OUT_PATH, PAGE_CAP, flags } from "./lib/config.js";
import { srcCushman } from "./sources/cushman-wakefield.js";
import { srcJll } from "./sources/jll.js";
import { srcJllInvestor } from "./sources/jll-investor.js";
import { srcMarcusMillichap } from "./sources/marcus-millichap.js";
import { srcNaiGlobal } from "./sources/nai-global.js";
import { srcNewmark } from "./sources/newmark.js";
import { srcSavills } from "./sources/savills.js";
import { srcTranswestern } from "./sources/transwestern.js";
import { SourceResult, Tx } from "./types.js";
import { prune } from "./lib/util.js";


// ---------- CLI ----------

const SOURCE_KEYS = [
  "cbre",
  "cbre-dealflow",
  "jll",
  "jll-investor",
  "cushman-wakefield",
  "colliers",
  "colliers-main",
  "newmark",
  "marcus-millichap",
  "avison-young",
  "savills",
  "svn",
  "nai-global",
  "lee-associates",
  "transwestern",
] as const;
type SourceKey = (typeof SOURCE_KEYS)[number];

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
      return srcBuildout(
        "SVN",
        "b933480474026c41d248b77156c84aef37dcac68",
        "https://svn.com/properties/",
        tx,
        max,
        monitor,
        {
          preferDirectJson: true,
          directReferer: "https://svn.com/properties/",
          pageConcurrency: 1,
          requireCompletePages: true,
          cacheSlug: "svn",
          usePageCache: true,
          recoveryPasses: 1,
          recoveryCooldownMs: 15000,
          maxRecoveryPages: 60,
        }
      );
    case "lee-associates":
      return srcBuildout(
        "Lee & Associates",
        "9a64a93980aeae8db347e72cdfa8ca61017acc9a",
        "https://www.lee-associates.com/properties/",
        tx,
        max,
        monitor,
        {
          preferDirectJson: true,
          directReferer: "https://www.lee-associates.com/properties/",
          pageConcurrency: 1,
          requireCompletePages: true,
          cacheSlug: "lee-associates",
          usePageCache: true,
          recoveryPasses: 1,
          recoveryCooldownMs: 15000,
          maxRecoveryPages: 60,
        }
      );
    case "nai-global":
      return srcNaiGlobal(tx, max, monitor);
    case "transwestern":
      return srcTranswestern(tx, max, monitor);
    default:
      throw new Error(`unhandled source ${key}`);
  }
}

async function main() {
  const startedAt = new Date().toISOString();
  const sources: any[] = [];
  const listings: any[] = [];

  for (const key of requestedSources) {
    if (UNSUPPORTED[key]) {
      console.error(`skipping unsupported source ${key}`);
      sources.push({ sourceKey: key, supported: false, note: UNSUPPORTED[key] });
      continue;
    }
    for (const tx of TRANSACTIONS) {
      console.error(
        `collecting ${key}/${tx} (max ${Number.isFinite(MAX_ITEMS) ? MAX_ITEMS : "unlimited"})...`
      );
      try {
        const res = await runSource(key, tx, MAX_ITEMS, MONITOR);
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
        });
        for (const l of res.listings) {
          listings.push(
            prune({ sourceKey: key, sourceCompany: res.company, transactionMode: tx, ...l })
          );
        }
        console.error(
          `  ${key}/${tx}: ${res.listings.length} listings (source total: ${res.totalAvailable ?? "unknown"})`
        );
      } catch (err) {
        console.error(`  ${key}/${tx} FAILED: ${err}`);
        sources.push({
          sourceKey: key,
          transaction: tx,
          supported: true,
          error: String(err).slice(0, 300),
        });
      }
    }
  }

  const succeeded = new Set(
    sources.filter((s) => s.listingsCollected > 0).map((s) => s.sourceKey)
  ).size;
  if (listings.length === 0) {
    // Full mode: zero listings means collection genuinely failed -> hard error.
    // Monitor mode: zero listings is legitimate (e.g. monitoring only sources that
    // are excluded from monitor: jll, jll-investor, cbre-dealflow, colliers), so
    // write an empty artifact instead of failing the pipeline. Per-source errors are
    // still recorded in `sources[].error` (enforced by the downstream coverage gate),
    // and a source emitting 0 rows is never evaluated for disappearance because its
    // key is absent from run_source_keys.
    if (!MONITOR) {
      throw new Error("no listings collected from any source");
    }
    console.error(
      "monitor: 0 listings enumerated (all targeted sources are monitor-excluded or empty); writing empty artifact"
    );
  }
  console.error(
    `done: ${listings.length} listings from ${succeeded} sources, ${brokers.length} unique brokers`
  );

  const out = {
    description:
      "Commercial real estate listings (for sale and for lease) collected from major brokerage websites via local self-hosted Firecrawl, normalized to a common structure.",
    runMeta: {
      apiUrl: API_URL,
      transactions: TRANSACTIONS,
      maxItemsPerSource: Number.isFinite(MAX_ITEMS) ? MAX_ITEMS : null,
      pageCap: PAGE_CAP,
      mode: MONITOR ? "monitor" : "full",
      startedAt,
      finishedAt: new Date().toISOString(),
    },
    sources,
    listings,
    brokers: brokers.map((b) => prune(b) ?? {}),
    totalListings: listings.length,
  };
  const json = JSON.stringify(out);
  if (OUT_PATH) {
    mkdirSync(dirname(OUT_PATH), { recursive: true });
    writeFileSync(OUT_PATH, json);
    console.error(`wrote ${OUT_PATH} (${(json.length / 1e6).toFixed(1)} MB)`);
  } else {
    process.stdout.write(json);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
