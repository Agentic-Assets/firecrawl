import {
  C10ReceiptError,
  C10_RECEIPT_SCHEMA_VERSION,
  NO_WRITE,
  type PublicReceipt,
  sealPublicReceipt,
} from "./contracts.js";
import { type SourceBoundOneShotTransport } from "./transport.js";

export interface ReceiptProducerContext {
  readonly transport: SourceBoundOneShotTransport;
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
  const { transport } = context;
  const accounting = transport.requestAccounting();
  const requestAccounting = Object.freeze({
    logicalRequests: accounting.logicalRequests,
    attempts: accounting.attempts,
    retries: accounting.retries,
    eventsSha256: accounting.eventsSha256,
  });
  const privateArtifact = await transport.store.sealJson(`${stage}-${transport.sourceKey}`, {
    binding: transport.binding,
    sourceKey: transport.sourceKey,
    stage,
    memberKey,
    requestAccounting,
    evidence: privateEvidence,
  });
  return sealPublicReceipt({
    schemaVersion: C10_RECEIPT_SCHEMA_VERSION,
    kind: "cre_capacity_c10_private_source_receipt",
    stage,
    sourceKey: transport.sourceKey,
    memberKey,
    binding: transport.binding,
    noWrite: NO_WRITE,
    requestAccounting,
    privateArtifactSha256: privateArtifact.sha256,
  });
}

/** Reject source-specific producers before they can execute a mismatched transport. */
export function requireReceiptSource(
  context: ReceiptProducerContext,
  expectedSourceKey: string,
): SourceBoundOneShotTransport {
  if (context.transport.sourceKey !== expectedSourceKey) {
    throw new C10ReceiptError("receipt producer source binding mismatch");
  }
  return context.transport;
}
