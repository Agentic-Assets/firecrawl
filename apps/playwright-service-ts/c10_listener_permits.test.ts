/**
 * C10 listener permit and deadline accounting (AGENTIC-3758) with injected
 * fakes: no browser, no network, and the listener binds 127.0.0.1 only. The
 * invariant: every request is answered by its deadline, and the page permit
 * and lease slot are held exactly as long as a context may be live.
 */
import assert from "node:assert/strict";
import { generateKeyPairSync, randomUUID } from "node:crypto";
import type http from "node:http";
import type net from "node:net";
import test from "node:test";
import type { BrowserContext, Page } from "playwright";

import { Semaphore } from "./browser_resources";
import {
  C10_BROWSER_INTERNAL_PATH,
  canonicalJson,
  issueC10SidecarCapability,
  publicKeyId,
  sha256,
} from "./c10_browser_internal";
import {
  createC10BrowserListener,
  readC10BrowserListenerConfig,
} from "./c10_browser_listener";
import { BrowserResourceLeakError } from "./permit_lease";

const CAPACITY = 2;
const CARD_TIMEOUT_MS = 300;
const TRANSPORT_KEY = "t".repeat(48);

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}
const pemPair = () => {
  const pair = generateKeyPairSync("ed25519");
  return {
    privateKeyPem: pair.privateKey.export({ type: "pkcs8", format: "pem" }).toString(),
    publicKeyPem: pair.publicKey.export({ type: "spki", format: "pem" }).toString(),
  };
};
const b64 = (value: string) => Buffer.from(value, "utf8").toString("base64");

let currentObserver: ((event: unknown) => void) | undefined;
function fakePage(): Page {
  return {
    async setExtraHTTPHeaders() {},
    async goto() {
      return { status: () => 200 };
    },
    url: () => "https://property.jll.com/",
    async evaluate(_operation: unknown, instruction: { url: string }) {
      currentObserver?.({ response: { fromDiskCache: false } });
      return {
        status: 200,
        finalUrl: instruction.url,
        redirected: false,
        contentType: "text/html",
        bodyBase64: Buffer.from("<html>ok</html>").toString("base64"),
      };
    },
    async close() {},
  } as unknown as Page;
}

class FakeC10Context {
  closeCalls = 0;
  constructor(private readonly newPageImpl?: () => Promise<Page>) {}
  async newPage(): Promise<Page> {
    if (this.newPageImpl) return this.newPageImpl();
    return fakePage();
  }
  async newCDPSession() {
    return {
      on(event: string, observer: (event: unknown) => void) {
        if (event === "Network.responseReceived") currentObserver = observer;
      },
      async send() {},
    };
  }
  async close() {
    this.closeCalls += 1;
  }
}

async function startListener(
  createContext: () => Promise<{ context: BrowserContext }>,
) {
  const coordinator = pemPair();
  const sidecar = pemPair();
  const config = readC10BrowserListenerConfig({
    C10_COORDINATOR_PUBLIC_KEY_PEM_B64: b64(coordinator.publicKeyPem),
    C10_SIDECAR_EVIDENCE_PRIVATE_KEY_PEM_B64: b64(sidecar.privateKeyPem),
    PLAYWRIGHT_HOST_TRANSPORT_V3_KEY: TRANSPORT_KEY,
    C10_BROWSER_INTERNAL_PORT: "40000",
    C10_PROFILE_SHA256: "f".repeat(64),
  });
  const semaphore = new Semaphore(CAPACITY);
  const listener = createC10BrowserListener({
    config,
    maxConcurrentPages: CAPACITY,
    proxyServer: null,
    proxyCountry: undefined,
    pageSemaphore: semaphore,
    getBrowser: () => ({}) as never,
    async initializeBrowser() {},
    createContext,
    async assertSafeTargetUrl() {},
  });
  const server: http.Server = await new Promise((resolve) => {
    const started = listener.app.listen(0, "127.0.0.1", () => resolve(started));
  });
  const base = `http://127.0.0.1:${(server.address() as net.AddressInfo).port}`;

  const execute = () => {
    const card = {
      id: "member-1",
      sourceKey: "jll",
      stage: "member" as const,
      method: "GET" as const,
      url: "https://property.jll.com/listings/member-1",
      allowedHost: "property.jll.com",
      headers: {},
      contentType: null,
      body: null,
      browserBootstrapUrl: "https://property.jll.com/",
      cacheMode: "no-store" as const,
      timeoutMs: CARD_TIMEOUT_MS,
      maxBytes: 4_096,
      bodySha256: null,
      expectedMemberRoutes: null,
    };
    const now = Date.now();
    const capability = {
      protocolVersion: 3 as const,
      coordinatorKeyId: publicKeyId(coordinator.publicKeyPem),
      nonce: randomUUID(),
      expiresAtMs: now + 60_000,
      hostDeadlineAtMs: now + 60_000,
      cardSequence: 0,
      sourceKey: "jll",
      binding: {
        planSha256: "a".repeat(64),
        cohortSha256: "b".repeat(64),
        cardSha256: sha256(canonicalJson(card)),
        manifestSha256: "c".repeat(64),
        sessionSha256: "d".repeat(64),
        armSha256: "e".repeat(64),
        profileSha256: "f".repeat(64),
      },
    };
    return fetch(`${base}${C10_BROWSER_INTERNAL_PATH}`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-firecrawl-host-transport-key": TRANSPORT_KEY,
        "x-c10-browser-authorization": issueC10SidecarCapability(
          coordinator.privateKeyPem,
          capability,
        ),
      },
      body: JSON.stringify({ capability, card }),
    }).then(async (response) => ({
      status: response.status,
      body: (await response.json()) as Record<string, unknown>,
    }));
  };
  const activePages = async () => {
    const response = await fetch(`${base}/health`, {
      headers: { "x-firecrawl-host-transport-key": TRANSPORT_KEY },
    });
    return ((await response.json()) as { activePages: number }).activePages;
  };
  const stop = async () => {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(() => resolve(undefined)));
  };
  return { semaphore, execute, activePages, stop };
}

/** Resolves true if the request is answered within `ms`. */
const answeredWithin = (request: Promise<unknown>, ms: number) =>
  Promise.race([request.then(() => true), sleep(ms).then(() => false)]);

test("C10 control: success signs evidence and returns lease and permit", async () => {
  const context = new FakeC10Context();
  const harness = await startListener(async () => ({
    context: context as unknown as BrowserContext,
  }));
  try {
    const result = await harness.execute();
    assert.equal(result.status, 200, JSON.stringify(result.body));
    assert.equal(typeof result.body.evidenceSignature, "string");
    assert.equal(context.closeCalls, 1);
    assert.equal(harness.semaphore.getAvailablePermits(), CAPACITY);
    assert.equal(await harness.activePages(), 0);
  } finally {
    await harness.stop();
  }
});

test("bug 1 (C10): a createContext leak keeps the page permit and lease slot quarantined", async () => {
  const harness = await startListener(async () => {
    throw new BrowserResourceLeakError(new Error("addInitScript failed"));
  });
  try {
    const result = await harness.execute();
    assert.equal(result.status, 502);
    assert.deepEqual(
      {
        permitsAvailable: harness.semaphore.getAvailablePermits(),
        activePages: await harness.activePages(),
      },
      { permitsAvailable: CAPACITY - 1, activePages: 1 },
      "an unconfirmed-closed context must keep counting against capacity",
    );
  } finally {
    await harness.stop();
  }
});

test("bug 3a (C10): a hung createContext is answered by the deadline", async () => {
  const pending = deferred<{ context: BrowserContext }>();
  const harness = await startListener(() => pending.promise);
  const request = harness.execute();
  try {
    assert.equal(
      await answeredWithin(request, CARD_TIMEOUT_MS + 1_000),
      true,
      `no response ${CARD_TIMEOUT_MS + 1_000} ms after a ${CARD_TIMEOUT_MS} ms deadline`,
    );
    assert.equal((await request).status, 502);
  } finally {
    pending.resolve({ context: new FakeC10Context() as unknown as BrowserContext });
    await request;
    await harness.stop();
  }
});

test("bug 3a (C10): a context arriving after the deadline is closed and capacity returned", async () => {
  const pending = deferred<{ context: BrowserContext }>();
  const harness = await startListener(() => pending.promise);
  const request = harness.execute();
  try {
    await sleep(CARD_TIMEOUT_MS + 200);
    const lateContext = new FakeC10Context();
    pending.resolve({ context: lateContext as unknown as BrowserContext });
    const result = await request;
    await sleep(20);
    assert.equal(result.status, 502);
    assert.deepEqual(
      {
        closeCalls: lateContext.closeCalls,
        permitsAvailable: harness.semaphore.getAvailablePermits(),
        activePages: await harness.activePages(),
      },
      { closeCalls: 1, permitsAvailable: CAPACITY, activePages: 0 },
      "a closable late context must be closed and its permit and lease returned",
    );
  } finally {
    await harness.stop();
  }
});

test("bug 3b (C10): a hung newPage is answered by the deadline and capacity returned", async () => {
  const pendingPage = deferred<Page>();
  const context = new FakeC10Context(() => pendingPage.promise);
  const harness = await startListener(async () => ({
    context: context as unknown as BrowserContext,
  }));
  const request = harness.execute();
  try {
    assert.equal(
      await answeredWithin(request, CARD_TIMEOUT_MS + 1_000),
      true,
      "newPage() is not bounded by the C10 deadline",
    );
    assert.equal((await request).status, 502);
    assert.deepEqual(
      {
        closeCalls: context.closeCalls,
        permitsAvailable: harness.semaphore.getAvailablePermits(),
        activePages: await harness.activePages(),
      },
      { closeCalls: 1, permitsAvailable: CAPACITY, activePages: 0 },
    );
  } finally {
    pendingPage.resolve(fakePage());
    await request;
    await harness.stop();
  }
});

// page.goto's timeout is the remaining budget, so an ordinary navigation
// timeout used to land exactly on the deadline and skip cleanup.
test("bug 3c (C10): a navigation timeout still closes the context and returns capacity", async () => {
  const page = fakePage() as unknown as Record<string, unknown>;
  page.goto = (_url: string, options: { timeout: number }) =>
    new Promise((_resolve, reject) =>
      setTimeout(() => reject(new Error("page.goto: Timeout exceeded")), options.timeout),
    );
  const context = new FakeC10Context(async () => page as unknown as Page);
  const harness = await startListener(async () => ({
    context: context as unknown as BrowserContext,
  }));
  try {
    const result = await harness.execute();
    assert.equal(result.status, 502);
    assert.deepEqual(
      {
        closeCalls: context.closeCalls,
        permitsAvailable: harness.semaphore.getAvailablePermits(),
        activePages: await harness.activePages(),
      },
      { closeCalls: 1, permitsAvailable: CAPACITY, activePages: 0 },
      "a timed-out navigation leaves the context open and its permit and lease quarantined",
    );
  } finally {
    await harness.stop();
  }
});
