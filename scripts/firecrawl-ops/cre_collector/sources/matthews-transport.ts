import { readBoundedUtf8, ResponseBodyDeadlineError, ResponseBodyLimitError } from "../lib/response-body.js";
import { Agent, fetch as undiciFetch } from "undici";
import { requireFreshDetails } from "../lib/freshness.js";

export const MATTHEWS_FETCH_TIMEOUT_MS = 30_000;
export const MATTHEWS_MAX_RESPONSE_BYTES = 32 * 1024 * 1024;
const NATIVE_FETCH = globalThis.fetch;

let matthewsNextSlot = 0;
let matthewsInterval = 1800;

const matthewsSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

async function matthewsGate(): Promise<void> {
  const now = Date.now();
  const slot = Math.max(now, matthewsNextSlot);
  matthewsNextSlot = slot + matthewsInterval;
  const wait = slot - now;
  if (wait > 0) await matthewsSleep(wait);
}

export function matthewsFetchOptions(
  strict = requireFreshDetails(),
  timeoutMs = MATTHEWS_FETCH_TIMEOUT_MS,
  redirect: RequestRedirect = "follow",
  signal: AbortSignal = AbortSignal.timeout(timeoutMs)
): RequestInit {
  const headers: Record<string, string> = {
    "User-Agent":
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
  };
  if (strict) headers["Cache-Control"] = "no-cache";
  return {
    headers,
    signal,
    redirect,
    ...(strict ? { cache: "no-store" as const } : {}),
  };
}

/**
 * AbortSignal is necessary to close a request, but an upstream HTTP stack can
 * occasionally fail to settle the fetch promise after it observes that abort.
 * Race the header phase against an explicit rejecting deadline so one stalled
 * provider connection can never hold the full refresh indefinitely.
 */
export async function matthewsFetchResponse(
  url: string,
  options: RequestInit,
  controller: AbortController,
  timeoutMs = MATTHEWS_FETCH_TIMEOUT_MS,
  request: (url: string, options: RequestInit) => Promise<Response> = fetch
): Promise<Response> {
  let timeout: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_, reject) => {
    timeout = setTimeout(() => {
      controller.abort();
      reject(new Error(`Matthews response headers timed out after ${timeoutMs}ms`));
    }, timeoutMs);
  });
  try {
    return await Promise.race([request(url, options), deadline]);
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

/** A deterministic refusal, never a transient provider/transport retry. */
export class MatthewsResponseLimitError extends Error {
  constructor(maxBytes: number) {
    super(`Matthews response body exceeds ${maxBytes} byte limit`);
    this.name = "MatthewsResponseLimitError";
  }
}

/**
 * Count actual HTTP-decoded stream bytes before copying them. Content-Length
 * cannot establish the decoded body size. This is an input-byte bound, not a
 * process heap ceiling: fetch owns each incoming chunk, growth briefly retains
 * two buffers, and the final decoded string has its own representation cost.
 * One deadline race avoids retaining a pending deadline reaction per chunk.
 */
export async function matthewsResponseText(
  response: Pick<Response, "body">,
  controller: AbortController,
  timeoutMs = MATTHEWS_FETCH_TIMEOUT_MS,
  maxBytes = MATTHEWS_MAX_RESPONSE_BYTES
): Promise<string> {
  if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0 || maxBytes > MATTHEWS_MAX_RESPONSE_BYTES) {
    throw new RangeError("Matthews response body limit must be a positive safe integer at most 32 MiB");
  }
  if (response.body === null) return "";
  const reader = response.body.getReader();
  try {
    return await readBoundedUtf8(reader, {
      maxBytes,
      signal: controller.signal,
      deadlineAt: performance.now() + timeoutMs,
    });
  } catch (error) {
    const cause = error instanceof ResponseBodyLimitError
      ? new MatthewsResponseLimitError(maxBytes)
      : error instanceof ResponseBodyDeadlineError
        ? new Error(`Matthews response body timed out after ${timeoutMs}ms`) : error;
    controller.abort(cause);
    // Matthews retains best-effort cleanup; sibling retry acknowledgement
    // policy does not apply to this separately owned transport.
    try { void Promise.resolve(reader.cancel(cause)).catch(() => {}); } catch { /* best effort */ }
    throw cause;
  } finally {
    try { reader.releaseLock(); } catch { /* preserve the request outcome */ }
  }
}

type MatthewsFetchResult = {
  html: string | null;
  status: number;
  location: string | null;
};

/**
 * A response body can time out after a successful HTTP status. Treat that as
 * a transport failure, not as a deterministic HTTP 200 validation failure.
 */
export function matthewsRetryableFetchFailure(status: number, error: unknown): boolean {
  if (error instanceof MatthewsResponseLimitError) return false;
  return error != null || status === 0 || status === 429 || status === 403 || status === 503;
}

function matthewsRequestRole(url: string): "inventory" | "sitemap" | "detail" | "other" {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || !["matthews.com", "www.matthews.com"].includes(parsed.hostname)) return "other";
    if (parsed.pathname === "/sitemap.xml") return "sitemap";
    if (parsed.pathname === "/listings") return "inventory";
    if (/^\/properties\/[^/]+\/?$/.test(parsed.pathname)) return "detail";
  } catch { /* A diagnostic role never changes transport behavior. */ }
  return "other";
}

function logMatthewsAttempt(event: {
  role: ReturnType<typeof matthewsRequestRole>;
  attempt: number;
  maxAttempts: number;
  phase: "headers" | "body" | "http";
  status: number | null;
  elapsedMs: number;
  cooldownMs: number;
  nextAction: "retry_after_cooldown" | "final_cooldown_before_failure" | "terminal_failure";
}): void {
  // Never include URL, response content, headers or exception text. Diagnostics
  // must not turn an otherwise bounded request/retry into a logging failure.
  try { console.warn(`  matthews/http ${JSON.stringify(event)}`); } catch { /* best effort */ }
}

export async function matthewsFetch(
  url: string,
  manualRedirect = false,
  strict = requireFreshDetails(),
  observeTemporaryRedirect = false
): Promise<MatthewsFetchResult> {
  let lastError: unknown = null;
  const role = matthewsRequestRole(url);
  for (let attempt = 0; attempt < 6; attempt++) {
    await matthewsGate();
    const started = performance.now();
    let phase: "headers" | "body" | "http" = "headers";
    let status = 0;
    let attemptError: unknown = null;
    const controller = new AbortController();
    // Own each transport so a provider socket that ignores abort cannot remain
    // alive after a timed-out attempt and keep the collector process running.
    const agent = globalThis.fetch === NATIVE_FETCH
      ? new Agent({
          connectTimeout: MATTHEWS_FETCH_TIMEOUT_MS,
          headersTimeout: MATTHEWS_FETCH_TIMEOUT_MS,
          bodyTimeout: MATTHEWS_FETCH_TIMEOUT_MS,
          keepAliveTimeout: 1,
        })
      : null;
    try {
      const res = await matthewsFetchResponse(
        url,
        matthewsFetchOptions(
          strict,
          MATTHEWS_FETCH_TIMEOUT_MS,
          manualRedirect ? "manual" : "follow",
          controller.signal
        ),
        controller,
        MATTHEWS_FETCH_TIMEOUT_MS,
        (requestUrl, requestOptions) => {
          if (!agent) return globalThis.fetch(requestUrl, requestOptions);
          return undiciFetch(
            requestUrl,
            { ...requestOptions, dispatcher: agent } as unknown as Parameters<typeof undiciFetch>[1]
          ) as unknown as Promise<Response>;
        }
      );
      status = res.status;
      phase = "http";
      if (res.ok) {
        phase = "body";
        return {
          html: await matthewsResponseText(res, controller),
          status,
          location: null,
        };
      }
      if (manualRedirect && (status === 301 || status === 308 || (observeTemporaryRedirect && status === 307))) {
        return { html: null, status, location: res.headers.get("location") };
      }
    } catch (error) {
      // This includes the separately bounded response-body timeout. `status`
      // may already be 200 at this point, so retaining the error is essential
      // to avoid converting a transient body stall into a hard HTTP failure.
      attemptError = error;
    } finally {
      agent?.destroy();
    }
    if (matthewsRetryableFetchFailure(status, attemptError)) {
      lastError = attemptError;
      matthewsInterval = Math.min(matthewsInterval + 700, 7000);
      const cooldownMs = 20000 + attempt * 15000 + Math.random() * 5000;
      logMatthewsAttempt({
        role, attempt: attempt + 1, maxAttempts: 6, phase,
        status: status > 0 ? status : null,
        elapsedMs: Math.max(0, Math.round(performance.now() - started)),
        cooldownMs,
        nextAction: attempt < 5 ? "retry_after_cooldown" : "final_cooldown_before_failure",
      });
      await matthewsSleep(cooldownMs);
      continue;
    }
    logMatthewsAttempt({
      role, attempt: attempt + 1, maxAttempts: 6, phase,
      status: status > 0 ? status : null,
      elapsedMs: Math.max(0, Math.round(performance.now() - started)),
      cooldownMs: 0, nextAction: "terminal_failure",
    });
    if (attemptError != null) throw attemptError;
    throw new Error(`Matthews HTTP ${status}`);
  }
  if (lastError instanceof Error) throw lastError;
  throw new Error("Matthews: throttled after retries");
}
