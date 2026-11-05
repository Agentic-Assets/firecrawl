import { createHash } from "node:crypto";
import { observationNow } from "../lib/observation-clock.js";
import { NativeEvidenceTerminalError } from "../lib/source-evidence/terminal-error.js";
import { LyonSearchAccessError, LyonSearchHttpError } from "./lyon-search-headers.js";
import { LYON_SEARCH_URLS, isLyonStahlPacedFetch, lyonStahlFetchText, lyonStahlListingId, lyonStahlPacedFetch } from "./lyon-stahl.js";

type Filter = keyof typeof LYON_SEARCH_URLS;
type Page = { filter: Filter; observedAt: string; responseText: string };
type Occurrence = { filter: Filter; index: number };
type SourceIdentity = { kind: "property_path" | "wordpress_post"; value: string };

function sourceIdentity(url: string): SourceIdentity {
  const parsed = new URL(url);
  if (parsed.protocol !== "https:" || parsed.host !== "lyonstahl.com" || parsed.username || parsed.password || parsed.hash) throw new Error();
  if (!parsed.search && parsed.pathname.startsWith("/properties/")) return { kind: "property_path", value: lyonStahlListingId(url) };
  const entries = [...parsed.searchParams.entries()];
  const postId = parsed.searchParams.get("p");
  if (parsed.pathname === "/" && entries.length === 2 && parsed.searchParams.get("post_type") === "properties" &&
      postId && /^[1-9][0-9]*$/.test(postId) && new Set(entries.map(([key]) => key)).size === 2 &&
      entries.every(([key]) => key === "post_type" || key === "p")) return { kind: "wordpress_post", value: postId };
  throw new Error();
}

/** Preserve every page and card occurrence. Search membership is not lifecycle authority. */
export function projectLyonSearch(pages: Page[]) {
  const seen = new Set<Filter>();
  const candidates = new Map<string, { url: string; sourceIdentity: SourceIdentity; occurrences: Occurrence[] }>();
  const held: Array<Occurrence & { reason: string }> = [];
  const snapshots = pages.map(({ filter, observedAt, responseText }) => {
    if (!Object.hasOwn(LYON_SEARCH_URLS, filter) || seen.has(filter) || !Number.isFinite(Date.parse(observedAt)) || typeof responseText !== "string") {
      throw new Error("Invalid Lyon search snapshot");
    }
    seen.add(filter);
    const rows: unknown = JSON.parse(responseText);
    if (!Array.isArray(rows)) throw new Error("Lyon search response is not an array");
    rows.forEach((value, index) => {
      const occurrence = { filter, index };
      if (!value || typeof value !== "object" || Array.isArray(value)) {
        held.push({ ...occurrence, reason: "invalid_card" });
        return;
      }
      const url = (value as Record<string, unknown>).permalink;
      try {
        if (typeof url !== "string") throw new Error();
        const identity = sourceIdentity(url);
        const previous = candidates.get(url);
        if (previous) previous.occurrences.push(occurrence);
        else candidates.set(url, { url, sourceIdentity: identity, occurrences: [occurrence] });
      } catch {
        held.push({ ...occurrence, reason: "invalid_permalink" });
      }
    });
    return { filter, endpoint: LYON_SEARCH_URLS[filter], observedAt,
      responseTextSha256: createHash("sha256").update(responseText, "utf8").digest("hex"), responseText };
  });
  const idCounts = new Map<string, number>();
  const identityKey = (identity: SourceIdentity) => `${identity.kind}:${identity.value}`;
  for (const row of candidates.values()) {
    const key = identityKey(row.sourceIdentity);
    idCounts.set(key, (idCounts.get(key) ?? 0) + 1);
  }
  return { schema: "lyon-search-observation-v1", coverage: "partial_unknown" as const,
    snapshots, candidates: [...candidates.values()].map(row => ({ ...row,
      identityConflict: (idCounts.get(identityKey(row.sourceIdentity)) ?? 0) > 1 || new Set(row.occurrences.map(item => item.filter)).size !== row.occurrences.length,
    })), held };
}

function snapshot(page: Page) {
  return { ...page, endpoint: LYON_SEARCH_URLS[page.filter],
    responseTextSha256: createHash("sha256").update(page.responseText, "utf8").digest("hex") };
}

/** The source owner supplies the same paced transport used by any other Lyon route. */
export async function captureLyonSearch(paced: ReturnType<typeof lyonStahlPacedFetch>) {
  if (!isLyonStahlPacedFetch(paced)) throw new Error("Lyon search requires an invocation-owned paced fetch");
  const pages: Page[] = [];
  const valid: Page[] = [];
  const failures: Array<{ filter: Filter; reason: "acquisition_failed" | "invalid_response" | "access_stopped"; status?: number }> = [];
  let stopped = false;
  for (const filter of ["all", "sale", "lease"] as const) {
    try {
      const responseText = await lyonStahlFetchText(LYON_SEARCH_URLS[filter], "search", paced, undefined, undefined, 1);
      const page = { filter, observedAt: observationNow(), responseText };
      pages.push(page);
      try { projectLyonSearch([page]); valid.push(page); }
      catch { failures.push({ filter, reason: "invalid_response" }); }
    } catch (error) {
      if (error instanceof NativeEvidenceTerminalError) {
        Object.defineProperty(error, "retainedObservations", { value: [{ schema: "lyon-search-incomplete-v1",
          coverage: "partial_unknown", snapshots: pages.map(snapshot), failures }], enumerable: false });
        throw error;
      }
      if (!(error instanceof Error)) throw error;
      if (error instanceof LyonSearchAccessError) {
        failures.push({ filter, reason: "access_stopped", ...(error.status === undefined ? {} : { status: error.status }) });
        stopped = true;
        break;
      }
      failures.push({ filter, reason: "acquisition_failed", ...(error instanceof LyonSearchHttpError ? { status: error.status } : {}) });
    }
  }
  return { ...projectLyonSearch(valid), snapshots: pages.map(snapshot), failures, stopped };
}
