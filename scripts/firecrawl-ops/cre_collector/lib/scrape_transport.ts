import { setTimeout as sleep } from "node:timers/promises";
import { SdkError } from "@mendable/firecrawl-js";

/**
 * The pinned SDK exposes neither an AbortSignal nor a transport override on
 * scrape(). Keep the v2 scrape wire contract here, with transport cancellation
 * passed separately from the JSON options. Aborting HTTP is not a receipt that
 * the API/browser has settled.
 */
export class CancellableScrapeClient {
  constructor(
    private readonly configuration: {
      apiUrl: string;
      apiKey: string;
      maxAttempts: number;
    },
  ) {}

  async scrape(
    url: string,
    options: Record<string, unknown> = {},
    transport: { signal?: AbortSignal; singleAttempt?: boolean } = {},
  ): Promise<Record<string, unknown>> {
    if (!url.trim()) throw new Error("URL cannot be empty");
    const endpoint = `${this.configuration.apiUrl.replace(/\/$/, "")}/v2/scrape`;
    const maxAttempts = transport.singleAttempt ? 1 : this.configuration.maxAttempts;
    for (let attempt = 0; attempt < maxAttempts; attempt++) {
      transport.signal?.throwIfAborted();
      const response = await fetch(endpoint, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.configuration.apiKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          url: url.trim(),
          ...options,
          origin: "cre-collector",
        }),
        signal: transport.signal,
      });
      // Fully settle this response before another HTTP attempt. The SDK retries
      // only 502, with 0.5s exponential backoff; retain that bounded behavior.
      const body = await response.text();
      if (
        response.status === 502 &&
        attempt + 1 < maxAttempts
      ) {
        await sleep(500 * 2 ** attempt, undefined, {
          signal: transport.signal,
        });
        continue;
      }
      let result: {
        success?: boolean;
        data?: Record<string, unknown>;
        code?: string;
        error?: string;
        message?: string;
        details?: unknown;
      };
      try {
        result = JSON.parse(body);
      } catch {
        throw Object.assign(
          new Error("Firecrawl returned an invalid scrape response"),
          {
            status: response.status,
          },
        );
      }
      if (response.status !== 200 || result?.success !== true) {
        throw new SdkError(
          result?.error ||
            result?.message ||
            `Request failed (${response.status}) while trying to scrape`,
          response.status,
          result?.code,
          result?.details,
        );
      }
      return result.data ?? {};
    }
    throw new Error("Firecrawl scrape attempts exhausted");
  }
}
