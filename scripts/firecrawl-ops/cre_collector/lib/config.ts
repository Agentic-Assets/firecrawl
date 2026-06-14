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
  },
});
export const PAGE_CAP = Math.max(1, Number(flags["page-cap"] ?? "60"));
export const CONCURRENCY = Math.max(1, Math.min(6, Number(flags.concurrency ?? "3")));
export const OUT_PATH = flags.out ?? null;
