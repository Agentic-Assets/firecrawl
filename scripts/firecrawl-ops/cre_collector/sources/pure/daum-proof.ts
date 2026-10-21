import type { DaumCurrentInventoryProof, DaumVerifiedSubsetInventoryProof, DaumEmptyTitleSubsetProof, Tx } from "../../types.js";
import type { DaumCompleteCohort } from "../daum-commercial.js";
import { daumListingId, resolveDaumTenure } from "./daum-identity.js";

/** Receipt serialization follows global acquisition/identity validation. */
export function daumProof(
  cohort: DaumCompleteCohort, tx: Tx, generationId: string, generationStartedAt: string,
  mode: "complete" | "verified_subset" | "verified_subset_empty_title"
): DaumCurrentInventoryProof | DaumVerifiedSubsetInventoryProof | DaumEmptyTitleSubsetProof {
  const rows = [...cohort.details].sort((a,b) => a.inventory.url < b.inventory.url ? -1 : a.inventory.url > b.inventory.url ? 1 : 0);
  const identity = { sourceKey: "daum-commercial" as const, transaction: tx };
  const base = {
    generationId, generationStartedAt,
    observedAt: cohort.snapshot.observedAt, total: cohort.snapshot.total,
    passes: structuredClone(cohort.snapshot.passes),
  };
  const members: DaumVerifiedSubsetInventoryProof["cohort"] = rows.map(({ inventory, detail }) => [
    String(detail.postId), inventory.url, daumListingId(inventory.url),
    inventory.tenure, inventory.tenureText, detail.tenure, detail.tenureText,
    resolveDaumTenure(inventory.tenure, detail.tenure),
  ]);
  const details: DaumCurrentInventoryProof["details"] = rows.map(row => [
    String(row.detail.postId), row.detail.observedCanonicalUrl!, row.observedAt, row.bodySha256,
  ]);
  const heldObservations = (held: ReadonlyArray<readonly [string, string]>) => {
    const ids = new Set(held.map(row => row[0]));
    const heldRows = rows.filter(row => ids.has(String(row.detail.postId)));
    return {
      heldInventoryObservations: heldRows.map(({ inventory, detail }) => ({
        postId: String(detail.postId), inventory: structuredClone(inventory),
      })),
      heldDetailObservations: heldRows.map(({ detail }) => ({
        postId: String(detail.postId), detail: structuredClone(detail),
      })),
    };
  };
  if (mode === "verified_subset_empty_title") {
    const titleObservations: DaumEmptyTitleSubsetProof["titleObservations"] = rows
      .filter(row => "emptyRequiredTitle" in row.detail).map(row => [String(row.detail.postId), 1, "empty"]);
    const empty = new Set(titleObservations.map(row => row[0]));
    const held: DaumEmptyTitleSubsetProof["held"] = members
      .filter(row => empty.has(row[0]) || row[7] === "unknown")
      .map(row => [row[0], empty.has(row[0]) ? "empty_required_property_title" : "unknown_inventory_and_detail_tenure"]);
    return {
      schema: "daum-verified-subset-v2", ...identity, kind: "verified_subset_with_holds", ...base,
      subsetPolicy: "tenure-and-empty-title-v2", cohort: members, details, titleObservations,
      held, ...heldObservations(held),
    };
  }
  if (mode === "verified_subset") {
    const held: DaumVerifiedSubsetInventoryProof["held"] = members.filter(row => row[7] === "unknown")
      .map(row => [row[0], "unknown_inventory_and_detail_tenure"]);
    return {
      schema: "daum-verified-subset-v1", ...identity, kind: "verified_subset_with_holds", ...base,
      cohort: members, details,
      held, ...heldObservations(held),
    };
  }
  const complete: DaumCurrentInventoryProof["cohort"] = members.map(row => {
    const [post, url, key, inventory, inventoryText, detail, detailText, resolved] = row;
    if (resolved === "unknown") throw new Error("DAUM strict proof has unresolved tenure");
    return [post, url, key, inventory, inventoryText, detail, detailText, resolved];
  });
  return { schema: "daum-strict-detail-v1", ...identity, kind: "complete_current_inventory", ...base, cohort: complete, details };
}
