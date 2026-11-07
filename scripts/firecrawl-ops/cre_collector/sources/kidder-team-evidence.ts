import { createHash } from "node:crypto";
import * as cheerio from "cheerio";
import { observationNow } from "../lib/observation-clock.js";
import { cancelResponseBody, readBoundedBytes, settleResponseBodyRead } from "../lib/response-body.js";
import { isSourceChallengeShell } from "../lib/source-page-context/index.js";
import { NativeEvidenceTerminalError } from "../lib/source-evidence/terminal-error.js";

const URL = "https://kidder.com/teams/team-anthony-steck/";
const MAX_BYTES = 1_000_000;
const TIMEOUT_MS = 15_000;

/** Each card is an unqualified occurrence, never a canonical listing identity. */
export function parseKidderTeamPage(html: string, observedAt: string) {
  if (/cf-chl-|challenge-platform/i.test(html) || isSourceChallengeShell(html)) throw new Error("Kidder team page challenge or error shell");
  const $ = cheerio.load(html);
  const sections = $("section#available-properties");
  const elements = sections.length === 1 ? sections.find("article.card-post").toArray() : [];
  if (elements.length > 1000) throw new Error("Kidder team page card limit exceeded");
  const observation = {
    schema: "kidder-team-page-observation-v1", sourceUrl: URL, observedAt,
    coverage: "partial_unknown", identity: "unverified", recognizedSection: sections.length === 1,
    decodedHtmlSha256: createHash("sha256").update(html, "utf8").digest("hex"),
    decodedHtml: html,
    cards: elements.map((element, index) => ({ index, html: $.html(element),
      href: $(element).find("a.card-post__link").first().attr("href") ?? null })),
  };
  if (Buffer.byteLength(JSON.stringify(observation)) > 3_000_000) throw new Error("Kidder team observation limit exceeded");
  return observation;
}

export async function captureKidderTeamPage(fetchImpl: typeof fetch = fetch) {
  const controller = new AbortController();
  const deadlineAt = performance.now() + TIMEOUT_MS;
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetchImpl(URL, { signal: controller.signal, redirect: "manual", headers: { accept: "text/html" } });
    if (!response.ok || !/^text\/html(?:\s*;|$)/i.test(response.headers.get("content-type") ?? "")) {
      if (!await cancelResponseBody(response.body, 1000)) throw new NativeEvidenceTerminalError("Kidder team response cleanup unsettled");
      throw new Error("Kidder team page unavailable");
    }
    if (!response.body) throw new Error("Kidder team page empty");
    const reader = response.body.getReader();
    try {
      const bytes = await readBoundedBytes(reader, { maxBytes: MAX_BYTES, signal: controller.signal, deadlineAt });
      const html = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      return { ...parseKidderTeamPage(html, observationNow()), httpStatus: response.status,
        responseSha256: createHash("sha256").update(bytes).digest("hex"), responseBytes: bytes.byteLength };
    } catch (error) {
      controller.abort();
      if (!await settleResponseBodyRead(reader, error, 1000)) throw new NativeEvidenceTerminalError("Kidder team response read cleanup unsettled", { cause: error });
      throw error;
    } finally { try { reader.releaseLock(); } catch { /* preserve failure */ } }
  } finally { clearTimeout(timer); }
}
