import assert from "node:assert/strict";
import { link, mkdir, mkdtemp, readFile, readdir, rename, rm, stat, symlink, unlink } from "node:fs/promises";
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
import { MemoryReceiptStore } from "./receipt_test_store.js";

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
      stage: "enumeration",
      method: "GET",
      url: "https://example.test/search?page=1",
      allowedHost: "example.test",
      headers: { accept: "application/json" },
      contentType: null,
      body: null,
      cacheMode: "no-store",
      timeoutMs: 1_000,
      maxBytes: 64,
    },
  ]);
}

class FakeTransport implements DirectProviderTransport {
  readonly calls: string[] = [];
  readonly requestCards: Readonly<RequestCard>[] = [];

  constructor(
    private readonly result: TransportResponse | ((card: Readonly<RequestCard>) => TransportResponse),
  ) {}

  async execute(card: Readonly<RequestCard>): Promise<TransportResponse> {
    this.calls.push(card.id);
    this.requestCards.push(card);
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

function projection(finalUrl: string) {
  return { providerId: "provider-1", canonicalUrl: finalUrl, requiredFields: ["title"] };
}

test("canonical JSON is deterministic and matches the ASCII C10 convention", () => {
  assert.equal(canonicalJson({ b: "é", a: -0 }), '{"a":0,"b":"\\u00e9"}');
});

test("one-shot transport seals private artifacts and exposes URL-free accounting", async () => {
  const store = new MemoryReceiptStore();
  const fake = new FakeTransport((card) => response(card.url));
  const transport = new SourceBoundOneShotTransport("jll", binding, cards(), store, fake);

  let leakedBody: Uint8Array | undefined;
  const event = await transport.oneShot("enum", (view) => {
    leakedBody = view.body;
    return projection(view.finalUrl);
  });
  assert.equal(fake.calls.length, 1);
  assert.equal(event.status, 200);
  assert.match(event.bodySha256, /^[0-9a-f]{64}$/);
  assert.match(event.projectionSha256, /^[0-9a-f]{64}$/);
  assert.deepEqual(transport.requestAccounting().retries, 0);
  assert.equal(JSON.stringify(transport.requestAccounting()).includes("example.test"), false);
  assert.equal(JSON.stringify(transport.requestAccounting()).includes('{"ok":true}'), false);
  await assert.rejects(transport.oneShot("not-allowlisted", (view) => projection(view.finalUrl)), /not allowlisted/);
  await assert.rejects(transport.oneShot("enum", (view) => projection(view.finalUrl)), C10ReceiptError);
  assert.equal(fake.calls.length, 1);
  assert.deepEqual([...(leakedBody ?? [])], Array.from({ length: 11 }, () => 0));
  assert.equal(Object.isFrozen(event.projection), true);
  assert.throws(() => { (event.projection as { providerId: string }).providerId = "mutated"; }, TypeError);
  assert.ok(store.artifacts.size > 0);
});

test("projection failure consumes the card without exposing a reusable body handle", async () => {
  const store = new MemoryReceiptStore();
  const fake = new FakeTransport((card) => response(card.url));
  const transport = new SourceBoundOneShotTransport("jll", binding, cards(), store, fake);
  await assert.rejects(transport.oneShot("enum", () => { throw new Error("native parser rejected body"); }), /projection failed/);
  await assert.rejects(transport.oneShot("enum", (view) => projection(view.finalUrl)), /already consumed/);
  assert.equal(fake.calls.length, 1);
  assert.equal(transport.requestAccounting().events[0]?.privateEventSha256, null);
});

test("response failures are terminal, counted once, and never retried", async () => {
  const store = new MemoryReceiptStore();
  const fake = new FakeTransport({ ...response("https://example.test/search?page=1"), redirectCount: 1 });
  const transport = new SourceBoundOneShotTransport("jll", binding, cards(), store, fake);

  await assert.rejects(transport.oneShot("enum", (view) => projection(view.finalUrl)), /violates/);
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
  await assert.rejects(transport.oneShot("enum", (view) => projection(view.finalUrl)), /already consumed/);
  assert.equal(fake.calls.length, 1);
});

test("POST cards seal exact canonical bodies and redact them from public accounting", async () => {
  const store = new MemoryReceiptStore();
  const fake = new FakeTransport((card) => response(card.url));
  const body = canonicalJson({ query: "publicPosts", variables: { limit: 100, offset: 0 } });
  const postCards = allowlistedCards("nai-global", [{
    id: "posts-0",
    sourceKey: "nai-global",
    stage: "enumeration",
    method: "POST",
    url: "https://infabode.com/graphql",
    allowedHost: "infabode.com",
    headers: { authorization: "Bearer private", "content-type": "application/json" },
    contentType: "application/json",
    body,
    cacheMode: "no-store",
    timeoutMs: 1_000,
    maxBytes: 64,
  }]);
  const transport = new SourceBoundOneShotTransport("nai-global", binding, postCards, store, fake);
  const event = await transport.oneShot("posts-0", (view) => projection(view.finalUrl));
  assert.equal(fake.requestCards[0]?.body, body);
  assert.match(fake.requestCards[0]?.bodySha256 ?? "", /^[0-9a-f]{64}$/);
  assert.equal(JSON.stringify(event).includes("Bearer private"), false);
  assert.equal(JSON.stringify(transport.requestAccounting()).includes("publicPosts"), false);
  assert.throws(() => allowlistedCards("nai-global", [{
    ...postCards.get("posts-0")!, body: '{"variables":{"offset":0,"limit":100},"query":"publicPosts"}',
  }]), /not canonical/);
  assert.throws(() => allowlistedCards("nai-global", [{
    ...postCards.get("posts-0")!, method: "GET", contentType: "application/json",
  }]), /cannot have a body/);
});

test("staged graph rejects arbitrary, replayed, backward, excess, and post-freeze cards", async () => {
  const store = new MemoryReceiptStore();
  const fake = new FakeTransport((card) => response(card.url));
  const transport = new SourceBoundOneShotTransport("jll", binding, cards(), store, fake);
  const first = await transport.oneShot("enum", (view) => ({ ...projection(view.finalUrl), nextPage: 2 }));
  const enumerationFactory = {
    sourceKey: "jll",
    stage: "enumeration" as const,
    maximumCards: 3,
    create(parent: Readonly<typeof first>, page: number) {
      if (parent.projection.nextPage !== page || page !== 2) throw new C10ReceiptError("non-native next page");
      return {
        id: "enum-2", sourceKey: "jll", stage: "enumeration" as const, method: "GET" as const,
        url: "https://example.test/search?page=2", allowedHost: "example.test", headers: {},
        contentType: null, body: null, cacheMode: "no-store" as const, timeoutMs: 1_000, maxBytes: 64,
      };
    },
  };
  await assert.rejects(transport.appendFrom(first, enumerationFactory, 1), /non-native/);
  const expansion = await transport.appendFrom(first, enumerationFactory, 2);
  assert.equal(expansion.parentProjectionSha256, first.projectionSha256);
  await assert.rejects(transport.appendFrom(first, enumerationFactory, 2), /duplicated/);
  await assert.rejects(transport.appendFrom(first, { ...enumerationFactory, maximumCards: 2 }, 2), /cap exceeded/);
  const otherCards = allowlistedCards("other", [{
    ...cards().get("enum")!, sourceKey: "other", url: "https://other.test/search?page=1", allowedHost: "other.test",
  }]);
  const other = new SourceBoundOneShotTransport("other", binding, otherCards, store, fake);
  await assert.rejects(other.appendFrom(first, enumerationFactory, 2), /another source/);
  const second = await transport.oneShot("enum-2", (view) => projection(view.finalUrl));
  const memberFactory = {
    sourceKey: "jll",
    stage: "member" as const,
    maximumCards: 1,
    create(parent: Readonly<typeof second>, id: string) {
      if (id !== "provider-1") throw new C10ReceiptError("arbitrary member identity");
      return {
        id: "member-1", sourceKey: "jll", stage: "member" as const, method: "GET" as const,
        url: "https://example.test/member/1", allowedHost: "example.test", headers: {},
        contentType: null, body: null, cacheMode: "no-store" as const, timeoutMs: 1_000, maxBytes: 64,
      };
    },
  };
  await transport.appendFrom(second, memberFactory, "provider-1");
  await assert.rejects(transport.oneShot("member-1", (view) => projection(view.finalUrl)), /must be frozen/);
  await transport.freezeMemberGraph();
  await assert.rejects(transport.appendFrom(second, memberFactory, "provider-1"), /after freeze/);
  await assert.rejects(transport.oneShot("enum-2", (view) => projection(view.finalUrl)), /(already consumed|frozen)/);
  await transport.oneShot("member-1", (view) => projection(view.finalUrl));
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
    const store = new MemoryReceiptStore();
    const fake = new FakeTransport({ ...response("https://example.test/search?page=1"), ...override });
    const transport = new SourceBoundOneShotTransport("jll", binding, cards(), store, fake);
    await assert.rejects(transport.oneShot("enum", (view) => projection(view.finalUrl)), /violates/);
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
  const store = new MemoryReceiptStore();
  const fake = new FakeTransport((card) => response(card.url));
  const context = { transport: new SourceBoundOneShotTransport("jll", binding, cards(), store, fake) };
  let enumerationEvent: Awaited<ReturnType<typeof context.transport.oneShot>> | undefined;
  const producer: ReceiptProducer = {
    async produceEnumerationReceipt(receiptContext) {
      enumerationEvent = await receiptContext.transport.oneShot("enum", (view) => projection(view.finalUrl));
      return sealStageReceipt(receiptContext, "enumeration", null, { providerCount: 1 });
    },
    async produceMemberReceipt(receiptContext, member) {
      await receiptContext.transport.oneShot("member", (view) => projection(view.finalUrl));
      return sealStageReceipt(receiptContext, "member", member.key, { providerId: member.providerId });
    },
  };
  const enumeration = await producer.produceEnumerationReceipt(context);
  await context.transport.appendFrom(enumerationEvent!, {
    sourceKey: "jll",
    stage: "member",
    maximumCards: 1,
    create(parent, memberKey: string) {
      assert.equal(parent.projection.providerId, "provider-1");
      if (memberKey !== "member-1") throw new C10ReceiptError("unexpected member coordinate");
      return {
        id: "member",
        sourceKey: "jll",
        stage: "member",
        method: "GET",
        url: "https://example.test/member/1",
        allowedHost: "example.test",
        headers: { accept: "application/json" },
        contentType: null,
        body: null,
        cacheMode: "no-store",
        timeoutMs: 1_000,
        maxBytes: 64,
      };
    },
  }, "member-1");
  await context.transport.freezeMemberGraph();
  const member = await producer.produceMemberReceipt(context, { key: "member-1", providerId: "provider-1" });
  assert.equal(enumeration.binding.planSha256, binding.planSha256);
  assert.equal(member.binding.armSha256, binding.armSha256);
  assert.equal(enumeration.noWrite.cache_writes, 0);
  assert.equal(JSON.stringify(enumeration).includes("example.test"), false);
  assert.equal(JSON.stringify(enumeration).includes("providerCount"), false);
});

test("private receipt store fails closed without FD-relative primitives and production imports stay isolated", async () => {
  const parent = await mkdtemp(join(tmpdir(), "c10-receipts-"));
  const target = join(parent, "target");
  const link = join(parent, "link");
  if (PrivateReceiptStore.safeRuntimeAvailable()) {
    const store = await PrivateReceiptStore.create(target);
    await symlink(target, link);
    await assert.rejects(PrivateReceiptStore.create(link), /root could not retain|real mode/);
    await store.close();
  } else {
    await assert.rejects(PrivateReceiptStore.create(target), /fd-relative filesystem primitives/);
  }

  const root = fileURLToPath(new URL("../../../capacity_c10/receipts/", import.meta.url));
  const files = await readdir(root);
  const imports = await Promise.all(files.filter((name) => name.endsWith(".ts")).map(async (name) => readFile(join(root, name), "utf8")));
  const forbidden = /from\s+["'][^"']*(?:collect|ingest|checkpoint|cache|scrape)[^"']*["']/;
  for (const source of imports) assert.equal(forbidden.test(source), false, "receipt package imported a forbidden collector surface");
});

test("FD-relative private store rejects root replacement, symlink, hardlink, and duplicate seal races", async (t) => {
  if (!PrivateReceiptStore.safeRuntimeAvailable()) {
    t.skip("host lacks Node-accessible FD-relative filesystem primitives");
    return;
  }
  const root = await mkdtemp(join(tmpdir(), "c10-receipts-"));
  const store = await PrivateReceiptStore.create(root);
  const artifact = await store.sealBytes("evidence", Buffer.from("private bytes"));
  assert.equal((await stat(root)).mode & 0o777, 0o700);
  assert.equal((await stat(join(root, artifact.name))).mode & 0o777, 0o600);
  await assert.rejects(store.sealBytes("evidence", Buffer.from("private bytes")), C10ReceiptError);
  await link(join(root, artifact.name), join(root, "attacker-hardlink"));
  await assert.rejects(store.verifySealed(artifact), /sealed artifact is invalid/);
  await unlink(join(root, "attacker-hardlink"));
  await store.verifySealed(artifact);

  const replaced = `${root}-replaced`;
  await rename(root, replaced);
  await mkdir(root, { mode: 0o700 });
  await assert.rejects(store.sealBytes("after-replace", Buffer.from("x")), /root identity changed/);
  await rm(root, { recursive: true, force: true });
  await symlink(replaced, root);
  await assert.rejects(store.verifySealed(artifact), /root identity changed/);
  await store.close();
});
