import assert from "node:assert/strict";
import { createHash, createHmac } from "node:crypto";
import test from "node:test";

import { parseC10SidecarInput, verifyC10SidecarAuthorization } from "./c10_browser_internal";

const secret = "c10-sidecar-test-secret-material-which-is-long-enough";
const armSha256 = "a".repeat(64);
const tokenId = "token-1";

function hmac(purpose: string, fields: readonly string[]) {
  return createHmac("sha256", secret).update([purpose, ...fields].join("\u0000"), "utf8").digest("hex");
}

function input() {
  const card = {
    id: "card-1", sourceKey: "source-1", stage: "enumeration", method: "POST",
    url: "https://example.test/api/list", allowedHost: "example.test", headers: { "content-type": "application/json" },
    contentType: "application/json", body: '{"page":1}', browserBootstrapUrl: "https://example.test/",
    cacheMode: "no-store", timeoutMs: 1_000, maxBytes: 1_024,
    bodySha256: createHash("sha256").update('{"page":1}').digest("hex"),
  };
  const canonical = (value: unknown): unknown => {
    if (value === null || typeof value === "string" || typeof value === "boolean") return value;
    if (typeof value === "number") return Object.is(value, -0) ? 0 : value;
    if (Array.isArray(value)) return value.map(canonical);
    const output: Record<string, unknown> = {};
    for (const key of Object.keys(value as Record<string, unknown>).sort()) output[key] = canonical((value as Record<string, unknown>)[key]);
    return output;
  };
  const cardSha256 = createHash("sha256").update(JSON.stringify(canonical(card))).digest("hex");
  const tokenSha256 = createHash("sha256").update(hmac("cre-capacity-c10-browser-arm-v1", ["source-1", armSha256, tokenId])).digest("hex");
  return { sourceKey: "source-1", armSha256, tokenId, tokenSha256, cardSha256, card };
}

test("C10 sidecar accepts exactly one authenticated, reviewed no-store browser card", () => {
  const value = input();
  const parsed = parseC10SidecarInput(value);
  const authorization = hmac("cre-capacity-c10-browser-request-v1", [parsed.sourceKey, parsed.armSha256, parsed.tokenId, parsed.cardSha256]);
  assert.equal(verifyC10SidecarAuthorization(secret, parsed, authorization), true);
  assert.equal(parsed.card.browserBootstrapUrl, "https://example.test/");
});

test("C10 sidecar rejects altered cards, cache drift, and forged authorization", () => {
  const value = input();
  assert.throws(() => parseC10SidecarInput({ ...value, card: { ...value.card, cacheMode: "default" } }), /not executable/);
  assert.throws(() => parseC10SidecarInput({ ...value, card: { ...value.card, url: "https://other.test/api/list" } }), /reviewed origin/);
  const parsed = parseC10SidecarInput(value);
  assert.equal(verifyC10SidecarAuthorization(secret, parsed, "0".repeat(64)), false);
  assert.equal(verifyC10SidecarAuthorization(undefined, parsed, "0".repeat(64)), false);
});
