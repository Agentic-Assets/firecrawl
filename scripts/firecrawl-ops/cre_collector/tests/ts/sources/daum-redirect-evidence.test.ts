import test from "node:test";
import assert from "node:assert/strict";
import { DaumPoliteTransport } from "../../../sources/daum-transport.js";

const oldUrl = "https://daumcommercial.com/property/old-path/";
const newUrl = "https://daumcommercial.com/property/new-path/";

function reply(status: number, location: string | undefined, cancelled: () => void): Response {
  return new Response(new ReadableStream({ cancel: cancelled }), {
    status,
    headers: location === undefined ? {} : { location },
  });
}

test("DAUM permanent redirect evidence preserves the source Location and settles its body", async () => {
  let calls = 0;
  let cancelled = 0;
  const transport = new DaumPoliteTransport(async (url, init) => {
    calls++;
    assert.equal(String(url), oldUrl);
    assert.equal(init?.redirect, "manual");
    assert.equal(init?.cache, "no-store");
    return reply(301, "/property/new-path/", () => { cancelled++; });
  }, 0);
  const receipt = await transport.observePermanentRedirect(oldUrl, newUrl);
  assert.equal(receipt.status, 301);
  assert.equal(receipt.oldUrl, oldUrl);
  assert.equal(receipt.newUrl, newUrl);
  assert.equal(receipt.rawLocation, "/property/new-path/");
  assert.ok(Number.isFinite(Date.parse(receipt.observedAt)));
  assert.equal(calls, 1);
  assert.equal(cancelled, 1);
});

for (const [name, status, location] of [
  ["temporary redirect", 302, newUrl],
  ["wrong path", 301, "https://daumcommercial.com/property/other/"],
  ["foreign origin", 301, "https://example.com/property/new-path/"],
  ["missing location", 301, undefined],
  ["non-redirect", 200, undefined],
] as const) {
  test("DAUM redirect evidence refuses " + name, async () => {
    let cancelled = 0;
    const transport = new DaumPoliteTransport(async () => reply(status, location, () => { cancelled++; }), 0);
    await assert.rejects(transport.observePermanentRedirect(oldUrl, newUrl));
    assert.equal(cancelled, 1);
  });
}

test("DAUM redirect evidence poisons transport on unacknowledged cleanup", async () => {
  let calls = 0;
  const transport = new DaumPoliteTransport(async () => {
    calls++;
    return reply(301, newUrl, () => Promise.reject(new Error("cleanup failed")));
  }, 0);
  await assert.rejects(transport.observePermanentRedirect(oldUrl, newUrl));
  await assert.rejects(transport.get(newUrl), /uncertain response-body cleanup/);
  assert.equal(calls, 1);
});
