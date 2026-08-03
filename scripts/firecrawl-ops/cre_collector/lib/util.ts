// lib/util.ts - extracted verbatim from collect.ts (see tasks/tmp backup)


// ---------- shared helpers ----------

export function clean(s: any): string | null {
  if (typeof s !== "string") return null;
  const t = s.replace(/\s+/g, " ").trim();
  return t || null;
}

export function num(v: any): number | null {
  return typeof v === "number" && isFinite(v) && v !== 0 ? v : null;
}

export function boundedInt(value: string | undefined, fallback: number, lo: number, hi: number): number {
  const parsed = value === undefined ? fallback : Number(value);
  const finite = Number.isFinite(parsed) ? parsed : fallback;
  return Math.max(lo, Math.min(hi, Math.trunc(finite)));
}

export function moneyToNumber(t: string | null): number | null {
  if (!t) return null;
  const m = t.replace(/,/g, "").match(/\$\s*([0-9]+(?:\.[0-9]+)?)/);
  return m ? Number(m[1]) : null;
}

export function isPerSfPriceText(t: string | null): boolean {
  return Boolean(t && /(?:\/|\bper\s+)\s*(?:s\.?f\.?|sq\.?\s*ft|square\s*feet)|\bpsf\b/i.test(t));
}

export function prune(v: any): any {
  if (v === null || v === undefined || v === false || v === "") return undefined;
  if (Array.isArray(v)) {
    const arr = v.map(prune).filter((x) => x !== undefined);
    return arr.length ? arr : undefined;
  }
  if (typeof v === "object") {
    const out: Record<string, any> = {};
    for (const [k, val] of Object.entries(v)) {
      const p = prune(val);
      if (p !== undefined) out[k] = p;
    }
    return Object.keys(out).length ? out : undefined;
  }
  return v;
}

// Bounded-concurrency map that preserves input order in the result.
export async function pmap<T, R>(items: T[], limit: number, fn: (t: T, i: number) => Promise<R>): Promise<R[]> {
  const results: R[] = new Array(items.length);
  let next = 0;
  let failed = false;
  async function worker() {
    while (!failed && next < items.length) {
      const i = next++;
      try {
        results[i] = await fn(items[i], i);
      } catch (error) {
        failed = true;
        throw error;
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return results;
}
