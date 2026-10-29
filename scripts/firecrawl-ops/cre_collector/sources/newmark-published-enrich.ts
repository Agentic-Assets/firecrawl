import type { EnrichItem, SourceEnricher } from "../lib/enrich.js";
import type { ScrapedDoc } from "../types.js";
import { scrapeDoc, type CollectorScrapeOpts } from "../lib/scrape.js";
import { observationNow } from "../lib/observation-clock.js";
import { requireFreshDetails } from "../lib/freshness.js";
import { possibleChallengeHeader } from "../lib/page-observations/index.js";
import { captureEnrichmentEvidence, failedEnrichmentEvidence, limitEnrichmentObservations } from "../lib/enrichment-evidence/index.js";
import { NativeEvidenceTerminalError } from "../lib/source-evidence/terminal-error.js";
import { resolveNewmarkDetailLink } from "./pure/newmark-detail-link.js";
import { extractNewmarkRcmDetail, matchNewmarkRcmProperty } from "./pure/newmark-rcm-detail.js";
import { mapNewmarkRcmFields } from "./pure/newmark-rcm-fields.js";

/** The explicit experimental path retains observations and qualifies substantive matches. */
export function createNewmarkPublishedDetailEnricher(fetchDoc: (url: string, options?: CollectorScrapeOpts) => Promise<ScrapedDoc> = scrapeDoc,
  clock: () => string = observationNow): SourceEnricher {
  async function batch(items: EnrichItem[]) {
    const outcomes: Array<{ listing: any | null; observation: unknown }> = [];
    let stop: "access_denial" | "possible_challenge_header" | "uncertain_settlement" | null = null;
    for (const item of items) {
      if (stop) {
        outcomes.push({ listing: null, observation: { ...failedEnrichmentEvidence(item), failure: `not_attempted_after_${stop}`, newmarkRcmObservation: true } });
        continue;
      }
      const resolution = resolveNewmarkDetailLink(item);
      if (resolution.state !== "resolved") {
        outcomes.push({ listing: null, observation: { ...failedEnrichmentEvidence(item, "native_redirect_not_listing"), resolution: "unresolved", newmarkRcmObservation: true } });
        continue;
      }
      let doc: ScrapedDoc;
      try {
        doc = await fetchDoc(resolution.pageUrl, { singleAttempt: true, quietErrors: true, allowEmptyBody: true,
          ...(requireFreshDetails() ? { maxAge: 0 } : {}) });
      } catch (error) {
        if (error instanceof NativeEvidenceTerminalError) throw error;
        const status = (error as { status?: unknown })?.status;
        if ([401, 403, 429, 451].includes(status as number)) stop = "access_denial";
        else if (typeof status !== "number" || !Number.isInteger(status) || status < 200 || status >= 300) stop = "uncertain_settlement";
        outcomes.push({ listing: null, observation: { ...failedEnrichmentEvidence(item), resolution: "resolved", requestedPageUrl: resolution.pageUrl,
          ...(typeof status === "number" ? { localApiStatus: status } : {}), ...(stop ? { stopReason: stop } : {}), newmarkRcmObservation: true } });
        continue;
      }
      let capturedAt: string | null = null;
      try { capturedAt = clock(); } catch { /* Preserve an unavailable observation clock. */ }
      const detail = extractNewmarkRcmDetail(doc.rawHtml);
      const match = detail.status === "observed" ? matchNewmarkRcmProperty(item.sourcePublishedDetail, detail.parsed) : "unresolved";
      const sourceObservation = { schema: "newmark-rcm-detail-observation/v1", identity: "unverified", visibility: "unverified",
        sourceClock: resolution.sourceClock, sourceClockStatus: resolution.sourceClockStatus,
        witness: item.sourcePublishedDetail, extraction: detail.status, match,
        matchMethod: "strict_json_visitor_source_candidate",
        literal: detail.literal, numericFidelity: detail.numericFidelity,
        parsed: detail.parsedSerializable ? detail.parsed : null, parsedSerializable: detail.parsedSerializable };
      let observation: unknown;
      let evidenceRetained = false;
      try { const captured = captureEnrichmentEvidence(item, doc, capturedAt, { pageUrl: resolution.pageUrl,
        sourceObservation, sourceObservationIncomplete: detail.status !== "observed" || !detail.parsedSerializable });
        evidenceRetained = !!captured.sourceObservation && !captured.omissions.sourceObservation;
        observation = { ...captured, newmarkRcmObservation: true }; }
      catch { observation = { ...failedEnrichmentEvidence(item, "evidence_capture_failed"), resolution: "resolved", newmarkRcmObservation: true }; }
      const status = doc.metadata?.statusCode;
      if ([401, 403, 429, 451].includes(status as number)) stop = "access_denial";
      else if (possibleChallengeHeader(doc.rawHtml)) stop = "possible_challenge_header";
      else if (typeof status !== "number" || !Number.isInteger(status) || status < 200 || status >= 300) stop = "uncertain_settlement";
      if (stop) { outcomes.push({ listing: null, observation: { ...(observation as object), stopReason: stop } }); continue; }
      if (!evidenceRetained || match !== "matched" || !detail.parsedSerializable || status < 200 || status >= 300) {
        outcomes.push({ listing: null, observation }); continue;
      }
      const fields = mapNewmarkRcmFields(detail.parsed, resolution.pageUrl, item.transaction ?? "sale");
      const substantive = !!fields.description || fields.salePriceUsd !== undefined || !!fields.photos?.length ||
        !!fields.contactsDetailed?.length || !!fields.documents?.length;
      if (!substantive) { outcomes.push({ listing: null, observation }); continue; }
      outcomes.push({ listing: { id: item.externalId, url: item.url, canonicalUrl: item.url,
        transactionMode: null, preserveChildCollections: true, ...fields }, observation });
    }
    const bounded = limitEnrichmentObservations(outcomes.map(outcome => outcome.observation));
    return { listings: outcomes.slice(0, bounded.retainedObservations.length).map(outcome => outcome.listing).filter(Boolean), ...bounded,
      ...(stop && !bounded.evidenceError ? { evidenceError: `newmark_detail_${stop}` } : {}) };
  }
  return { async enrich(items) { return (await batch(items)).listings; }, async enrichWithEvidence(items) { return batch(items); } };
}
