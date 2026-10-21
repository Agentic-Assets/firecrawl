import { clean } from "../../lib/util.js";
export const DAUM_HOST = "https://daumcommercial.com";
export const DAUM_SEARCH_URL = `${DAUM_HOST}/property-search/`;
export type DaumTenure = "sale" | "lease" | "sale_or_lease" | "unknown";
export function daumPageUrl(page: number): string { if (!Number.isInteger(page) || page < 1) throw new Error(`DAUM page must be a positive integer, got ${page}`); return page === 1 ? DAUM_SEARCH_URL : `${DAUM_SEARCH_URL}page/${page}/`; }
export function daumAbsoluteUrl(value: unknown): string | null {
  const raw = clean(value);
  if (!raw || /^(?:javascript|mailto|tel):/i.test(raw)) return null;
  try {
    const url = new URL(raw, DAUM_HOST);
    if (
      url.protocol !== "https:"
      || url.hostname !== "daumcommercial.com"
      || url.username
      || url.password
      || url.port
    ) {
      return null;
    }
    return url.toString();
  } catch {
    return null;
  }
}
export function canonicalDaumPropertyUrl(value: unknown): string | null {
  const raw = clean(value); if (!raw || /^(?:javascript|mailto|tel):/i.test(raw)) return null;
  try { const url = new URL(raw, DAUM_HOST); return url.protocol === "https:" && url.hostname === "daumcommercial.com" && !url.username && !url.password && !url.port && /^\/property\/[^/]+\/$/.test(url.pathname) && !url.search && !url.hash ? url.toString() : null; } catch { return null; }
}
export function daumListingId(value: unknown): string {
  const url = canonicalDaumPropertyUrl(value);
  if (!url) throw new Error("DAUM listing key requires an owned property URL");
  return new URL(url).pathname.replace(/^\/+|\/+$/g, "");
}
export function daumTenure(value: unknown): DaumTenure { const text = clean(value)?.toLowerCase(); if (!text) return "unknown"; if (text === "lease or sale" || text === "sale or lease") return "sale_or_lease"; if (text === "lease" || text === "sublease") return "lease"; if (text === "sale- user" || text === "sale- investment" || text === "sale") return "sale"; throw new Error(`DAUM inventory contains unknown transaction type ${JSON.stringify(value)}`); }

export function resolveDaumTenure(
  inventoryTenure: DaumTenure,
  detailTenure: DaumTenure
): DaumTenure {
  if (inventoryTenure === "unknown") return detailTenure;
  if (detailTenure === "unknown" || detailTenure === inventoryTenure) {
    return inventoryTenure;
  }
  throw new Error(
    `DAUM detail tenure ${detailTenure} disagrees with inventory tenure ${inventoryTenure}`
  );
}
