#!/usr/bin/env node
/**
 * No-database throughput and reliability calibration for Colliers Main detail
 * renders. This does not invoke collect.ts, checkpoint refresh, a gate, or an
 * ingest command. It is deliberately isolated from generation caches so its
 * evidence can choose a safe concurrency without contaminating a live run.
 *
 * Example:
 *   npx tsx cre_colliers_runtime_calibration.ts --count=200 --concurrency=2 \
 *     --out=out/calibration/colliers-c2.json
 */
import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { parseArgs } from "node:util";

const originalArgv = process.argv.slice(2);
const { values } = parseArgs({
  args: originalArgv,
  strict: true,
  options: {
    count: { type: "string", default: "20" },
    concurrency: { type: "string", default: "1" },
    out: { type: "string" },
    sample: { type: "string", default: "primary" },
    "max-gap-ms": { type: "string", default: "90000" },
  },
});

function boundedInt(value: string | undefined, fallback: number, min: number, max: number): number {
  const parsed = Number(value ?? fallback);
  return Number.isInteger(parsed) && parsed >= min && parsed <= max ? parsed : fallback;
}

const count = boundedInt(values.count, 20, 1, 500);
const concurrency = boundedInt(values.concurrency, 1, 1, 3);
const maxGapMs = boundedInt(values["max-gap-ms"], 90000, 1000, 180000);
const sampleName = values.sample ?? "primary";
if (!values.out) throw new Error("--out is required");
const outPath = resolve(values.out);

// Keep lib/config strict, but prevent it from parsing this calibration CLI.
process.argv = [process.argv[0]!, process.argv[1]!, "--concurrency", String(concurrency)];
process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
process.env.CRE_REFRESH_GENERATION = `calibration-${new Date().toISOString().replace(/[:.]/g, "-")}`;
for (const key of [
  "DATABASE_URL",
  "EQUIRE_DATABASE_URL",
  "PGHOST",
  "PGPORT",
  "PGDATABASE",
  "PGUSER",
  "PGPASSWORD",
]) {
  delete process.env[key];
}

const [{ fetchColliersMainEntries, parseColliersMainDetail, scrapeColliersMainDetailDoc }, { pmap }] =
  await Promise.all([
    import("./sources/colliers-main.js"),
    import("./lib/util.js"),
  ]);

type CalibrationEntry = Awaited<ReturnType<typeof fetchColliersMainEntries>>[number];
type Outcome = {
  id: string;
  url: string;
  outcome: "parsed" | "tombstone" | "error";
  startedAt: string;
  completedAt: string;
  elapsedMs: number;
  error?: string;
  transactionType?: string | null;
};

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function deterministicSample(entries: CalibrationEntry[]): CalibrationEntry[] {
  return [...entries]
    .sort((left, right) => {
      const a = sha256(`${sampleName}:${left.id}:${left.url}`);
      const b = sha256(`${sampleName}:${right.id}:${right.url}`);
      return a.localeCompare(b);
    })
    .slice(0, count);
}

const entries = await fetchColliersMainEntries();
const sample = deterministicSample(entries);
if (sample.length !== count) {
  throw new Error(`sitemap exposed ${sample.length} candidate(s), below requested calibration count ${count}`);
}

let lastCompletionMs = Date.now();
let maxCompletionGapMs = 0;
const transport = {
  attempts: 0,
  retries: 0,
  challenges: 0,
  transportErrors: 0,
  cooldowns: 0,
};
const calibrationStartedMs = Date.now();
const outcomes = await pmap(sample, concurrency, async (entry): Promise<Outcome> => {
  const startedMs = Date.now();
  const startedAt = new Date(startedMs).toISOString();
  try {
    const listing = parseColliersMainDetail(
      entry,
      await scrapeColliersMainDetailDoc(
        entry.url,
        undefined,
        undefined,
        undefined,
        (event) => {
          transport.attempts++;
          if (event.attempt > 1) transport.retries++;
          if (event.kind === "challenge") transport.challenges++;
          if (event.kind === "transport_error") transport.transportErrors++;
          if (event.kind === "cooldown") transport.cooldowns++;
        }
      )
    );
    const completedMs = Date.now();
    maxCompletionGapMs = Math.max(maxCompletionGapMs, completedMs - lastCompletionMs);
    lastCompletionMs = completedMs;
    return {
      id: entry.id,
      url: entry.url,
      outcome: listing.skip === "not_found" ? "tombstone" : "parsed",
      startedAt,
      completedAt: new Date(completedMs).toISOString(),
      elapsedMs: completedMs - startedMs,
      transactionType: listing.transactionType ?? null,
    };
  } catch (err) {
    const completedMs = Date.now();
    maxCompletionGapMs = Math.max(maxCompletionGapMs, completedMs - lastCompletionMs);
    lastCompletionMs = completedMs;
    return {
      id: entry.id,
      url: entry.url,
      outcome: "error",
      startedAt,
      completedAt: new Date(completedMs).toISOString(),
      elapsedMs: completedMs - startedMs,
      error: String(err).slice(0, 600),
    };
  }
});

const elapsedMs = Math.max(1, Date.now() - calibrationStartedMs);
const errors = outcomes.filter((result) => result.outcome === "error");
const orderedLatencies = outcomes.map((result) => result.elapsedMs).sort((a, b) => a - b);
const p95LatencyMs = orderedLatencies[Math.max(0, Math.ceil(orderedLatencies.length * 0.95) - 1)] ?? 0;
const terminal = errors.length === 0 && maxCompletionGapMs <= maxGapMs;
const report = {
  schemaVersion: 2,
  kind: "colliers_main_runtime_calibration",
  databaseMutationPath: false,
  ingestionInvoked: false,
  sample: {
    name: sampleName,
    requestedCount: count,
    sampleSha256: sha256(sample.map((entry) => `${entry.id}|${entry.url}`).join("\n")),
    sitemapCount: entries.length,
    sitemapSha256: sha256(entries.map((entry) => `${entry.id}|${entry.url}|${entry.lastmod ?? ""}`).join("\n")),
  },
  runtime: {
    concurrency,
    startIntervalMs: Number(process.env.COLLIERS_MAIN_DETAIL_START_INTERVAL_MS ?? 1500),
    challengeCooldownMs: Number(process.env.COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS ?? 30000),
    maxCompletionGapMs,
    maxAllowedCompletionGapMs: maxGapMs,
    transport,
  },
  summary: {
    terminal,
    requested: count,
    parsed: outcomes.filter((result) => result.outcome === "parsed").length,
    tombstones: outcomes.filter((result) => result.outcome === "tombstone").length,
    errors: errors.length,
    elapsedMs,
    rowsPerMinute: Number((outcomes.length / (elapsedMs / 60000)).toFixed(3)),
    p95LatencyMs,
  },
  outcomes,
};

mkdirSync(dirname(outPath), { recursive: true });
writeFileSync(outPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
console.log(JSON.stringify(report.summary));
if (!terminal) process.exitCode = 2;
