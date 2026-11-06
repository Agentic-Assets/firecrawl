import { createHash } from "node:crypto";
import * as cheerio from "cheerio";
import { DaumPoliteTransport } from "./daum-transport.js";
import { canonicalDaumPropertyUrl } from "./pure/daum-identity.js";

type CandidateEnvelope = {
  schema: "daum-redirect-candidate/v1";
  artifact_sha256: string;
  generation_id: string;
  producer_sha: string;
  database_target_sha256: string;
  member: { native_id: string; external_id: string; source_url: string };
  candidate: {
    rowId: string; oldExternalId: string; oldUrl: string; nativeId: string;
    currentExternalId: string; currentUrl: string; rowSha256: string; indexSha256: string | null;
  };
  write_authority: false;
};

// Observation only: the envelope must be independently rederived before use.
export async function observeDaumMove(envelope: CandidateEnvelope, transport: DaumPoliteTransport) {
  const c = envelope.candidate;
  const m = envelope.member;
  const digest = /^[a-f0-9]{64}$/;
  const old = canonicalDaumPropertyUrl(c?.oldUrl);
  const current = canonicalDaumPropertyUrl(c?.currentUrl);
  if (envelope.schema !== "daum-redirect-candidate/v1" || envelope.write_authority !== false
      || !digest.test(envelope.artifact_sha256) || !digest.test(envelope.database_target_sha256)
      || !/^[a-f0-9]{40}$/.test(envelope.producer_sha)
      || typeof envelope.generation_id !== "string" || !envelope.generation_id
      || !old || old !== c.oldUrl || !current || current !== c.currentUrl || old === current
      || c.nativeId !== m?.native_id || !/^[1-9][0-9]{0,15}$/.test(c.nativeId)
      || !Number.isSafeInteger(Number(c.nativeId))
      || c.currentExternalId !== m.external_id || c.currentUrl !== m.source_url
      || c.oldExternalId !== new URL(old).pathname.slice(1, -1)
      || c.currentExternalId !== new URL(current).pathname.slice(1, -1)
      || !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(c.rowId)
      || !digest.test(c.rowSha256) || (c.indexSha256 !== null && !digest.test(c.indexSha256))) {
    throw new Error("DAUM move candidate binding invalid");
  }
  const redirect = await transport.observePermanentRedirect(old, current);
  const html = await transport.observeCurrentPage(current);
  const $ = cheerio.load(html);
  const canonical = $('link[rel="canonical"]');
  const shortlink = $('link[rel="shortlink"]');
  if (canonical.length !== 1 || shortlink.length !== 1
      || canonicalDaumPropertyUrl(canonical.attr("href")) !== current) {
    throw new Error("DAUM move current page identity invalid");
  }
  let shortUrl: URL;
  try { shortUrl = new URL(shortlink.attr("href") ?? ""); }
  catch { throw new Error("DAUM move shortlink invalid"); }
  if (shortUrl.href !== `https://daumcommercial.com/?p=${c.nativeId}`) {
    throw new Error("DAUM move native identity differs");
  }
  return {
    schema: "daum-redirect-observation/v1",
    candidate: envelope,
    redirect,
    page: { url: current, shortlink: shortUrl.href, observedAt: new Date().toISOString(),
      sha256: createHash("sha256").update(html).digest("hex"), html },
    write_authority: false as const,
  };
}
