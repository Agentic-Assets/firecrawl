/** Bounded JLL-only receipt collection for the reviewed C10 admission lane. */
import {
  C10ReceiptError,
  canonicalSha256,
  sha256,
  type PublicReceipt,
} from "../contracts.js";
import type { ReceiptProducerContext } from "../producer.js";
import type { ReceiptArtifactStore, SealedArtifact } from "../private_store.js";
import {
  createJllReceiptProducer,
  jllEnumerationCard,
  type JllReceiptMember,
} from "./jll.js";

export const JLL_ADMISSION_MEMBER_COUNT = 16;

export interface JllAdmissionReceiptSet {
  readonly schemaVersion: 1;
  readonly kind: "cre_capacity_c10_jll_v1_receipt_set";
  readonly members: readonly JllReceiptMember[];
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

function collectionIntentSha256(members: readonly JllReceiptMember[]): string {
  const enumeration = jllEnumerationCard({ transaction: "sale", propertyType: "office", page: 1 });
  return canonicalSha256({
    sourceKey: "jll",
    enumerationBodySha256: sha256(enumeration.body ?? ""),
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
export async function collectJllAdmissionReceipts(
  context: ReceiptProducerContext,
  members: readonly JllReceiptMember[],
): Promise<JllAdmissionReceiptSet> {
  if (!(context.transport.store instanceof RecordingReceiptStore)) {
    throw new C10ReceiptError("JLL admission requires a controller-owned recording receipt store");
  }
  const fixedMembers = immutableMembers(members);
  const producer = createJllReceiptProducer({
    enumerations: [{ transaction: "sale", propertyType: "office", page: 1 }],
    members: fixedMembers,
    enumerationCards: [],
  });
  const enumeration = await producer.produceEnumerationReceipt(context);
  const memberReceipts = await Promise.all(
    fixedMembers.map((member) => producer.produceMemberReceipt(context, member)),
  );
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
    enumeration,
    memberReceipts: Object.freeze(memberReceipts),
    collectionIntentSha256: collectionIntentSha256(fixedMembers),
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
      no_write: set.enumeration.noWrite,
    },
    collection_intent_sha256: set.collectionIntentSha256,
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
