// lib/config.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import { parseArgs } from "node:util";

export const API_URL = process.env.FIRECRAWL_API_URL ?? "http://localhost:3002";

export const { values: flags } = parseArgs({
  strict: true,
  options: {
    source: { type: "string" }, // all | comma-separated keys
    transaction: { type: "string" }, // sale | lease | both (default both)
    "max-items": { type: "string" }, // per source per transaction; 0 = unlimited
    "page-cap": { type: "string" }, // page-scrape sources: max rendered pages per tx
    out: { type: "string" }, // output JSON path (default stdout)
    concurrency: { type: "string" }, // concurrent page fetches within a source
    monitor: { type: "boolean" }, // cheap-enumeration-only pass: skip detail render/enrichment
    "enrich-input": { type: "string" }, // path to a claim.json; targeted-detail (enrich) mode
    "observe-input": { type: "string" }, // private source-page observation input
    "matthews-targeted-refresh": { type: "boolean" }, // caller-bound partial Matthews refresh
  },
});
const pageCapText = flags["page-cap"] ?? (flags.monitor ? "60" : "0");
export const PAGE_CAP = Number(pageCapText);
if (!/^\+?\d+$/.test(pageCapText) || !Number.isSafeInteger(PAGE_CAP) || PAGE_CAP < 0) {
  throw new Error("page-cap must be a nonnegative safe integer (0 means unlimited)");
}
export const CONCURRENCY = Math.max(1, Math.min(6, Number(flags.concurrency ?? "3")));
export const OUT_PATH = flags.out ?? null;
