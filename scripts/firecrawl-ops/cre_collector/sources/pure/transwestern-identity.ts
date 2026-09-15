import { clean } from "../../lib/util.js";

export const TRANSWESTERN_HOST = "https://transwestern.com";
export function canonicalTranswesternUrl(href: string | null): string | null {
  const value = clean(href);
  if (!value || /^javascript:/i.test(value) || value === "-") return null;
  try { return new URL(value, TRANSWESTERN_HOST).toString(); } catch { return null; }
}
export function transwesternDetailUrl(pageUrl: unknown): string | null {
  const slug = clean(String(pageUrl ?? ""));
  return !slug || slug === "-" ? null : `${TRANSWESTERN_HOST}/property/${encodeURIComponent(slug).replace(/%2F/g, "/")}`;
}
