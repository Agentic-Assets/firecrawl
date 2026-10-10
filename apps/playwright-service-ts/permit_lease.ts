/**
 * One owner for a request's capacity permits. A lease is held by the request,
 * handed off to a browser allocation that outlived the request, released
 * exactly once, or quarantined while a browser context may still be live.
 * /scrape, /browser-batch-fetch, /health and the C10 listener all settle their
 * permits through it, so "who releases" has a single implementation.
 */
import { type Semaphore, closeBrowserResources } from "./browser_resources";

/**
 * A browser allocation failed and its partial context could not be confirmed
 * closed. Its lease stays quarantined. The message and code are the /scrape
 * failure contract (503 SCRAPE_RESOURCE_LEAK).
 */
export class BrowserResourceLeakError extends Error {
  constructor(public readonly cause: unknown) {
    super("Browser scrape resource cleanup was not confirmed");
    this.name = "BrowserResourceLeakError";
  }

  get code(): "SCRAPE_RESOURCE_LEAK" {
    return "SCRAPE_RESOURCE_LEAK";
  }
}

/** Anything that grants one permit at a time (a Semaphore, or a test double). */
export type PermitSource = Readonly<{
  acquire(timeoutMs?: number): Promise<void>;
  release(): void;
}>;

export type LeaseGate = Readonly<{
  source: PermitSource;
  /**
   * True when the permit stands for a browser context (page capacity, a C10
   * page slot). Quarantine keeps these and returns admission-only permits
   * such as the browser-batch slot.
   */
  countsBrowser: boolean;
}>;

type HeldPermit = Readonly<{ release: () => void; countsBrowser: boolean }>;

type LeaseState = "held" | "handedOff" | "released" | "quarantined";

export class PermitLease {
  private state: LeaseState = "held";
  private readonly permits: HeldPermit[] = [];

  private constructor() {}

  /**
   * Acquires each gate in order, each bounded by a fresh timeoutMs(). On any
   * failure (including timeoutMs() throwing) the permits already taken are
   * returned and the error is rethrown.
   */
  static async acquire(
    gates: readonly LeaseGate[],
    timeoutMs: () => number,
  ): Promise<PermitLease> {
    const lease = new PermitLease();
    try {
      for (const { source, countsBrowser } of gates) {
        await source.acquire(timeoutMs());
        lease.add(() => source.release(), countsBrowser);
      }
    } catch (error) {
      lease.release();
      throw error;
    }
    return lease;
  }

  /** Takes one free page permit without waiting, or returns null. */
  static tryAcquire(semaphore: Semaphore): PermitLease | null {
    if (!semaphore.tryAcquire()) return null;
    const lease = new PermitLease();
    lease.add(() => semaphore.release(), true);
    return lease;
  }

  /** Adds a permit taken outside a semaphore (C10's page slot) to a held lease. */
  add(release: () => void, countsBrowser: boolean): void {
    if (this.state !== "held") {
      throw new Error("Permit lease is no longer held by its request");
    }
    this.permits.push({ release, countsBrowser });
  }

  /** True while the request itself is responsible for settling the lease. */
  get heldByRequest(): boolean {
    return this.state === "held";
  }

  /** The request gives up ownership; the late allocation's owner settles it. */
  handOff(): void {
    if (this.state === "held") this.state = "handedOff";
  }

  /**
   * Returns every permit, newest first, once. Later calls do nothing. Each
   * permit is returned independently, so one that throws cannot strand the rest.
   */
  release(): void {
    if (this.state === "released" || this.state === "quarantined") return;
    this.state = "released";
    for (const permit of this.permits.splice(0).reverse()) {
      PermitLease.returnPermit(permit);
    }
  }

  /**
   * The request's own cleanup: closes its page and context, then releases or
   * quarantines the lease (see releaseAfterClose). After a hand-off a late
   * allocation owns the lease, so this does nothing and returns false, and a
   * caller can never return a permit early.
   */
  async settleByRequest(
    closePage: (() => Promise<unknown>) | null,
    closeContext: (() => Promise<unknown>) | null,
    timeoutMs?: number,
  ): Promise<boolean> {
    if (!this.heldByRequest) return false;
    return releaseAfterClose(this, closePage, closeContext, timeoutMs);
  }

  private static returnPermit(permit: HeldPermit): void {
    try {
      permit.release();
    } catch (error) {
      console.error("Capacity permit release failed; continuing with the rest:", error);
    }
  }

  /**
   * A context may still be live: keep its browser permits for the process
   * lifetime (restart is the recovery path) and return only admission permits.
   */
  quarantine(reason: string): void {
    if (this.state === "released" || this.state === "quarantined") return;
    this.state = "quarantined";
    for (const permit of this.permits.splice(0).reverse()) {
      if (!permit.countsBrowser) PermitLease.returnPermit(permit);
    }
    console.error(`${reason}; retaining the capacity permit`);
  }
}

/**
 * Closes the request's page and context, then releases the lease, or
 * quarantines it when the close cannot be confirmed. Returns whether the
 * close was confirmed.
 */
export async function releaseAfterClose(
  lease: PermitLease,
  closePage: (() => Promise<unknown>) | null,
  closeContext: (() => Promise<unknown>) | null,
  timeoutMs?: number,
): Promise<boolean> {
  const closed = await closeBrowserResources(closePage, closeContext, timeoutMs);
  if (closed) {
    lease.release();
  } else {
    lease.quarantine("Browser resource cleanup was not confirmed");
  }
  return closed;
}

/**
 * Waits for a browser context allocation on the lease's behalf.
 *
 * - The allocation fails in time: a leak quarantines the lease; any other
 *   error leaves it held for the request's own cleanup.
 * - The wait gives up first (deadline, client gone): the allocation inherits
 *   the lease. A late context is closed and the lease released once the close
 *   is confirmed; a late rejection releases unless it reports a leak.
 */
export async function awaitAllocation<T>(
  lease: PermitLease,
  allocate: () => Promise<T>,
  wait: (allocation: Promise<T>) => Promise<T>,
  closeLate: (value: T) => Promise<unknown>,
  cleanupTimeoutMs?: number,
): Promise<T> {
  const allocation = (async () => allocate())();
  let failure: { error: unknown } | undefined;
  // Registered before wait() subscribes, so an in-time failure is recorded
  // by the time the wait rethrows it.
  allocation.catch((error: unknown) => {
    failure = { error };
  });
  try {
    return await wait(allocation);
  } catch (error) {
    if (failure && failure.error === error) {
      if (error instanceof BrowserResourceLeakError) {
        lease.quarantine("Browser context setup leaked a context");
      }
      throw error;
    }
    lease.handOff();
    allocation.then(
      (late) =>
        releaseAfterClose(lease, null, () => closeLate(late), cleanupTimeoutMs),
      (lateError: unknown) => {
        if (lateError instanceof BrowserResourceLeakError) {
          lease.quarantine("Late browser context setup leaked a context");
        } else {
          lease.release();
        }
      },
    );
    throw error;
  }
}
