import assert from "node:assert/strict";
import test from "node:test";

import {
  C10ReceiptError,
  RecordingReceiptStore,
  SourceBoundOneShotTransport,
  allowlistedCards,
  type DirectProviderTransport,
  type ReceiptBinding,
  type RequestCard,
  type TransportResponse,
} from "../../../capacity_c10/receipts/index.js";
import { collectJllAdmissionReceipts, sealJllAdmissionManifest } from "../../../capacity_c10/receipts/strict_detail/jll_admission.js";
import { jllEnumerationCard } from "../../../capacity_c10/receipts/strict_detail/jll.js";
import { MemoryReceiptStore } from "./receipt_test_store.js";

const binding: ReceiptBinding = {
  planSha256: "a".repeat(64), cohortSha256: "b".repeat(64), policySha256: "c".repeat(64),
  sourceSha256: "d".repeat(64), armSha256: "e".repeat(64), implementationSha256: "f".repeat(64),
};

type Candidate = { id: unknown; pageUrl: unknown };

function numbered(count: number): Candidate[] {
  return Array.from({ length: count }, (_, i) => ({ id: String(i + 1), pageUrl: `/listings/member-${i + 1}` }));
}

class FixtureTransport implements DirectProviderTransport {
  readonly urls: string[] = [];
  constructor(private readonly candidates: readonly Candidate[] = numbered(16)) {}
  async execute(card: Readonly<RequestCard>): Promise<TransportResponse> {
    this.urls.push(card.url);
    const slug = card.url.split("/listings/").at(-1);
    const source = this.candidates.find((item) => String(item.pageUrl).replace(/[?#].*$/, "").replace(/\/$/, "").endsWith(`/listings/${slug}`));
    const body = card.id === "jll-enumeration-0"
      ? JSON.stringify({ data: { properties: { count: this.candidates.length, items: this.candidates } } })
      : `<script id="__NEXT_DATA__">${JSON.stringify({ props: { pageProps: { property: { id: String(source?.id), pageUrl: card.url, images: [] } } } })}</script>`;
    return { status: 200, finalUrl: card.url, redirectCount: 0, elapsedMs: 1, challengeDetected: false, body: Buffer.from(body), contentType: card.id === "jll-enumeration-0" ? "application/json" : "text/html", providerAttempts: 1, cacheMode: "no-store" };
  }
}

function fixture(candidates?: readonly Candidate[]) {
  const memory = new MemoryReceiptStore();
  const store = new RecordingReceiptStore(memory);
  const direct = new FixtureTransport(candidates);
  const transport = new SourceBoundOneShotTransport("jll", binding, allowlistedCards("jll", [jllEnumerationCard({ transaction: "sale", propertyType: "office", page: 1 })]), store, direct);
  return { memory, direct, transport };
}


test("JLL admission collector drives one fixed card plus exactly sixteen members", async () => {
  const memory = new MemoryReceiptStore();
  const store = new RecordingReceiptStore(memory);
  const transport = new SourceBoundOneShotTransport("jll", binding, allowlistedCards("jll", [jllEnumerationCard({ transaction: "sale", propertyType: "office", page: 1 })]), store, new FixtureTransport());
  const result = await collectJllAdmissionReceipts({ transport });
  assert.equal(result.memberReceipts.length, 16);
  assert.equal(result.enumeration.stage, "enumeration");
  assert.match(result.receiptSetSha256, /^[a-f0-9]{64}$/);
  assert.ok(result.artifacts.length >= 17);
  const manifest = await sealJllAdmissionManifest({ transport }, "/private/receipt-root", "d".repeat(64), result);
  const value = memory.jsonFor(manifest.sha256) as { member_receipts: unknown[]; artifacts: unknown[] };
  assert.equal(value.member_receipts.length, 16);
  assert.ok(value.artifacts.length >= 17);
});

test("JLL admission source selects the lexicographic first sixteen and seals the rule and digest", async () => {
  // Numeric order differs from code-unit order: member-10 < member-2, and uppercase sorts before lowercase.
  const candidates = [...numbered(18), { id: "900", pageUrl: "https://property.jll.com/listings/Zeta/?utm=1#x" }].reverse();
  const { memory, direct, transport } = fixture(candidates);
  const result = await collectJllAdmissionReceipts({ transport });
  const expected = [...candidates.map((item) => `https://property.jll.com/listings/${String(item.pageUrl).replace(/^https:\/\/property\.jll\.com/, "").replace(/^\/listings\//, "").replace(/[?#].*$/, "").replace(/\/$/, "")}`)]
    .sort((left, right) => (left < right ? -1 : left > right ? 1 : 0)).slice(0, 16);
  assert.deepEqual(result.members.map((member) => member.canonicalUrl), expected);
  assert.equal(result.members[0]?.canonicalUrl, "https://property.jll.com/listings/Zeta");
  assert.deepEqual(result.members.map((member) => member.key), Array.from({ length: 16 }, (_, i) => `jll-${i + 1}`));
  assert.equal(result.selection.rule, "jll-canonical-url-lexicographic-v1");
  assert.equal(result.selection.candidateCount, 19);
  assert.deepEqual(direct.urls.slice(1), expected);
  const manifestArtifact = await sealJllAdmissionManifest({ transport }, "/private/receipt-root", "d".repeat(64), result);
  const manifest = memory.jsonFor(manifestArtifact.sha256) as Record<string, any>;
  assert.equal(manifest.selection_digest, result.selection.digest);
  assert.deepEqual(Object.keys(manifest.collection_intent).sort(), ["enumeration", "member_count", "no_write", "selection_rule", "source_key"]);
  assert.equal(manifest.members[0].canonical_url, "https://property.jll.com/listings/Zeta");
});

for (const [name, candidates] of [
  ["fewer than sixteen canonical candidates", numbered(15)],
  ["a duplicate canonical route", [...numbered(16), { id: "99", pageUrl: "/listings/member-3/" }]],
  ["a duplicate provider id", [...numbered(16), { id: "3", pageUrl: "/listings/other" }]],
  ["a numeric provider id", [...numbered(16), { id: 17, pageUrl: "/listings/member-17" }]],
  ["a dot-segment route", [...numbered(16), { id: "17", pageUrl: "/listings/../admin" }]],
  ["an off-origin route", [...numbered(16), { id: "17", pageUrl: "https://evil.example/listings/a" }]],
] as const) {
  test(`JLL admission source rejects ${name} before any member request`, async () => {
    const { direct, transport } = fixture(candidates as readonly Candidate[]);
    await assert.rejects(collectJllAdmissionReceipts({ transport }), C10ReceiptError);
    assert.equal(direct.urls.length, 1);
    assert.equal(transport.requestAccounting().attempts, 1);
    assert.equal(transport.requestAccounting().events[0]?.outcome, "rejected");
  });
}

class RawEnumerationTransport extends FixtureTransport {
  constructor(private readonly raw: Uint8Array) { super(numbered(16)); }
  override async execute(card: Readonly<RequestCard>): Promise<TransportResponse> {
    const response = await super.execute(card);
    return card.id === "jll-enumeration-0" ? { ...response, body: this.raw } : response;
  }
}

test("JLL admission source decodes enumeration bytes like the Python validator (BOM accepted, invalid UTF-8 rejected)", async () => {
  const json = Buffer.from(JSON.stringify({ data: { properties: { count: 16, items: numbered(16) } } }));
  const build = (raw: Uint8Array) => new SourceBoundOneShotTransport("jll", binding, allowlistedCards("jll", [jllEnumerationCard({ transaction: "sale", propertyType: "office", page: 1 })]), new RecordingReceiptStore(new MemoryReceiptStore()), new RawEnumerationTransport(raw));
  const withBom = await collectJllAdmissionReceipts({ transport: build(Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), json])) });
  assert.equal(withBom.members.length, 16);
  await assert.rejects(collectJllAdmissionReceipts({ transport: build(Buffer.concat([json.subarray(0, 10), Buffer.from([0xff]), json.subarray(10)])) }), C10ReceiptError);
});
