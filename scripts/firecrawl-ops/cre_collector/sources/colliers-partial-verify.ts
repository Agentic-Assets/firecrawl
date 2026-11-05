/** Re-derive one partial Colliers row from its captured paired page and detail. */
import { createHash } from "node:crypto";
import { isDeepStrictEqual } from "node:util";
import { enrichColliersCard, groupColliersMapLocations, parseColliersCards } from "./colliers.js";
import { replayColliersPage } from "./pure/colliers-partial-replay.js";
import { assertPartialNativeIdentity } from "./colliers-partial-native-id.js";

type Binding = { generation: string; attempt: number; startedAt: string; finishedAt: string; detailFinishedAt: string };
type Page = { start: number; page: number; pageSize: number; observedAt: string; list: string; map: string; cards: any[] };
const hash = (text: string) => createHash("sha256").update(text, "utf8").digest("hex");

/** Caller must bind both sealed sidecars, code, target and owner before admission. */
export async function deriveColliersPartialDetail(page: Page, source: any, decodedDetail: string, binding: Binding, total: number, available: number) {
  const sourceKeys = ["generation", "attempt", "start", "page", "pageSize", "offset", "inventoryObservedAt",
    "detailObservedAt", "mapProjectId", "listSha256", "mapSha256", "detailTokenSha256", "decodedDetailSha256", "nativeSha256"].sort().join(",");
  if (!source || typeof source !== "object" || Array.isArray(source) || Object.keys(source).sort().join(",") !== sourceKeys ||
      typeof decodedDetail !== "string") throw new Error("partial detail record invalid");
  if (source.generation !== binding.generation || source.attempt !== binding.attempt || source.start !== page.start ||
      source.page !== page.page || source.pageSize !== page.pageSize || source.inventoryObservedAt !== page.observedAt ||
      !Number.isSafeInteger(source.offset) || source.offset < 0 || source.offset >= page.pageSize ||
      source.listSha256 !== hash(page.list) || source.mapSha256 !== hash(page.map) ||
      source.decodedDetailSha256 !== hash(decodedDetail) || typeof source.nativeSha256 !== "string" ||
      !/^[0-9a-f]{64}$/.test(source.nativeSha256)) throw new Error("partial source binding differs");
  const inventoryAt = Date.parse(source.inventoryObservedAt), detailAt = Date.parse(source.detailObservedAt);
  const start = Date.parse(binding.startedAt), finish = Date.parse(binding.finishedAt), detailFinish = Date.parse(binding.detailFinishedAt);
  if (![inventoryAt, detailAt, start, finish, detailFinish].every(Number.isFinite) || inventoryAt < start || inventoryAt > finish || detailAt < start || detailAt > detailFinish || detailFinish > Date.now() + 5 * 60_000)
    throw new Error("partial observation clock differs");
  const identities = replayColliersPage(page, total, available);
  const identity = identities[source.offset];
  if (!identity || !identity.detailPv || !identity.detailUrl || source.mapProjectId !== identity.mapProjectId ||
      source.detailTokenSha256 !== hash(identity.detailPv)) throw new Error("partial page identity differs");
  const list = JSON.parse(page.list), map = JSON.parse(page.map);
  const card = parseColliersCards(list.html, groupColliersMapLocations(map.projectLocations), page.start, false)[source.offset];
  if (!card || card.mapProjectId !== identity.mapProjectId || card.detailPv !== identity.detailPv || card.detailUrl !== identity.detailUrl)
    throw new Error("partial card mapping differs");
  const detail = JSON.parse(decodedDetail);
  if (!detail || typeof detail !== "object" || Array.isArray(detail) || detail.success === false) throw new Error("partial detail invalid");
  assertPartialNativeIdentity(detail, identity.mapProjectId);
  const mapped = await enrichColliersCard(card, { fetchDetail: async () => detail, stopOnAccessFailure: true, skipBrokerRefs: true });
  const expected = { ...mapped, sourceKey: "colliers", sourceCompany: "Colliers", transactionMode: "sale", brokerIds: [],
    preserveChildCollections: true, inventoryObservedAt: source.inventoryObservedAt, detailObservedAt: source.detailObservedAt,
    freshnessProvenance: { generationId: binding.generation, detailScope: "detail_page", method: "colliers_salestracker_slp_detail", cacheDisposition: "live" },
    colliersPartialEvidence: { schema: "colliers-partial-detail-v1", ...source } };
  if (mapped?.inventoryOnly || mapped?.id !== identity.mapProjectId)
    throw new Error("partial mapped listing differs");
  return { externalId: identity.mapProjectId, position: page.start + source.offset, listing: expected };
}

export async function verifyColliersPartialDetail(page: Page, item: any, binding: Binding, total: number, available: number) {
  if (!item || typeof item !== "object" || !item.listing || !(item.nativeBytes instanceof Uint8Array) ||
      item.nativeBytes.length > 16 * 1024 * 1024 ||
      createHash("sha256").update(item.nativeBytes).digest("hex") !== item.source?.nativeSha256 ||
      new TextDecoder("utf-8", { fatal: true }).decode(item.nativeBytes) !== item.decodedDetail)
    throw new Error("partial native detail binding differs");
  const result = await deriveColliersPartialDetail(page, item.source, item.decodedDetail, binding, total, available);
  if (!isDeepStrictEqual(item.listing, result.listing)) throw new Error("partial mapped listing differs");
  return { externalId: result.externalId, position: result.position };
}
