import { DAUM_SEARCH_URL, daumAbsoluteUrl, daumPageUrl } from "./pure/daum-identity.js";
import {
  cancelResponseBody,
  readBoundedUtf8,
  ResponseBodyDeadlineError,
  ResponseBodyLimitError,
  settleResponseBodyRead,
} from "../lib/response-body.js";

export const DAUM_ROBOTS_DELAY_MS = 3_000;
export const DAUM_MAX_RESPONSE_BYTES = 5_000_000;
const DAUM_BODY_CLEANUP_TIMEOUT_MS = 1_000;
const DAUM_USER_AGENT = "Mozilla/5.0 (compatible; AgenticAssetsCRE/1.0)";
const DAUM_RETRYABLE_HTTP_STATUSES = new Set([429, 500, 502, 503, 504]);
const DAUM_RETRYABLE_NATIVE_CODES = new Set([
  "ETIMEDOUT",
  "ECONNRESET",
  "UND_ERR_SOCKET",
  "UND_ERR_CONNECT_TIMEOUT",
  "UND_ERR_HEADERS_TIMEOUT",
  "UND_ERR_BODY_TIMEOUT",
]);

type FetchLike = (
  input: string | URL | Request,
  init?: RequestInit
) => Promise<Response>;

type DaumRequestMethod = "GET" | "POST";

class DaumRetryableTransportError extends Error {}

class DaumUnsettledBodyError extends Error {}

function daumErrorCode(error: unknown): string | null {
  if (!error || typeof error !== "object") return null;
  const value = error as { code?: unknown; cause?: { code?: unknown } };
  if (typeof value.cause?.code === "string") return value.cause.code;
  return typeof value.code === "string" ? value.code : null;
}

function daumKnownTransientError(error: unknown, timedOut: boolean): boolean {
  return timedOut || DAUM_RETRYABLE_NATIVE_CODES.has(daumErrorCode(error) ?? "");
}

function daumCanRetry(method: DaumRequestMethod, url: string): boolean {
  if (method === "GET") return true;
  if (method !== "POST") return false;
  if (url === DAUM_SEARCH_URL) return true;
  const match = url.match(/^https:\/\/daumcommercial\.com\/property-search\/page\/([1-9]\d*)\/$/);
  if (!match) return false;
  const page = Number(match[1]);
  return Number.isSafeInteger(page) && daumPageUrl(page) === url;
}

async function daumCancelBody(body: { cancel(): Promise<unknown> } | null): Promise<boolean> {
  return cancelResponseBody(body, DAUM_BODY_CLEANUP_TIMEOUT_MS);
}

async function daumBoundedResponseText(
  response: Response,
  maxBytes: number,
  controller: AbortController,
  deadlineAt: number,
): Promise<string> {
  const contentLength = Number(response.headers.get("content-length"));
  if (Number.isFinite(contentLength) && contentLength > maxBytes) {
    if (!await daumCancelBody(response.body)) {
      throw new DaumUnsettledBodyError("DAUM oversized response body did not settle");
    }
    throw new Error(`response Content-Length ${contentLength} exceeds ${maxBytes} bytes`);
  }
  if (!response.body) return "";
  const reader = response.body.getReader();
  try {
    return await readBoundedUtf8(reader, { maxBytes, signal: controller.signal, deadlineAt });
  } catch (error) {
    if (error instanceof ResponseBodyLimitError) {
      if (!await daumCancelBody(reader)) {
        throw new DaumUnsettledBodyError("DAUM response body did not settle after exceeding its size limit", { cause: error });
      }
      throw new Error(`response body exceeds ${maxBytes} bytes`, { cause: error });
    }
    const timedOut = error instanceof ResponseBodyDeadlineError || controller.signal.aborted;
    // An abort-induced closure must not impersonate the reader's deadline error.
    if (error instanceof ResponseBodyDeadlineError) {
      controller.abort(new Error("DAUM response body deadline exceeded", { cause: error }));
    }
    if (!await settleResponseBodyRead(reader, error, DAUM_BODY_CLEANUP_TIMEOUT_MS)) {
      throw new DaumUnsettledBodyError("DAUM response body read failed and did not settle", { cause: error });
    }
    if (!daumKnownTransientError(error, timedOut)) throw error;
    throw new DaumRetryableTransportError("DAUM response body read failed", { cause: error });
  } finally {
    try { reader.releaseLock(); } catch { /* preserve the primary outcome */ }
  }
}

export class DaumPoliteTransport {
  private lastStartedAt = 0;
  private queue: Promise<unknown> = Promise.resolve();
  private poisoned = false;

  constructor(
    private readonly fetchImpl: FetchLike = fetch,
    private readonly delayMs = DAUM_ROBOTS_DELAY_MS,
    private readonly timeoutMs = 30_000,
    private readonly maxResponseBytes = DAUM_MAX_RESPONSE_BYTES,
  ) {}

  private request(url: string, method: DaumRequestMethod): Promise<string> {
    const safeUrl = daumAbsoluteUrl(url);
    if (!safeUrl) {
      return Promise.reject(new Error(`DAUM refused unsafe URL ${url}`));
    }
    if (new URL(safeUrl).search) {
      return Promise.reject(new Error(`DAUM robots policy forbids query URL ${url}`));
    }
    const retryAllowed = daumCanRetry(method, safeUrl);
    const task = this.queue.then(async () => {
      let lastError: unknown;
      for (let attempt = 1; attempt <= 2; attempt++) {
        if (this.poisoned) {
          throw new Error("DAUM transport refuses requests after uncertain response-body cleanup");
        }
        const waitMs = Math.max(0, this.delayMs - (Date.now() - this.lastStartedAt));
        if (waitMs) await new Promise((resolve) => setTimeout(resolve, waitMs));
        if (this.poisoned) {
          throw new Error("DAUM transport refuses requests after uncertain response-body cleanup");
        }

        const controller = new AbortController();
        const deadlineAt = performance.now() + this.timeoutMs;
        const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
        this.lastStartedAt = Date.now();
        try {
          let response: Response;
          try {
            response = await this.fetchImpl(safeUrl, {
              method,
              headers: {
                "User-Agent": DAUM_USER_AGENT,
                Accept: "text/html",
                ...(method === "POST"
                  ? { "Content-Type": "application/x-www-form-urlencoded" }
                  : {}),
              },
              ...(method === "POST" ? { body: "" } : {}),
              redirect: "manual",
              signal: controller.signal,
            });
          } catch (error) {
            if (daumKnownTransientError(error, controller.signal.aborted)) {
              throw new DaumRetryableTransportError("DAUM fetch failed", { cause: error });
            }
            throw error;
          }
          if (!response.ok) {
            if (!await daumCancelBody(response.body)) {
              this.poisoned = true;
              throw new DaumUnsettledBodyError(`DAUM HTTP ${response.status} body did not settle`);
            }
            if (
              retryAllowed
              && DAUM_RETRYABLE_HTTP_STATUSES.has(response.status)
              && !response.headers.has("retry-after")
            ) {
              throw new DaumRetryableTransportError(`DAUM HTTP ${response.status}`);
            }
            throw new Error(`HTTP ${response.status}`);
          }
          return await daumBoundedResponseText(response, this.maxResponseBytes, controller, deadlineAt);
        } catch (error) {
          lastError = error;
          if (error instanceof DaumUnsettledBodyError) this.poisoned = true;
          const retryable = retryAllowed && error instanceof DaumRetryableTransportError;
          if (!retryable || attempt === 2 || this.poisoned) break;
        } finally {
          clearTimeout(timeout);
        }
      }
        throw new Error(`DAUM request failed for ${safeUrl}: ${String(lastError)}`, { cause: lastError });
    });
    this.queue = task.catch(() => undefined);
    return task as Promise<string>;
  }

  get(url: string): Promise<string> {
    return this.request(url, "GET");
  }

  post(url: string): Promise<string> {
    return this.request(url, "POST");
  }
}
