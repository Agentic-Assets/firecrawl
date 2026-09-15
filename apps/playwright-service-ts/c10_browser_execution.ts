import type { Page } from "playwright";

import type { C10SidecarCard } from "./c10_browser_internal";

export type C10BrowserPageResponse = {
  readonly status: number;
  readonly finalUrl: string;
  readonly redirected: boolean;
  readonly contentType: string | null;
  readonly bodyBase64: string;
};

/**
 * Perform the sole C10 browser action on an already admitted Playwright page.
 * Admission, page leasing, SSRF checks, and HMAC authorization remain in the
 * sidecar route; this function intentionally has no alternate engine path.
 */
export async function executeC10BrowserPageFetch(
  page: Page,
  card: Readonly<C10SidecarCard>,
  deadlineAt = Date.now() + card.timeoutMs,
): Promise<C10BrowserPageResponse> {
  const remaining = () => {
    const value = deadlineAt - Date.now();
    if (value < 1) throw new Error("C10 browser deadline expired");
    return value;
  };
  await page.setExtraHTTPHeaders({
    "cache-control": "no-store, no-cache, max-age=0",
    pragma: "no-cache",
  });
  const bootstrap = await page.goto(card.browserBootstrapUrl, {
    waitUntil: "load",
    timeout: remaining(),
  });
  if (!bootstrap || bootstrap.status() < 200 || bootstrap.status() >= 300) {
    throw new Error("C10 browser bootstrap did not return a successful response");
  }
  if (new URL(page.url()).origin !== new URL(card.browserBootstrapUrl).origin) {
    throw new Error("C10 browser bootstrap left its reviewed origin");
  }
  const response = await page.evaluate(async (instruction) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), instruction.timeoutMs);
    try {
      const browserResponse = await fetch(instruction.url, {
        method: instruction.method,
        headers: instruction.headers,
        body: instruction.body,
        credentials: "same-origin",
        cache: "no-store",
        redirect: "manual",
        signal: controller.signal,
      });
      const reader = browserResponse.body?.getReader();
      if (!reader) throw new Error("C10 browser response has no readable body");
      const chunks: Uint8Array[] = [];
      let total = 0;
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        total += chunk.value.byteLength;
        if (total > instruction.maxBytes) {
          await reader.cancel();
          throw new Error("C10 browser response exceeds reviewed byte limit");
        }
        chunks.push(chunk.value);
      }
      const bytes = new Uint8Array(total);
      let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
      let binary = "";
      for (let offset = 0; offset < bytes.length; offset += 0x8000) {
        binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
      }
      return {
        status: browserResponse.status,
        finalUrl: browserResponse.url,
        redirected: browserResponse.redirected,
        contentType: browserResponse.headers.get("content-type"),
        bodyBase64: btoa(binary),
      };
    } finally {
      clearTimeout(timer);
    }
  }, {
    url: card.url,
    method: card.method,
    headers: card.headers,
    body: card.body,
    timeoutMs: remaining(),
    maxBytes: card.maxBytes,
  });
  const finalUrl = new URL(response.finalUrl);
  if (finalUrl.protocol !== "https:" || finalUrl.host !== card.allowedHost) {
    throw new Error("C10 browser response left its reviewed origin");
  }
  return response;
}
