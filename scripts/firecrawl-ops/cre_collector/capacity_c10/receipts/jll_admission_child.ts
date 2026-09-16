/**
 * Narrow framed child for the controller-owned JLL admission collection.
 *
 * This process has neither a browser endpoint nor a filesystem root.  Every
 * provider attempt and every private-artifact write is an authenticated-ish
 * (nonce-bound by the parent session) request back to its owning controller.
 * Stdout is protocol only.
 */
import readline from "node:readline";

import { C10ReceiptError, type ReceiptBinding } from "./contracts.js";
import type { ReceiptArtifactStore, SealedArtifact } from "./private_store.js";
import type { DirectProviderTransport, RequestCard, TransportResponse } from "./transport.js";
import { allowlistedCards, SourceBoundOneShotTransport } from "./transport.js";
import { collectJllAdmissionReceipts, RecordingReceiptStore, sealJllAdmissionManifest } from "./strict_detail/jll_admission.js";
import { jllEnumerationCard, type JllReceiptMember } from "./strict_detail/jll.js";

type Frame = Readonly<Record<string, unknown>>;
const PROTOCOL = "c10-jll-admission-rpc-v1";
const MAX_FRAME_BYTES = 8 * 1024 * 1024;
let nextId = 1;
const pending = new Map<string, (frame: Frame) => void>();

function fail(message: string): never { throw new C10ReceiptError(message); }
function send(frame: Frame): void {
  const text = JSON.stringify(frame);
  if (Buffer.byteLength(text, "utf8") > MAX_FRAME_BYTES) fail("admission RPC frame exceeds its bound");
  process.stdout.write(`${text}\n`);
}
function request(type: "execute" | "seal", payload: Frame): Promise<Frame> {
  const id = String(nextId++);
  send({ protocol: PROTOCOL, type, id, ...payload });
  return new Promise((resolve) => pending.set(id, resolve));
}
function artifact(value: unknown): SealedArtifact {
  if (!value || typeof value !== "object") fail("controller artifact response is invalid");
  const record = value as Record<string, unknown>;
  if (typeof record.name !== "string" || !/^[a-z0-9][a-z0-9-]*-[a-f0-9]{64}\.sealed$/.test(record.name)
    || typeof record.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(record.sha256)
    || !Number.isInteger(record.bytes) || Number(record.bytes) < 1) fail("controller artifact response is invalid");
  return Object.freeze({ name: record.name, sha256: record.sha256, bytes: Number(record.bytes) });
}
class ControllerStore implements ReceiptArtifactStore {
  async sealJson(stem: string, value: unknown): Promise<SealedArtifact> {
    const reply = await request("seal", { encoding: "json", stem, value });
    if (reply.ok !== true) fail("controller refused a private JSON artifact");
    return artifact(reply.artifact);
  }
  async sealBytes(stem: string, value: Uint8Array): Promise<SealedArtifact> {
    const reply = await request("seal", { encoding: "base64", stem, bodyBase64: Buffer.from(value).toString("base64") });
    if (reply.ok !== true) fail("controller refused a private byte artifact");
    return artifact(reply.artifact);
  }
}
class ControllerTransport implements DirectProviderTransport {
  async execute(card: Readonly<RequestCard>): Promise<TransportResponse> {
    const reply = await request("execute", { card });
    if (reply.ok !== true || !reply.response || typeof reply.response !== "object") fail("controller refused the source-bound card");
    const value = reply.response as Record<string, unknown>;
    if (typeof value.bodyBase64 !== "string") fail("controller response body is invalid");
    const body = Buffer.from(value.bodyBase64, "base64");
    return {
      status: Number(value.status), finalUrl: String(value.finalUrl), redirectCount: Number(value.redirectCount),
      elapsedMs: Number(value.elapsedMs), challengeDetected: value.challengeDetected === true,
      body, contentType: typeof value.contentType === "string" ? value.contentType : null,
      providerAttempts: Number(value.providerAttempts), cacheMode: value.cacheMode === "no-store" ? "no-store" : "invalid" as never,
      trustedBrowserEvidence: value.trustedBrowserEvidence as TransportResponse["trustedBrowserEvidence"],
    };
  }
}
function members(value: unknown): readonly JllReceiptMember[] {
  if (!Array.isArray(value) || value.length !== 16) fail("controller admission members are invalid");
  return Object.freeze(value.map((entry, index) => {
    if (!entry || typeof entry !== "object") fail("controller admission member is invalid");
    const item = entry as Record<string, unknown>;
    if (item.key !== `jll-${index + 1}` || typeof item.providerId !== "string" || typeof item.canonicalUrl !== "string") fail("controller admission member is invalid");
    return Object.freeze({ key: item.key, providerId: item.providerId, canonicalUrl: item.canonicalUrl });
  }));
}
function binding(value: unknown): ReceiptBinding {
  if (!value || typeof value !== "object") fail("controller receipt binding is invalid");
  const item = value as Record<string, unknown>;
  const required = ["planSha256", "cohortSha256", "policySha256", "sourceSha256", "armSha256", "implementationSha256"];
  if (required.some((key) => typeof item[key] !== "string" || !/^[a-f0-9]{64}$/.test(item[key] as string))) fail("controller receipt binding is invalid");
  return item as unknown as ReceiptBinding;
}
async function run(init: Frame): Promise<void> {
  if (init.protocol !== PROTOCOL || init.type !== "init" || typeof init.receiptRoot !== "string" || typeof init.adapterImplementationSha256 !== "string") fail("controller init is invalid");
  const store = new RecordingReceiptStore(new ControllerStore());
  const transport = new SourceBoundOneShotTransport("jll", binding(init.binding), allowlistedCards("jll", [jllEnumerationCard({ transaction: "sale", propertyType: "office", page: 1 })]), store, new ControllerTransport());
  const set = await collectJllAdmissionReceipts({ transport }, members(init.members));
  const manifest = await sealJllAdmissionManifest({ transport }, init.receiptRoot, init.adapterImplementationSha256, set);
  send({ protocol: PROTOCOL, type: "result", ok: true, manifest, receiptSetSha256: set.receiptSetSha256, artifacts: store.artifacts() });
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
let initialized = false;
input.on("line", (line) => {
  void (async () => {
    if (Buffer.byteLength(line, "utf8") > MAX_FRAME_BYTES) fail("admission RPC input exceeds its bound");
    const frame = JSON.parse(line) as Frame;
    if (!initialized) { initialized = true; await run(frame); return; }
    if (frame.protocol !== PROTOCOL || frame.type !== "reply" || typeof frame.id !== "string") fail("controller reply is invalid");
    const resolve = pending.get(frame.id);
    if (!resolve) fail("controller reply does not match an outstanding request");
    pending.delete(frame.id); resolve(frame);
  })().catch((error) => { send({ protocol: PROTOCOL, type: "result", ok: false, error: error instanceof Error ? error.message : "admission child failed" }); process.exitCode = 1; input.close(); });
});
