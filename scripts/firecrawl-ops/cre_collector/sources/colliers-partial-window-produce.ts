/** Stage one verified rotating window. Caller owns lock, custody, sink and settlement. */
import type { Readable } from "node:stream";
import { createHash } from "node:crypto";
import { observationNow } from "../lib/observation-clock.js";
import { ColliersAccessStop, ColliersIdentityError, enrichColliersCard, groupColliersMapLocations, parseColliersCards } from "./colliers.js";
import { NativeEvidenceTerminalError } from "../lib/source-evidence/terminal-error.js";
import { ColliersDetailHold, ColliersSettledSourceStop, fetchColliersPartialDetail } from "./colliers-partial-fetch.js";
import { scanColliersPartial, type Candidate } from "./pure/colliers-partial-spool.js";
import { selectColliersWindow } from "./pure/colliers-partial-window.js";
import { assertPartialNativeIdentity } from "./colliers-partial-native-id.js";

type Binding = { generation: string; attempt: number; startedAt: string; finishedAt: string };
type Native = { detail: any; decodedText: string; nativeBytes: Uint8Array };
type WindowMember = { candidate: Candidate; eligible: boolean };
type Hold = { candidate: Candidate; reason: string; observedAt?: string; evidence?: Uint8Array; status?: number };

/**
 * Sink must persist the whole current item before returning stop. It owns byte
 * accounting and private evidence, and must discard unsealed output on failure.
 * Access refusal and uncertain settlement always abort.
 */
export async function produceColliersWindow(
  open: () => Readable, verify: () => Promise<void>, binding: Binding, cursor: number,
  sink: (item: { listing: any; source: any; decodedDetail: string; nativeBytes: Uint8Array }) => Promise<"continue" | "stop">,
  hold: (item: Hold) => Promise<"continue" | "stop" | void>,
  fetchDetail: (token: string) => Promise<Native> = fetchColliersPartialDetail,
  limit?: number,
) {
  await verify();
  const selection = await selectColliersWindow(open, verify, binding, cursor, limit);
  const byPosition = new Map<number, WindowMember>();
  for (const member of selection.window) {
    const p = member.candidate.start + member.candidate.offset;
    if (byPosition.has(p)) throw new Error("selected positions differ");
    byPosition.set(p, member);
  }
  const processed = new Set<number>(), unadmitted = new Set<number>();
  let stopped = false;
  const drainHeldPrefix = async () => {
    while (!stopped) {
      const member = selection.window.find(item => !processed.has(item.candidate.start + item.candidate.offset));
      if (!member || member.eligible) return;
      const p = member.candidate.start + member.candidate.offset;
      const decision = await hold({ candidate: member.candidate, reason: "identity_conflict" });
      processed.add(p); unadmitted.add(p);
      if (decision === "stop") stopped = true;
    }
  };
  await drainHeldPrefix();
  const hash = (v: string | Uint8Array) => createHash("sha256").update(v).digest("hex");
  // Captured pages need not be in rotating cursor order. Re-scan as needed
  // so an early physical page cannot starve a later cursor position.
  while (!stopped && processed.size < selection.window.length) {
    const before = processed.size;
    const scan = await scanColliersPartial(open(), binding.generation, binding.attempt, binding.startedAt, binding.finishedAt,
    async page => {
      const cards = parseColliersCards(JSON.parse(page.list).html,
        groupColliersMapLocations(JSON.parse(page.map).projectLocations), page.start, false);
      for (const [offset, card] of cards.entries()) {
        const p = page.start + offset, member = byPosition.get(p);
        if (!member?.eligible || processed.has(p) || stopped) continue;
        const next = selection.window.find(item => !processed.has(item.candidate.start + item.candidate.offset));
        if (next !== member) continue;
        const candidate = member.candidate;
        if (candidate.page !== page.page || candidate.pageSize !== page.pageSize || candidate.observedAt !== page.observedAt ||
            candidate.mapProjectId !== card.mapProjectId || candidate.detailPv !== card.detailPv || candidate.detailUrl !== card.detailUrl)
          throw new Error("selected source identity changed");
        let native: Native | undefined;
        let captured: { listing: any; source: any; decodedDetail: string; nativeBytes: Uint8Array } | undefined;
        try {
          native = await fetchDetail(candidate.detailPv!);
          if (!(native.nativeBytes instanceof Uint8Array) ||
              new TextDecoder("utf-8", { fatal: true }).decode(native.nativeBytes) !== native.decodedText)
            throw new Error("native bytes differ");
          const detail = JSON.parse(native.decodedText);
          assertPartialNativeIdentity(detail, candidate.mapProjectId);
          const mapped = await enrichColliersCard(card, { fetchDetail: async () => detail,
            stopOnAccessFailure: true, skipBrokerRefs: true });
          if (!mapped || mapped.inventoryOnly || mapped.id !== candidate.mapProjectId ||
              mapped.colliersSalesTrackerDetail?.projectId !== candidate.mapProjectId) throw new ColliersIdentityError();
          const detailObservedAt = observationNow();
          const source = { generation: binding.generation, attempt: binding.attempt,
            start: page.start, page: page.page, pageSize: page.pageSize, offset,
            inventoryObservedAt: page.observedAt, detailObservedAt, mapProjectId: candidate.mapProjectId,
            listSha256: hash(page.list), mapSha256: hash(page.map), detailTokenSha256: hash(candidate.detailPv!),
            decodedDetailSha256: hash(native.decodedText), nativeSha256: hash(native.nativeBytes) };
          const listing = { ...mapped, sourceKey: "colliers", sourceCompany: "Colliers", transactionMode: "sale",
            brokerIds: [], preserveChildCollections: true, inventoryObservedAt: page.observedAt, detailObservedAt,
            freshnessProvenance: { generationId: binding.generation, detailScope: "detail_page",
              method: "colliers_salestracker_slp_detail", cacheDisposition: "live" },
            colliersPartialEvidence: { schema: "colliers-partial-detail-v1", ...source } };
          captured = { listing, source, decodedDetail: native.decodedText, nativeBytes: native.nativeBytes };
        } catch (error) {
          if (error instanceof ColliersAccessStop || error instanceof NativeEvidenceTerminalError) throw error;
          if (error instanceof ColliersIdentityError || error instanceof ColliersDetailHold || error instanceof ColliersSettledSourceStop) {
            const reason = error instanceof ColliersIdentityError ? "detail_identity" : error.reason;
            const evidence = error instanceof ColliersIdentityError ? native?.nativeBytes : error.evidence;
            const status = error instanceof ColliersIdentityError ? undefined : error.status;
            const observedAt = observationNow();
            const decision = await hold({ candidate, reason, evidence, status, observedAt });
            processed.add(p); unadmitted.add(p);
            if (decision === "stop" || error instanceof ColliersSettledSourceStop) stopped = true;
            await drainHeldPrefix();
            continue;
          }
          throw error;
        }
        // Custody/sink errors are never record-local detail errors.
        const decision = await sink(captured!);
        if (decision !== "continue" && decision !== "stop") throw new Error("invalid sink decision");
        processed.add(p);
        if (decision === "stop") stopped = true;
        await drainHeldPrefix();
      }
    }, () => {});
  await verify();
    if (scan.heldPages || scan.stopped || scan.total !== selection.total || scan.holes !== selection.holes)
      throw new Error("partial source changed during production");
    if (!stopped && processed.size === before) throw new Error("partial window member disappeared");
  }
  // Cross-run cursor may only advance over the contiguous, settled prefix in
  // rotation order. A caller must wait for exact DB commit/readback.
  let nextCursor = cursor;
  for (const member of selection.window) {
    const p = member.candidate.start + member.candidate.offset;
    if (!processed.has(p)) break;
    nextCursor = (p % selection.total) + 1;
  }
  return { selection, processed: processed.size, held: unadmitted.size,
    unattempted: selection.window.length - processed.size, proposedCursor: nextCursor, stopped };
}
