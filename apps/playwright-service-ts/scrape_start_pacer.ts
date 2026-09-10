/** One process-wide gate; permits and deadlines bound the number of waiting callers. */
export class ScrapeStartPacer {
  private nextStartAt = 0;

  constructor(private readonly intervalMs: number) {
    if (!Number.isInteger(intervalMs) || intervalMs < 0 || intervalMs > 5000) {
      throw new Error(
        'SCRAPE_START_INTERVAL_MS must be an integer from 0 to 5000',
      );
    }
  }

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

export function scrapeStartIntervalMs(
  value = process.env.SCRAPE_START_INTERVAL_MS,
): number {
  const intervalMs = value === undefined || value === '' ? 0 : Number(value);
  if (!Number.isInteger(intervalMs) || intervalMs < 0 || intervalMs > 5000) {
    throw new Error(
      'SCRAPE_START_INTERVAL_MS must be an integer from 0 to 5000',
    );
  }
  return intervalMs;
}
