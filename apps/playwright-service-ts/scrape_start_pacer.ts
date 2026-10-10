const MAX_SCRAPE_START_INTERVAL_MS = 5000;

function assertValidInterval(intervalMs: number): void {
  if (
    !Number.isInteger(intervalMs) ||
    intervalMs < 0 ||
    intervalMs > MAX_SCRAPE_START_INTERVAL_MS
  ) {
    throw new Error(
      `SCRAPE_START_INTERVAL_MS must be an integer from 0 to ${MAX_SCRAPE_START_INTERVAL_MS}`,
    );
  }
}

/**
 * One process-wide gate that spaces browser-context allocations. Page permits
 * and per-request deadlines bound the number of callers waiting here.
 */
export class ScrapeStartPacer {
  private nextStartAt = 0;

  constructor(private readonly intervalMs: number) {
    assertValidInterval(intervalMs);
  }

  /** `remainingMs` must throw once the caller's deadline has passed. */
  async waitForStart(remainingMs: () => number): Promise<void> {
    while (true) {
      const remaining = remainingMs();
      const now = performance.now();
      const waitMs = this.nextStartAt - now;
      if (this.intervalMs === 0 || waitMs <= 0) {
        // Claim synchronously. Competing callers recheck after waking, so an
        // event-loop stall cannot collapse overdue reservations into a burst.
        this.nextStartAt = now + this.intervalMs;
        return;
      }
      await new Promise<void>((resolve) => {
        setTimeout(resolve, Math.min(Math.ceil(waitMs), remaining));
      });
    }
  }
}

/** Default 0 (unpaced). Invalid values fail service startup. */
export function scrapeStartIntervalMs(
  value = process.env.SCRAPE_START_INTERVAL_MS,
): number {
  const intervalMs = value === undefined || value === "" ? 0 : Number(value);
  assertValidInterval(intervalMs);
  return intervalMs;
}
