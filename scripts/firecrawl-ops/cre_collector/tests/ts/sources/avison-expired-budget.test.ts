process.argv = [process.argv[0]!, process.argv[1]!];
import assert from "node:assert/strict";
import test from "node:test";
import https from "node:https";
import { syncBuiltinESMExports } from "node:module";
const originalRequest = https.request;
let requests = 0;
https.request = (() => { requests++; throw new Error("Expired operation dispatched HTTPS"); }) as typeof https.request;
syncBuiltinESMExports();
const source = await import("../../../sources/avison-young.js");
const transport = await import("../../../sources/avison-young-transport.js");
test.after(() => { https.request = originalRequest; syncBuiltinESMExports(); });

test("AY entity expired zero budget dispatches no fetch", { concurrency: false }, async (t) => {
  const originalFetch = globalThis.fetch;
  let fetches = 0;
  globalThis.fetch = (async () => { fetches++; throw new Error("Expired operation dispatched fetch"); }) as typeof fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  await assert.rejects(() => source.fetchAvisonYoungEntity("website", "owned-synthetic-key", 0), /deadline/i);
  assert.equal(fetches, 0);
});

test("AY pinned helper retains expired original absolute deadline", { concurrency: false }, async () => {
  await assert.rejects(() => transport.requestAvisonYoungPinned(
    new URL("https://www.avisonyoung.us/properties/owned-expired"),
    "8.8.8.8",
    80,
    performance.now() - 1,
  ), /deadline/i);
  assert.equal(requests, 0);
});