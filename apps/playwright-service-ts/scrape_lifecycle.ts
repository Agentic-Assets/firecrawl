import {
  Semaphore,
  cleanupBrowserBatchResources,
  withBrowserBatchHardTimeout,
} from './browser_batch_fetch';

export class ScrapeDeadlineError extends Error {
  constructor(public readonly phase: 'admission' | 'work') {
    super(`Browser scrape ${phase} deadline exceeded`);
  }
}

/** One budget includes validation, queueing, setup, navigation and body reads. */
export async function runScrapeLifecycle<C, P, R>(options: {
  deadlineAt: number;
  semaphore: Semaphore;
  prepare: (remainingMs: () => number) => Promise<void>;
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
  let phase: 'admission' | 'work' = 'admission';
  const remaining = () => {
    const ms = options.deadlineAt - Date.now();
    if (ms <= 0) throw new ScrapeDeadlineError(phase);
    return ms;
  };
  const bounded = async <T>(
    operation: () => Promise<T>,
    late?: (value: T) => Promise<unknown>,
  ) => {
    const ms = remaining();
    const message = `Browser scrape ${phase} deadline exceeded`;
    try {
      return await withBrowserBatchHardTimeout(operation(), ms, message, late);
    } catch (error) {
      if (error instanceof Error && error.message === message)
        throw new ScrapeDeadlineError(phase);
      throw error;
    }
  };
  await bounded(() => options.prepare(remaining));
  try {
    // The semaphore removes expired waiters. Racing an unbounded acquire leaks permits.
    await options.semaphore.acquire(remaining());
  } catch (error) {
    if (
      error instanceof Error &&
      error.message === 'Semaphore acquisition exceeded its hard deadline'
    ) {
      throw new ScrapeDeadlineError('admission');
    }
    throw error;
  }
  let context: C | undefined;
  let page: P | undefined;
  try {
    if (options.paceStart) await bounded(() => options.paceStart!(remaining));
    phase = 'work';
    context = await bounded(options.createContext, async (lateContext) => {
      await cleanupBrowserBatchResources(
        null,
        () => options.closeContext(lateContext),
        () => {},
        options.cleanupTimeoutMs,
      );
    });
    page = await bounded(
      () => options.createPage(context!),
      async (latePage) => {
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
    await cleanupBrowserBatchResources(
      page === undefined ? null : () => options.closePage(page!),
      context === undefined ? null : () => options.closeContext(context!),
      () => options.semaphore.release(),
      options.cleanupTimeoutMs,
    );
  }
}
