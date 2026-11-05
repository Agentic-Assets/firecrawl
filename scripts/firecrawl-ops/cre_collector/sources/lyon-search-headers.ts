import { cancelResponseBody } from "../lib/response-body.js";
import { NativeEvidenceTerminalError } from "../lib/source-evidence/terminal-error.js";

/** A timed-out search must settle its owned header request before a peer can run. */
export async function lyonSearchHeaders(fetchImpl: (input: string | URL | Request, init?: RequestInit) => Promise<Response>, url: string, init: RequestInit, signal: AbortSignal): Promise<Response> {
  signal.throwIfAborted();
  const outcome = Promise.resolve().then(() => fetchImpl(url, init)).then(
    response => ({ kind: "response" as const, response }),
    error => ({ kind: "error" as const, error })
  );
  let onAbort = () => {};
  const aborted = new Promise<{ kind: "aborted" }>(resolve => {
    onAbort = () => resolve({ kind: "aborted" });
    signal.addEventListener("abort", onAbort, { once: true });
    if (signal.aborted) onAbort();
  });
  try {
    const first = await Promise.race([outcome, aborted]);
    if (first.kind === "response") return first.response;
    if (first.kind === "error") throw first.error;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let settled: Awaited<typeof outcome> | null;
    try {
      settled = await Promise.race([outcome, new Promise<null>(resolve => { timer = setTimeout(() => resolve(null), 1000); })]);
    } finally { if (timer) clearTimeout(timer); }
    if (!settled) throw new NativeEvidenceTerminalError("Lyon Stahl search header request did not settle");
    if (settled.kind === "response" && !await cancelResponseBody(settled.response.body, 1000)) {
      throw new NativeEvidenceTerminalError("Lyon Stahl late search response did not settle");
    }
    throw new Error("Lyon Stahl search header deadline exceeded");
  } finally { signal.removeEventListener("abort", onAbort); }
}
