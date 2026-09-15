import {
  C10_RECEIPT_SCHEMA_VERSION,
  NO_WRITE,
  type PublicReceipt,
  type ReceiptBinding,
  sealPublicReceipt,
} from "./contracts.js";
import { type PrivateReceiptStore } from "./private_store.js";
import { type SourceBoundOneShotTransport } from "./transport.js";

export interface ReceiptProducerContext {
  readonly sourceKey: string;
  readonly binding: ReceiptBinding;
  readonly transport: SourceBoundOneShotTransport;
  readonly store: PrivateReceiptStore;
}

export interface C10Member {
  readonly key: string;
  readonly providerId: string;
}

/** Source modules may implement only these two source-bound receipt operations. */
export interface ReceiptProducer {
  produceEnumerationReceipt(context: ReceiptProducerContext): Promise<PublicReceipt>;
  produceMemberReceipt(context: ReceiptProducerContext, member: C10Member): Promise<PublicReceipt>;
}

export async function sealStageReceipt(
  context: ReceiptProducerContext,
  stage: "enumeration" | "member",
  memberKey: string | null,
  privateEvidence: unknown,
): Promise<PublicReceipt> {
  const privateArtifact = await context.store.sealJson(`${stage}-${context.sourceKey}`, {
    binding: context.binding,
    sourceKey: context.sourceKey,
    stage,
    memberKey,
    requestAccounting: context.transport.requestAccounting(),
    evidence: privateEvidence,
  });
  return sealPublicReceipt({
    schemaVersion: C10_RECEIPT_SCHEMA_VERSION,
    kind: "cre_capacity_c10_private_source_receipt",
    stage,
    sourceKey: context.sourceKey,
    memberKey,
    binding: context.binding,
    noWrite: NO_WRITE,
    requestAccounting: context.transport.requestAccounting(),
    privateArtifactSha256: privateArtifact.sha256,
  });
}
