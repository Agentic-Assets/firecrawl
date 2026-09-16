/**
 * Test-only C10 listener process used by the Python/Node loopback boundary
 * test. It has no browser or provider traffic: the injected page returns an
 * in-memory reviewed response after emitting the CDP network observation the
 * listener requires before it can sign success evidence.
 */
import type { BrowserContext, Page } from "playwright";

import {
  createC10BrowserListener,
  readC10BrowserListenerConfig,
} from "./c10_browser_listener";

const config = readC10BrowserListenerConfig(process.env);
if (!config.enabled || !config.port) {
  throw new Error("C10 loopback fixture requires complete listener configuration");
}
const port = Number(config.port);

type NetworkObserver = (event: { response: { fromDiskCache: boolean } }) => void;

let networkObserver: NetworkObserver | undefined;
const page = {
  async setExtraHTTPHeaders(): Promise<void> {},
  async goto(): Promise<{ status(): number }> {
    return { status: () => 200 };
  },
  url(): string {
    return "https://property.jll.com/";
  },
  async evaluate(
    _operation: unknown,
    instruction: { url: string; method: "GET" | "POST" },
  ): Promise<{
    status: number;
    finalUrl: string;
    redirected: boolean;
    contentType: string;
    bodyBase64: string;
  }> {
    networkObserver?.({ response: { fromDiskCache: false } });
    return {
      status: 200,
      finalUrl: instruction.url,
      redirected: false,
      contentType:
        instruction.method === "POST" ? "application/json" : "text/html",
      bodyBase64: Buffer.from(
        instruction.method === "POST"
          ? JSON.stringify({
              data: {
                properties: {
                  items: Array.from({ length: 16 }, (_, index) => ({
                    pageUrl: `https://property.jll.com/listings/member-${index + 1}`,
                  })),
                },
              },
            })
          : "<html>loopback</html>",
      ).toString("base64"),
    };
  },
  async close(): Promise<void> {},
} as unknown as Page;

const context = {
  async newPage(): Promise<Page> {
    return page;
  },
  async newCDPSession(): Promise<{
    on(event: string, observer: NetworkObserver): void;
    send(method: string): Promise<void>;
  }> {
    return {
      on(event, observer): void {
        if (event === "Network.responseReceived") networkObserver = observer;
      },
      async send(): Promise<void> {},
    };
  },
  async close(): Promise<void> {},
} as unknown as BrowserContext;

const listener = createC10BrowserListener({
  config,
  maxConcurrentPages: 4,
  proxyServer: null,
  proxyCountry: undefined,
  pageSemaphore: { async acquire(): Promise<void> {}, release(): void {} },
  getBrowser: () => ({}) as never,
  async initializeBrowser(): Promise<void> {},
  async createContext() {
    return { context };
  },
  async assertSafeTargetUrl(url: string): Promise<void> {
    if (new URL(url).hostname !== "property.jll.com") {
      throw new Error("loopback fixture only admits reviewed JLL cards");
    }
  },
});

const server = listener.app.listen(port, "127.0.0.1", () => {
  process.stdout.write("c10-loopback-ready\n");
});

function stop(): void {
  server.close(() => process.exit(0));
}

process.once("SIGTERM", stop);
process.once("SIGINT", stop);
