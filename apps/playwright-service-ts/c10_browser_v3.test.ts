import assert from "node:assert/strict";
import { generateKeyPairSync } from "node:crypto";
import test from "node:test";
import { C10SidecarCapabilityRegistry, canonicalJson, issueC10SidecarCapability, parseC10SidecarInput, publicKeyId, sha256 } from "./c10_browser_internal";

const pem = () => { const pair = generateKeyPairSync("ed25519"); return { privateKeyPem: pair.privateKey.export({ type: "pkcs8", format: "pem" }).toString(), publicKeyPem: pair.publicKey.export({ type: "spki", format: "pem" }).toString() }; };
test("v3 verifies an asymmetric capability once and prunes expiry", () => {
  const coordinator = pem(), other = pem(); const card = { id: "one", sourceKey: "jll", stage: "member" as const, method: "GET" as const, url: "https://example.test/a", allowedHost: "example.test", headers: {}, contentType: null, body: null, browserBootstrapUrl: "https://example.test/", cacheMode: "no-store" as const, timeoutMs: 1_000, maxBytes: 1_024, bodySha256: null };
  const capability = { protocolVersion: 3 as const, coordinatorKeyId: publicKeyId(coordinator.publicKeyPem), nonce: "v3-once", expiresAtMs: 10_100, sourceKey: "jll", binding: { planSha256: "a".repeat(64), cohortSha256: "b".repeat(64), cardSha256: sha256(canonicalJson(card)), manifestSha256: "c".repeat(64), sessionSha256: "d".repeat(64), armSha256: "e".repeat(64), profileSha256: "f".repeat(64) } };
  const input = parseC10SidecarInput({ capability, card }); const signed = issueC10SidecarCapability(coordinator.privateKeyPem, capability); const registry = new C10SidecarCapabilityRegistry();
  assert.equal(registry.consume(coordinator.publicKeyPem, input, signed, 10_000), true); assert.equal(registry.consume(coordinator.publicKeyPem, input, signed, 10_000), false); assert.equal(registry.consume(other.publicKeyPem, input, signed, 10_000), false); assert.equal(registry.size(10_101), 0);
});
