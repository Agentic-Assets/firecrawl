/** Derived from the production Response contract, including its byte backing type. */
export type ResponseBodyReader = ReadableStreamDefaultReader<
  NonNullable<Response["body"]> extends ReadableStream<infer Chunk> ? Chunk : never
>;

export class ResponseBodyLimitError extends Error {
  declare readonly prefix?: Uint8Array;
  constructor(maxBytes: number, prefix?: Uint8Array) {
    super(`Response body exceeds ${maxBytes} bytes`);
    this.name = "ResponseBodyLimitError";
    if (prefix !== undefined) Object.defineProperty(this, "prefix", { value: prefix, enumerable: false, configurable: false, writable: false });
  }
}

export class ResponseBodyDeadlineError extends Error {
  constructor() {
    super("Response body deadline exceeded");
    this.name = "ResponseBodyDeadlineError";
  }
}

/**
 * Read actual HTTP-decoded bytes under one absolute monotonic deadline. This
 * bounds retained input bytes, not the process heap or fetch-owned chunks.
 * The caller owns cancellation, acknowledgement, lock release and retry policy.
 */
export async function readBoundedBytes(
  reader: ResponseBodyReader,
  { maxBytes, signal, deadlineAt, retainPrefix = false }: { maxBytes: number; signal: AbortSignal; deadlineAt: number; retainPrefix?: boolean }
): Promise<Uint8Array> {
  if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0 || (retainPrefix && maxBytes === Number.MAX_SAFE_INTEGER) || !Number.isFinite(deadlineAt)) {
    throw new RangeError("Response body requires a positive safe byte limit and finite deadline");
  }
  signal.throwIfAborted();
  const deadlineError = new ResponseBodyDeadlineError();
  if (performance.now() >= deadlineAt) throw deadlineError;
  let stopped = false;
  let readFailure: { error: unknown } | undefined;
  let timeout: ReturnType<typeof setTimeout> | undefined;
  let onAbort = () => {};
  const deadline = new Promise<never>((_, reject) => {
    onAbort = () => { stopped = true; reject(signal.reason); };
    signal.addEventListener("abort", onAbort, { once: true });
    timeout = setTimeout(() => { stopped = true; reject(deadlineError); }, Math.ceil(Math.max(0, deadlineAt - performance.now())));
  });
  const consume = async (): Promise<Uint8Array> => {
    let buffer = new Uint8Array(0);
    let total = 0;
    while (true) {
      if (stopped || performance.now() >= deadlineAt) throw deadlineError;
      let chunk: Awaited<ReturnType<ResponseBodyReader["read"]>>;
      try { chunk = await reader.read(); }
      catch (error) { readFailure = { error }; throw error; }
      if (stopped || performance.now() >= deadlineAt) throw deadlineError;
      if (chunk.done) return buffer.subarray(0, total);
      const value = chunk.value;
      if (value.byteLength > maxBytes - total) {
        if (!retainPrefix) throw new ResponseBodyLimitError(maxBytes);
        // Retain one byte beyond the bound as proof of an oversized body.
        const prefix = new Uint8Array(maxBytes + 1);
        prefix.set(buffer.subarray(0, total));
        prefix.set(value.subarray(0, maxBytes - total + 1), total);
        throw new ResponseBodyLimitError(maxBytes, prefix);
      }
      const required = total + value.byteLength;
      if (required > buffer.byteLength) {
        const capacity = Math.min(maxBytes, Math.max(required, buffer.byteLength * 2, 64 * 1024));
        const grown = new Uint8Array(capacity);
        grown.set(buffer.subarray(0, total));
        buffer = grown;
      }
      buffer.set(value, total);
      total = required;
    }
  };
  try {
    return await Promise.race([consume(), deadline]);
  } catch (error) {
    // Only a rejection actually observed from this owned read supplies exact
    // closure evidence. An arbitrary later closed error cannot authorize retry.
    if (readFailure) throw readFailure.error;
    throw error;
  } finally {
    stopped = true;
    if (timeout) clearTimeout(timeout);
    signal.removeEventListener("abort", onAbort);
  }
}

/** Decode using the historical replacement-character behavior. */
export async function readBoundedUtf8(
  reader: ResponseBodyReader,
  options: { maxBytes: number; signal: AbortSignal; deadlineAt: number }
): Promise<string> {
  return new TextDecoder().decode(await readBoundedBytes(reader, options));
}

/** Discarded HTTP/redirect bodies require actual cancellation acknowledgement. */
export async function cancelResponseBody(
  body: { cancel(): Promise<unknown> } | null,
  timeoutMs: number
): Promise<boolean> {
  if (!body) return true;
  let cancellation: Promise<unknown>;
  try { cancellation = Promise.resolve(body.cancel()); } catch { return false; }
  let timeout: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      cancellation.then(() => true, () => false),
      new Promise<boolean>(resolve => { timeout = setTimeout(() => resolve(false), timeoutMs); }),
    ]);
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

/**
 * While the owner still holds the lock, either the exact read failure on closed
 * or fulfilled cancellation proves settlement. Closed fulfillment does not:
 * cancel may close the reader while underlying cleanup rejects or never settles.
 */
export async function settleResponseBodyRead(
  reader: ResponseBodyReader,
  originalError: unknown,
  timeoutMs: number
): Promise<boolean> {
  const exactFailure = reader.closed.then(
    () => Promise.reject(),
    error => error === originalError ? true : Promise.reject()
  );
  const acknowledged = Promise.resolve().then(() => reader.cancel()).then(() => true);
  let timeout: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      Promise.any([exactFailure, acknowledged]).catch(() => false),
      new Promise<boolean>(resolve => { timeout = setTimeout(() => resolve(false), timeoutMs); }),
    ]);
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}
