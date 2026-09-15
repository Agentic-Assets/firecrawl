import { createHash } from "node:crypto";

export const C10_RECEIPT_SCHEMA_VERSION = 1;
export const NO_WRITE = Object.freeze({
  database_writes: 0,
  cache_writes: 0,
  status_writes: 0,
  scheduler_writes: 0,
  model_or_ocr_changes: 0,
});

const SHA256 = /^[0-9a-f]{64}$/;

export class C10ReceiptError extends Error {
  override name = "C10ReceiptError";
}

export interface ReceiptBinding {
  readonly planSha256: string;
  readonly cohortSha256: string;
  readonly policySha256: string;
  readonly sourceSha256: string;
  readonly armSha256: string;
  readonly implementationSha256: string;
}

export interface RequestAccountingEvent {
  readonly cardId: string;
  readonly outcome: "accepted" | "transport_error" | "rejected";
  readonly status: number | null;
  readonly elapsedMs: number | null;
  readonly bytes: number | null;
  readonly bodySha256: string | null;
  readonly privateEventSha256: string | null;
}

export interface RequestAccounting {
  readonly logicalRequests: number;
  readonly attempts: number;
  readonly retries: 0;
  readonly eventsSha256: string;
  readonly events: readonly RequestAccountingEvent[];
}

export interface PublicReceipt {
  readonly schemaVersion: typeof C10_RECEIPT_SCHEMA_VERSION;
  readonly kind: "cre_capacity_c10_private_source_receipt";
  readonly stage: "enumeration" | "member";
  readonly sourceKey: string;
  readonly memberKey: string | null;
  readonly binding: ReceiptBinding;
  readonly noWrite: typeof NO_WRITE;
  readonly requestAccounting: RequestAccounting;
  readonly privateArtifactSha256: string;
  readonly receiptSha256: string;
}

function normalize(value: unknown): unknown {
  if (value === null || typeof value === "string" || typeof value === "boolean") {
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new C10ReceiptError("receipt JSON must be finite");
    return Object.is(value, -0) ? 0 : value;
  }
  if (Array.isArray(value)) return value.map(normalize);
  if (typeof value !== "object") throw new C10ReceiptError("receipt JSON is unsupported");
  const source = value as Record<string, unknown>;
  const result: Record<string, unknown> = {};
  for (const key of Object.keys(source).sort()) {
    if (source[key] === undefined) throw new C10ReceiptError("receipt JSON cannot omit values");
    result[key] = normalize(source[key]);
  }
  return result;
}

export function canonicalJson(value: unknown): string {
  return JSON.stringify(normalize(value)).replace(
    /[^\u0000-\u007f]/g,
    (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}

export function sha256(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

export function canonicalSha256(value: unknown): string {
  return sha256(canonicalJson(value));
}

export function requireSha256(value: string, label: string): string {
  if (!SHA256.test(value)) throw new C10ReceiptError(`${label} must be a SHA-256 digest`);
  return value;
}

export function assertBinding(binding: ReceiptBinding): ReceiptBinding {
  const expected = [
    "armSha256",
    "cohortSha256",
    "implementationSha256",
    "planSha256",
    "policySha256",
    "sourceSha256",
  ];
  if (Object.keys(binding).sort().join(",") !== expected.join(",")) {
    throw new C10ReceiptError("receipt binding has an unexpected key set");
  }
  for (const [key, value] of Object.entries(binding)) requireSha256(value, key);
  return Object.freeze({ ...binding });
}

export function sealPublicReceipt(
  unsigned: Omit<PublicReceipt, "receiptSha256">,
): PublicReceipt {
  if (unsigned.kind !== "cre_capacity_c10_private_source_receipt") {
    throw new C10ReceiptError("unsupported receipt kind");
  }
  assertBinding(unsigned.binding);
  requireSha256(unsigned.privateArtifactSha256, "privateArtifactSha256");
  const receiptSha256 = canonicalSha256(unsigned);
  return Object.freeze({ ...unsigned, receiptSha256 });
}
