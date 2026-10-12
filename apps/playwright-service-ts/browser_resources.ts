/**
 * Route-neutral primitives for bounded browser work: capacity semaphores, hard
 * timeouts that clean up late values, and confirmed resource teardown. Shared
 * by /scrape, /browser-batch-fetch, /health and the C10 listener; permit
 * ownership itself lives in permit_lease.ts.
 */

export class HardTimeoutError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "HardTimeoutError";
  }
}

export class SemaphoreTimeoutError extends Error {
  constructor() {
    super("Semaphore acquisition exceeded its hard deadline");
    this.name = "SemaphoreTimeoutError";
  }
}

export class Semaphore {
  private permits: number;
  private queue: Array<() => void> = [];

  constructor(permits: number) {
    this.permits = permits;
  }

  async acquire(timeoutMs?: number, signal?: AbortSignal): Promise<void> {
    signal?.throwIfAborted();
    if (this.permits > 0) {
      this.permits--;
      return;
    }

    return new Promise<void>((resolve, reject) => {
      let settled = false;
      let timer: ReturnType<typeof setTimeout> | undefined;
      const cleanup = () => {
        if (timer) clearTimeout(timer);
        signal?.removeEventListener("abort", aborted);
      };
      const remove = () => {
        const index = this.queue.indexOf(waiter);
        if (index >= 0) this.queue.splice(index, 1);
      };
      const aborted = () => {
        if (settled) return;
        settled = true;
        cleanup();
        remove();
        reject(signal?.reason ?? new Error("Semaphore acquisition cancelled"));
      };
      const waiter = (): void => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve();
      };
      this.queue.push(waiter);
      signal?.addEventListener("abort", aborted, { once: true });
      if (timeoutMs !== undefined) {
        timer = setTimeout(
          () => {
            if (settled) return;
            settled = true;
            cleanup();
            remove();
            reject(new SemaphoreTimeoutError());
          },
          Math.max(1, timeoutMs),
        );
      }
    });
  }

  /** Takes a permit only if one is free right now; never queues. */
  tryAcquire(): boolean {
    if (this.permits <= 0) return false;
    this.permits--;
    return true;
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

export function withHardTimeout<T>(
  operation: Promise<T>,
  timeoutMs: number,
  message: string,
  cleanupLateValue?: (value: T) => Promise<unknown> | unknown,
  signal?: AbortSignal,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    let timedOut = false;
    const detach = () => signal?.removeEventListener("abort", aborted);
    const aborted = () => {
      timedOut = true;
      clearTimeout(timer);
      detach();
      reject(signal?.reason ?? new Error("Browser operation cancelled"));
    };
    const timer = setTimeout(() => {
      timedOut = true;
      detach();
      reject(new HardTimeoutError(message));
    }, timeoutMs);
    operation.then(
      (value) => {
        if (timedOut) {
          if (cleanupLateValue) {
            void Promise.resolve(cleanupLateValue(value)).catch((error) => {
              console.error("Browser batch late-value cleanup error:", error);
            });
          }
          return;
        }
        clearTimeout(timer);
        detach();
        resolve(value);
      },
      (error) => {
        if (timedOut) return;
        clearTimeout(timer);
        detach();
        reject(error);
      },
    );
    signal?.addEventListener("abort", aborted, { once: true });
    if (signal?.aborted) aborted();
  });
}

/**
 * Closes a page and/or context within timeoutMs each and reports whether the
 * browser resources are confirmed gone. Permits are the caller's business.
 */
export async function closeBrowserResources(
  closePage: (() => Promise<unknown>) | null,
  closeContext: (() => Promise<unknown>) | null,
  timeoutMs = 5_000,
): Promise<boolean> {
  let pageClosed = closePage === null;
  let contextClosed = closeContext === null;
  try {
    if (closePage) {
      await withHardTimeout(
        closePage(),
        timeoutMs,
        "Browser batch page cleanup timed out",
      );
      pageClosed = true;
    }
  } catch (error) {
    console.error("Browser batch page cleanup error:", error);
  }
  try {
    if (closeContext) {
      await withHardTimeout(
        closeContext(),
        timeoutMs,
        "Browser batch context cleanup timed out",
      );
      contextClosed = true;
    }
  } catch (error) {
    console.error("Browser batch context cleanup error:", error);
  }
  // A successfully closed context owns and tears down all of its pages, even
  // when the earlier page.close() call timed out.
  return closeContext ? contextClosed : pageClosed;
}
