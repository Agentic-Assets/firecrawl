import {
  HardTimeoutError,
  Semaphore,
  SemaphoreTimeoutError,
  closeBrowserResources,
  withHardTimeout,
} from "./browser_resources";
import {
  BrowserResourceLeakError,
  PermitLease,
  awaitAllocation,
  releaseAfterClose,
} from "./permit_lease";

export type ScrapePhase = "admission" | "work";

export class ScrapeDeadlineError extends Error {
  constructor(public readonly phase: ScrapePhase) {
    super(`Browser scrape ${phase} deadline exceeded`);
    this.name = "ScrapeDeadlineError";
  }

  // Prototype getters, not own fields, so logged errors print unchanged.
  get code(): "SCRAPE_ADMISSION_TIMEOUT" | "SCRAPE_WORK_TIMEOUT" {
    return this.phase === "admission"
      ? "SCRAPE_ADMISSION_TIMEOUT"
      : "SCRAPE_WORK_TIMEOUT";
  }
}

/** The caller disconnected; no response can be delivered. */
export class ScrapeClientGoneError extends Error {
  constructor() {
    super("Browser scrape client disconnected");
    this.name = "ScrapeClientGoneError";
  }
}

/**
 * Extra time given to Playwright's own operation timeouts (goto,
 * waitForSelector) beyond the lifecycle deadline, so the lifecycle timer
 * fires first and a deadline is reported as a work timeout, not as a
 * Playwright TimeoutError.
 */
export const PLAYWRIGHT_DEADLINE_GRACE_MS = 250;

/**
 * Upper bound, in ms, for an explicit timeout or wait_after_load. Values above
 * 2^31-1 ms overflow Node timers and fire at once. apps/api sends its
 * remaining scrape budget and caps waitFor at 60 s, so 24 hours never rejects
 * a request the API can make in practice.
 */
export const MAX_SCRAPE_INPUT_MS = 24 * 60 * 60 * 1000;

/** Default navigation budget when no explicit timeout is sent. */
const DEFAULT_SCRAPE_NAVIGATION_MS = 15000;

export type ScrapeTiming =
  | { ok: true; timeout: number; waitAfterLoad: number }
  | { ok: false; error: string };

/**
 * Validates /scrape timing input. null is treated like an omitted field. An
 * explicit timeout is the whole request budget; without one the budget is
 * 15 s of navigation on top of wait_after_load.
 */
export function parseScrapeTiming(
  requestedTimeout: unknown,
  requestedWaitAfterLoad: unknown,
): ScrapeTiming {
  const waitAfterLoad = requestedWaitAfterLoad ?? 0;
  if (
    typeof waitAfterLoad !== "number" ||
    !Number.isFinite(waitAfterLoad) ||
    waitAfterLoad < 0
  ) {
    return {
      ok: false,
      error: "wait_after_load must be a non-negative finite number",
    };
  }
  if (waitAfterLoad > MAX_SCRAPE_INPUT_MS) {
    return {
      ok: false,
      error: `wait_after_load must not exceed ${MAX_SCRAPE_INPUT_MS} ms`,
    };
  }
  const timeout =
    requestedTimeout ?? DEFAULT_SCRAPE_NAVIGATION_MS + waitAfterLoad;
  if (
    typeof timeout !== "number" ||
    !Number.isFinite(timeout) ||
    timeout <= 0
  ) {
    return { ok: false, error: "Timeout must be a positive finite number" };
  }
  if (timeout > MAX_SCRAPE_INPUT_MS) {
    return {
      ok: false,
      error: `Timeout must not exceed ${MAX_SCRAPE_INPUT_MS} ms`,
    };
  }
  return { ok: true, timeout, waitAfterLoad };
}

/**
 * One budget covers validation, permit queueing, optional pacing, context and
 * page setup, navigation and body reads. The page permit is a PermitLease: it
 * is released only once its browser resources are confirmed closed (or were
 * never allocated), exactly once, and quarantined on a leak.
 */
export async function runScrapeLifecycle<C, P, R>(options: {
  deadlineAt: number;
  semaphore: Semaphore;
  /** Aborted when the caller disconnects; checked at every phase boundary. */
  signal?: AbortSignal;
  prepare: (remainingMs: () => number) => Promise<void>;
  /** Runs while holding the permit, before any browser allocation. */
  paceStart?: (remainingMs: () => number) => Promise<void>;
  createContext: () => Promise<C>;
  createPage: (context: C) => Promise<P>;
  work: (page: P, context: C, remainingMs: () => number) => Promise<R>;
  /** Deliver synchronously before cleanup, while this request still owns its permit. */
  deliverResult?: (result: R) => void;
  closeContext: (context: C) => Promise<unknown>;
  closePage: (page: P) => Promise<unknown>;
  cleanupTimeoutMs?: number;
}): Promise<R> {
  let phase: ScrapePhase = "admission";
  const remaining = () => {
    if (options.signal?.aborted) throw new ScrapeClientGoneError();
    const ms = options.deadlineAt - Date.now();
    if (ms <= 0) throw new ScrapeDeadlineError(phase);
    return ms;
  };
  const bounded = async <T>(
    operation: () => Promise<T>,
    late?: (value: T) => Promise<unknown>,
  ): Promise<T> => {
    const ms = remaining();
    const message = `Browser scrape ${phase} deadline exceeded`;
    try {
      return await withHardTimeout(operation(), ms, message, late);
    } catch (error) {
      if (
        error instanceof HardTimeoutError &&
        error.message === message
      ) {
        throw new ScrapeDeadlineError(phase);
      }
      // The deadline wins: browser work that fails at or after the deadline
      // (for example a Playwright timeout racing the lifecycle timer) is a
      // work timeout. Lifecycle-owned errors keep their meaning.
      if (
        phase === "work" &&
        Date.now() >= options.deadlineAt &&
        !(error instanceof ScrapeDeadlineError) &&
        !(error instanceof ScrapeClientGoneError) &&
        !(error instanceof BrowserResourceLeakError)
      ) {
        throw new ScrapeDeadlineError(phase);
      }
      throw error;
    }
  };

  await bounded(() => options.prepare(remaining));
  let lease: PermitLease;
  try {
    // The semaphore removes expired waiters itself. Racing an unbounded
    // acquire against a timer would leak the permit it later grants.
    lease = await PermitLease.acquire(
      [{ source: options.semaphore, countsBrowser: true }],
      remaining,
    );
  } catch (error) {
    if (error instanceof SemaphoreTimeoutError) {
      throw new ScrapeDeadlineError("admission");
    }
    throw error;
  }

  let context: C | undefined;
  let page: P | undefined;
  try {
    if (options.paceStart) await bounded(() => options.paceStart!(remaining));
    // Checked before entering the work phase: a deadline that passes after
    // the permit is granted but before any context exists is admission.
    remaining();
    phase = "work";
    // A context that outlives the deadline takes over the lease.
    context = await awaitAllocation(
      lease,
      options.createContext,
      (allocation) => bounded(() => allocation),
      options.closeContext,
      options.cleanupTimeoutMs,
    );
    page = await bounded(
      () => options.createPage(context!),
      async (latePage) => {
        // The owning context is closed by the finally block below; closing a
        // late page is best-effort and never touches the permit.
        await closeBrowserResources(
          () => options.closePage(latePage),
          null,
          options.cleanupTimeoutMs,
        );
      },
    );
    const result = await bounded(() =>
      options.work(page!, context!, remaining),
    );
    options.deliverResult?.(result);
    return result;
  } finally {
    if (lease.heldByRequest) {
      await releaseAfterClose(
        lease,
        page === undefined ? null : () => options.closePage(page!),
        context === undefined ? null : () => options.closeContext(context!),
        options.cleanupTimeoutMs,
      );
    }
  }
}
