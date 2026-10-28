import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, chmod, symlink, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { NAI_CAPTURE_MAX_BODY_BYTES, publishNaiNativePage } from "../../../lib/source-evidence/nai-page.js";
import { NAI_FEED_QUERY, NAI_PUBLIC_POST_URL, NAI_WIDGET_URL, naiGraphqlPost, fetchNaiFeedPage, naiFeedPageCache } from "../../../sources/nai-global.js";

process.env.CRE_NAI_NATIVE_CAPTURE_SINGLE_WRITER = "1";

const digest = (x: Uint8Array) => createHash("sha256").update(x).digest("hex");
const request = (offset = 0) => ({ operationName: "GET_LISTINGS_PUBLIC_POSTS", query: NAI_FEED_QUERY,
  variables: { offset, limit: 100, filter: { contentTypesIds: [4, 10], sourcesIds: [1, 2], locationsIds: [], title: "" } } });
const context = (offset = 0) => ({ requestUrl: NAI_PUBLIC_POST_URL, finalUrl: NAI_PUBLIC_POST_URL, status: 200,
  startedAt: "2026-10-28T12:00:00.000Z", finishedAt: "2026-10-28T12:00:01.000Z",
  requestBody: JSON.stringify(request(offset)), responseBody: Buffer.from('{"data":{"publicPosts":[]}}') });

test("publishes exact response bytes and scope and accepts only identical replay", async () => {
  const dir = await mkdtemp(join(tmpdir(), "nai-native-")); const page = context();
  const path = await publishNaiNativePage(dir, page); const saved = JSON.parse(await readFile(path, "utf8"));
  assert.equal((await stat(path)).mode & 0o777, 0o600);
  assert.deepEqual(Buffer.from(saved.responseBodyBase64, "base64"), page.responseBody);
  assert.equal(saved.receipt.bodySha256, digest(page.responseBody)); assert.equal(saved.receipt.bodyBytes, page.responseBody.length);
  assert.equal(saved.requestBody, page.requestBody); assert.equal(saved.requestBodySha256, digest(Buffer.from(page.requestBody)));
  assert.equal(saved.provenance.bodyHashKind, "fetch_delivered_body_bytes_not_wire");
  assert.equal(await publishNaiNativePage(dir, page), path);
  const differentBody = await publishNaiNativePage(dir, { ...page, responseBody: Buffer.from("different") });
  const differentClock = await publishNaiNativePage(dir, { ...page, finishedAt: "2026-10-28T12:00:02.000Z" });
  assert.notEqual(differentBody, path); assert.notEqual(differentClock, path);
  assert.equal((await readdir(dir)).length, 3);
  await writeFile(path, "tampered");
  await assert.rejects(publishNaiNativePage(dir, page), /NAI native capture failed/);
  assert.equal((await readdir(dir)).length, 3);
});

test("refuses insecure directory, symlink, malformed scope and oversized input", async () => {
  const dir = await mkdtemp(join(tmpdir(), "nai-native-")); const alias = dir + "-alias";
  await symlink(dir, alias); await assert.rejects(publishNaiNativePage(alias, context()), /capture failed/);
  await chmod(dir, 0o755); await assert.rejects(publishNaiNativePage(dir, context()), /capture failed/); await chmod(dir, 0o700);
  await assert.rejects(publishNaiNativePage(dir, { ...context(), requestBody: JSON.stringify(request(-1)) }), /capture failed/);
  await assert.rejects(publishNaiNativePage(dir, { ...context(), responseBody: Buffer.alloc(NAI_CAPTURE_MAX_BODY_BYTES + 1) }), /capture failed/);
  assert.deepEqual(await readdir(dir), []);
});

test("opted-in POST captures all rows before GraphQL partial errors, previous pages survive", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "nai-native-")); const old = process.env.CRE_NAI_NATIVE_CAPTURE_DIR;
  const oldFetch = globalThis.fetch; process.env.CRE_NAI_NATIVE_CAPTURE_DIR = dir;
  t.after(() => { globalThis.fetch = oldFetch; if (old === undefined) delete process.env.CRE_NAI_NATIVE_CAPTURE_DIR; else process.env.CRE_NAI_NATIVE_CAPTURE_DIR = old; });
  const payloads = [JSON.stringify({ data: { publicPosts: Array.from({ length: 100 }, (_, id) => ({ id, unknown: [null, id] })) } }),
    JSON.stringify({ data: { publicPosts: [{ id: 101, detail: "unknown" }] }, errors: [{ message: "fixture failure" }] })];
  let calls = 0;
  globalThis.fetch = (async () => { const response = new Response(payloads[calls++], { status: 200 }); Object.defineProperty(response, "url", { value: NAI_PUBLIC_POST_URL }); return response; }) as typeof fetch;
  assert.equal((await naiGraphqlPost(NAI_PUBLIC_POST_URL, request(), NAI_WIDGET_URL)).publicPosts.length, 100);
  await assert.rejects(naiGraphqlPost(NAI_PUBLIC_POST_URL, request(100), NAI_WIDGET_URL), /fixture failure/);
  const names = (await readdir(dir)).sort(); assert.equal(names.length, 2); assert.equal(calls, 2);
  const first = JSON.parse(await readFile(join(dir, names[0]), "utf8"));
  const second = JSON.parse(await readFile(join(dir, names[1]), "utf8"));
  assert.equal(JSON.parse(Buffer.from(first.responseBodyBase64, "base64").toString()).data.publicPosts.length, 100);
  assert.equal(JSON.parse(Buffer.from(second.responseBodyBase64, "base64").toString()).errors.length, 1);
});
test("cached page reuses its original capture without another fetch or timestamp", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "nai-native-")); const old = process.env.CRE_NAI_NATIVE_CAPTURE_DIR; const oldFetch = globalThis.fetch;
  process.env.CRE_NAI_NATIVE_CAPTURE_DIR = dir; naiFeedPageCache.clear(); let calls = 0;
  t.after(() => { globalThis.fetch = oldFetch; naiFeedPageCache.clear(); if (old === undefined) delete process.env.CRE_NAI_NATIVE_CAPTURE_DIR; else process.env.CRE_NAI_NATIVE_CAPTURE_DIR = old; });
  globalThis.fetch = (async () => { calls++; const r = new Response('{"data":{"publicPosts":[{"id":1}]}}', { status: 200 }); Object.defineProperty(r, "url", { value: NAI_PUBLIC_POST_URL }); return r; }) as typeof fetch;
  await fetchNaiFeedPage(0, [123]); const first = (await readdir(dir))[0]; const bytes = await readFile(join(dir, first));
  await fetchNaiFeedPage(0, [123]); assert.equal(calls, 1); assert.deepEqual(await readFile(join(dir, first)), bytes); assert.equal((await readdir(dir)).length, 1);
});
test("unrelated GraphQL operation and disabled capture create no page", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "nai-native-")); const old = process.env.CRE_NAI_NATIVE_CAPTURE_DIR; const oldFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = oldFetch; if (old === undefined) delete process.env.CRE_NAI_NATIVE_CAPTURE_DIR; else process.env.CRE_NAI_NATIVE_CAPTURE_DIR = old; });
  globalThis.fetch = (async () => new Response('{"data":{"ok":true}}', { status: 200 })) as typeof fetch;
  process.env.CRE_NAI_NATIVE_CAPTURE_DIR = dir; await naiGraphqlPost(NAI_PUBLIC_POST_URL, { operationName: "OTHER" }, NAI_WIDGET_URL);
  delete process.env.CRE_NAI_NATIVE_CAPTURE_DIR; await naiGraphqlPost(NAI_PUBLIC_POST_URL, request(), NAI_WIDGET_URL);
  assert.deepEqual(await readdir(dir), []);
});
test("oversized streamed response refuses without publishing a partial page", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "nai-native-")); const old = process.env.CRE_NAI_NATIVE_CAPTURE_DIR; const oldFetch = globalThis.fetch;
  process.env.CRE_NAI_NATIVE_CAPTURE_DIR = dir;
  t.after(() => { globalThis.fetch = oldFetch; if (old === undefined) delete process.env.CRE_NAI_NATIVE_CAPTURE_DIR; else process.env.CRE_NAI_NATIVE_CAPTURE_DIR = old; });
  globalThis.fetch = (async () => { const r = new Response(Buffer.alloc(NAI_CAPTURE_MAX_BODY_BYTES + 1), { status: 200 }); Object.defineProperty(r, "url", { value: NAI_PUBLIC_POST_URL }); return r; }) as typeof fetch;
  await assert.rejects(naiGraphqlPost(NAI_PUBLIC_POST_URL, request(), NAI_WIDGET_URL), /NAI native capture failed/);
  assert.deepEqual(await readdir(dir), []);
});
test("a timed-out fetch that resolves late is cancelled and cannot publish", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "nai-native-")); const old = process.env.CRE_NAI_NATIVE_CAPTURE_DIR; const oldFetch = globalThis.fetch;
  process.env.CRE_NAI_NATIVE_CAPTURE_DIR = dir; let resolveFetch!: (value: Response) => void; let cancelled = false;
  t.after(() => { globalThis.fetch = oldFetch; if (old === undefined) delete process.env.CRE_NAI_NATIVE_CAPTURE_DIR; else process.env.CRE_NAI_NATIVE_CAPTURE_DIR = old; });
  globalThis.fetch = (() => new Promise<Response>((resolve) => { resolveFetch = resolve; })) as typeof fetch;
  await assert.rejects(naiGraphqlPost(NAI_PUBLIC_POST_URL, request(), NAI_WIDGET_URL, 15), /settlement unconfirmed/);
  const stream = new ReadableStream<Uint8Array>({ cancel() { cancelled = true; } });
  const response = new Response(stream, { status: 200 }); Object.defineProperty(response, "url", { value: NAI_PUBLIC_POST_URL }); resolveFetch(response);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(cancelled, true); assert.deepEqual(await readdir(dir), []);
});
test("a changed publicPosts query is refused before any fetch", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "nai-native-")); const old = process.env.CRE_NAI_NATIVE_CAPTURE_DIR; const oldFetch = globalThis.fetch;
  process.env.CRE_NAI_NATIVE_CAPTURE_DIR = dir; let calls = 0;
  t.after(() => { globalThis.fetch = oldFetch; if (old === undefined) delete process.env.CRE_NAI_NATIVE_CAPTURE_DIR; else process.env.CRE_NAI_NATIVE_CAPTURE_DIR = old; });
  globalThis.fetch = (async () => { calls++; return new Response('{}', { status: 200 }); }) as typeof fetch;
  await assert.rejects(naiGraphqlPost(NAI_PUBLIC_POST_URL, { ...request(), query: "changed" }, NAI_WIDGET_URL), /NAI native capture failed/);
  assert.equal(calls, 0); assert.deepEqual(await readdir(dir), []);
});
test("serializes concurrent publications at exact page and byte quota and preserves replay", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "nai-native-quota-")); const seed = await mkdtemp(join(tmpdir(), "nai-native-seed-"));
  const seedPath = await publishNaiNativePage(seed, context()); const entryBytes = (await stat(seedPath)).size;
  const oldPages = process.env.CRE_NAI_NATIVE_CAPTURE_MAX_PAGES; const oldBytes = process.env.CRE_NAI_NATIVE_CAPTURE_MAX_BYTES;
  t.after(() => { if (oldPages === undefined) delete process.env.CRE_NAI_NATIVE_CAPTURE_MAX_PAGES; else process.env.CRE_NAI_NATIVE_CAPTURE_MAX_PAGES = oldPages;
    if (oldBytes === undefined) delete process.env.CRE_NAI_NATIVE_CAPTURE_MAX_BYTES; else process.env.CRE_NAI_NATIVE_CAPTURE_MAX_BYTES = oldBytes; });
  process.env.CRE_NAI_NATIVE_CAPTURE_MAX_PAGES = "2"; process.env.CRE_NAI_NATIVE_CAPTURE_MAX_BYTES = String(entryBytes * 2);
  const second = { ...context(), finishedAt: "2026-10-28T12:00:02.000Z" };
  const third = { ...context(), finishedAt: "2026-10-28T12:00:03.000Z" };
  const results = await Promise.allSettled([publishNaiNativePage(dir, context()), publishNaiNativePage(dir, second), publishNaiNativePage(dir, third)]);
  assert.deepEqual(results.map(r => r.status), ["fulfilled", "fulfilled", "rejected"]);
  assert.equal((await readdir(dir)).length, 2);
  assert.equal(await publishNaiNativePage(dir, context()), (results[0] as PromiseFulfilledResult<string>).value);
  assert.equal((await readdir(dir)).length, 2);
});
test("rejects unowned writer assertion and unsafe or partial directory entries", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "nai-native-unsafe-"));
  t.after(() => { process.env.CRE_NAI_NATIVE_CAPTURE_SINGLE_WRITER = "1"; });
  delete process.env.CRE_NAI_NATIVE_CAPTURE_SINGLE_WRITER;
  await assert.rejects(publishNaiNativePage(dir, context()), /capture failed/);
  process.env.CRE_NAI_NATIVE_CAPTURE_SINGLE_WRITER = "1";
  await writeFile(join(dir, ".page-interrupted.tmp"), "partial", { mode: 0o600 });
  await assert.rejects(publishNaiNativePage(dir, context()), /capture failed/);
  assert.equal((await readdir(dir)).length, 1);
});
test("byte cap refuses before publication while preserving previous entries", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "nai-native-bytes-")); const first = await publishNaiNativePage(dir, context());
  const before = await readFile(first); const old = process.env.CRE_NAI_NATIVE_CAPTURE_MAX_BYTES;
  t.after(() => { if (old === undefined) delete process.env.CRE_NAI_NATIVE_CAPTURE_MAX_BYTES; else process.env.CRE_NAI_NATIVE_CAPTURE_MAX_BYTES = old; });
  process.env.CRE_NAI_NATIVE_CAPTURE_MAX_BYTES = String(before.length);
  await assert.rejects(publishNaiNativePage(dir, { ...context(), finishedAt: "2026-10-28T12:00:02.000Z" }), /capture failed/);
  assert.deepEqual(await readFile(first), before); assert.equal((await readdir(dir)).length, 1);
});
test("rejects a symlink masquerading as a published page", async () => {
  const dir = await mkdtemp(join(tmpdir(), "nai-native-link-")); const other = await mkdtemp(join(tmpdir(), "nai-native-outside-")); const outside = join(other, "outside");
  await writeFile(outside, "data", { mode: 0o600 });
  await symlink(outside, join(dir, `page-0-${"a".repeat(64)}.json`));
  await assert.rejects(publishNaiNativePage(dir, context()), /capture failed/);
});
