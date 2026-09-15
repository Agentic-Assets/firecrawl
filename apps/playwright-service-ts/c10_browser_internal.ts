import { createHash, createHmac, timingSafeEqual } from "node:crypto";

export const C10_BROWSER_INTERNAL_PATH = "/internal/c10/browser-execute";

const TOKEN_PURPOSE = "cre-capacity-c10-browser-arm-v1";
const REQUEST_PURPOSE = "cre-capacity-c10-browser-request-v1";
const SHA256 = /^[0-9a-f]{64}$/;

export type C10SidecarCard = {
  id: string;
  sourceKey: string;
  stage: "enumeration" | "member";
  method: "GET" | "POST";
  url: string;
  allowedHost: string;
  headers: Record<string, string>;
  contentType: "application/json" | null;
  body: string | null;
  browserBootstrapUrl: string;
  cacheMode: "no-store";
  timeoutMs: number;
  maxBytes: number;
  bodySha256: string | null;
};

export type C10SidecarInput = {
  sourceKey: string;
  armSha256: string;
  tokenId: string;
  tokenSha256: string;
  cardSha256: string;
  card: C10SidecarCard;
};

function canonical(value: unknown): unknown {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("C10 value must be finite");
    return Object.is(value, -0) ? 0 : value;
  }
  if (Array.isArray(value)) return value.map(canonical);
  if (!value || typeof value !== "object") throw new Error("C10 value is unsupported");
  const output: Record<string, unknown> = {};
  for (const key of Object.keys(value as Record<string, unknown>).sort()) {
    const nested = (value as Record<string, unknown>)[key];
    if (nested === undefined) throw new Error("C10 value cannot omit fields");
    output[key] = canonical(nested);
  }
  return output;
}

function canonicalJson(value: unknown): string {
  return JSON.stringify(canonical(value)).replace(
    /[^\u0000-\u007f]/g,
    (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function hmac(secret: string, purpose: string, fields: readonly string[]): string {
  return createHmac("sha256", secret)
    .update([purpose, ...fields].join("\u0000"), "utf8")
    .digest("hex");
}

function exactKeys(value: unknown, expected: readonly string[]): value is Record<string, unknown> {
  return value !== null
    && typeof value === "object"
    && !Array.isArray(value)
    && Object.keys(value).sort().join(",") === [...expected].sort().join(",");
}

function requireDigest(value: unknown, label: string): string {
  if (typeof value !== "string" || !SHA256.test(value)) throw new Error(`${label} must be a SHA-256 digest`);
  return value;
}

function requireString(value: unknown, label: string, maximum = 4_096): string {
  if (typeof value !== "string" || value.length === 0 || value.length > maximum) {
    throw new Error(`${label} is invalid`);
  }
  return value;
}

function requirePositiveInteger(value: unknown, label: string): number {
  if (!Number.isInteger(value) || (value as number) < 1) {
    throw new Error(`${label} is invalid`);
  }
  return value as number;
}

function requireNullableString(value: unknown, label: string, maximum = 4_096): string | null {
  if (value === null) return null;
  return requireString(value, label, maximum);
}

function cardFrom(value: unknown, sourceKey: string): C10SidecarCard {
  if (!exactKeys(value, [
    "allowedHost", "body", "bodySha256", "browserBootstrapUrl", "cacheMode", "contentType", "headers",
    "id", "maxBytes", "method", "sourceKey", "stage", "timeoutMs", "url",
  ])) {
    throw new Error("C10 browser card schema is invalid");
  }
  if (
    value.sourceKey !== sourceKey
    || (value.stage !== "enumeration" && value.stage !== "member")
    || (value.method !== "GET" && value.method !== "POST")
    || value.cacheMode !== "no-store"
    || !Number.isInteger(value.timeoutMs)
    || (value.timeoutMs as number) < 1
    || !Number.isInteger(value.maxBytes)
    || (value.maxBytes as number) < 1
  ) {
    throw new Error("C10 browser card is not executable");
  }
  const url = new URL(requireString(value.url, "C10 browser URL", 2_048));
  const bootstrap = new URL(requireString(value.browserBootstrapUrl, "C10 browser bootstrap URL", 2_048));
  if (
    url.protocol !== "https:"
    || bootstrap.protocol !== "https:"
    || typeof value.allowedHost !== "string"
    || url.host !== value.allowedHost
    || bootstrap.host !== value.allowedHost
    || bootstrap.origin !== url.origin
    || url.hash
    || bootstrap.hash
  ) {
    throw new Error("C10 browser card is outside its reviewed origin");
  }
  if (!value.headers || typeof value.headers !== "object" || Array.isArray(value.headers)) {
    throw new Error("C10 browser headers are invalid");
  }
  const headers: Record<string, string> = {};
  for (const [name, header] of Object.entries(value.headers)) {
    if (!/^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$/.test(name) || typeof header !== "string" || header.length > 4_096) {
      throw new Error("C10 browser header is invalid");
    }
    headers[name] = header;
  }
  if (value.method === "GET" && (value.body !== null || value.contentType !== null || value.bodySha256 !== null)) {
    throw new Error("C10 GET browser card cannot carry a body");
  }
  if (value.method === "POST") {
    if (typeof value.body !== "string" || value.contentType !== "application/json" || digest(value.body) !== value.bodySha256) {
      throw new Error("C10 POST browser card body is invalid");
    }
  }
  const stage = value.stage as C10SidecarCard["stage"];
  const method = value.method as C10SidecarCard["method"];
  const contentType = value.contentType as C10SidecarCard["contentType"];
  return {
    id: requireString(value.id, "C10 browser card id", 81), sourceKey, stage, method,
    url: url.toString(), allowedHost: requireString(value.allowedHost, "C10 browser allowed host", 255), headers, contentType,
    body: requireNullableString(value.body, "C10 browser body"), browserBootstrapUrl: bootstrap.toString(), cacheMode: "no-store",
    timeoutMs: requirePositiveInteger(value.timeoutMs, "C10 browser timeout"),
    maxBytes: requirePositiveInteger(value.maxBytes, "C10 browser byte limit"),
    bodySha256: value.bodySha256 === null ? null : requireDigest(value.bodySha256, "C10 browser body"),
  };
}

/** Parse the one-card private sidecar message. This is not a Firecrawl public request schema. */
export function parseC10SidecarInput(value: unknown): C10SidecarInput {
  if (!exactKeys(value, ["armSha256", "card", "cardSha256", "sourceKey", "tokenId", "tokenSha256"])) {
    throw new Error("C10 browser request schema is invalid");
  }
  const sourceKey = requireString(value.sourceKey, "C10 source key", 81);
  const input = {
    sourceKey,
    armSha256: requireDigest(value.armSha256, "C10 arm"),
    tokenId: requireString(value.tokenId, "C10 token id", 128),
    tokenSha256: requireDigest(value.tokenSha256, "C10 token"),
    cardSha256: requireDigest(value.cardSha256, "C10 card"),
    card: cardFrom(value.card, sourceKey),
  };
  if (digest(canonicalJson(input.card)) !== input.cardSha256) {
    throw new Error("C10 browser request card digest is invalid");
  }
  return input;
}

/** Verify a card-specific HMAC without ever returning the coordinator secret. */
export function verifyC10SidecarAuthorization(
  secret: string | undefined,
  input: C10SidecarInput,
  authorization: string | undefined,
): boolean {
  if (!secret || secret.length < 32 || !authorization || !/^[0-9a-f]{64}$/.test(authorization)) return false;
  const expectedToken = digest(hmac(secret, TOKEN_PURPOSE, [input.sourceKey, input.armSha256, input.tokenId]));
  const expectedAuthorization = hmac(secret, REQUEST_PURPOSE, [input.sourceKey, input.armSha256, input.tokenId, input.cardSha256]);
  return input.tokenSha256 === expectedToken
    && authorization.length === expectedAuthorization.length
    && timingSafeEqual(Buffer.from(authorization), Buffer.from(expectedAuthorization));
}
