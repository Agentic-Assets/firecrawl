import test from "node:test";
import assert from "node:assert/strict";
import {
  DAUM_MAX_RESPONSE_BYTES,
  DAUM_ROBOTS_DELAY_MS,
  DaumPoliteTransport,
  daumPageUrl,
} from "../../../sources/daum-commercial.js";

const PROPERTY_URL = "https://daumcommercial.com/property/synthetic/";

function unreadResponse(
  status: number,
  headers: HeadersInit = {},
  onCancel: () => void | Promise<void> = () => {},
): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new Uint8Array([1]));
    },
    cancel() {
      return onCancel();
    },
  });
  return new Response(body, { status, headers });
}

function codedError(code: string, message = "synthetic transport failure"): TypeError {
  return new TypeError(message, { cause: { code } });
}

test("DAUM exposes the existing transport defaults", () => {
  assert.equal(DAUM_ROBOTS_DELAY_MS, 3_000);
  assert.equal(DAUM_MAX_RESPONSE_BYTES, 5_000_000);
});

test("DAUM refuses definitive HTTP responses and Retry-After without retrying", async (t) => {
  const cases: Array<{ name: string; status: number; headers?: HeadersInit }> = [
    { name: "401", status: 401 },
    { name: "403", status: 403 },
    { name: "404", status: 404 },
    { name: "manual redirect", status: 302, headers: { location: "https://example.com/" } },
    { name: "503 with Retry-After", status: 503, headers: { "retry-after": "120" } },
  ];

  for (const item of cases) {
    await t.test(item.name, async () => {
      let calls = 0;
      let cancellations = 0;
      let redirectMode: RequestRedirect | undefined;
      const transport = new DaumPoliteTransport(async (_input, init) => {
        calls++;
        redirectMode = init?.redirect;
        return unreadResponse(item.status, item.headers, () => { cancellations++; });
      }, 0);

      await assert.rejects(transport.get(PROPERTY_URL));
      assert.equal(calls, 1);
      assert.equal(cancellations, 1, "discarded response body is cancelled");
      assert.equal(redirectMode, "manual");
    });
  }
});

test("DAUM retries only the bounded transient HTTP status set without Retry-After", async (t) => {
  for (const status of [429, 500, 502, 503, 504]) {
    await t.test(String(status), async () => {
      let calls = 0;
      let cancellations = 0;
      const transport = new DaumPoliteTransport(async () => {
        calls++;
        if (calls === 1) return unreadResponse(status, {}, () => { cancellations++; });
        return new Response("recovered");
      }, 0);

      assert.equal(await transport.get(PROPERTY_URL), "recovered");
      assert.equal(calls, 2);
      assert.equal(cancellations, 1, "the first response settled before its replacement");
    });
  }
});

test("DAUM retries known transient transport failures but not arbitrary exceptions", async (t) => {
  await t.test("recognized connection reset recovers once", async () => {
    let calls = 0;
    const transport = new DaumPoliteTransport(async () => {
      calls++;
      if (calls === 1) throw codedError("ECONNRESET");
      return new Response("recovered");
    }, 0);

    assert.equal(await transport.get(PROPERTY_URL), "recovered");
    assert.equal(calls, 2);
  });

  await t.test("recognized timeout exhaustion remains at two attempts", async () => {
    let calls = 0;
    const transport = new DaumPoliteTransport(async () => {
      calls++;
      throw codedError("UND_ERR_CONNECT_TIMEOUT");
    }, 0);

    await assert.rejects(transport.get(PROPERTY_URL));
    assert.equal(calls, 2);
  });

  await t.test("exhausted body timeout keeps the original transport cause", async () => {
    let calls = 0;
    const timeout = codedError("UND_ERR_BODY_TIMEOUT", "body timeout");
    const transport = new DaumPoliteTransport(async () => {
      calls++;
      return new Response(new ReadableStream<Uint8Array>({ start(controller) { controller.error(timeout); } }));
    }, 0);

    const failure = await transport.get(PROPERTY_URL).then(() => null, (error: unknown) => error);
    assert.ok(failure instanceof Error);
    assert.equal(calls, 2);
    const marker = failure.cause as Error & { cause?: unknown };
    assert.equal(marker.message, "DAUM response body read failed");
    assert.equal(marker.cause, timeout);
  });

  await t.test("arbitrary TypeError fails without replay", async () => {
    let calls = 0;
    const transport = new DaumPoliteTransport(async () => {
      calls++;
      throw new TypeError("not a recognized transport failure");
    }, 0);

    await assert.rejects(transport.get(PROPERTY_URL));
    assert.equal(calls, 1);
  });

  await t.test("the attempt timeout abort is a recognized transient", async () => {
    let calls = 0;
    let firstSignal: AbortSignal | undefined;
    const transport = new DaumPoliteTransport(async (_input, init) => {
      calls++;
      if (calls > 1) return new Response("recovered");
      const signal = init?.signal as AbortSignal;
      firstSignal = signal;
      return await new Promise<Response>((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(codedError("UND_ERR_HEADERS_TIMEOUT")), { once: true });
      });
    }, 0, 5);

    assert.equal(await transport.get(PROPERTY_URL), "recovered");
    assert.equal(calls, 2);
    assert.equal(firstSignal?.aborted, true);
  });
});

test("DAUM retries only the exact empty search POST routes, not arbitrary POSTs", async (t) => {
  for (const url of [daumPageUrl(1), daumPageUrl(2)]) {
    await t.test(url, async () => {
      const calls: Array<{ url: string; method: unknown; body: unknown; contentType: string | null }> = [];
      const transport = new DaumPoliteTransport(async (input, init) => {
        calls.push({
          url: String(input),
          method: init?.method,
          body: init?.body,
          contentType: new Headers(init?.headers).get("content-type"),
        });
        if (calls.length === 1) return unreadResponse(503);
        return new Response("search result");
      }, 0);

      assert.equal(await transport.post(url), "search result");
      assert.deepEqual(calls, [
        { url, method: "POST", body: "", contentType: "application/x-www-form-urlencoded" },
        { url, method: "POST", body: "", contentType: "application/x-www-form-urlencoded" },
      ]);
    });
  }

  await t.test("another owned POST path is not replayed", async () => {
    let calls = 0;
    const transport = new DaumPoliteTransport(async () => {
      calls++;
      return unreadResponse(503);
    }, 0);

    await assert.rejects(transport.post("https://daumcommercial.com/property/contact/"));
    assert.equal(calls, 1);
  });
});

test("DAUM keeps serial spacing at every physical start, including retries", async () => {
  const starts: number[] = [];
  let firstCalls = 0;
  const transport = new DaumPoliteTransport(async (input) => {
    starts.push(Date.now());
    if (String(input) === PROPERTY_URL && firstCalls++ === 0) return unreadResponse(503);
    return new Response("ok");
  }, 25);

  const first = transport.get(PROPERTY_URL);
  const second = transport.post(daumPageUrl(2));
  assert.deepEqual(await Promise.all([first, second]), ["ok", "ok"]);
  assert.equal(starts.length, 3);
  assert.ok(starts[1] - starts[0] >= 18, "retry start observes the configured interval");
  assert.ok(starts[2] - starts[1] >= 18, "next queued request observes the configured interval");
});

test("DAUM preserves raw empty and non-HTML response text behavior", async (t) => {
  await t.test("empty success remains empty text", async () => {
    const transport = new DaumPoliteTransport(async () => new Response(null, { status: 204 }), 0);
    assert.equal(await transport.get(PROPERTY_URL), "");
  });

  await t.test("content type does not transform transport text", async () => {
    const transport = new DaumPoliteTransport(
      async () => new Response('{"raw":true}', { headers: { "content-type": "application/json" } }),
      0,
    );
    assert.equal(await transport.get(PROPERTY_URL), '{"raw":true}');
  });
});

test("DAUM known errored-stream timeout retries only after terminal read settlement", async (t) => {
  await t.test("the same reader-closed timeout error permits one replacement", async () => {
    let calls = 0;
    const timeout = codedError("UND_ERR_BODY_TIMEOUT", "body timeout");
    const transport = new DaumPoliteTransport(async () => {
      calls++;
      if (calls > 1) return new Response("recovered");
      return new Response(new ReadableStream<Uint8Array>({ start(controller) { controller.error(timeout); } }));
    }, 0);

    assert.equal(await transport.get(PROPERTY_URL), "recovered");
    assert.equal(calls, 2);
  });

  await t.test("unknown errored-stream failure is not retried", async () => {
    let calls = 0;
    const failure = new TypeError("unrecognized body failure");
    const transport = new DaumPoliteTransport(async () => {
      calls++;
      return new Response(new ReadableStream<Uint8Array>({ start(controller) { controller.error(failure); } }));
    }, 0);

    await assert.rejects(transport.get(PROPERTY_URL));
    assert.equal(calls, 1);
  });

  await t.test("a known body timeout does not replay an arbitrary POST", async () => {
    let calls = 0;
    const timeout = codedError("UND_ERR_BODY_TIMEOUT", "body timeout");
    const transport = new DaumPoliteTransport(async () => {
      calls++;
      return new Response(new ReadableStream<Uint8Array>({ start(controller) { controller.error(timeout); } }));
    }, 0);

    await assert.rejects(transport.post("https://daumcommercial.com/property/contact/"));
    assert.equal(calls, 1);
  });
});

test("a settled definitive HTTP response is not retried when cleanup outlasts the attempt timer", async () => {
  let calls = 0;
  let cancellations = 0;
  const transport = new DaumPoliteTransport(async () => {
    calls++;
    return unreadResponse(403, {}, () => new Promise<void>((resolve) => {
      cancellations++;
      setTimeout(resolve, 20);
    }));
  }, 0, 5);

  await assert.rejects(transport.get(PROPERTY_URL), /HTTP 403/);
  assert.equal(calls, 1);
  assert.equal(cancellations, 1);
});

test("DAUM settled refusal leaves queue usable, uncertain cleanup poisons it", async (t) => {
  await t.test("resolved cancellation permits the next queued request", async () => {
    let calls = 0;
    const transport = new DaumPoliteTransport(async () => {
      calls++;
      if (calls === 1) return unreadResponse(403);
      return new Response("later request");
    }, 0);

    const first = transport.get(PROPERTY_URL);
    const second = transport.get("https://daumcommercial.com/property/next/");
    const results = await Promise.allSettled([first, second]);
    assert.equal(results[0].status, "rejected");
    assert.deepEqual(results[1], { status: "fulfilled", value: "later request" });
    assert.equal(calls, 2);
  });

  await t.test("rejected body cancellation blocks retry and queued fetch", async () => {
    let calls = 0;
    let cancellations = 0;
    const transport = new DaumPoliteTransport(async () => {
      calls++;
      if (calls === 1) {
        return unreadResponse(403, {}, () => {
          cancellations++;
          throw new Error("cancel failed");
        });
      }
      return new Response("must not fetch");
    }, 0);

    const first = transport.get(PROPERTY_URL);
    const second = transport.get("https://daumcommercial.com/property/next/");
    const results = await Promise.allSettled([first, second]);
    assert.equal(results[0].status, "rejected");
    assert.equal(results[1].status, "rejected");
    assert.equal(cancellations, 1);
    assert.equal(calls, 1);
  });

  await t.test("cleanup exceeding its bound blocks retry and queued fetch", async () => {
    let calls = 0;
    let cancellations = 0;
    const transport = new DaumPoliteTransport(async () => {
      calls++;
      if (calls === 1) {
        return unreadResponse(403, {}, () => {
          cancellations++;
          return new Promise<void>((resolve) => setTimeout(resolve, 1_100));
        });
      }
      return new Response("must not fetch");
    }, 0);

    const first = transport.get(PROPERTY_URL);
    const second = transport.get("https://daumcommercial.com/property/next/");
    const results = await Promise.allSettled([first, second]);
    assert.equal(results[0].status, "rejected");
    assert.equal(results[1].status, "rejected");
    assert.equal(cancellations, 1);
    assert.equal(calls, 1);
  });
});

test("DAUM oversize responses are not retried and settled bodies do not poison the queue", async () => {
  let calls = 0;
  let cancellations = 0;
  const transport = new DaumPoliteTransport(async () => {
    calls++;
    if (calls === 1) return unreadResponse(200, { "content-length": "8" }, () => { cancellations++; });
    return new Response("OK");
  }, 0, 30_000, 4);

  const first = transport.get(PROPERTY_URL);
  const second = transport.get("https://daumcommercial.com/property/next/");
  const results = await Promise.allSettled([first, second]);
  assert.equal(results[0].status, "rejected");
  assert.deepEqual(results[1], { status: "fulfilled", value: "OK" });
  assert.equal(cancellations, 1);
  assert.equal(calls, 2);
});

test("DAUM streaming size overflow cancels its body before failing the response", async () => {
  let calls = 0;
  let cancellations = 0;
  const transport = new DaumPoliteTransport(async () => {
    calls++;
    if (calls === 1) {
      return new Response(new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new Uint8Array([1, 2, 3, 4, 5]));
        },
        cancel() {
          cancellations++;
        },
      }));
    }
    return new Response("OK");
  }, 0, 30_000, 4);

  await assert.rejects(transport.get(PROPERTY_URL), /exceeds 4 bytes/);
  assert.equal(cancellations, 1);
  assert.equal(calls, 1);
});
