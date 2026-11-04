import assert from "node:assert/strict";
import test from "node:test";
import { lyonStahlFetchText } from "../../../sources/lyon-stahl.js";

const encoder = new TextEncoder();

function responseWithBody(
  status: number,
  body: string,
  headers: Record<string, string> = {},
  onCancel: () => void | Promise<void> = () => undefined
): Response {
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(body));
      },
      cancel() {
        return onCancel();
      },
    }),
    { status, headers }
  );
}

test("Lyon definitive HTTP failures are cancelled and fail fast", async () => {
  for (const status of [403, 404, 501]) {
    let calls = 0;
    let cancellations = 0;
    await assert.rejects(
      lyonStahlFetchText(
        "https://lyonstahl.com/properties/example/",
        "html",
        async () => {
          calls++;
          return responseWithBody(status, "denied", { "content-type": "text/html" }, () => {
            cancellations++;
          });
        },
        1_000,
        1_000,
        3
      ),
      new RegExp(`Lyon Stahl HTTP ${status}`)
    );
    assert.equal(calls, 1, `HTTP ${status} must not repeat`);
    assert.equal(cancellations, 1, `HTTP ${status} response body must be cancelled`);
  }
});

test("Lyon 429 with Retry-After remains fail-fast and cancels its body", async () => {
  let calls = 0;
  let cancellations = 0;
  await assert.rejects(
    lyonStahlFetchText(
      "https://lyonstahl.com/properties/example/",
      "html",
      async () => {
        calls++;
        return responseWithBody(
          429,
          "slow down",
          { "content-type": "text/plain", "retry-after": "60" },
          () => { cancellations++; }
        );
      },
      1_000,
      1_000,
      3
    ),
    /Lyon Stahl HTTP 429/
  );
  assert.equal(calls, 1);
  assert.equal(cancellations, 1);
});

test("Lyon retries a selected transient status after cancelling its body", async () => {
  let calls = 0;
  let cancellations = 0;
  const text = await lyonStahlFetchText(
    "https://lyonstahl.com/properties/example/",
    "html",
    async () => {
      calls++;
      if (calls === 1) {
        return responseWithBody(503, "temporarily unavailable", { "content-type": "text/html" }, () => {
          cancellations++;
        });
      }
      return new Response("<html>ok</html>", { headers: { "content-type": "text/html" } });
    },
    1_000,
    1_000,
    3
  );
  assert.equal(text, "<html>ok</html>");
  assert.equal(calls, 2);
  assert.equal(cancellations, 1);
});

test("Lyon transient HTTP recovery stops at the configured attempt bound", async () => {
  let calls = 0;
  let cancellations = 0;
  await assert.rejects(
    lyonStahlFetchText(
      "https://lyonstahl.com/properties/example/",
      "html",
      async () => {
        calls++;
        return responseWithBody(503, "unavailable", { "content-type": "text/html" }, () => {
          cancellations++;
        });
      },
      1_000,
      1_000,
      2
    ),
    /Lyon Stahl HTTP 503/
  );
  assert.equal(calls, 2);
  assert.equal(cancellations, 2);
});

test("Lyon retries a typed transport timeout but not a TLS failure", async () => {
  let calls = 0;
  const text = await lyonStahlFetchText(
    "https://lyonstahl.com/properties/example/",
    "html",
    async () => {
      calls++;
      if (calls === 1) {
        throw new TypeError("fetch failed", { cause: { code: "UND_ERR_CONNECT_TIMEOUT" } });
      }
      return new Response("<html>ok</html>", { headers: { "content-type": "text/html" } });
    },
    1_000,
    1_000,
    3
  );
  assert.equal(text, "<html>ok</html>");
  assert.equal(calls, 2);

  let tlsCalls = 0;
  await assert.rejects(
    lyonStahlFetchText(
      "https://lyonstahl.com/properties/example/",
      "html",
      async () => {
        tlsCalls++;
        throw new TypeError("fetch failed", { cause: { code: "CERT_HAS_EXPIRED" } });
      },
      1_000,
      1_000,
      3
    ),
    /fetch failed/
  );
  assert.equal(tlsCalls, 1, "TLS validation failures are not transient");

  for (const contentType of ["application/pdf", "application/json"]) {
    let validationCalls = 0;
    let cancellations = 0;
    await assert.rejects(
      lyonStahlFetchText(
        "https://lyonstahl.com/properties/example/",
        "html",
        async () => {
          validationCalls++;
          return responseWithBody(200, "body", { "content-type": contentType }, () => {
            cancellations++;
          });
        },
        1_000,
        1_000,
        3
      ),
      /Lyon Stahl (refused PDF response|detail returned)/
    );
    assert.equal(validationCalls, 1, `${contentType} must not repeat`);
    assert.equal(cancellations, 1, `${contentType} response body must be cancelled`);
  }
});

test("Lyon retries its own timeout only after the timed-out fetch settles", async () => {
  let calls = 0;
  let firstSettled = false;
  const text = await lyonStahlFetchText(
    "https://lyonstahl.com/properties/example/",
    "html",
    (_input, init) => {
      calls++;
      if (calls === 1) {
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            firstSettled = true;
            reject(init.signal?.reason);
          }, { once: true });
        });
      }
      assert.equal(firstSettled, true);
      return Promise.resolve(new Response("<html>ok</html>", { headers: { "content-type": "text/html" } }));
    },
    10,
    1_000,
    2
  );
  assert.equal(text, "<html>ok</html>");
  assert.equal(calls, 2);
});

test("Lyon treats any Retry-After header as a no-retry boundary", async () => {
  let calls = 0;
  let cancellations = 0;
  await assert.rejects(
    lyonStahlFetchText(
      "https://lyonstahl.com/properties/example/",
      "html",
      async () => {
        calls++;
        return responseWithBody(503, "busy", { "retry-after": "", "content-type": "text/html" }, () => {
          cancellations++;
        });
      },
      1_000,
      1_000,
      3
    ),
    /Lyon Stahl HTTP 503/
  );
  assert.equal(calls, 1);
  assert.equal(cancellations, 1);
});

test("Lyon unsafe redirects and redirect-cap refusals cancel bodies without retry", async () => {
  let unsafeCalls = 0;
  let unsafeCancellations = 0;
  await assert.rejects(
    lyonStahlFetchText(
      "https://lyonstahl.com/properties/example/",
      "html",
      async () => {
        unsafeCalls++;
        return responseWithBody(302, "redirect", {
          location: "http://lyonstahl.com/properties/unsafe/",
        }, () => { unsafeCancellations++; });
      },
      1_000,
      1_000,
      3
    ),
    /refused unsafe html URL/
  );
  assert.equal(unsafeCalls, 1);
  assert.equal(unsafeCancellations, 1);

  let redirectCalls = 0;
  let redirectCancellations = 0;
  await assert.rejects(
    lyonStahlFetchText(
      "https://lyonstahl.com/properties/example/",
      "html",
      async () => {
        redirectCalls++;
        return responseWithBody(302, "redirect", {
          location: "/properties/example/",
        }, () => { redirectCancellations++; });
      },
      1_000,
      1_000,
      3
    ),
    /exceeded redirect cap/
  );
  assert.equal(redirectCalls, 4);
  assert.equal(redirectCancellations, 4);
});

test("Lyon settles a redirect body cancellation before the next physical request", async () => {
  let calls = 0;
  let cancellationSettled = false;
  const text = await lyonStahlFetchText(
    "https://lyonstahl.com/properties/example/",
    "html",
    async () => {
      calls++;
      if (calls === 1) {
        return responseWithBody(302, "redirect", { location: "/properties/next/" }, async () => {
          await new Promise((resolve) => setTimeout(resolve, 20));
          cancellationSettled = true;
        });
      }
      assert.equal(cancellationSettled, true);
      return new Response("<html>ok</html>", { headers: { "content-type": "text/html" } });
    },
    1_000,
    1_000,
    1
  );
  assert.equal(text, "<html>ok</html>");
  assert.equal(calls, 2);
});

test("Lyon bounds a hanging refusal-body cancellation and preserves its HTTP cause", async () => {
  let calls = 0;
  await assert.rejects(
    lyonStahlFetchText(
      "https://lyonstahl.com/properties/example/",
      "html",
      async () => {
        calls++;
        return responseWithBody(404, "missing", { "content-type": "text/html" }, () =>
          new Promise(() => undefined)
        );
      },
      25,
      1_000,
      3
    ),
    /Lyon Stahl HTTP 404/
  );
  assert.equal(calls, 1);
});

test("Lyon retries a settled response-body transport failure only", async () => {
  let calls = 0;
  let cancellations = 0;
  const text = await lyonStahlFetchText(
    "https://lyonstahl.com/properties/example/",
    "html",
    async () => {
      calls++;
      if (calls === 1) {
        return new Response(new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(encoder.encode("partial"));
          },
          pull(controller) {
            controller.error(new TypeError("fetch failed", { cause: { code: "ECONNRESET" } }));
          },
          cancel() {
            cancellations++;
          },
        }), { headers: { "content-type": "text/html" } });
      }
      return new Response("<html>ok</html>", { headers: { "content-type": "text/html" } });
    },
    1_000,
    1_000,
    2
  );
  assert.equal(text, "<html>ok</html>");
  assert.equal(calls, 2);
  assert.equal(cancellations, 0, "an already-errored stream is settled without a cancel call");
});

test("Lyon decoded-byte cap is cancelled and is not retried", async () => {
  let calls = 0;
  let cancellations = 0;
  await assert.rejects(
    lyonStahlFetchText(
      "https://lyonstahl.com/properties/example/",
      "html",
      async () => {
        calls++;
        return responseWithBody(200, "oversized", { "content-type": "text/html" }, () => {
          cancellations++;
        });
      },
      1_000,
      4,
      3
    ),
    /exceeds 4 bytes/
  );
  assert.equal(calls, 1);
  assert.equal(cancellations, 1);
});
