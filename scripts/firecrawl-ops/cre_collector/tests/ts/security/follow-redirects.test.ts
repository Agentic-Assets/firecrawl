import assert from "node:assert/strict";
import http, { type IncomingHttpHeaders, type RequestOptions } from "node:http";
import { createRequire } from "node:module";
import { test } from "node:test";

const require = createRequire(import.meta.url);
const redirects = require("follow-redirects") as {
  http: Pick<typeof http, "request">;
};

type RedirectOptions = RequestOptions & { sensitiveHeaders?: string[] };

async function redirectedHeaders(options: RedirectOptions, sameOrigin = false) {
  let sinkRequests = 0;
  let sourceHeaders: IncomingHttpHeaders | undefined;
  const receive = (request: http.IncomingMessage, response: http.ServerResponse) => {
    sinkRequests++;
    response.setHeader("Content-Type", "application/json");
    response.end(JSON.stringify(request.headers));
  };
  const target = http.createServer(receive);
  const source = http.createServer((request, response) => {
    if (sameOrigin && request.url === "/destination") {
      receive(request, response);
      return;
    }
    sourceHeaders = request.headers;
    const address = sameOrigin ? source.address() : target.address();
    assert.ok(address && typeof address !== "string");
    response.writeHead(302, {
      Location: `http://${sameOrigin ? "127.0.0.1" : "localhost"}:${address.port}/destination`,
    });
    response.end();
  });

  const listen = (server: http.Server, host: string) =>
    new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, host, resolve);
    });
  const close = (server: http.Server) =>
    new Promise<void>((resolve, reject) => {
      if (!server.listening) return resolve();
      server.close((error) => (error ? reject(error) : resolve()));
    });

  try {
    await listen(target, "127.0.0.1");
    await listen(source, "127.0.0.1");
    const address = source.address();
    assert.ok(address && typeof address !== "string");
    // Distinct loopback hosts exercise the library's cross-host strip rule;
    // different ports on the same hostname are not sufficient for that rule.
    const received = await new Promise<IncomingHttpHeaders>((resolve, reject) => {
      const request = redirects.http.request(
        {
          ...options,
          headers: Array.isArray(options.headers)
            ? ["Host", `127.0.0.1:${address.port}`, ...options.headers]
            : options.headers,
          hostname: "127.0.0.1",
          lookup: ((_host: string, opts: any, callback: any) => {
            if (opts?.all) callback(null, [{ address: "127.0.0.1", family: 4 }]);
            else callback(null, "127.0.0.1", 4);
          }) as RequestOptions["lookup"],
          port: address.port,
          path: "/start",
          method: "GET",
        },
        (response) => {
          let body = "";
          response.setEncoding("utf8");
          response.on("data", (chunk) => {
            body += chunk;
          });
          response.on("error", reject);
          response.on("end", () => {
            try {
              resolve(JSON.parse(body));
            } catch (error) {
              reject(error);
            }
          });
        },
      );
      request.on("error", reject);
      request.setTimeout(5000, () => request.destroy(new Error("loopback timeout")));
      request.end();
    });
    assert.equal(sinkRequests, 1, "the redirect must reach the owned sink once");
    return { received, sourceHeaders };
  } finally {
    await Promise.all([close(source), close(target)]);
  }
}

test("cross-host redirects strip standard credentials from array-form headers", async () => {
  const { received, sourceHeaders } = await redirectedHeaders({
    headers: [
      "Authorization", "Bearer dummy-authorization",
      "Proxy-Authorization", "Basic dummy-proxy",
      "Cookie", "session=dummy-cookie",
      "X-Trace", "retained-control",
    ],
  });
  assert.equal(sourceHeaders?.authorization, "Bearer dummy-authorization");
  assert.equal(sourceHeaders?.["proxy-authorization"], "Basic dummy-proxy");
  assert.equal(sourceHeaders?.cookie, "session=dummy-cookie");
  assert.equal(received["x-trace"], "retained-control");
  assert.deepEqual(
    [received.authorization, received["proxy-authorization"], received.cookie],
    [undefined, undefined, undefined],
  );
});

test("cross-host redirects strip custom sensitive headers containing valid regex metacharacters", async () => {
  const { received, sourceHeaders } = await redirectedHeaders({
    headers: {
      "X-Api^Key": "dummy-caret-secret",
      "X-Token|Secret": "dummy-pipe-secret",
      "X-Control-Key": "dummy-control-secret",
      "X-Trace": "retained-control",
    },
    sensitiveHeaders: ["X-Api^Key", "X-Token|Secret", "X-Control-Key"],
  });
  assert.equal(sourceHeaders?.["x-api^key"], "dummy-caret-secret");
  assert.equal(sourceHeaders?.["x-token|secret"], "dummy-pipe-secret");
  assert.equal(sourceHeaders?.["x-control-key"], "dummy-control-secret");
  assert.equal(received["x-trace"], "retained-control");
  assert.deepEqual(
    [received["x-api^key"], received["x-token|secret"], received["x-control-key"]],
    [undefined, undefined, undefined],
  );
});


test("same-origin redirects preserve sent credentials and custom sensitive headers", async () => {
  const headers = {
    Authorization: "Bearer dummy-same-origin",
    Cookie: "session=dummy-same-origin",
    "X-Api^Key": "dummy-same-origin-caret",
    "X-Token|Secret": "dummy-same-origin-pipe",
  };
  const { received, sourceHeaders } = await redirectedHeaders(
    { headers, sensitiveHeaders: ["X-Api^Key", "X-Token|Secret"] },
    true,
  );
  for (const [name, value] of Object.entries(headers)) {
    assert.equal(sourceHeaders?.[name.toLowerCase()], value);
    assert.equal(received[name.toLowerCase()], value);
  }
});
