import assert from "node:assert/strict";
import { generateKeyPairSync } from "node:crypto";
import test from "node:test";
import { C10_JLL_ADMISSION_LANE, C10SidecarCapabilityRegistry, canonicalJson, issueC10SidecarCapability, parseC10SidecarInput, publicKeyId, sha256 } from "./c10_browser_internal";

const pem = () => { const pair = generateKeyPairSync("ed25519"); return { privateKeyPem: pair.privateKey.export({ type: "pkcs8", format: "pem" }).toString(), publicKeyPem: pair.publicKey.export({ type: "spki", format: "pem" }).toString() }; };
test("v3 verifies an asymmetric capability once and prunes expiry", () => {
  const coordinator = pem(), other = pem(); const card = { id: "one", sourceKey: "jll", stage: "member" as const, method: "GET" as const, url: "https://example.test/a", allowedHost: "example.test", headers: {}, contentType: null, body: null, browserBootstrapUrl: "https://example.test/", cacheMode: "no-store" as const, timeoutMs: 1_000, maxBytes: 1_024, bodySha256: null, expectedMemberRoutes: null };
  const capability = { protocolVersion: 3 as const, coordinatorKeyId: publicKeyId(coordinator.publicKeyPem), nonce: "v3-once", expiresAtMs: 10_100, hostDeadlineAtMs: 10_200, cardSequence: 0, sourceKey: "jll", binding: { planSha256: "a".repeat(64), cohortSha256: "b".repeat(64), cardSha256: sha256(canonicalJson(card)), manifestSha256: "c".repeat(64), sessionSha256: "d".repeat(64), armSha256: "e".repeat(64), profileSha256: "f".repeat(64) } };
  const input = parseC10SidecarInput({ capability, card }); const signed = issueC10SidecarCapability(coordinator.privateKeyPem, capability); const registry = new C10SidecarCapabilityRegistry();
  assert.equal(registry.consume(coordinator.publicKeyPem, input, signed, 10_000), true); assert.equal(registry.consume(coordinator.publicKeyPem, input, signed, 10_000), false); assert.equal(registry.consume(other.publicKeyPem, input, signed, 10_000), false); assert.equal(registry.size(10_100), 0); assert.equal(registry.consume(coordinator.publicKeyPem, input, signed, 10_100), false);
});

test("v3 rejects a host deadline that exceeds the bounded coordinator lifetime", () => {
  const coordinator = pem(); const card = { id: "one", sourceKey: "jll", stage: "member" as const, method: "GET" as const, url: "https://example.test/a", allowedHost: "example.test", headers: {}, contentType: null, body: null, browserBootstrapUrl: "https://example.test/", cacheMode: "no-store" as const, timeoutMs: 1_000, maxBytes: 1_024, bodySha256: null, expectedMemberRoutes: null };
  const capability = { protocolVersion: 3 as const, coordinatorKeyId: publicKeyId(coordinator.publicKeyPem), nonce: "host-deadline", expiresAtMs: 10_100, hostDeadlineAtMs: 130_001, cardSequence: 0, sourceKey: "jll", binding: { planSha256: "a".repeat(64), cohortSha256: "b".repeat(64), cardSha256: sha256(canonicalJson(card)), manifestSha256: "c".repeat(64), sessionSha256: "d".repeat(64), armSha256: "e".repeat(64), profileSha256: "f".repeat(64) } };
  const input = parseC10SidecarInput({ capability, card }); const signed = issueC10SidecarCapability(coordinator.privateKeyPem, capability);
  assert.equal(new C10SidecarCapabilityRegistry().consume(coordinator.publicKeyPem, input, signed, 10_000), false);
});

const dummyBinding = () => ({ planSha256: "a".repeat(64), cohortSha256: "b".repeat(64), cardSha256: "c".repeat(64), manifestSha256: "d".repeat(64), sessionSha256: "e".repeat(64), armSha256: "f".repeat(64), profileSha256: "1".repeat(64) });
const dummyCapability = (sourceKey: string) => ({ protocolVersion: 3 as const, coordinatorKeyId: "a".repeat(64), nonce: "n", expiresAtMs: 1, hostDeadlineAtMs: 1, cardSequence: 0, sourceKey, binding: dummyBinding() });
const graphqlBody = "{}";
const baseEnumerationCard = (sourceKey: string) => ({ id: "enum", sourceKey, stage: "enumeration" as const, method: "POST" as const, url: "https://example.test/api/graphql", allowedHost: "example.test", headers: {}, contentType: "application/json" as const, body: graphqlBody, bodySha256: sha256(graphqlBody), browserBootstrapUrl: "https://example.test/", cacheMode: "no-store" as const, timeoutMs: 1_000, maxBytes: 1_024 });
const sixteenRoutes = Array.from({ length: 16 }, (_, index) => `https://example.test/route-${index + 1}`);

test("v3 strict parse rejects an enumeration card without its sealed sixteen-route membership", () => {
  const card = { ...baseEnumerationCard("jll"), expectedMemberRoutes: null };
  assert.throws(() => parseC10SidecarInput({ capability: dummyCapability("jll"), card }), /lacks its sealed membership/);
});

test("v3 admission parse accepts a null-route JLL enumeration and rejects a sealed list or a non-JLL source", () => {
  const options = { admissionLane: C10_JLL_ADMISSION_LANE };
  const admittedCard = { ...baseEnumerationCard("jll"), expectedMemberRoutes: null };
  const admittedCapability = { ...dummyCapability("jll"), binding: { ...dummyBinding(), cardSha256: sha256(canonicalJson(admittedCard)) } };
  const admitted = parseC10SidecarInput({ capability: admittedCapability, card: admittedCard }, options);
  assert.equal(admitted.card.expectedMemberRoutes, null);

  assert.throws(
    () => parseC10SidecarInput(
      { capability: dummyCapability("jll"), card: { ...baseEnumerationCard("jll"), expectedMemberRoutes: sixteenRoutes } },
      options,
    ),
    /outside its lane/,
  );

  assert.throws(
    () => parseC10SidecarInput(
      { capability: dummyCapability("other"), card: { ...baseEnumerationCard("other"), expectedMemberRoutes: null } },
      options,
    ),
    /outside its lane/,
  );
});

test("v3 a member card carrying enumeration membership is rejected in strict and admission modes alike", () => {
  const memberWithRoutes = { ...baseEnumerationCard("jll"), stage: "member" as const, method: "GET" as const, contentType: null, body: null, bodySha256: null, expectedMemberRoutes: ["https://example.test/route-1"] };
  assert.throws(() => parseC10SidecarInput({ capability: dummyCapability("jll"), card: memberWithRoutes }), /cannot carry enumeration membership/);
  assert.throws(
    () => parseC10SidecarInput({ capability: dummyCapability("jll"), card: memberWithRoutes }, { admissionLane: C10_JLL_ADMISSION_LANE }),
    /cannot carry enumeration membership/,
  );
});

test("v3 registry.consume requires matching admission options to accept a JLL admission enumeration", () => {
  const coordinator = pem();
  const card = { ...baseEnumerationCard("jll"), expectedMemberRoutes: null };
  const capability = { protocolVersion: 3 as const, coordinatorKeyId: publicKeyId(coordinator.publicKeyPem), nonce: "admission-once", expiresAtMs: 10_100, hostDeadlineAtMs: 10_200, cardSequence: 0, sourceKey: "jll", binding: { ...dummyBinding(), cardSha256: sha256(canonicalJson(card)) } };
  const admissionOptions = { admissionLane: C10_JLL_ADMISSION_LANE };
  const input = parseC10SidecarInput({ capability, card }, admissionOptions);
  const signed = issueC10SidecarCapability(coordinator.privateKeyPem, capability);

  assert.equal(
    new C10SidecarCapabilityRegistry().consume(coordinator.publicKeyPem, input, signed, 10_000, admissionOptions),
    true,
  );
  assert.equal(
    new C10SidecarCapabilityRegistry().consume(coordinator.publicKeyPem, input, signed, 10_000),
    false,
  );
});
