/**
 * POST /browser-batch-fetch: one bootstrap navigation, then bounded
 * same-origin fetches evaluated inside the page. Input parsing and limits live
 * in browser_batch_fetch.ts.
 */
import type { Request, Response } from "express";
import type { BrowserContext, Page } from "playwright";
import {
  BROWSER_BATCH_FETCH_MAX_RESPONSE_BYTES,
  BROWSER_BATCH_FETCH_MAX_TOTAL_DURATION_MS,
  BROWSER_BATCH_FETCH_MAX_TOTAL_RESPONSE_BYTES,
  parseBrowserBatchFetchInput,
} from "./browser_batch_fetch";
import type { BrowserPool } from "./browser_context";
import {
  HardTimeoutError,
  type Semaphore,
  cleanupBrowserResources,
  withHardTimeout,
} from "./browser_resources";
import { TargetDnsUnavailableError } from "./target_dns";
import { type AssertSafeTargetUrl, InsecureConnectionError } from "./target_guard";

export type BrowserBatchRouteDeps = Readonly<{
  browsers: Pick<BrowserPool, "getBrowser" | "initializeBrowser" | "createContext">;
  pageSemaphore: Semaphore;
  browserBatchSemaphore: Semaphore;
  assertSafeTargetUrl: AssertSafeTargetUrl;
}>;

export const createBrowserBatchFetchHandler =
  (deps: BrowserBatchRouteDeps) => async (req: Request, res: Response) => {
    const { browsers } = deps;
    const batchDeadlineAt =
      Date.now() + BROWSER_BATCH_FETCH_MAX_TOTAL_DURATION_MS;
    let input;
    let batchPermitAcquired = false;
    let pagePermitAcquired = false;
    try {
      input = parseBrowserBatchFetchInput(req.body);
      // The parser already requires every request to share the bootstrap origin,
      // so resolve that one host once. This endpoint always rejects local/private
      // destinations, even when legacy /scrape local-webhook support is enabled.
      await withHardTimeout(
        deps.assertSafeTargetUrl(input.bootstrapUrl, false),
        Math.max(1, batchDeadlineAt - Date.now()),
        "Browser batch target validation exceeded its hard deadline",
      );
    } catch (error) {
      if (error instanceof TargetDnsUnavailableError) {
        // Still refused, but retryable rather than a malformed request.
        return res
          .status(503)
          .json({ error: error.message, code: error.code });
      }
      return res.status(400).json({
        error:
          error instanceof Error
            ? error.message
            : "Invalid browser batch request",
      });
    }

    try {
      if (!browsers.getBrowser()) {
        await withHardTimeout(
          browsers.initializeBrowser(),
          Math.max(1, batchDeadlineAt - Date.now()),
          "Browser batch initialization exceeded its hard deadline",
        );
      }
      await deps.browserBatchSemaphore.acquire(
        Math.max(1, batchDeadlineAt - Date.now()),
      );
      batchPermitAcquired = true;
      await deps.pageSemaphore.acquire(Math.max(1, batchDeadlineAt - Date.now()));
      pagePermitAcquired = true;
    } catch (error) {
      if (batchPermitAcquired && !pagePermitAcquired) {
        deps.browserBatchSemaphore.release();
      }
      console.error("Browser batch admission error:", error);
      return res
        .status(503)
        .json({ error: "Browser batch service is busy or unavailable" });
    }

    let requestContext: BrowserContext | null = null;
    let page: Page | null = null;
    let lateContextOwnsPermit = false;
    let permitsReleased = false;
    const releaseBatchPermits = (): void => {
      if (permitsReleased) return;
      permitsReleased = true;
      deps.pageSemaphore.release();
      deps.browserBatchSemaphore.release();
    };
    try {
      let contextBundle;
      try {
        contextBundle = await withHardTimeout(
          browsers.createContext({ allowLocalTargets: false }),
          Math.max(1, batchDeadlineAt - Date.now()),
          "Browser batch context creation exceeded its hard deadline",
          async (lateBundle) => {
            await cleanupBrowserResources(
              null,
              () => lateBundle.context.close(),
              releaseBatchPermits,
            );
          },
        );
      } catch (error) {
        if (error instanceof HardTimeoutError) {
          // The context may still arrive. Its late cleanup owns the permits; if
          // it never arrives or cannot close, capacity remains quarantined until
          // the service is restarted.
          lateContextOwnsPermit = true;
        }
        throw error;
      }
      requestContext = contextBundle.context;
      page = await withHardTimeout(
        requestContext.newPage(),
        Math.max(1, batchDeadlineAt - Date.now()),
        "Browser batch page creation exceeded its hard deadline",
      );
      const bootstrapTimeoutMs = Math.min(
        input.timeoutMs,
        Math.max(1, batchDeadlineAt - Date.now()),
      );
      const bootstrapResponse = await withHardTimeout(
        page.goto(input.bootstrapUrl, {
          waitUntil: "load",
          timeout: bootstrapTimeoutMs,
        }),
        Math.max(
          1,
          Math.min(batchDeadlineAt - Date.now(), bootstrapTimeoutMs + 5_000),
        ),
        "Browser batch bootstrap navigation exceeded its hard deadline",
      );
      const bootstrapStatus = bootstrapResponse?.status() ?? 0;
      const requestedOrigin = new URL(input.bootstrapUrl).origin;
      const finalOrigin = new URL(page.url()).origin;
      if (finalOrigin !== requestedOrigin) {
        return res.status(502).json({
          error: "Bootstrap redirected outside its requested origin",
          bootstrapStatus,
        });
      }
      if (input.waitAfterLoadMs > 0) {
        await withHardTimeout(
          page.waitForTimeout(input.waitAfterLoadMs),
          Math.max(
            1,
            Math.min(batchDeadlineAt - Date.now(), input.waitAfterLoadMs + 5_000),
          ),
          "Browser batch post-load wait exceeded its hard deadline",
        );
      }
      if (bootstrapStatus < 200 || bootstrapStatus >= 400) {
        return res.status(502).json({
          error: `Bootstrap returned HTTP ${bootstrapStatus}`,
          bootstrapStatus,
        });
      }

      const responses: Array<{
        status: number;
        contentType: string | null;
        body: string;
      }> = [];
      let totalResponseBytes = 0;
      for (const request of input.requests) {
        const remainingBatchMs = batchDeadlineAt - Date.now();
        if (remainingBatchMs <= 0) {
          throw new Error("Browser batch fetch exceeded its aggregate deadline");
        }
        const evaluateOperation = page.evaluate(
          ({ request, timeoutMs, maxResponseBytes }) => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeoutMs);
            return fetch(request.url, {
              method: request.method,
              headers: request.headers,
              body: request.body,
              credentials: "same-origin",
              cache: "no-store",
              redirect: "manual",
              signal: controller.signal,
            })
              .then((fetched) => {
                const declaredLength = Number(
                  fetched.headers.get("content-length"),
                );
                if (
                  Number.isFinite(declaredLength) &&
                  declaredLength > maxResponseBytes
                ) {
                  throw new Error(
                    "Browser batch response exceeds the per-response byte limit",
                  );
                }
                if (!fetched.body) {
                  return {
                    status: fetched.status,
                    contentType: fetched.headers.get("content-type"),
                    body: "",
                  };
                }
                const reader = fetched.body.getReader();
                const decoder = new TextDecoder();
                let bytes = 0;
                let body = "";
                const readNext = (): Promise<{
                  status: number;
                  contentType: string | null;
                  body: string;
                }> =>
                  reader.read().then((chunk) => {
                    if (chunk.done) {
                      body += decoder.decode();
                      return {
                        status: fetched.status,
                        contentType: fetched.headers.get("content-type"),
                        body,
                      };
                    }
                    bytes += chunk.value.byteLength;
                    if (bytes > maxResponseBytes) {
                      return reader.cancel().then(() => {
                        throw new Error(
                          "Browser batch response exceeds the per-response byte limit",
                        );
                      });
                    }
                    body += decoder.decode(chunk.value, { stream: true });
                    return readNext();
                  });
                return readNext();
              })
              .finally(() => {
                clearTimeout(timer);
              });
          },
          {
            request,
            timeoutMs: Math.min(input.timeoutMs, remainingBatchMs),
            maxResponseBytes: BROWSER_BATCH_FETCH_MAX_RESPONSE_BYTES,
          },
        );
        const response = await withHardTimeout(
          evaluateOperation,
          Math.max(1, Math.min(remainingBatchMs, input.timeoutMs + 5_000)),
          "Browser batch page evaluation exceeded its hard deadline",
        );
        totalResponseBytes += Buffer.byteLength(response.body, "utf8");
        if (totalResponseBytes > BROWSER_BATCH_FETCH_MAX_TOTAL_RESPONSE_BYTES) {
          throw new Error(
            "Browser batch responses exceed the aggregate byte limit",
          );
        }
        responses.push(response);
      }
      return res.json({ bootstrapStatus, responses });
    } catch (error) {
      if (error instanceof InsecureConnectionError) {
        return res.status(403).json({ error: error.message });
      }
      console.error("Browser batch fetch error:", error);
      return res.status(502).json({ error: "Browser batch fetch failed" });
    } finally {
      if (!lateContextOwnsPermit) {
        await cleanupBrowserResources(
          page ? () => page!.close() : null,
          requestContext ? () => requestContext!.close() : null,
          releaseBatchPermits,
        );
      }
    }
  };
