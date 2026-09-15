import assert from "node:assert/strict";
import { mkdtemp, readdir, readFile, stat, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  C10ReceiptError,
  PrivateReceiptStore,
  SourceBoundOneShotTransport,
  allowlistedCards,
  canonicalJson,
  parseReceiptInvocation,
  sealStageReceipt,
  type DirectProviderTransport,
  type RequestCard,
  type ReceiptBinding,
  type ReceiptProducer,
  type TransportResponse,
} from "../../../capacity_c10/receipts/index.js";

const DIGEST = "a".repeat(64);
const binding: ReceiptBinding = Object.freeze({
  planSha256: DIGEST,
  cohortSha256: "b".repeat(64),
  policySha256: "c".repeat(64),
  sourceSha256: "d".repeat(64),
  armSha256: "e".repeat(64),
  implementationSha256: "f".repeat(64),
});

function cards() {
  return allowlistedCards("jll", [
    {
      id: "enum",
      sourceKey: "jll",
      method: "GET",
      url: "https://example.test/search?page=1",
      allowedHost: "example.test",
      headers: { accept: "application/json" },
      cacheMode: "no-store",
      timeoutMs: 1_000,
      maxBytes: 64,
    },
    {
      id: "member",
      sourceKey: "jll",
      method: "GET",
      url: "https://example.test/member/1",
      allowedHost: "example.test",
      headers: { accept: "application/json" },
      cacheMode: "no-store",
      timeoutMs: 1_000,
      maxBytes: 64,
    },
  ]);
}

class FakeTransport implements DirectProviderTransport {
  readonly calls: string[] = [];

  constructor(
    private readonly result: TransportResponse | ((card: Readonly<RequestCard>) => TransportResponse),
  ) {}

  async execute(card: Readonly<RequestCard>): Promise<TransportResponse> {
    this.calls.push(card.id);
    return typeof this.result === "function" ? this.result(card) : this.result;
  }
}

function response(url: string): TransportResponse {
  return {
    status: 200,
    finalUrl: url,
    redirectCount: 0,
    elapsedMs: 12,
    challengeDetected: false,
    body: Buffer.from('{"ok":true}'),
    contentType: "application/json",
    providerAttempts: 1,
    cacheMode: "no-store",
  };
}

test("canonical JSON is deterministic and matches the ASCII C10 convention", () => {
  assert.equal(canonicalJson({ b: "é", a: -0 }), '{"a":0,"b":"\\u00e9"}');
});

test("one-shot transport seals private artifacts and exposes URL-free accounting", async () => {
  const root = await mkdtemp(join(tmpdir(), "c10-receipts-"));
  const store = await PrivateReceiptStore.create(root);
  const fake = new FakeTransport((card) => response(card.url));
  const transport = new SourceBoundOneShotTransport("jll", binding, cards(), store, fake);

  const event = await transport.oneShot("enum");
  assert.equal(fake.calls.length, 1);
  assert.equal(event.status, 200);
  assert.match(event.bodySha256, /^[0-9a-f]{64}$/);
  assert.deepEqual(transport.requestAccounting().retries, 0);
  assert.equal(JSON.stringify(transport.requestAccounting()).includes("example.test"), false);
  assert.equal(JSON.stringify(transport.requestAccounting()).includes('{"ok":true}'), false);
  await assert.rejects(transport.oneShot("not-allowlisted"), /not allowlisted/);
  await assert.rejects(transport.oneShot("enum"), C10ReceiptError);
  assert.equal(fake.calls.length, 1);
  assert.ok((await readdir(root)).some((name) => name.endsWith(".sealed")));
});

test("response failures are terminal, counted once, and never retried", async () => {
  const root = await mkdtemp(join(tmpdir(), "c10-receipts-"));
  const store = await PrivateReceiptStore.create(root);
  const fake = new FakeTransport({ ...response("https://example.test/search?page=1"), redirectCount: 1 });
  const transport = new SourceBoundOneShotTransport("jll", binding, cards(), store, fake);

  await assert.rejects(transport.oneShot("enum"), /violates/);
  assert.equal(fake.calls.length, 1);
  assert.deepEqual(transport.requestAccounting(), {
    logicalRequests: 1,
    attempts: 1,
    retries: 0,
    eventsSha256: transport.requestAccounting().eventsSha256,
    events: [{
      cardId: "enum",
      outcome: "rejected",
      status: 200,
      elapsedMs: 12,
      bytes: 11,
      bodySha256: transport.requestAccounting().events[0]?.bodySha256,
      privateEventSha256: null,
    }],
  });
  await assert.rejects(transport.oneShot("enum"), /already consumed/);
  assert.equal(fake.calls.length, 1);
});

test("challenge, status, byte, time, and final-URL bounds are terminal", async () => {
  const invalidResponses: readonly Partial<TransportResponse>[] = [
    { status: 503 },
    { challengeDetected: true },
    { elapsedMs: 1_001 },
    { body: Buffer.alloc(65) },
    { finalUrl: "https://example.test/unexpected" },
    { providerAttempts: 2 },
  ];
  for (const override of invalidResponses) {
    const root = await mkdtemp(join(tmpdir(), "c10-receipts-"));
    const store = await PrivateReceiptStore.create(root);
    const fake = new FakeTransport({ ...response("https://example.test/search?page=1"), ...override });
    const transport = new SourceBoundOneShotTransport("jll", binding, cards(), store, fake);
    await assert.rejects(transport.oneShot("enum"), /violates/);
    assert.equal(fake.calls.length, 1);
    assert.equal(transport.requestAccounting().events[0]?.outcome, "rejected");
  }
});

test("request cards reject host drift and the controller rejects collector surfaces", async () => {
  assert.throws(
    () => allowlistedCards("jll", [{
      ...Array.from(cards().values())[0]!,
      url: "https://other.test/listings",
    }]),
    /allowlisted/,
  );
  assert.deepEqual(
    parseReceiptInvocation(["--source=jll", "--receipt-root=/private/c10"], {}),
    { sourceKey: "jll", receiptRoot: "/private/c10" },
  );
  for (const args of [["--monitor"], ["--mark-missing"], ["--source=jll", "--url=https://x"]]) {
    assert.throws(() => parseReceiptInvocation(args, {}), C10ReceiptError);
  }
  assert.throws(
    () => parseReceiptInvocation(["--source=jll", "--receipt-root=/private/c10"], { CRE_PERFORMANCE_PATH: "/tmp/x" }),
    /rejects/,
  );
});

test("producer protocol binds each sealed public receipt to immutable C10 hashes", async () => {
  const root = await mkdtemp(join(tmpdir(), "c10-receipts-"));
  const store = await PrivateReceiptStore.create(root);
  const fake = new FakeTransport((card) => response(card.url));
  const context = {
    sourceKey: "jll",
    binding,
    store,
    transport: new SourceBoundOneShotTransport("jll", binding, cards(), store, fake),
  };
  const producer: ReceiptProducer = {
    async produceEnumerationReceipt(receiptContext) {
      await receiptContext.transport.oneShot("enum");
      return sealStageReceipt(receiptContext, "enumeration", null, { providerCount: 1 });
    },
    async produceMemberReceipt(receiptContext, member) {
      await receiptContext.transport.oneShot("member");
      return sealStageReceipt(receiptContext, "member", member.key, { providerId: member.providerId });
    },
  };
  const enumeration = await producer.produceEnumerationReceipt(context);
  const member = await producer.produceMemberReceipt(context, { key: "member-1", providerId: "provider-1" });
  assert.equal(enumeration.binding.planSha256, binding.planSha256);
  assert.equal(member.binding.armSha256, binding.armSha256);
  assert.equal(enumeration.noWrite.cache_writes, 0);
  assert.equal(JSON.stringify(enumeration).includes("example.test"), false);
  assert.equal(JSON.stringify(enumeration).includes("providerCount"), false);
});

test("private root refuses symlink roots and production imports stay isolated", async () => {
  const parent = await mkdtemp(join(tmpdir(), "c10-receipts-"));
  const target = join(parent, "target");
  const link = join(parent, "link");
  await PrivateReceiptStore.create(target);
  await symlink(target, link);
  await assert.rejects(PrivateReceiptStore.create(link), /real directory/);

  const root = fileURLToPath(new URL("../../../capacity_c10/receipts/", import.meta.url));
  const files = await readdir(root);
  const imports = await Promise.all(files.filter((name) => name.endsWith(".ts")).map(async (name) => readFile(join(root, name), "utf8")));
  const forbidden = /from\s+["'][^"']*(?:collect|ingest|checkpoint|cache|scrape)[^"']*["']/;
  for (const source of imports) assert.equal(forbidden.test(source), false, "receipt package imported a forbidden collector surface");
});

test("sealed artifacts are private, immutable, and never overwrite a prior seal", async () => {
  const root = await mkdtemp(join(tmpdir(), "c10-receipts-"));
  const store = await PrivateReceiptStore.create(root);
  const artifact = await store.sealBytes("evidence", Buffer.from("private bytes"));
  assert.equal((await stat(root)).mode & 0o777, 0o700);
  assert.equal((await stat(join(root, artifact.name))).mode & 0o777, 0o600);
  await assert.rejects(store.sealBytes("evidence", Buffer.from("private bytes")), C10ReceiptError);
});
