import { observationNow } from "../lib/observation-clock.js";
// Short-lived foreground child only. The Python coordinator owns the canonical
// lock, host guards, process deadline, exact runtime and remote settlement.
import { constants, fstatSync, openSync, closeSync, readSync, writeFileSync,
  lstatSync, fsyncSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import { matchedCohortSha256, runMatchedCapacityCell, withProductionMatchedCapture,
  MatchedCapacityError, type MatchedCohort, type MatchedCell, type CapturedDetail,
  type TrialContext } from "./matched-capacity.js";

const MAX_FILE_BYTES = 8 * 1024 * 1024;
const MAX_CAPTURE_BYTES = 64 * 1024 * 1024;
function readBounded(fd: number, limit: number): Buffer {
  const data = Buffer.alloc(limit + 1);
  let used = 0;
  while (used <= limit) {
    const count = readSync(fd, data, used, data.length - used, null);
    if (!count) break;
    used += count;
  }
  if (used > limit) throw new MatchedCapacityError("input_size");
  return data.subarray(0, used);
}
export type WorkerInput = {
  cohort: MatchedCohort;
  cohortSha256: string;
  generationId: string;
  concurrency: number;
  baseline?: MatchedCell;
  context: TrialContext;
  inventoryArtifact: string;
  maxInventoryAgeHours: number;
};
function privateDirectory(path: string): void {
  const info = lstatSync(path);
  if (!info.isDirectory() || info.isSymbolicLink() || info.uid !== process.getuid?.()
      || (info.mode & 0o077) !== 0) throw new MatchedCapacityError("private_directory");
}
export function readWorkerInput(path: string): WorkerInput {
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = fstatSync(fd);
    if (!info.isFile() || info.uid !== process.getuid?.() || info.nlink !== 1
        || (info.mode & 0o077) !== 0 || info.size > MAX_FILE_BYTES) throw new MatchedCapacityError("private_input");
    return JSON.parse(readBounded(fd, MAX_FILE_BYTES).toString("utf8"));
  } finally { closeSync(fd); }
}
export async function verifyInventory(input: WorkerInput): Promise<void> {
  if (!Number.isFinite(input.maxInventoryAgeHours) || input.maxInventoryAgeHours <= 0
      || input.maxInventoryAgeHours > 24 || Date.now() - Date.parse(input.cohort.inventoryStartedAt)
      > input.maxInventoryAgeHours * 3600000) throw new MatchedCapacityError("inventory_age");
  const fd = openSync(input.inventoryArtifact, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  let raw: Buffer;
  try {
    const info = fstatSync(fd);
    if (!info.isFile() || info.uid !== process.getuid?.() || info.nlink !== 1
        || (info.mode & 0o077) !== 0 || info.size > 32 * 1024 * 1024) throw new MatchedCapacityError("inventory_input");
    raw = readBounded(fd, 32 * 1024 * 1024);
    if (raw.length > 32 * 1024 * 1024) throw new MatchedCapacityError("inventory_input");
  } finally { closeSync(fd); }
  if (createHash("sha256").update(raw).digest("hex") !== input.cohort.inventorySha256) throw new MatchedCapacityError("inventory_hash");
  const artifact = JSON.parse(raw.toString("utf8"));
  const enumeration = artifact.kind === "cre_matched_inventory" && artifact.runMeta?.mode === "enumeration_only";
  if (input.cohort.source === "jll" && !enumeration) throw new MatchedCapacityError("native_inventory_required");
  if (!Array.isArray(artifact.listings) || (!enumeration && artifact.runMeta?.mode !== "monitor")
      || artifact.runMeta?.startedAt !== input.cohort.inventoryStartedAt
      || artifact.runMeta?.finishedAt !== input.cohort.inventoryFinishedAt) throw new MatchedCapacityError("inventory_rows");
  const canonical = (value: any): any => Array.isArray(value) ? value.map(canonical) : value && typeof value === "object"
    ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])])) : value;
  if (enumeration) {
    const scope = artifact.scope;
    if (input.cohort.source !== "jll" || scope?.source !== "jll" || scope?.page !== 1
        || scope?.clientDispatches !== 1 || scope?.fullInventory !== false
        || !["sale", "lease"].includes(scope?.transaction)
        || typeof artifact.nativeResponse?.body !== "string"
        || createHash("sha256").update(artifact.nativeResponse.body).digest("hex") !== artifact.nativeResponse.sha256) throw new MatchedCapacityError("native_inventory_binding");
    const jll = await import("./jll.js");
    if (!(jll.JLL_PROPERTY_TYPES as readonly string[]).includes(scope.propertyType)) throw new MatchedCapacityError("native_inventory_binding");
    const parsed = jll.parseJllGraphqlSearchPage(JSON.parse(artifact.nativeResponse.body), scope.transaction, scope.propertyType, 1);
    const projected = parsed.listings.map((row: any, index: number) => ({ ...row, sourceKey: "jll", sourceCompany: "JLL",
      transactionMode: scope.transaction, inventoryObservedAt: artifact.listings[index]?.inventoryObservedAt }));
    if (JSON.stringify(canonical(projected)) !== JSON.stringify(canonical(artifact.listings))) throw new MatchedCapacityError("native_inventory_projection");
  }
  for (const member of input.cohort.members) {
    const rows = artifact.listings.filter((row: any) => String(row.id) === member.providerId && row.url === member.canonicalUrl
      && row.sourceKey === input.cohort.source);
    if (rows.length !== 1 || rows[0]?.detailObservedAt || rows[0]?.freshnessProvenance?.detailScope === "detail_page"
        || JSON.stringify(canonical(rows[0])) !== JSON.stringify(canonical(member.baseRow))) throw new MatchedCapacityError("inventory_member_binding");
  }
}
export function writeWorkerOutput(path: string, value: unknown): number {
  privateDirectory(dirname(path));
  const encoded = JSON.stringify(value) + "\n";
  const bytes = Buffer.byteLength(encoded);
  if (bytes > MAX_FILE_BYTES) throw new MatchedCapacityError("output_size");
  const fd = openSync(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try { writeFileSync(fd, encoded); fsyncSync(fd); } finally { closeSync(fd); }
  return bytes;
}

export async function executeMatchedWorker(input: WorkerInput, output: string, signal: AbortSignal): Promise<MatchedCell> {
  privateDirectory(output);
  if (matchedCohortSha256(input.cohort) !== input.cohortSha256
      || process.env.CRE_REFRESH_GENERATION !== input.generationId
      || process.env.CRE_REFRESH_STARTED_AT !== input.cohort.inventoryStartedAt) throw new MatchedCapacityError("worker_binding");
  await verifyInventory(input);
  const captured = new Map<string, CapturedDetail>();
  let captureBytes = 0;
  const startedAt = observationNow();
  const result = await withProductionMatchedCapture({ source: input.cohort.source, generationId: input.generationId,
    onCaptured: (member, value) => {
      // Private evidence shares this real rendered result with AGENTIC-2898.
      // No row/doc/contact body is copied into the body-free cell summary.
      const key = createHash("sha256").update(member.sampleId).digest("hex");
      captureBytes += writeWorkerOutput(join(output, `capture-${key}.json`), { member, ...value });
      if (captureBytes > MAX_CAPTURE_BYTES) throw new MatchedCapacityError("capture_size");
      captured.set(member.sampleId, structuredClone(value));
    },
  }, (capture) => runMatchedCapacityCell({ ...input, capture, signal }));
  const broker = await import("../lib/broker.js");
  const source = input.cohort.source;
  const company = source === "jll" ? "JLL" : "Marcus & Millichap";
  const listings = input.cohort.members.map((member) => {
    const listing = captured.get(member.sampleId)!.listing;
    const transaction = /lease|rent/i.test(String(listing.transactionType)) ? "lease" : "sale";
    return { ...listing, sourceKey: source, sourceCompany: company, transactionMode: transaction };
  });
  const transactions = [...new Set(listings.map((row) => row.transactionMode))];
  writeWorkerOutput(join(output, "collector.json"), {
    description: "Bounded no-write matched detail sample; not a full inventory sweep",
    runMeta: { mode: "enrich", startedAt, finishedAt: observationNow(), transactions,
      freshness: { generationId: input.generationId, generationStartedAt: input.cohort.inventoryStartedAt,
        requireFreshDetails: true, requireFreshPropertyDetails: true } },
    sources: transactions.map((transaction) => ({ sourceKey: source, transaction, company, supported: true,
      method: "matched_capacity_real_detail", listingsCollected: listings.filter((row) => row.transactionMode === transaction).length,
      truncated: true, note: "Immutable bounded sample; no disappearance or whole-inventory claim" })),
    listings, brokers: broker.brokers, totalListings: listings.length,
  });
  writeWorkerOutput(join(output, "cell.json"), result);
  return result;
}

export type PreparationInput = { kind: "prepare_jll"; transaction: "sale" | "lease";
  propertyType: string; members: number };
export async function prepareJllInventory(input: PreparationInput, output: string, signal: AbortSignal): Promise<void> {
  privateDirectory(output);
  if (!["sale", "lease"].includes(input.transaction) || !Number.isInteger(input.members)
      || input.members < 8 || input.members > 24) throw new MatchedCapacityError("preparation_scope");
  const jll = await import("./jll.js");
  if (!(jll.JLL_PROPERTY_TYPES as readonly string[]).includes(input.propertyType)) throw new MatchedCapacityError("preparation_scope");
  const originalFetch = globalThis.fetch;
  const startedAt = observationNow();
  let dispatches = 0;
  let nativeBody: string | undefined;
  globalThis.fetch = async (url, init) => {
    if (signal.aborted || dispatches++ !== 0 || String(url) !== jll.JLL_GRAPHQL_URL) throw new MatchedCapacityError("preparation_dispatch_budget");
    const response = await originalFetch(url, { ...init,
      signal: init?.signal ? AbortSignal.any([signal, init.signal]) : signal });
    if (response.status !== 200 || response.redirected || response.url !== String(url)) throw new MatchedCapacityError("preparation_response_binding");
    const text = response.text.bind(response);
    response.text = async () => {
      const body = await text();
      if (Buffer.byteLength(body) > 4 * 1024 * 1024) throw new MatchedCapacityError("preparation_body_size");
      nativeBody = body;
      return body;
    };
    return response;
  };
  try {
    const page = await jll.fetchJllSearchPage(input.transaction, input.propertyType, 1);
    if (signal.aborted || dispatches !== 1 || nativeBody === undefined || page.listings.length < input.members) throw new MatchedCapacityError("preparation_incomplete");
    const observedAt = observationNow();
    const listings = page.listings.map((row) => ({ ...row, sourceKey: "jll", sourceCompany: "JLL",
      transactionMode: input.transaction, inventoryObservedAt: observedAt }));
    const finishedAt = observationNow();
    const artifact = { kind: "cre_matched_inventory", runMeta: { mode: "enumeration_only", startedAt, finishedAt },
      scope: { source: "jll", transaction: input.transaction, propertyType: input.propertyType,
        page: 1, clientDispatches: dispatches, fullInventory: false },
      nativeResponse: { body: nativeBody, sha256: createHash("sha256").update(nativeBody).digest("hex") }, listings };
    writeWorkerOutput(join(output, "inventory.json"), artifact);
    // writeWorkerOutput's bytes are the immutable original artifact contract.
    const inventorySha256 = createHash("sha256").update(JSON.stringify(artifact) + "\n").digest("hex");
    const cohort: MatchedCohort = { source: "jll", inventorySha256, inventoryStartedAt: startedAt,
      inventoryFinishedAt: finishedAt, members: listings.slice(0, input.members).map((row) => ({
        sampleId: createHash("sha256").update(`${row.id}\n${row.url}`).digest("hex"),
        providerId: String(row.id), canonicalUrl: row.url, baseRow: row })) };
    writeWorkerOutput(join(output, "cohort.json"), { cohort, cohortSha256: matchedCohortSha256(cohort) });
  } finally { globalThis.fetch = originalFetch; }
}

export async function prepareMarcusInventory(members: number, output: string, signal: AbortSignal): Promise<void> {
  privateDirectory(output);
  if (!Number.isInteger(members) || members < 8 || members > 128) throw new MatchedCapacityError("preparation_scope");
  const original = globalThis.fetch;
  let dispatches = 0;
  let active = 0;
  let peak = 0;
  const bodies: { url: string; body: string; sha256: string }[] = [];
  const startedAt = observationNow();
  globalThis.fetch = async (url, init) => {
    const address = String(url);
    if (signal.aborted || dispatches++ >= members + 2 || init?.method !== "POST"
        || !/^https:\/\/www\.marcusmillichap\.com\/api\/contentsearch\/(properties|mapproperties|mappropertydetail)$/.test(address)) throw new MatchedCapacityError("preparation_dispatch_budget");
    // Bound actual requests independently of ordinary source config defaults.
    while (active >= 2) {
      if (signal.aborted) throw new MatchedCapacityError("cancelled");
      await new Promise((done) => setTimeout(done, 5));
    }
    active++; peak = Math.max(peak, active);
    try {
      const response = await original(url, { ...init, signal: init?.signal ? AbortSignal.any([signal, init.signal]) : signal });
      if (response.status !== 200 || response.redirected || response.url !== address) throw new MatchedCapacityError("preparation_response_binding");
      const text = response.text.bind(response);
      response.json = async () => {
        try {
          const body = await text();
          if (Buffer.byteLength(body) > 4 * 1024 * 1024) throw new MatchedCapacityError("preparation_body_size");
          bodies.push({ url: address, body, sha256: createHash("sha256").update(body).digest("hex") });
          return JSON.parse(body);
        } finally { active--; }
      };
      return response;
    } catch (error) { active--; throw error; }
  };
  try {
    const marcus = await import("./marcus-millichap.js");
    const source = await marcus.srcMarcusMillichap("sale", members, true);
    if (signal.aborted || source.listings.length !== members || dispatches !== members + 2
        || bodies.length !== dispatches || source.listings.some((row: any) => row.detailError || row.detailObservedAt || !row.id || !row.url)) throw new MatchedCapacityError("preparation_incomplete");
    const listings = source.listings.map((row: any) => ({ ...row, sourceKey: "marcus-millichap",
      sourceCompany: source.company, transactionMode: "sale" }));
    const finishedAt = observationNow();
    const artifact = { runMeta: { mode: "monitor", startedAt, finishedAt },
      scope: { fullInventory: false, maximumMembers: members, actualClientCap: 2, observedClientPeak: peak,
        clientDispatches: dispatches, heavyDetailsRequested: false },
      sources: [{ sourceKey: "marcus-millichap", transaction: "sale", supported: true,
        truncated: source.truncated, method: source.method, listingsCollected: listings.length }],
      rawResponses: bodies, listings };
    writeWorkerOutput(join(output, "inventory.json"), artifact);
    const cohort: MatchedCohort = { source: "marcus-millichap",
      inventorySha256: createHash("sha256").update(JSON.stringify(artifact) + "\n").digest("hex"),
      inventoryStartedAt: startedAt, inventoryFinishedAt: finishedAt,
      members: listings.map((row: any) => ({ sampleId: createHash("sha256").update(`${row.id}\n${row.url}`).digest("hex"),
        providerId: String(row.id), canonicalUrl: row.url, baseRow: row })) };
    writeWorkerOutput(join(output, "cohort.json"), { cohort, cohortSha256: matchedCohortSha256(cohort) });
  } finally { globalThis.fetch = original; }
}

async function main(): Promise<void> {
  const input = process.env.CRE_MATCHED_CAPACITY_INPUT;
  const output = process.env.CRE_MATCHED_CAPACITY_OUTPUT;
  if (!input || !output) throw new MatchedCapacityError("worker_paths");
  const controller = new AbortController();
  const abort = () => controller.abort();
  process.on("SIGINT", abort); process.on("SIGTERM", abort);
  try {
    const value = readWorkerInput(input);
    if ((value as unknown as PreparationInput).kind === "prepare_jll") await prepareJllInventory(value as unknown as PreparationInput, output, controller.signal);
    else if ((value as any).kind === "prepare_marcus") await prepareMarcusInventory((value as any).members, output, controller.signal);
    else await executeMatchedWorker(value, output, controller.signal);
  }
  finally { process.off("SIGINT", abort); process.off("SIGTERM", abort); }
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    console.error(error instanceof MatchedCapacityError ? error.message : "matched capacity worker failed");
    process.exitCode = 1;
  });
}
