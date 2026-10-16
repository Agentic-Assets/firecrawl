import { observationNow } from "../lib/observation-clock.js";
import { refreshGenerationId, refreshStartedAt } from "../lib/freshness.js";
import { ScrapedDoc } from "../types.js";
import {
  transwesternIdentityDigest,
  transwesternPrimaryIdentityMatches,
  transwesternPublishedPlaceholderUrl,
} from "./pure/transwestern-published-identity.js";

export interface TranswesternPublishedIdentityProof {
  url: string;
  sitemapUrl: string;
  identitySha256: string;
  generationId: string | null;
  verifiedAt: string;
}

async function boundedOfficialGet(url: string, limit: number, signal: AbortSignal): Promise<string> {
  const requestSignal = AbortSignal.any([signal, AbortSignal.timeout(10000)]);
  const response = await fetch(url, { signal: requestSignal, redirect: "error", cache: "no-store" });
  if (!response.ok || response.url !== url || !response.body) {
    throw new Error("Transwestern published identity response unavailable");
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      size += chunk.value.byteLength;
      if (size > limit) throw new Error("Transwestern published identity response exceeds its bound");
      chunks.push(chunk.value);
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
  return Buffer.concat(chunks).toString("utf8");
}

export async function proveTranswesternPublishedIdentity(
  row: any,
  isUsable: (doc: ScrapedDoc, row: any) => boolean,
): Promise<TranswesternPublishedIdentityProof> {
  if (typeof row?.PageUrl !== "string" || row.PageUrl.trim() !== "-") {
    throw new Error("Transwestern published identity exception requires the placeholder slug");
  }
  const identitySha256 = transwesternIdentityDigest(row);
  const generationId = refreshGenerationId();
  const signal = AbortSignal.timeout(20000);
  const sitemapUrl = "https://transwestern.com/sitemap.xml";
  const url = transwesternPublishedPlaceholderUrl(await boundedOfficialGet(sitemapUrl, 2 * 1024 * 1024, signal));
  const rawHtml = await boundedOfficialGet(url, 1024 * 1024, signal);
  if (!isUsable({ rawHtml, markdown: "", links: [] }, row) || !transwesternPrimaryIdentityMatches(rawHtml, row)) {
    throw new Error("Transwestern published property did not match the complete feed identity");
  }
  if (generationId !== refreshGenerationId()) throw new Error("Transwestern published identity generation changed");
  return { url, sitemapUrl, identitySha256, generationId, verifiedAt: observationNow() };
}

export function requireCurrentTranswesternPublishedProof(
  proof: TranswesternPublishedIdentityProof,
  row: any,
  inventoryObservedAt: string,
): void {
  const verified = Date.parse(proof.verifiedAt);
  const inventory = Date.parse(inventoryObservedAt);
  const started = refreshStartedAt();
  if (
    typeof row?.PageUrl !== "string" || row.PageUrl.trim() !== "-" ||
    proof.url !== "https://transwestern.com/property/-" ||
    proof.sitemapUrl !== "https://transwestern.com/sitemap.xml" ||
    proof.identitySha256 !== transwesternIdentityDigest(row) ||
    proof.generationId !== refreshGenerationId() ||
    !Number.isFinite(verified) || !Number.isFinite(inventory) || verified < inventory ||
    verified > Date.now() + 1000 || (started !== null && verified < Date.parse(started))
  ) throw new Error("Transwestern published identity proof is stale or mismatched");
}
