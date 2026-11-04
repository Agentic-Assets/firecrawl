import assert from "node:assert/strict";
import test from "node:test";
import { fetchJllDirectDoc } from "../../../sources/jll-direct.js";
import type { ScrapedDoc } from "../../../types.js";

const url = "https://property.jll.com/listings/example";
const dependencies = {
  resolveHost: async () => ["93.184.216.34"],
  requestPinned: async () => ({
    status: 200,
    location: null,
    contentType: "text/html; charset=utf-8",
    body: "<h1>Example</h1>",
  }),
};
const doc = (): ScrapedDoc => ({
  rawHtml: "<h1>Example</h1>",
  markdown: "Example",
  links: [],
  metadata: {},
});

test("direct reconstruction receives actual accepted response context", async () => {
  let received: unknown;
  const result = await fetchJllDirectDoc(url, 2000, {
    ...dependencies,
    reconstruct: async (...args: unknown[]) => {
      received = args;
      return doc();
    },
  });
  assert.deepEqual(received, [
    "<h1>Example</h1>",
    { url, statusCode: 200, contentType: "text/html; charset=utf-8" },
  ]);
  assert.equal(result.metadata?.providerAttempts, 1);
  assert.equal(result.detailObservation?.method, "jll_direct_candidate");
});

for (const reason of ["abort", "deadline"] as const) {
  test(`direct ${reason} waits for reconstruction settlement and never credits fresh success`, async () => {
    const controller = new AbortController();
    let entered!: () => void;
    const ready = new Promise<void>((resolve) => {
      entered = resolve;
    });
    let finish!: (value: ScrapedDoc) => void;
    let settled = false;
    const returned = doc();
    const pending = fetchJllDirectDoc(url, reason === "deadline" ? 500 : 2000, {
      ...dependencies,
      signal: controller.signal,
      reconstruct: async () => {
        entered();
        return new Promise<ScrapedDoc>((resolve) => {
          finish = resolve;
        });
      },
    });
    const observed = pending.then(
      () => {
        settled = true;
      },
      (error) => {
        settled = true;
        throw error;
      },
    );
    await Promise.race([
      ready,
      new Promise<never>((_, reject) =>
        setTimeout(
          () => reject(new Error("reconstruction was not called")),
          1000,
        ),
      ),
    ]);
    if (reason === "abort") controller.abort(new Error("cancel conversion"));
    await new Promise((resolve) =>
      setTimeout(resolve, reason === "deadline" ? 550 : 20),
    );
    assert.equal(
      settled,
      false,
      "must not race-return while converter is pending",
    );
    finish(returned);
    await assert.rejects(
      observed,
      reason === "deadline" ? /deadline/ : /cancel conversion/,
    );
    assert.equal(returned.detailObservation, undefined);
  });
}

test("conversion failure is returned once without another request", async () => {
  let requests = 0;
  await assert.rejects(
    fetchJllDirectDoc(url, 2000, {
      ...dependencies,
      requestPinned: async () => {
        requests++;
        return dependencies.requestPinned();
      },
      reconstruct: async () => {
        throw new Error("native unavailable");
      },
    }),
    /native unavailable/,
  );
  assert.equal(requests, 1);
});
