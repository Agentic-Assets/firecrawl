import { clean } from "../../lib/util.js";

/** Pure server-rendered Savills list-state parsing. */
export function parseSavillsNextData(html: string): any | null {
  const raw = html.match(/<script id="__NEXT_DATA__" type="application\/json">([\s\S]*?)<\/script>/)?.[1];
  if (!raw) return null;
  try { return JSON.parse(raw); } catch { return null; }
}

export function savillsNextDataProperties(html: string): any[] {
  const properties = parseSavillsNextData(html)?.props?.initialReduxState?.properties;
  return properties && typeof properties === "object" ? Object.values(properties) : [];
}

export function savillsFailedSearchInformation(html: string): string | null {
  const state = parseSavillsNextData(html)?.props?.initialReduxState;
  const candidate = state?.FailedSearchInformation ?? state?.failedSearchInformation ?? state?.listPage?.FailedSearchInformation ?? state?.listPage?.failedSearchInformation;
  if (typeof candidate === "string") return clean(candidate);
  return candidate && typeof candidate === "object" ? clean(candidate.Message ?? candidate.message ?? candidate.Text ?? candidate.text) : null;
}

export function savillsListHtmlIsUsable(html: string): boolean {
  const state = parseSavillsNextData(html)?.props?.initialReduxState;
  return !!state?.listPage && !!state?.properties && typeof state.properties === "object";
}
