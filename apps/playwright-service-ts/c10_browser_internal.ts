/** C10 browser protocol v3: separated Ed25519 capability and evidence keys. */
import { createHash, sign, verify } from "node:crypto";

export const C10_BROWSER_INTERNAL_PATH = "/internal/c10/v3/browser-execute";
export const C10_PROTOCOL_VERSION = 3 as const;
const SHA256 = /^[0-9a-f]{64}$/;
const MAX_LIFETIME_MS = 120_000;
const MAX_REPLAY_ENTRIES = 4_096;

export type C10Binding = Readonly<{ planSha256: string; cohortSha256: string; cardSha256: string; manifestSha256: string; sessionSha256: string; armSha256: string; profileSha256: string }>;
export type C10SidecarCard = Readonly<{ id: string; sourceKey: string; stage: "enumeration" | "member"; method: "GET" | "POST"; url: string; allowedHost: string; headers: Record<string, string>; contentType: "application/json" | null; body: string | null; browserBootstrapUrl: string; cacheMode: "no-store"; timeoutMs: number; maxBytes: number; bodySha256: string | null }>;
export type C10CapabilityPayload = Readonly<{ protocolVersion: 3; coordinatorKeyId: string; nonce: string; expiresAtMs: number; hostDeadlineAtMs: number; cardSequence: number; sourceKey: string; binding: C10Binding }>;
export type C10SidecarInput = Readonly<{ capability: C10CapabilityPayload; card: C10SidecarCard }>;

export function canonicalJson(value: unknown): string {
  const normalize = (input: unknown): unknown => {
    if (input === null || typeof input === "string" || typeof input === "boolean") return input;
    if (typeof input === "number") { if (!Number.isFinite(input)) throw new Error("C10 JSON must be finite"); return Object.is(input, -0) ? 0 : input; }
    if (Array.isArray(input)) return input.map(normalize);
    if (!input || typeof input !== "object") throw new Error("C10 JSON is unsupported");
    const output: Record<string, unknown> = {};
    for (const key of Object.keys(input as Record<string, unknown>).sort()) { const nested = (input as Record<string, unknown>)[key]; if (nested === undefined) throw new Error("C10 JSON cannot omit fields"); output[key] = normalize(nested); }
    return output;
  };
  return JSON.stringify(normalize(value)).replace(/[^\u0000-\u007f]/g, (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`);
}
export const sha256 = (value: string | Uint8Array): string => createHash("sha256").update(value).digest("hex");
export const publicKeyId = (publicKeyPem: string): string => sha256(publicKeyPem);
function exactKeys(value: unknown, expected: readonly string[]): value is Record<string, unknown> { return value !== null && typeof value === "object" && !Array.isArray(value) && Object.keys(value).sort().join(",") === [...expected].sort().join(","); }
function digest(value: unknown, label: string): string { if (typeof value !== "string" || !SHA256.test(value)) throw new Error(`${label} must be a SHA-256 digest`); return value; }
function text(value: unknown, label: string, maximum = 4_096): string { if (typeof value !== "string" || value.length === 0 || value.length > maximum) throw new Error(`${label} is invalid`); return value; }
function positiveInteger(value: unknown, label: string): number { if (!Number.isInteger(value) || (value as number) < 1) throw new Error(`${label} is invalid`); return value as number; }
function nonnegativeInteger(value: unknown, label: string): number { if (!Number.isInteger(value) || (value as number) < 0) throw new Error(`${label} is invalid`); return value as number; }
function bindingFrom(value: unknown): C10Binding {
  const names = ["armSha256", "cardSha256", "cohortSha256", "manifestSha256", "planSha256", "profileSha256", "sessionSha256"];
  if (!exactKeys(value, names)) throw new Error("C10 binding schema is invalid");
  return Object.freeze({ planSha256: digest(value.planSha256, "C10 plan"), cohortSha256: digest(value.cohortSha256, "C10 cohort"), cardSha256: digest(value.cardSha256, "C10 card"), manifestSha256: digest(value.manifestSha256, "C10 manifest"), sessionSha256: digest(value.sessionSha256, "C10 session"), armSha256: digest(value.armSha256, "C10 arm"), profileSha256: digest(value.profileSha256, "C10 profile") });
}
function cardFrom(value: unknown, sourceKey: string): C10SidecarCard {
  const names = ["allowedHost", "body", "bodySha256", "browserBootstrapUrl", "cacheMode", "contentType", "headers", "id", "maxBytes", "method", "sourceKey", "stage", "timeoutMs", "url"];
  if (!exactKeys(value, names)) throw new Error("C10 browser card schema is invalid");
  if (value.sourceKey !== sourceKey || (value.stage !== "enumeration" && value.stage !== "member") || (value.method !== "GET" && value.method !== "POST") || value.cacheMode !== "no-store") throw new Error("C10 browser card is not executable");
  const url = new URL(text(value.url, "C10 browser URL", 2_048)), bootstrap = new URL(text(value.browserBootstrapUrl, "C10 browser bootstrap URL", 2_048));
  if (url.protocol !== "https:" || bootstrap.protocol !== "https:" || url.host !== value.allowedHost || bootstrap.host !== value.allowedHost || bootstrap.origin !== url.origin || url.hash || bootstrap.hash) throw new Error("C10 browser card is outside its reviewed origin");
  if (!value.headers || typeof value.headers !== "object" || Array.isArray(value.headers)) throw new Error("C10 browser headers are invalid");
  const headers: Record<string, string> = {};
  for (const [name, header] of Object.entries(value.headers)) { if (!/^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$/.test(name) || typeof header !== "string" || header.length > 4_096) throw new Error("C10 browser header is invalid"); headers[name] = header; }
  const contentType: "application/json" | null = value.contentType === "application/json" ? "application/json" : value.contentType === null ? null : (() => { throw new Error("C10 browser content type is invalid"); })();
  const body: string | null = typeof value.body === "string" ? value.body : value.body === null ? null : (() => { throw new Error("C10 browser body is invalid"); })();
  if (value.method === "GET" && (body !== null || contentType !== null || value.bodySha256 !== null)) throw new Error("C10 GET browser card cannot carry a body");
  if (value.method === "POST" && (body === null || contentType !== "application/json" || sha256(body) !== value.bodySha256)) throw new Error("C10 POST browser card body is invalid");
  const timeoutMs = positiveInteger(value.timeoutMs, "C10 browser timeout"), maxBytes = positiveInteger(value.maxBytes, "C10 browser byte limit");
  if (timeoutMs > 30_000 || maxBytes > 2 * 1024 * 1024) throw new Error("C10 browser card exceeds reviewed bounds");
  return Object.freeze({ id: text(value.id, "C10 browser card id", 81), sourceKey, stage: value.stage as "enumeration" | "member", method: value.method as "GET" | "POST", url: url.toString(), allowedHost: text(value.allowedHost, "C10 browser allowed host", 255), headers, contentType, body, browserBootstrapUrl: bootstrap.toString(), cacheMode: "no-store", timeoutMs, maxBytes, bodySha256: value.bodySha256 === null ? null : digest(value.bodySha256, "C10 browser body") });
}
export function parseC10SidecarInput(value: unknown): C10SidecarInput {
  if (!exactKeys(value, ["capability", "card"]) || !exactKeys(value.capability, ["binding", "cardSequence", "coordinatorKeyId", "expiresAtMs", "hostDeadlineAtMs", "nonce", "protocolVersion", "sourceKey"])) throw new Error("C10 v3 browser request schema is invalid");
  const capability = value.capability; if (capability.protocolVersion !== C10_PROTOCOL_VERSION) throw new Error("C10 browser protocol version is invalid");
  const sourceKey = text(capability.sourceKey, "C10 source key", 81);
  const parsed = Object.freeze({ protocolVersion: C10_PROTOCOL_VERSION, coordinatorKeyId: digest(capability.coordinatorKeyId, "C10 coordinator key"), nonce: text(capability.nonce, "C10 nonce", 128), expiresAtMs: positiveInteger(capability.expiresAtMs, "C10 expiry"), hostDeadlineAtMs: positiveInteger(capability.hostDeadlineAtMs, "C10 host deadline"), cardSequence: nonnegativeInteger(capability.cardSequence, "C10 card sequence"), sourceKey, binding: bindingFrom(capability.binding) });
  const card = cardFrom(value.card, sourceKey); if (sha256(canonicalJson(card)) !== parsed.binding.cardSha256) throw new Error("C10 browser card digest is invalid"); return Object.freeze({ capability: parsed, card });
}
/** Coordinator-only helper: this private key must never reach the sidecar. */
export function issueC10SidecarCapability(privateKeyPem: string, capability: C10CapabilityPayload): string { if (capability.protocolVersion !== C10_PROTOCOL_VERSION || !Number.isInteger(capability.expiresAtMs)) throw new Error("C10 v3 capability is invalid"); const payload = Buffer.from(canonicalJson(capability), "utf8").toString("base64url"); return `${payload}.${sign(null, Buffer.from(payload, "utf8"), privateKeyPem).toString("base64url")}`; }
/** Bounded ephemeral replay state. Persistence is forbidden because v3 keys rotate every lifecycle. */
export class C10SidecarCapabilityRegistry {
  private readonly consumed = new Map<string, number>();
  consume(coordinatorPublicKeyPem: string | undefined, input: C10SidecarInput, authorization: string | undefined, now = Date.now()): boolean {
    this.prune(now); if (!coordinatorPublicKeyPem || !authorization) return false; const [payload, signature, extra] = authorization.split(".");
    if (!payload || !signature || extra || !verify(null, Buffer.from(payload, "utf8"), coordinatorPublicKeyPem, Buffer.from(signature, "base64url"))) return false;
    try { const value = JSON.parse(Buffer.from(payload, "base64url").toString("utf8")); if (!exactKeys(value, ["binding", "cardSequence", "coordinatorKeyId", "expiresAtMs", "hostDeadlineAtMs", "nonce", "protocolVersion", "sourceKey"])) return false; const parsed = parseC10SidecarInput({ capability: value, card: input.card }).capability; if (parsed.coordinatorKeyId !== publicKeyId(coordinatorPublicKeyPem) || parsed.expiresAtMs <= now || parsed.hostDeadlineAtMs <= now || parsed.expiresAtMs > parsed.hostDeadlineAtMs || parsed.expiresAtMs > now + MAX_LIFETIME_MS || parsed.hostDeadlineAtMs > now + MAX_LIFETIME_MS || canonicalJson(parsed) !== canonicalJson(input.capability) || this.consumed.has(parsed.nonce)) return false; if (this.consumed.size >= MAX_REPLAY_ENTRIES) this.prune(now, true); if (this.consumed.size >= MAX_REPLAY_ENTRIES) return false; this.consumed.set(parsed.nonce, parsed.expiresAtMs); return true; } catch { return false; }
  }
  size(now = Date.now()): number { this.prune(now); return this.consumed.size; }
  private prune(now: number, force = false): void { for (const [nonce, expiry] of this.consumed) if (expiry <= now || (force && this.consumed.size >= MAX_REPLAY_ENTRIES)) this.consumed.delete(nonce); }
}
export function signC10Evidence(sidecarPrivateKeyPem: string, evidence: Record<string, unknown>): string { return sign(null, Buffer.from(canonicalJson(evidence), "utf8"), sidecarPrivateKeyPem).toString("base64url"); }
export function verifyC10Evidence(sidecarPublicKeyPem: string, evidence: Record<string, unknown>, signature: unknown): boolean { return typeof signature === "string" && verify(null, Buffer.from(canonicalJson(evidence), "utf8"), sidecarPublicKeyPem, Buffer.from(signature, "base64url")); }
