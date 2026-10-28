// This fixture owns its process and globals; it never dispatches network requests.
import assert from "node:assert/strict";
import { runMatchedCapacityCell, withProductionMatchedCapture, type MatchedCohort } from "../../../sources/matched-capacity.js";

const mode = process.argv[2];
process.argv = [process.argv[0]!, process.argv[1]!];
const generationId = "production-marcus-millichap";
process.env.CRE_REFRESH_GENERATION = generationId;
process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
let sawAbort = false;
let fetchStarted!: () => void;
const started = new Promise<void>((resolve) => { fetchStarted = resolve; });
globalThis.fetch = async (_, init) => new Promise((_, reject) => {
  init!.signal!.addEventListener("abort", () => { sawAbort = true; reject(new Error("private request aborted")); }, { once: true });
  fetchStarted();
});
const fakeFetch = globalThis.fetch;
const controller = new AbortController();
const observedAt = new Date(Date.now() - 1000).toISOString();
const cohort: MatchedCohort = {
  source: "marcus-millichap", inventorySha256: "a".repeat(64), inventoryStartedAt: observedAt, inventoryFinishedAt: observedAt,
  members: Array.from({ length: 16 }, (_, index) => {
    const url = `https://www.marcusmillichap.com/properties/member-${index}`;
    return { sampleId: `sample-${index}`, providerId: String(index), canonicalUrl: url,
      baseRow: { id: String(index), url, sourceKey: "marcus-millichap", name: `Property ${index}`,
        transactionType: "Sale", inventoryObservedAt: observedAt } };
  }),
};
const result = withProductionMatchedCapture({ source: "marcus-millichap", generationId }, (capture) =>
  mode === "no-start" ? new Promise<never>((_, reject) => {
    setTimeout(() => reject(new Error("synthetic delayed wrapper settlement")), 750);
  }) : runMatchedCapacityCell({ cohort, concurrency: 1, generationId,
    context: { runtimeSnapshot: { project: "synthetic-owned" }, resourceJournal: "synthetic", concurrentHostWork: "unknown" },
    capture, signal: controller.signal }));
let wrapperSettled = false;
void result.then(() => { wrapperSettled = true; }, () => { wrapperSettled = true; });
let timer: ReturnType<typeof setTimeout> | undefined;
try {
  await Promise.race([started, result.then(
    () => { throw new Error("capture completed before native fetch started"); },
    (error) => { throw error; },
  ), new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error("native fetch did not start")), mode === "no-start" ? 50 : 5000);
  })]);
  if (mode === "no-start") throw new Error("unexpected fetch start");
  controller.abort();
  await assert.rejects(result, /cancelled/);
  assert.equal(sawAbort, true);
  assert.equal(globalThis.fetch, fakeFetch);
  console.log("PASS native cancellation and restoration");
} catch (error) {
  if (mode !== "no-start" || !(error instanceof Error) || error.message !== "native fetch did not start") throw error;
  // The wrapper is intentionally still awaiting its callback here. Exit the
  // owned process so its later finally can never mutate the parent test.
  assert.equal(wrapperSettled, false);
  controller.abort();
  if (timer !== undefined) clearTimeout(timer);
  await new Promise<void>((resolve, reject) => {
    process.stdout.write("PASS bounded no-start timeout with pending wrapper\n", (error) => {
      if (error) reject(error); else resolve();
    });
  });
  process.exit(0);
} finally {
  if (timer !== undefined) clearTimeout(timer);
  controller.abort();
}
