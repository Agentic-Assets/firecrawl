export const BROWSER_BATCH_FETCH_MAX_REQUESTS = 16;
export const BROWSER_BATCH_FETCH_MAX_BODY_BYTES = 64 * 1024;
export const BROWSER_BATCH_FETCH_MAX_TOTAL_BODY_BYTES = 512 * 1024;
export const BROWSER_BATCH_FETCH_MAX_RESPONSE_BYTES = 16 * 1024 * 1024;
export const BROWSER_BATCH_FETCH_MAX_TOTAL_RESPONSE_BYTES = 96 * 1024 * 1024;
export const BROWSER_BATCH_FETCH_MAX_TOTAL_DURATION_MS = 240_000;

const ALLOWED_HEADERS = new Set(['accept', 'content-type']);
const ALLOWED_CONTENT_TYPES = new Set([
  'application/json',
  'application/x-www-form-urlencoded',
]);

export type BrowserBatchFetchRequest = {
  url: string;
  method: 'POST';
  headers: Record<string, string>;
  body: string;
};

export type BrowserBatchFetchInput = {
  bootstrapUrl: string;
  waitAfterLoadMs: number;
  timeoutMs: number;
  requests: BrowserBatchFetchRequest[];
};

export class Semaphore {
  private permits: number;
  private queue: Array<() => void> = [];

  constructor(permits: number) {
    this.permits = permits;
  }

  async acquire(timeoutMs?: number): Promise<void> {
    if (this.permits > 0) {
      this.permits--;
      return;
    }

    return new Promise<void>((resolve, reject) => {
      let settled = false;
      let timer: ReturnType<typeof setTimeout> | undefined;
      const waiter = (): void => {
        if (settled) return;
        settled = true;
        if (timer) clearTimeout(timer);
        resolve();
      };
      this.queue.push(waiter);
      if (timeoutMs !== undefined) {
        timer = setTimeout(() => {
          if (settled) return;
          settled = true;
          const index = this.queue.indexOf(waiter);
          if (index >= 0) this.queue.splice(index, 1);
          reject(new Error('Semaphore acquisition exceeded its hard deadline'));
        }, Math.max(1, timeoutMs));
      }
    });
  }

  release(): void {
    this.permits++;
    const next = this.queue.shift();
    if (next) {
      this.permits--;
      next();
    }
  }

  getAvailablePermits(): number {
    return this.permits;
  }

  getQueueLength(): number {
    return this.queue.length;
  }
}

export function withBrowserBatchHardTimeout<T>(
  operation: Promise<T>,
  timeoutMs: number,
  message: string,
  cleanupLateValue?: (value: T) => Promise<unknown> | unknown,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      reject(new Error(message));
    }, timeoutMs);
    operation.then(
      (value) => {
        if (timedOut) {
          if (cleanupLateValue) {
            void Promise.resolve(cleanupLateValue(value)).catch((error) => {
              console.error('Browser batch late-value cleanup error:', error);
            });
          }
          return;
        }
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        if (timedOut) return;
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

export async function cleanupBrowserBatchResources(
  closePage: (() => Promise<unknown>) | null,
  closeContext: (() => Promise<unknown>) | null,
  release: () => void,
  timeoutMs = 5_000,
): Promise<void> {
  try {
    if (closePage) {
      await withBrowserBatchHardTimeout(
        closePage(),
        timeoutMs,
        'Browser batch page cleanup timed out',
      );
    }
  } catch (error) {
    console.error('Browser batch page cleanup error:', error);
  }
  try {
    if (closeContext) {
      await withBrowserBatchHardTimeout(
        closeContext(),
        timeoutMs,
        'Browser batch context cleanup timed out',
      );
    }
  } catch (error) {
    console.error('Browser batch context cleanup error:', error);
  } finally {
    release();
  }
}

function requireHttpUrl(value: unknown, field: string): URL {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error(`${field} must be a nonempty URL`);
  }
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`${field} must be a valid URL`);
  }
  if (!['http:', 'https:'].includes(url.protocol)) {
    throw new Error(`${field} must use http or https`);
  }
  if (url.username || url.password) {
    throw new Error(`${field} must not contain credentials`);
  }
  if (value.length > 2048) {
    throw new Error(`${field} exceeds the URL length limit`);
  }
  if (url.hash) {
    throw new Error(`${field} must not contain a fragment`);
  }
  return url;
}

function boundedInteger(
  value: unknown,
  fallback: number,
  minimum: number,
  maximum: number,
  field: string,
): number {
  if (value === undefined) return fallback;
  if (!Number.isInteger(value) || Number(value) < minimum || Number(value) > maximum) {
    throw new Error(`${field} must be an integer between ${minimum} and ${maximum}`);
  }
  return Number(value);
}

export function parseBrowserBatchFetchInput(value: unknown): BrowserBatchFetchInput {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('request body must be an object');
  }
  const body = value as Record<string, unknown>;
  const bootstrap = requireHttpUrl(body.bootstrapUrl, 'bootstrapUrl');
  if (!Array.isArray(body.requests) || body.requests.length < 1) {
    throw new Error('requests must be a nonempty array');
  }
  if (body.requests.length > BROWSER_BATCH_FETCH_MAX_REQUESTS) {
    throw new Error(`requests must contain at most ${BROWSER_BATCH_FETCH_MAX_REQUESTS} items`);
  }

  let totalBodyBytes = 0;
  const requests = body.requests.map((requestValue, index): BrowserBatchFetchRequest => {
    if (!requestValue || typeof requestValue !== 'object' || Array.isArray(requestValue)) {
      throw new Error(`requests[${index}] must be an object`);
    }
    const request = requestValue as Record<string, unknown>;
    const url = requireHttpUrl(request.url, `requests[${index}].url`);
    if (url.origin !== bootstrap.origin) {
      throw new Error(`requests[${index}].url must have the bootstrap origin`);
    }
    if (request.method !== 'POST') {
      throw new Error(`requests[${index}].method must be POST`);
    }
    if (typeof request.body !== 'string') {
      throw new Error(`requests[${index}].body must be a string`);
    }
    const bodyBytes = Buffer.byteLength(request.body, 'utf8');
    if (bodyBytes > BROWSER_BATCH_FETCH_MAX_BODY_BYTES) {
      throw new Error(`requests[${index}].body exceeds the per-request byte limit`);
    }
    totalBodyBytes += bodyBytes;
    if (totalBodyBytes > BROWSER_BATCH_FETCH_MAX_TOTAL_BODY_BYTES) {
      throw new Error('request bodies exceed the aggregate byte limit');
    }

    if (!request.headers || typeof request.headers !== 'object' || Array.isArray(request.headers)) {
      throw new Error(`requests[${index}].headers must be an object`);
    }
    const headers: Record<string, string> = {};
    for (const [name, headerValue] of Object.entries(request.headers as Record<string, unknown>)) {
      const normalizedName = name.toLowerCase();
      if (!ALLOWED_HEADERS.has(normalizedName)) {
        throw new Error(`requests[${index}].headers contains disallowed header ${name}`);
      }
      if (typeof headerValue !== 'string' || headerValue.length > 512) {
        throw new Error(`requests[${index}].headers.${name} must be a short string`);
      }
      headers[normalizedName] = headerValue;
    }
    const contentType = headers['content-type']?.split(';', 1)[0].trim().toLowerCase();
    if (!contentType || !ALLOWED_CONTENT_TYPES.has(contentType)) {
      throw new Error(`requests[${index}].headers.content-type is not allowed`);
    }

    return {
      url: url.toString(),
      method: 'POST',
      headers,
      body: request.body,
    };
  });

  return {
    bootstrapUrl: bootstrap.toString(),
    waitAfterLoadMs: boundedInteger(body.waitAfterLoadMs, 0, 0, 30_000, 'waitAfterLoadMs'),
    timeoutMs: boundedInteger(body.timeoutMs, 30_000, 1_000, 60_000, 'timeoutMs'),
    requests,
  };
}
