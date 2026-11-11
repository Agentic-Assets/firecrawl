import test from "node:test";
import assert from "node:assert/strict";
import { observeDaumMove } from "../../../sources/daum-redirect-receipt.js";
import { DaumPoliteTransport } from "../../../sources/daum-transport.js";

const old = "https://daumcommercial.com/property/old/";
const current = "https://daumcommercial.com/property/new/";
const candidate = () => ({
  schema: "daum-redirect-candidate/v1" as const,
  artifact_sha256: "a".repeat(64), generation_id: "generation",
  producer_sha: "b".repeat(40), database_target_sha256: "c".repeat(64),
  member: { native_id: "42734", external_id: "property/new", source_url: current },
  candidate: {
    rowId: "11111111-1111-4111-8111-111111111111",
    oldExternalId: "property/old", oldUrl: old, nativeId: "42734",
    currentExternalId: "property/new", currentUrl: current,
    rowSha256: "d".repeat(64), indexSha256: null,
  },
  write_authority: false as const,
});

function transport(shortlink = "https://daumcommercial.com/?p=42734", canonical = current) {
  return new DaumPoliteTransport(async (url, init) => {
    assert.equal(init?.cache, "no-store");
    return String(url) === old
      ? new Response(null, { status: 301, headers: { location: current } })
      : new Response(`<link rel="canonical" href="${canonical}"><link rel="shortlink" href="${shortlink}">`);
  }, 0);
}

test("move observation binds a settled redirect and current page to the candidate without write authority", async () => {
  const input = candidate();
  const receipt = await observeDaumMove(input, transport());
  assert.deepEqual(receipt.candidate, input);
  assert.equal(receipt.redirect.status, 301);
  assert.equal(receipt.page.url, current);
  assert.match(receipt.page.sha256, /^[a-f0-9]{64}$/);
  assert.match(receipt.page.html, /shortlink/);
  assert.equal(receipt.write_authority, false);
});

test("move observation requires an HTTP 200 for the current page", async () => {
  const source = new DaumPoliteTransport(async url => String(url) === old
    ? new Response(null, { status: 301, headers: { location: current } })
    : new Response("not a page", { status: 201 }), 0);
  await assert.rejects(observeDaumMove(candidate(), source), /HTTP 200/);
});

test("move observation refuses a different native ID or canonical page", async () => {
  await assert.rejects(observeDaumMove(candidate(), transport("https://daumcommercial.com/?p=42")));
  await assert.rejects(observeDaumMove(candidate(), transport(undefined, old)));
});

test("move observation refuses an artifact binding mismatch before any request", async () => {
  const input = candidate();
  input.member.native_id = "99";
  let called = false;
  const source = new DaumPoliteTransport(async () => { called = true; return new Response(); }, 0);
  await assert.rejects(observeDaumMove(input, source));
  assert.equal(called, false);
});
