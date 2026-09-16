/** Bounded JLL-only receipt collection for the reviewed C10 admission lane. */
import {
  C10ReceiptError,
  canonicalSha256,
  sha256,
  type PublicReceipt,
} from "../contracts.js";
import { requireReceiptSource, sealStageReceipt, type ReceiptProducerContext } from "../producer.js";
import type { ReceiptArtifactStore, SealedArtifact } from "../private_store.js";
import {
  jllDetailProjection,
  jllEnumerationCard,
  jllMemberCard,
  type JllReceiptMember,
} from "./jll.js";
import { normalizedJllListingUrl, parseJllGraphqlSearchEnvelope } from "../../../sources/pure/jll-receipt.js";

export const JLL_ADMISSION_MEMBER_COUNT = 16;
export const JLL_ADMISSION_SELECTION_RULE = "jll-canonical-url-lexicographic-v1";

export interface JllAdmissionSelection {
  readonly rule: typeof JLL_ADMISSION_SELECTION_RULE;
  readonly candidateCount: number;
  readonly selectedMembers: readonly JllReceiptMember[];
  readonly digest: string;
}

export interface JllAdmissionReceiptSet {
  readonly schemaVersion: 1;
  readonly kind: "cre_capacity_c10_jll_v1_receipt_set";
  readonly members: readonly JllReceiptMember[];
  readonly selection: JllAdmissionSelection;
  readonly enumeration: PublicReceipt;
  readonly memberReceipts: readonly PublicReceipt[];
  /** Pins the exact source-owned enumeration body and ordered member routes. */
  readonly collectionIntentSha256: string;
  readonly artifacts: readonly SealedArtifact[];
  readonly receiptSetSha256: string;
}

/** Controller-only wrapper which retains every producer-sealed artifact index. */
export class RecordingReceiptStore implements ReceiptArtifactStore {
  private readonly sealed: SealedArtifact[] = [];

  constructor(private readonly delegate: ReceiptArtifactStore) {}

  async sealJson(stem: string, value: unknown): Promise<SealedArtifact> {
    return this.record(await this.delegate.sealJson(stem, value));
  }

  async sealBytes(stem: string, value: Uint8Array): Promise<SealedArtifact> {
    return this.record(await this.delegate.sealBytes(stem, value));
  }

  artifacts(): readonly SealedArtifact[] {
    return Object.freeze(this.sealed.map((artifact) => Object.freeze({ ...artifact })));
  }

  private record(artifact: SealedArtifact): SealedArtifact {
    if (this.sealed.some((prior) => prior.name === artifact.name)) {
      throw new C10ReceiptError("JLL admission artifact index has a duplicate name");
    }
    this.sealed.push(artifact);
    return artifact;
  }
}

/**
 * `jll-canonical-url-lexicographic-v1`, mirrored exactly by the Python
 * controller and offline manifest validator (shared golden vectors pin both).
 *
 * A candidate is admissible only when its provider id is a string of ASCII
 * digits and its `pageUrl` is one listing slug, optionally absolute on the JLL
 * origin, optionally with one trailing slash, query, or fragment.  That grammar
 * makes canonicalization language-independent: drop query/fragment/trailing
 * slash and prefix the JLL origin.  Any malformed or duplicate candidate
 * rejects the whole enumeration.  Routes are ordered by UTF-16 code unit (the
 * canonical routes are ASCII, so this equals Python code-point order), never
 * by locale collation, and the first sixteen are selected.
 */
const JLL_ADMISSION_ROUTE = /^(?:https:\/\/property\.jll\.com)?\/listings\/([A-Za-z0-9][A-Za-z0-9._~-]*)\/?(?:[?#][^\n\r\u2028\u2029]*)?$/;
const JLL_ADMISSION_PROVIDER_ID = /^[0-9]+$/;

export function selectJllAdmissionMembers(payload: unknown): JllAdmissionSelection {
  let parsed: ReturnType<typeof parseJllGraphqlSearchEnvelope>;
  try {
    parsed = parseJllGraphqlSearchEnvelope(payload);
  } catch {
    throw new C10ReceiptError("JLL admission enumeration envelope is invalid");
  }
  const ids = new Set<string>();
  const routes = new Set<string>();
  const candidates = parsed.items.map((item) => {
    const providerId = item.id;
    const pageUrl = item.pageUrl;
    const match = typeof pageUrl === "string" ? JLL_ADMISSION_ROUTE.exec(pageUrl) : null;
    if (typeof providerId !== "string" || !JLL_ADMISSION_PROVIDER_ID.test(providerId) || match === null) {
      throw new C10ReceiptError("JLL admission enumeration contains a noncanonical candidate");
    }
    const canonicalUrl = `https://property.jll.com/listings/${match[1]}`;
    if (normalizedJllListingUrl(pageUrl as string) !== canonicalUrl) {
      throw new C10ReceiptError("JLL admission enumeration contains a noncanonical candidate");
    }
    if (ids.has(providerId) || routes.has(canonicalUrl)) {
      throw new C10ReceiptError("JLL admission enumeration contains duplicate candidates");
    }
    ids.add(providerId); routes.add(canonicalUrl);
    return { providerId, canonicalUrl };
  }).sort((left, right) => (left.canonicalUrl < right.canonicalUrl ? -1 : left.canonicalUrl > right.canonicalUrl ? 1 : 0));
  if (candidates.length < JLL_ADMISSION_MEMBER_COUNT) {
    throw new C10ReceiptError("JLL admission enumeration has insufficient canonical candidates");
  }
  const selectedMembers = Object.freeze(candidates.slice(0, JLL_ADMISSION_MEMBER_COUNT).map((candidate, index) => Object.freeze({
    key: `jll-${index + 1}`, providerId: candidate.providerId, canonicalUrl: candidate.canonicalUrl,
  })));
  const digest = canonicalSha256({ rule: JLL_ADMISSION_SELECTION_RULE, memberRoutes: selectedMembers.map((member) => member.canonicalUrl), providerIds: selectedMembers.map((member) => member.providerId) });
  return Object.freeze({ rule: JLL_ADMISSION_SELECTION_RULE, candidateCount: candidates.length, selectedMembers, digest });
}

function selectionFromEnumeration(body: Uint8Array): JllAdmissionSelection {
  let payload: unknown;
  try {
    payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
  } catch {
    throw new C10ReceiptError("JLL admission enumeration envelope is invalid");
  }
  return selectJllAdmissionMembers(payload);
}

function collectionIntentSha256(members: readonly JllReceiptMember[], selection: JllAdmissionSelection): string {
  const enumeration = jllEnumerationCard({ transaction: "sale", propertyType: "office", page: 1 });
  return canonicalSha256({
    sourceKey: "jll",
    enumerationBodySha256: sha256(enumeration.body ?? ""),
    selectionRule: selection.rule,
    selectionDigest: selection.digest,
    memberRoutes: members.map((member) => member.canonicalUrl),
  });
}

function immutableMembers(members: readonly JllReceiptMember[]): readonly JllReceiptMember[] {
  if (members.length !== JLL_ADMISSION_MEMBER_COUNT) {
    throw new C10ReceiptError("JLL admission requires exactly sixteen members");
  }
  const keys = new Set<string>();
  const providerIds = new Set<string>();
  const routes = new Set<string>();
  const copy = members.map((member, index) => {
    if (
      member.key !== `jll-${index + 1}`
      || !/^[0-9]+$/.test(member.providerId)
      || typeof member.canonicalUrl !== "string"
      || !member.canonicalUrl.startsWith("https://property.jll.com/listings/")
    ) {
      throw new C10ReceiptError("JLL admission member identity is invalid");
    }
    if (keys.has(member.key) || providerIds.has(member.providerId) || routes.has(member.canonicalUrl)) {
      throw new C10ReceiptError("JLL admission members must have unique identities");
    }
    keys.add(member.key);
    providerIds.add(member.providerId);
    routes.add(member.canonicalUrl);
    return Object.freeze({
      key: member.key,
      providerId: member.providerId,
      canonicalUrl: member.canonicalUrl,
    });
  });
  return Object.freeze(copy);
}

/**
 * Drive only the source-owned JLL receipt producer through an already-issued
 * one-shot transport.  This module has no URL, fetch, browser, or filesystem
 * construction seam: the production controller owns those authorities.
 */
export async function collectJllAdmissionReceipts(context: ReceiptProducerContext): Promise<JllAdmissionReceiptSet> {
  if (!(context.transport.store instanceof RecordingReceiptStore)) {
    throw new C10ReceiptError("JLL admission requires a controller-owned recording receipt store");
  }
  const transport = requireReceiptSource(context, "jll");
  transport.assertInitialCards([jllEnumerationCard({ transaction: "sale", propertyType: "office", page: 1 })]);
  const enumerationEvent = await transport.oneShot("jll-enumeration-0", (response) => ({ selection: selectionFromEnumeration(response.body) }));
  const selection = enumerationEvent.projection.selection as JllAdmissionSelection;
  const fixedMembers = immutableMembers(selection.selectedMembers);
  for (const [index, member] of fixedMembers.entries()) {
    await transport.appendFrom(enumerationEvent, {
      sourceKey: "jll", stage: "member", maximumCards: JLL_ADMISSION_MEMBER_COUNT,
      create: (parent, coordinate: { member: JllReceiptMember; index: number }) => jllMemberCard(parent, coordinate.member, coordinate.index, coordinate.member.canonicalUrl),
    }, { member, index });
  }
  const graph = await transport.freezeMemberGraph();
  const enumeration = await sealStageReceipt(context, "enumeration", null, { selection, memberGraph: graph });
  const memberReceipts: PublicReceipt[] = [];
  for (const [index, member] of fixedMembers.entries()) {
    const event = await transport.oneShot(`jll-member-${index}`, jllDetailProjection(member, member.canonicalUrl));
    memberReceipts.push(await sealStageReceipt(context, "member", member.key, { member: { canonicalUrl: member.canonicalUrl, providerId: member.providerId, sourceProjection: event.projection }, memberCardId: `jll-member-${index}`, memberProjectionSha256: event.projectionSha256 }));
  }
  if (
    enumeration.stage !== "enumeration"
    || enumeration.memberKey !== null
    || memberReceipts.some((receipt, index) =>
      receipt.stage !== "member" || receipt.memberKey !== fixedMembers[index]?.key,
    )
  ) {
    throw new C10ReceiptError("JLL admission receipt stages do not match the sealed cohort");
  }
  const unsigned = {
    schemaVersion: 1 as const,
    kind: "cre_capacity_c10_jll_v1_receipt_set" as const,
    members: fixedMembers,
    selection,
    enumeration,
    memberReceipts: Object.freeze(memberReceipts),
    collectionIntentSha256: collectionIntentSha256(fixedMembers, selection),
    artifacts: context.transport.store.artifacts(),
  };
  return Object.freeze({ ...unsigned, receiptSetSha256: canonicalSha256(unsigned) });
}

/**
 * Controller-owned handoff: serialize the source receipt set into one sealed
 * manifest. The receipt root is not a CLI option; the production controller
 * supplies its retained private-root identity.
 */
export async function sealJllAdmissionManifest(
  context: ReceiptProducerContext,
  receiptRoot: string,
  adapterImplementationSha256: string,
  set: JllAdmissionReceiptSet,
): Promise<SealedArtifact> {
  if (!(context.transport.store instanceof RecordingReceiptStore)) {
    throw new C10ReceiptError("JLL admission manifest requires a controller-owned recording receipt store");
  }
  if (!/^\/[\s\S]+/.test(receiptRoot) || !/^[a-f0-9]{64}$/.test(adapterImplementationSha256)) {
    throw new C10ReceiptError("JLL admission manifest controller inputs are invalid");
  }
  const unsigned = {
    schema_version: 1,
    kind: "cre_capacity_c10_jll_v1_receipt_manifest",
    receipt_root: receiptRoot,
    collection_intent: {
      source_key: "jll",
      enumeration: { transaction: "sale", property_type: "office", page: 1 },
      member_count: JLL_ADMISSION_MEMBER_COUNT,
      selection_rule: JLL_ADMISSION_SELECTION_RULE,
      no_write: set.enumeration.noWrite,
    },
    collection_intent_sha256: set.collectionIntentSha256,
    selection_digest: set.selection.digest,
    members: set.members.map((member) => ({ key: member.key, provider_id: member.providerId, canonical_url: member.canonicalUrl })),
    enumeration: set.enumeration,
    member_receipts: set.memberReceipts,
    artifacts: set.artifacts,
    adapter_implementation_sha256: adapterImplementationSha256,
    no_write: set.enumeration.noWrite,
  };
  return context.transport.store.sealJson("jll-admission-manifest", {
    ...unsigned,
    manifest_sha256: canonicalSha256(unsigned),
  });
}
