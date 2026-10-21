import assert from "node:assert/strict";
import test from "node:test";
import { foundryFetchText } from "../../../sources/foundry-commercial.js";

test("Foundry fetch validates redirect targets, streams byte bounds, and supplies an abort signal", async () => {
  let cancelled = false;
  await assert.rejects(
    () =>
      foundryFetchText(
        "https://www.foundrycommercial.com/property/oversized/",
        "html",
        async (_input, init) => {
          assert.ok(init?.signal);
          assert.equal(init?.redirect, "manual");
          return new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                controller.enqueue(new TextEncoder().encode("abc"));
                controller.enqueue(new TextEncoder().encode("def"));
              },
              cancel() {
                cancelled = true;
              },
            }),
            { headers: { "content-type": "text/html" } }
          );
        },
        1_000,
        4,
        1
      ),
    /exceeds 4 bytes/
  );
  assert.equal(cancelled, true);
  let redirectCalls = 0;
  assert.equal(
    await foundryFetchText(
      "https://www.foundrycommercial.com/sitemap.xml",
      "xml",
      async (input, init) => {
        assert.equal(init?.redirect, "manual");
        redirectCalls++;
        return redirectCalls === 1
          ? new Response("", {
              status: 301,
              headers: { location: "/sitemap_index.xml" },
            })
          : new Response("<sitemapindex><sitemap><loc>https://www.foundrycommercial.com/property-sitemap.xml</loc></sitemap></sitemapindex>", {
              headers: { "content-type": "application/xml" },
            });
      },
      1_000,
      1_000,
      1
    ),
    "<sitemapindex><sitemap><loc>https://www.foundrycommercial.com/property-sitemap.xml</loc></sitemap></sitemapindex>"
  );
  assert.equal(redirectCalls, 2);
  await assert.rejects(
    () =>
      foundryFetchText(
        "https://www.foundrycommercial.com/property/redirected/",
        "html",
        async () =>
          new Response("", {
            status: 302,
            headers: { location: "http://www.foundrycommercial.com/property/insecure/" },
          }),
        1_000,
        1_000,
        1
      ),
    /refused unsafe html URL/
  );
  const secret = "synthetic-redirect-secret";
  await assert.rejects(
    () =>
      foundryFetchText(
        "https://www.foundrycommercial.com/property/redirected/",
        "html",
        async () =>
          new Response("", {
            status: 302,
            headers: {
              location:
                `/property/redirected/?session=${secret}#${secret}`,
            },
          }),
        1_000,
        1_000,
        1
      ),
    (error: unknown) => {
      assert.match(String(error), /refused unsafe html URL/);
      assert.equal(String(error).includes(secret), false);
      return true;
    }
  );
  await assert.rejects(
    () =>
      foundryFetchText(
        "https://www.foundrycommercial.com/property/timeout/",
        "html",
        async (_input, init) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () => reject(new Error("aborted")), {
              once: true,
            });
          }),
        5,
        100,
        1
      ),
    /aborted/
  );
});

test("Foundry fetch refuses definitive HTTP responses once and settles their bodies", async () => {
  const cases = [
    { status: 400, headers: {} },
    { status: 403, headers: {} },
    { status: 404, headers: {} },
    { status: 429, headers: { "retry-after": "120" } },
    { status: 500, headers: { "retry-after": "120" } },
    { status: 502, headers: { "retry-after": "120" } },
    { status: 503, headers: { "retry-after": "120" } },
    { status: 504, headers: { "retry-after": "120" } },
  ] as const;

  for (const { status, headers } of cases) {
    let calls = 0;
    let cancellations = 0;
    const url = "https://www.foundrycommercial.com/property/retry-policy/";
    await assert.rejects(
      () =>
        foundryFetchText(
          url,
          "html",
          async () => {
            calls++;
            return new Response(
              new ReadableStream<Uint8Array>({
                start(controller) {
                  controller.enqueue(new TextEncoder().encode("denied"));
                },
                cancel() {
                  cancellations++;
                },
              }),
              { status, headers: { "content-type": "text/html", ...headers } }
            );
          },
          1_000,
          1_000,
          2
        ),
      new RegExp(`Foundry HTTP ${status}`)
    );
    assert.equal(calls, 1, `HTTP ${status} must not be retried`);
    assert.equal(cancellations, 1, `HTTP ${status} response body must be cancelled`);
  }
});

test("Foundry retries bounded transient failures but not arbitrary fetch errors", async () => {
  for (const status of [429, 500, 502, 503, 504]) {
    let statusCalls = 0;
    let firstBodyCancelled = false;
    const body = await foundryFetchText(
      "https://www.foundrycommercial.com/property/transient-status/",
      "html",
      async () => {
        statusCalls++;
        if (statusCalls === 1) {
          return new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                controller.enqueue(new TextEncoder().encode("busy"));
              },
              cancel() {
                firstBodyCancelled = true;
              },
            }),
            { status }
          );
        }
        assert.equal(firstBodyCancelled, true, `HTTP ${status} body must settle before retry`);
        return new Response("<html>ready</html>", { headers: { "content-type": "text/html" } });
      },
      1_000,
      1_000,
      2
    );
    assert.equal(body, "<html>ready</html>");
    assert.equal(statusCalls, 2);
  }

  let transportCalls = 0;
  const recovered = await foundryFetchText(
    "https://www.foundrycommercial.com/property/transient-transport/",
    "html",
    async () => {
      transportCalls++;
      if (transportCalls === 1) {
        throw new TypeError("fetch failed", { cause: { code: "ECONNRESET" } });
      }
      return new Response("<html>ready</html>", { headers: { "content-type": "text/html" } });
    },
    1_000,
    1_000,
    2
  );
  assert.equal(recovered, "<html>ready</html>");
  assert.equal(transportCalls, 2);

  let unexpectedCalls = 0;
  await assert.rejects(
    () =>
      foundryFetchText(
        "https://www.foundrycommercial.com/property/unexpected-error/",
        "html",
        async () => {
          unexpectedCalls++;
          throw new TypeError("invalid test harness response");
        },
        1_000,
        1_000
      ),
    /invalid test harness response/
  );
  assert.equal(unexpectedCalls, 1);
});

test("Foundry retries a timed-out request within the attempt bound", async () => {
  let calls = 0;
  const body = await foundryFetchText(
    "https://www.foundrycommercial.com/property/transient-timeout/",
    "html",
    async (_input, init) => {
      calls++;
      if (calls === 1) {
        return await new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new Error("timed out")), { once: true });
        });
      }
      return new Response("<html>ready</html>", { headers: { "content-type": "text/html" } });
    },
    5,
    1_000,
    2
  );
  assert.equal(body, "<html>ready</html>");
  assert.equal(calls, 2);
});

test("Foundry retries a terminal transient body-read error but not an unknown one", async () => {
  let transientCalls = 0;
  const recovered = await foundryFetchText(
    "https://www.foundrycommercial.com/property/transient-body-read/",
    "html",
    async () => {
      transientCalls++;
      if (transientCalls === 1) {
        const error = new TypeError("body timed out", { cause: { code: "UND_ERR_BODY_TIMEOUT" } });
        return new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              controller.error(error);
            },
          }),
          { headers: { "content-type": "text/html" } }
        );
      }
      return new Response("<html>ready</html>", { headers: { "content-type": "text/html" } });
    },
    1_000,
    1_000,
    2
  );
  assert.equal(recovered, "<html>ready</html>");
  assert.equal(transientCalls, 2);

  let unknownCalls = 0;
  await assert.rejects(
    () =>
      foundryFetchText(
        "https://www.foundrycommercial.com/property/unknown-body-read/",
        "html",
        async () => {
          unknownCalls++;
          const error = new TypeError("unexpected body error", { cause: { code: "UNEXPECTED" } });
          return new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                controller.error(error);
              },
            }),
            { headers: { "content-type": "text/html" } }
          );
        },
        1_000,
        1_000,
        2
      ),
    /unexpected body error/
  );
  assert.equal(unknownCalls, 1);
});

test("Foundry does not retry content, size, empty-body, or unsafe-redirect refusals", async () => {
  const cases = [
    {
      name: "PDF response",
      makeResponse: () => new Response("pdf", { headers: { "content-type": "application/pdf" } }),
      maxBytes: 1_000,
      expected: /refused PDF/,
    },
    {
      name: "unexpected content type",
      makeResponse: () => new Response("image", { headers: { "content-type": "image/png" } }),
      maxBytes: 1_000,
      expected: /returned image\/png/,
    },
    {
      name: "oversized body",
      makeResponse: () => new Response("oversized", { headers: { "content-type": "text/html" } }),
      maxBytes: 3,
      expected: /exceeds 3 bytes/,
    },
    {
      name: "empty body",
      makeResponse: () => new Response("", { headers: { "content-type": "text/html" } }),
      maxBytes: 1_000,
      expected: /returned empty html/,
    },
    {
      name: "unsafe redirect",
      makeResponse: () => new Response("redirect", {
        status: 302,
        headers: { location: "http://foreign.example/property/not-owned/" },
      }),
      maxBytes: 1_000,
      expected: /refused unsafe html URL/,
    },
  ];

  for (const refusal of cases) {
    let calls = 0;
    await assert.rejects(
      () =>
        foundryFetchText(
          "https://www.foundrycommercial.com/property/refusal/",
          "html",
          async () => {
            calls++;
            return refusal.makeResponse();
          },
          1_000,
          refusal.maxBytes,
          2
        ),
      refusal.expected,
      refusal.name
    );
    assert.equal(calls, 1, `${refusal.name} must not be retried`);
  }
});

test("Foundry refuses to retry a transient status when response cancellation does not settle", async () => {
  let calls = 0;
  await assert.rejects(
    () =>
      foundryFetchText(
        "https://www.foundrycommercial.com/property/unsettled-body/",
        "html",
        async () => {
          calls++;
          return new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                controller.enqueue(new TextEncoder().encode("busy"));
              },
              cancel() {
                return new Promise<void>(() => {});
              },
            }),
            { status: 503 }
          );
        },
        10,
        1_000,
        2
      ),
    /cancel|settle/i
  );
  assert.equal(calls, 1);
});

test("Foundry refuses to retry when response cancellation rejects", async () => {
  let calls = 0;
  await assert.rejects(
    () =>
      foundryFetchText(
        "https://www.foundrycommercial.com/property/rejected-cancel/",
        "html",
        async () => {
          calls++;
          return new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                controller.enqueue(new TextEncoder().encode("busy"));
              },
              cancel() {
                return Promise.reject(new Error("cancel failed"));
              },
            }),
            { status: 503 }
          );
        },
        1_000,
        1_000,
        2
      ),
    /cancel|settle/i
  );
  assert.equal(calls, 1);
});

test("Foundry stops at the redirect cap without retrying the original URL", async () => {
  let calls = 0;
  let cancellations = 0;
  await assert.rejects(
    () =>
      foundryFetchText(
        "https://www.foundrycommercial.com/property/redirect-start/",
        "html",
        async () => {
          calls++;
          return new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                controller.enqueue(new TextEncoder().encode("redirect"));
              },
              cancel() {
                cancellations++;
              },
            }),
            { status: 302, headers: { location: `/property/redirect-${calls}/` } }
          );
        },
        1_000,
        1_000,
        2
      ),
    /exceeded redirect cap/
  );
  assert.equal(calls, 4);
  assert.equal(cancellations, 4);
});

test("Foundry stops after the configured number of retryable responses", async () => {
  let calls = 0;
  await assert.rejects(
    () =>
      foundryFetchText(
        "https://www.foundrycommercial.com/property/retry-exhausted/",
        "html",
        async () => {
          calls++;
          return new Response("busy", { status: 503 });
        },
        1_000,
        1_000,
        2
      ),
    /Foundry HTTP 503/
  );
  assert.equal(calls, 2);
});

test("Foundry cancels a redirect response body before requesting its validated target", async () => {
  let redirectBodyCancelled = false;
  let calls = 0;
  const body = await foundryFetchText(
    "https://www.foundrycommercial.com/sitemap.xml",
    "xml",
    async () => {
      calls++;
      if (calls === 1) {
        return new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(new TextEncoder().encode("redirect"));
            },
            cancel() {
              redirectBodyCancelled = true;
            },
          }),
          { status: 302, headers: { location: "/sitemap_index.xml" } }
        );
      }
      assert.equal(redirectBodyCancelled, true);
      return new Response("<sitemapindex></sitemapindex>", {
        headers: { "content-type": "application/xml" },
      });
    },
    1_000,
    1_000,
    1
  );
  assert.equal(body, "<sitemapindex></sitemapindex>");
  assert.equal(calls, 2);
});

