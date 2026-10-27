import assert from "node:assert/strict";
import { once } from "node:events";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import test from "node:test";
import { createApp, readServiceConfig } from "./api";
import type { BrowserPool, ContextBundle } from "./browser_context";
import { Semaphore, withHardTimeout } from "./browser_resources";

// Removing deliverResult.url or using page.url() for a raw body must fail.
for (const fixture of [
  { contentType: "application/json", content: '{"property":"café"}\n' },
  { contentType: "text/plain", content: "property café\n" },
]) {
  test(`/scrape serializes response.url() with raw ${fixture.contentType} content`, async () => {
    const calls: string[] = [];
    let contextIsClosed = false;
    let contextClosed!: () => void;
    const closed = new Promise<void>((resolve) => {
      contextClosed = resolve;
    });
    const page = {
      goto: async () => ({
        status: () => 200,
        url: () => "https://property.example/raw-response",
        allHeaders: async () => ({ "content-type": fixture.contentType }),
        body: async () => Buffer.from(fixture.content, "utf8"),
      }),
      url: () => "https://property.example/rendered-page",
      content: async () => "<html>rendered content</html>",
      close: async () => {
        calls.push("close-page");
      },
    };
    const context = {
      newPage: async () => page,
      close: async () => {
        if (contextIsClosed) return;
        contextIsClosed = true;
        calls.push("close-context");
        contextClosed();
      },
    };
    const browsers: BrowserPool = {
      getBrowser: () => undefined,
      initializeBrowser: async () => {},
      createContext: async () =>
        ({
          context,
          securityState: {
            blockedNavigationRequestUrl: null,
            navigationDnsUnavailable: false,
          },
        }) as unknown as ContextBundle,
      close: async () => {},
    };
    const server = createServer(
      createApp({
        config: readServiceConfig({ MAX_CONCURRENT_PAGES: "1" }),
        browsers,
        pageSemaphore: new Semaphore(1),
        assertSafeTargetUrl: async () => {},
      }),
    );
    try {
      server.listen(0, "127.0.0.1");
      await once(server, "listening");
      const { port } = server.address() as AddressInfo;
      const response = await fetch(`http://127.0.0.1:${port}/scrape`, {
        method: "POST",
        signal: AbortSignal.timeout(5_000),
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ url: "https://property.example/request" }),
      });
      assert.equal(response.status, 200);
      assert.deepEqual(await response.json(), {
        content: fixture.content,
        pageStatusCode: 200,
        contentType: fixture.contentType,
        url: "https://property.example/raw-response",
      });
      await withHardTimeout(closed, 5_000, "Fixture context did not close");
      assert.deepEqual(calls, ["close-page", "close-context"]);
    } finally {
      server.closeAllConnections();
      await context.close();
      await new Promise<void>((resolve, reject) => {
        server.close((error) => (error ? reject(error) : resolve()));
      });
    }
  });
}
