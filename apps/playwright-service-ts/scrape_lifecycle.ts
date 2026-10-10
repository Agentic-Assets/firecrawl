import {
  BrowserBatchHardTimeoutError,
  Semaphore,
  cleanupBrowserBatchResources,
  withBrowserBatchHardTimeout,
} from "./browser_batch_fetch";

export type ScrapePhase = "admission" | "work";

export class ScrapeDeadlineError extends Error {
  constructor(public readonly phase: ScrapePhase) {
    super(`Browser scrape ${phase} deadline exceeded`);
    this.name = "ScrapeDeadlineError";
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
 * A browser resource allocation failed and its partial resource could not be
 * confirmed closed. The lifecycle keeps the page permit quarantined.
 */
export class ScrapeResourceLeakError extends Error {
  constructor(public readonly cause: unknown) {
    super("Browser scrape resource cleanup was not confirmed");
    this.name = "ScrapeResourceLeakError";
  }
}

const SEMAPHORE_TIMEOUT_MESSAGE =
  "Semaphore acquisition exceeded its hard deadline";

/**
 * One budget covers validation, permit queueing, optional pacing, context and
 * page setup, navigation and body reads. Capacity accounting matches
 * /browser-batch-fetch: a permit is released only once its browser resources
 * are confirmed closed (or were never allocated), exactly once.
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
      return await withBrowserBatchHardTimeout(operation(), ms, message, late);
    } catch (error) {
      if (
        error instanceof BrowserBatchHardTimeoutError &&
        error.message === message
      ) {
        throw new ScrapeDeadlineError(phase);
      }
      throw error;
    }
  };

  await bounded(() => options.prepare(remaining));
  try {
    // The semaphore removes expired waiters itself. Racing an unbounded
    // acquire against a timer would leak the permit it later grants.
    await options.semaphore.acquire(remaining());
  } catch (error) {
    if (error instanceof Error && error.message === SEMAPHORE_TIMEOUT_MESSAGE) {
      throw new ScrapeDeadlineError("admission");
    }
    throw error;
  }

  let released = false;
  const release = () => {
    if (released) return;
    released = true;
    options.semaphore.release();
  };
  // Set when a context allocation outlives the deadline: its settlement, not
  // this request's finally block, decides when the permit is released.
  let permitOwnedByLateContext = false;
  let permitQuarantined = false;
  let context: C | undefined;
  let page: P | undefined;
  try {
    if (options.paceStart) await bounded(() => options.paceStart!(remaining));
    phase = "work";
    remaining();
    const pendingContext = (async () => options.createContext())();
    let contextSettled = false;
    const markContextSettled = () => {
      contextSettled = true;
    };
    // Attached first, so it runs before the bounded race observes settlement.
    pendingContext.then(markContextSettled, markContextSettled);
    try {
      context = await bounded(() => pendingContext);
    } catch (error) {
      if (!contextSettled) {
        permitOwnedByLateContext = true;
        pendingContext.then(
          (lateContext) =>
            cleanupBrowserBatchResources(
              null,
              () => options.closeContext(lateContext),
              release,
              options.cleanupTimeoutMs,
            ),
          (lateError) => {
            if (lateError instanceof ScrapeResourceLeakError) {
              console.error(
                "Late browser context cleanup was not confirmed; retaining the capacity permit",
              );
            } else {
              release();
            }
          },
        );
      } else if (error instanceof ScrapeResourceLeakError) {
        permitQuarantined = true;
      }
      throw error;
    }
    page = await bounded(
      () => options.createPage(context!),
      async (latePage) => {
        // The owning context is closed by the finally block below; closing a
        // late page is best-effort and never touches the permit.
        await cleanupBrowserBatchResources(
          () => options.closePage(latePage),
          null,
          () => {},
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
    if (permitQuarantined) {
      console.error(
        "Browser context cleanup was not confirmed; retaining the capacity permit",
      );
    } else if (!permitOwnedByLateContext) {
      await cleanupBrowserBatchResources(
        page === undefined ? null : () => options.closePage(page!),
        context === undefined ? null : () => options.closeContext(context!),
        release,
        options.cleanupTimeoutMs,
      );
    }
  }
}
