import { request as httpsRequest } from "node:https";
import type { ClientRequest, IncomingMessage } from "node:http";
import { isIP } from "node:net";
import { cancelResponseBody, settleResponseBodyRead, type ResponseBodyReader } from "../lib/response-body.js";

// New local cleanup bound, separate from the acquisition budget. Failure to
// acknowledge closure stops further Avison dispatch in this collector process.
const CLEANUP_MS = 1_000;
export class AvisonYoungUnsettledError extends Error {
  constructor(cause: unknown) {
    super(`Avison Young transport cleanup was not acknowledged: ${String(cause)}`, { cause });
    this.name = "AvisonYoungUnsettledError";
  }
}
let unsettled: AvisonYoungUnsettledError | null = null;

export function assertAvisonYoungTransportSettled(): void {
  if (unsettled) throw unsettled;
}

function refuseUnsettled(cause: unknown): AvisonYoungUnsettledError {
  unsettled ??= new AvisonYoungUnsettledError(cause);
  return unsettled;
}

export function avisonYoungDeadlineError(): Error {
  return new Error("Avison Young request elapsed deadline exceeded");
}

export async function withinAvisonYoungDeadline<T>(pending: Promise<T>, deadlineAt: number): Promise<T> {
  const remaining = deadlineAt - performance.now();
  if (remaining <= 0) {
    void pending.catch(() => {});
    throw avisonYoungDeadlineError();
  }
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const result = await Promise.race([
      pending,
      new Promise<never>((_, reject) => { timer = setTimeout(() => reject(avisonYoungDeadlineError()), Math.ceil(remaining)); }),
    ]);
    if (performance.now() >= deadlineAt) throw avisonYoungDeadlineError();
    return result;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/** Fetch plus complete decoded body under a new explicit per-request budget. */
export async function fetchAvisonYoungText(
  url: string | URL,
  init: RequestInit,
  timeoutMs: number,
  httpError: (status: number) => Error
): Promise<string> {
  assertAvisonYoungTransportSettled();
  const controller = new AbortController();
  const deadlineAt = performance.now() + timeoutMs;
  let response: Response | undefined;
  let reader: ResponseBodyReader | undefined;
  let readFailure: { error: unknown } | undefined;
  let headerSettled = false;
  const headers = Promise.resolve().then(() => {
    assertAvisonYoungTransportSettled();
    if (performance.now() >= deadlineAt) throw avisonYoungDeadlineError();
    return fetch(url, { ...init, signal: controller.signal });
  })
    .then((value) => { response = value; headerSettled = true; return value; },
      (error: unknown) => { headerSettled = true; throw error; });
  const consume = async (): Promise<string> => {
    const res = await headers;
    controller.signal.throwIfAborted();
    if (!res.ok) throw httpError(res.status);
    if (!res.body) return "";
    reader = res.body.getReader();
    const decoder = new TextDecoder();
    let text = "";
    while (true) {
      controller.signal.throwIfAborted();
      if (performance.now() >= deadlineAt) throw avisonYoungDeadlineError();
      let chunk: Awaited<ReturnType<ResponseBodyReader["read"]>>;
      try { chunk = await reader.read(); }
      catch (error) { readFailure = { error }; throw error; }
      controller.signal.throwIfAborted();
      if (performance.now() >= deadlineAt) throw avisonYoungDeadlineError();
      if (chunk.done) return text + decoder.decode();
      text += decoder.decode(chunk.value, { stream: true });
    }
  };
  try {
    return await withinAvisonYoungDeadline(consume(), deadlineAt);
  } catch (error) {
    const cleanupDeadlineAt = performance.now() + CLEANUP_MS;
    const cleanupRemaining = () => Math.max(0, cleanupDeadlineAt - performance.now());
    controller.abort();
    // Observe a synchronous abort's pending owned read rejection before
    // selecting its exact closure witness.
    await Promise.resolve();
    // A late header result is observed, but cannot start body consumption after
    // abort. Wait for its settlement before deciding whether fallback is safe.
    if (!headerSettled) {
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        await Promise.race([
          headers.then(() => {}, () => {}),
          new Promise<void>((resolve) => { timer = setTimeout(resolve, cleanupRemaining()); }),
        ]);
      } finally { if (timer) clearTimeout(timer); }
    }
    if (!headerSettled) {
      // Discard any eventual response without allowing a later dispatch.
      void headers.then((res) => cancelResponseBody(res.body, CLEANUP_MS), () => {});
      throw refuseUnsettled(error);
    }
    const readError = readFailure ? readFailure.error : error;
    let settled = reader
      ? await settleResponseBodyRead(reader, readError, cleanupRemaining())
      : await cancelResponseBody(response?.body ?? null, cleanupRemaining());
    // Native abort errors can arrive during cleanup. Only an error observed
    // from our pending read can supply this later exact closure witness.
    if (!settled && reader && readFailure && readFailure.error !== readError) {
      settled = await settleResponseBodyRead(reader, readFailure.error, 0);
    }
    if (!settled) throw refuseUnsettled(error);
    throw error;
  } finally {
    reader?.releaseLock();
  }
}

export type AvisonYoungPinnedResponse = { status: number; location: string | null; body: string };

/** Own only the pinned HTTPS lifecycle; URL/IP and document policy stay in the source. */
export async function requestAvisonYoungPinned(
  url: URL,
  address: string,
  timeoutMs: number,
  deadlineAt = performance.now() + timeoutMs
): Promise<AvisonYoungPinnedResponse> {
  assertAvisonYoungTransportSettled();
  if (performance.now() >= deadlineAt) throw avisonYoungDeadlineError();
  return await new Promise((resolve, reject) => {
    let request: ClientRequest | undefined;
    let response: IncomingMessage | undefined;
    let requestClosed = false;
    let responseClosed = false;
    let finished = false;
    let failure: { error: unknown } | undefined;
    let cleanupTimer: ReturnType<typeof setTimeout> | undefined;
    const timer = setTimeout(() => fail(avisonYoungDeadlineError()), Math.max(0, Math.ceil(deadlineAt - performance.now())));
    const clearTimers = () => { clearTimeout(timer); if (cleanupTimer) clearTimeout(cleanupTimer); };
    const acknowledge = () => {
      if (!finished && failure && requestClosed && (!response || responseClosed)) {
        finished = true;
        clearTimers();
        reject(failure.error);
      }
    };
    const fail = (error: unknown) => {
      if (finished || failure) return;
      failure = { error };
      clearTimeout(timer);
      cleanupTimer = setTimeout(() => {
        if (finished) return;
        finished = true;
        reject(refuseUnsettled(error));
      }, CLEANUP_MS);
      // Keep error handlers installed: destroy can emit its error asynchronously.
      response?.destroy();
      request?.destroy();
      acknowledge();
    };
    try {
      if (performance.now() >= deadlineAt) throw avisonYoungDeadlineError();
      request = httpsRequest(url, {
        headers: {
          accept: "text/html,application/xhtml+xml",
          "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
        },
        lookup: ((_hostname: string, options: any, callback: Function) => {
          const family = isIP(address);
          if (options?.all === true) callback(null, [{ address, family }]);
          else callback(null, address, family);
        }) as any,
      }, (res) => {
        response = res;
        const chunks: Buffer[] = [];
        let bytes = 0;
        res.on("error", fail);
        res.on("aborted", () => fail(new Error("Avison Young direct detail response aborted")));
        res.on("close", () => {
          responseClosed = true;
          if (!finished && !failure) fail(new Error("Avison Young direct detail response closed before end"));
          acknowledge();
        });
        if (failure || finished) { res.destroy(); return; }
        res.on("data", (chunk: Buffer | string) => {
          if (failure || finished) return;
          if (performance.now() >= deadlineAt) { fail(avisonYoungDeadlineError()); return; }
          const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
          bytes += buffer.length;
          if (bytes > 5 * 1024 * 1024) { fail(new Error("Avison Young direct detail exceeded 5 MiB")); return; }
          chunks.push(buffer);
        });
        res.on("end", () => {
          if (failure || finished) return;
          if (performance.now() >= deadlineAt) { fail(avisonYoungDeadlineError()); return; }
          finished = true;
          clearTimers();
          resolve({ status: res.statusCode ?? 0, location: typeof res.headers.location === "string" ? res.headers.location : null,
            body: Buffer.concat(chunks).toString("utf8") });
        });
      });
      request.on("error", fail);
      request.on("close", () => { requestClosed = true; acknowledge(); });
      request.end();
    } catch (error) {
      if (request) fail(error);
      else { clearTimers(); reject(error); }
    }
  });
}
