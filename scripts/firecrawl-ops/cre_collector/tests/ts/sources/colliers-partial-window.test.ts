import { test } from "node:test";
import assert from "node:assert/strict";
import { Readable } from "node:stream";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { observationNow } from "../../../lib/observation-clock.js";
import { withColliersPartialSpool } from "../../../sources/colliers-partial-custody.js";
import { ColliersWindowEvidence } from "../../../sources/colliers-partial-window-evidence.js";
import { reverifyColliersWindow } from "../../../sources/colliers-partial-window-reverify.js";
import { buildColliersWindowArtifact } from "../../../sources/colliers-partial-window-worker.js";
import { produceColliersWindow } from "../../../sources/colliers-partial-window-produce.js";
import { assertPartialNativeIdentity } from "../../../sources/colliers-partial-native-id.js";
import { ColliersSettledSourceStop } from "../../../sources/colliers-partial-fetch.js";
import { ColliersAccessStop, ColliersIdentityError } from "../../../sources/colliers.js";
import { selectColliersWindow } from "../../../sources/pure/colliers-partial-window.js";

const binding = { generation: "gen", attempt: 1, startedAt: "2026-11-04T15:00:00Z", finishedAt: "2026-11-04T16:00:00Z" };
const header = { schema: "colliers-partial-pages/v1", sourceKey: "colliers", generation: "gen", attempt: 1 };
const list = { success: true, total: 3, totalAvail: 3, numProjects: 2, html: ["a", "b"].map(x => `<li class="item"><span class="headline">Example</span><a href="/handler/modern.aspx?pv=${x}">View</a></li>`).join("") };
const map = { success: true, total: 3, totalAvail: 3, projectLocations: [{ ProjectId: "a" }, { ProjectId: "b" }] };
const pair = { observedAt: "2026-11-04T15:30:00.000Z", transaction: "sale", pass: 1, page: 1, start: 1, pageSize: 2, kind: "unqualified_response_pair", documents: [{ list: JSON.stringify(list), map: JSON.stringify(map) }] };
const page = { observedAt: "2026-11-04T15:30:01.000Z", transaction: "sale", pass: 1, page: 1, start: 1, kind: "parsed_page", documents: ["a", "b"].map(x => ({ mapProjectId: x, detailPv: x, detailUrl: `https://my.rcm1.com/handler/modern.aspx?pv=${x}` })) };
const hole = { transaction: "sale", pass: 1, page: 2, start: 3, observedAt: "2026-11-04T15:30:02.000Z", kind: "recovery_hole", reason: "provider_failure", pageSize: 1, documents: [] };
function source(rows: unknown[]) {
  const text = rows.map(x => JSON.stringify(x)).join("\n") + "\n";
  return () => Readable.from([text]);
}
test("rotates a bounded window and reports peers outside it", async () => {
  const open = source([header,pair,page,hole]);
  let checks = 0;
  const first = await selectColliersWindow(open, async () => { checks++; }, binding, 1, 1);
  assert.deepEqual(first.eligible.map(x => x.mapProjectId), ["a"]);
  assert.equal(first.heldOther, 1); assert.equal(checks, 2);
  const second = await selectColliersWindow(open, async () => {}, binding, 2, 1);
  assert.deepEqual(second.eligible.map(x => x.mapProjectId), ["b"]);
  const wrapped = await selectColliersWindow(open, async () => {}, binding, 3, 1);
  assert.deepEqual(wrapped.eligible.map(x => x.mapProjectId), ["a"]);
  const shrunk = await selectColliersWindow(open, async () => {}, binding, 100, 1);
  assert.deepEqual(shrunk.eligible.map(x => x.mapProjectId), ["a"]);
});
test("holds candidates overlapping another page or a recovery hole", async () => {
  const duplicate = await selectColliersWindow(source([header,pair,page,pair,page,hole]), async () => {}, binding, 1, 1);
  assert.equal(duplicate.eligible.length, 0); assert.equal(duplicate.heldSelected, 1);
  const overlap = await selectColliersWindow(source([header,pair,page,{...hole,start:1}]), async () => {}, binding, 1, 1);
  assert.equal(overlap.eligible.length, 0);
});
test("refuses unidentified observed pages and changed custody", async () => {
  await assert.rejects(selectColliersWindow(source([header,pair,page,hole,pair]), async () => {}, binding, 1, 1), /safely scoped/);
  await assert.rejects(selectColliersWindow(source([header,pair,page,hole]), async () => { throw new Error("changed"); }, binding, 1, 1), /changed/);
});

function driftRows(id = "c") {
  const driftList = { ...list, total: 4, totalAvail: 4, numProjects: 1,
    html: `<li class="item"><span class="headline">Changed</span><a href="/handler/modern.aspx?pv=${id}">View</a></li>` };
  const driftMap = { ...map, total: 4, totalAvail: 4, numProjects: 1, projectLocations: [{ ProjectId: id }] };
  const driftPair = { ...pair, page: 2, start: 3, pageSize: 1,
    documents: [{ list: JSON.stringify(driftList), map: JSON.stringify(driftMap) }] };
  const driftHole = { ...hole, page: 2, start: 3, reason: "count_drift" };
  return [header, pair, page, driftPair, driftHole];
}

test("holds count-drifted pages while retaining earlier identity-verified peers", async () => {
  const selection = await selectColliersWindow(source(driftRows()), async () => {}, binding, 1, 2);
  assert.deepEqual(selection.eligible.map(row => row.mapProjectId), ["a", "b"]);
  assert.equal(selection.heldSelected, 0);
  const duplicate = await selectColliersWindow(source(driftRows("a")), async () => {}, binding, 1, 2);
  assert.deepEqual(duplicate.eligible.map(row => row.mapProjectId), ["b"]);
  assert.equal(duplicate.heldSelected, 1);
  const reordered = driftRows("a") as any[];
  const mapBody = JSON.parse(reordered[3].documents[0].map);
  mapBody.projectLocations[0].ProjectId = "b";
  reordered[3].documents[0].map = JSON.stringify(mapBody);
  const conflicts = await selectColliersWindow(source(reordered), async () => {}, binding, 1, 2);
  assert.equal(conflicts.eligible.length, 0);
  assert.equal(conflicts.heldSelected, 2);
});

test("accepts only a verified short changed-count final response as held evidence", async () => {
  const short = structuredClone(driftRows()) as any[];
  for (const field of ["list", "map"]) {
    const prior = JSON.parse(short[1].documents[0][field]); prior.total = 4; prior.totalAvail = 4;
    short[1].documents[0][field] = JSON.stringify(prior);
    const changed = JSON.parse(short[3].documents[0][field]); changed.total = 3;
    short[3].documents[0][field] = JSON.stringify(changed);
  }
  short[3].pageSize = 100;
  short[4].pageSize = 2;
  const result = await selectColliersWindow(source(short), async () => {}, binding, 1, 2);
  assert.deepEqual(result.eligible.map(row => row.mapProjectId), ["a", "b"]);
  assert.equal(result.holes, 1);
  short.pop();
  await assert.rejects(selectColliersWindow(source(short), async () => {}, binding, 1, 2), /safely scoped/);
});

test("count drift cannot conceal malformed, unmatched or later source evidence", async () => {
  const rows = driftRows();
  const malformed = structuredClone(rows) as any[];
  const payload = malformed[3].documents[0];
  const mismatched = JSON.parse(payload.map); mismatched.total = 5; payload.map = JSON.stringify(mismatched);
  await assert.rejects(selectColliersWindow(source(malformed), async () => {}, binding, 1, 2), /safely scoped/);
  const unmatched = structuredClone(rows) as any[]; unmatched[4].start = 2;
  await assert.rejects(selectColliersWindow(source(unmatched), async () => {}, binding, 1, 2), /safely scoped/);
  const ambiguous = structuredClone(rows) as any[]; ambiguous[4].reason = "recovery_stopped";
  await assert.rejects(selectColliersWindow(source(ambiguous), async () => {}, binding, 1, 2), /safely scoped/);
  await assert.rejects(selectColliersWindow(source([...rows, pair, page]), async () => {}, binding, 1, 2), /safely scoped/);
  await assert.rejects(selectColliersWindow(source([header, pair, page, { ...hole, reason: "recovery_stopped" }]), async () => {}, binding, 1, 2), /safely scoped/);
  const missing = structuredClone(driftRows()) as any[];
  for (const field of ["list", "map"]) {
    const body = JSON.parse(missing[1].documents[0][field]); body.total = 4; body.totalAvail = 4;
    missing[1].documents[0][field] = JSON.stringify(body);
    const changed = JSON.parse(missing[3].documents[0][field]); changed.total = 5; changed.totalAvail = 5;
    missing[3].documents[0][field] = JSON.stringify(changed);
  }
  missing[3].start = 4; missing[4].start = 4;
  await assert.rejects(selectColliersWindow(source(missing), async () => {}, binding, 1, 2), /safely scoped/);
});

test("sealed worker replays a count-drift source without promoting the changed range", async t => {
  const dir = mkdtempSync(join(tmpdir(), "colliers-drift-worker-")); chmodSync(dir, 0o700);
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const bytes = Buffer.from(driftRows("a").map(row => JSON.stringify(row)).join("\n") + "\n");
  const path = join(dir, "source"); writeFileSync(path, bytes, { mode: 0o600 });
  const requests: string[] = [];
  const result = await buildColliersWindowArtifact({
    ...binding, generationStartedAt: binding.startedAt, cursor: 1, limit: 2,
    sourcePath: path, sourceBytes: bytes.length, sourceSha256: createHash("sha256").update(bytes).digest("hex"),
    originalSourceMainSha256: "c".repeat(64), producerSha: "a".repeat(40), targetSha256: "b".repeat(64),
    evidencePath: join(dir, "evidence"), artifactPath: join(dir, "artifact"),
  }, async token => {
    requests.push(token);
    const decodedText = JSON.stringify({ ProjectSummary: { ProjectId: token } });
    return { detail: JSON.parse(decodedText), decodedText, nativeBytes: Buffer.from(decodedText) };
  });
  assert.deepEqual(requests, ["b"]);
  assert.equal(result.admitted, 1); assert.equal(result.held, 1);
  assert.equal(result.proposedCursor, 3);
  const artifact = JSON.parse(readFileSync(join(dir, "artifact"), "utf8"));
  assert.equal(artifact.coverage, "unverified");
  assert.equal(artifact.sourceOutcome, "source_error");
  assert.deepEqual(artifact.listings.map((row: any) => row.id), ["b"]);
});

test("producer obeys rotating order and stops after preserving the current row", async () => {
  const requests: string[] = [], saved: string[] = [];
  const result = await produceColliersWindow(source([header,pair,page,hole]), async () => {}, binding, 2,
    async item => { saved.push(item.listing.id); return "stop"; }, async () => {},
    async token => { requests.push(token); const decodedText = JSON.stringify({ ProjectSummary: { ProjectId: token } });
      return { detail: JSON.parse(decodedText), decodedText, nativeBytes: Buffer.from(decodedText) }; }, 2);
  assert.deepEqual(requests, ["b"]); assert.deepEqual(saved, ["b"]);
  assert.equal(result.unattempted, 1); assert.equal(result.proposedCursor, 3);
});
test("producer never converts sink custody failure into a record hold", async () => {
  let held = 0;
  await assert.rejects(produceColliersWindow(source([header,pair,page,hole]), async () => {}, binding, 1,
    async () => { throw new ColliersIdentityError("sink failed"); }, async () => { held++; },
    async token => { const decodedText = JSON.stringify({ ProjectSummary: { ProjectId: token } });
      return { detail: JSON.parse(decodedText), decodedText, nativeBytes: Buffer.from(decodedText) }; }, 1), /sink failed/);
  assert.equal(held, 0);
});

test("byte budget stops only after storing complete native evidence", async t => {
  const dir = mkdtempSync(join(tmpdir(), "colliers-window-test-")); chmodSync(dir, 0o700);
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const path = join(dir, "evidence");
  const evidence = new ColliersWindowEvidence(path, "gen", 1, 1024);
  const native = Buffer.from(JSON.stringify({ ProjectSummary: { ProjectId: "a" }, padding: "x".repeat(4096) }));
  const sourceBytes = Buffer.from([header,pair,page,hole].map(row => JSON.stringify(row)).join("\n") + "\n");
  const sourcePath = join(dir, "source"); writeFileSync(sourcePath, sourceBytes, { mode: 0o600 });
  const result = await withColliersPartialSpool(sourcePath, sourceBytes.length, createHash("sha256").update(sourceBytes).digest("hex"),
    (open, verify) => produceColliersWindow(open, verify, binding, 1,
      async item => evidence.detail(item), async item => evidence.hold(item),
      async () => ({ detail: JSON.parse(native.toString()), decodedText: native.toString(), nativeBytes: native }), 2));
  const receipt = evidence.finish(), bytes = readFileSync(path);
  assert.equal(result.unattempted, 1); assert.equal(result.proposedCursor, 2);
  assert.equal(receipt.bytes, bytes.length);
  assert.equal(receipt.sha256, createHash("sha256").update(bytes).digest("hex"));
  const records = bytes.toString().trim().split("\n").map(line => JSON.parse(line));
  assert.deepEqual(Buffer.from(records[1].nativeBase64, "base64"), native);
  const saved: any[] = [];
  const replay = await withColliersPartialSpool(sourcePath, sourceBytes.length, createHash("sha256").update(sourceBytes).digest("hex"),
    (openSource, verifySource) => withColliersPartialSpool(path, receipt.bytes, receipt.sha256,
      (openEvidence, verifyEvidence) => reverifyColliersWindow(openSource, verifySource, openEvidence, verifyEvidence,
        { ...binding, detailFinishedAt: new Date(Date.now() + 1000).toISOString() }, 1, receipt.records,
        result.proposedCursor, async item => { saved.push(item); }, 2)));
  assert.equal(replay.emitted, 1); assert.equal(saved[0].listing.id, "a");
  await assert.rejects(withColliersPartialSpool(sourcePath, sourceBytes.length, createHash("sha256").update(sourceBytes).digest("hex"),
    (openSource, verifySource) => withColliersPartialSpool(path, receipt.bytes, receipt.sha256,
      (openEvidence, verifyEvidence) => reverifyColliersWindow(openSource, verifySource, openEvidence, verifyEvidence,
        { ...binding, detailFinishedAt: new Date(Date.now() + 1000).toISOString() }, 1, receipt.records,
        3, async () => {}, 2))), /cursor differs/);
  const altered = Buffer.from(native); altered[0] ^= 1;
  records[1].nativeBase64 = altered.toString("base64");
  const forgedBytes = Buffer.from(records.map(record => JSON.stringify(record)).join("\n") + "\n");
  const forgedPath = join(dir, "forged"); writeFileSync(forgedPath, forgedBytes, { mode: 0o600 });
  await assert.rejects(withColliersPartialSpool(sourcePath, sourceBytes.length, createHash("sha256").update(sourceBytes).digest("hex"),
    (openSource, verifySource) => withColliersPartialSpool(forgedPath, forgedBytes.length, createHash("sha256").update(forgedBytes).digest("hex"),
      (openEvidence, verifyEvidence) => reverifyColliersWindow(openSource, verifySource, openEvidence, verifyEvidence,
        { ...binding, detailFinishedAt: new Date(Date.now() + 1000).toISOString() }, 1, receipt.records,
        result.proposedCursor, async () => {}, 2))), /native hash differs/);
});

test("a settled provider stop records the hold without fetching later peers", async () => {
  let calls = 0; const reasons: string[] = [];
  const result = await produceColliersWindow(source([header,pair,page,hole]), async () => {}, binding, 1,
    async () => "continue", async item => { reasons.push(item.reason); },
    async () => { calls++; throw new ColliersSettledSourceStop("detail_http_failure", 503); }, 2);
  assert.equal(calls, 1); assert.deepEqual(reasons, ["detail_http_failure"]);
  assert.equal(result.unattempted, 1); assert.equal(result.proposedCursor, 1);
});
test("access refusal aborts without fetching later peers", async () => {
  let calls = 0;
  await assert.rejects(produceColliersWindow(source([header,pair,page,hole]), async () => {}, binding, 1,
    async () => "continue", async () => {},
    async () => { calls++; throw new ColliersAccessStop("denied"); }, 2), /denied/);
  assert.equal(calls, 1);
});

test("replay refuses evidence after a source-wide stop", async t => {
  const dir = mkdtempSync(join(tmpdir(), "colliers-window-stop-")); chmodSync(dir, 0o700);
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const sourceBytes = Buffer.from([header,pair,page,hole].map(row => JSON.stringify(row)).join("\n") + "\n");
  const sourcePath = join(dir, "source"); writeFileSync(sourcePath, sourceBytes, { mode: 0o600 });
  const sourceHash = createHash("sha256").update(sourceBytes).digest("hex"), path = join(dir, "detail");
  const evidence = new ColliersWindowEvidence(path, "gen", 1);
  const result = await withColliersPartialSpool(sourcePath, sourceBytes.length, sourceHash, (open, verify) =>
    produceColliersWindow(open, verify, binding, 1, async item => evidence.detail(item),
      async item => evidence.hold(item), async () => { throw new ColliersSettledSourceStop("detail_http_failure", 503); }, 2));
  const receipt = evidence.finish();
  const replay = await withColliersPartialSpool(sourcePath, sourceBytes.length, sourceHash, (openSource, verifySource) =>
    withColliersPartialSpool(path, receipt.bytes, receipt.sha256, (openEvidence, verifyEvidence) =>
      reverifyColliersWindow(openSource, verifySource, openEvidence, verifyEvidence,
        { ...binding, detailFinishedAt: observationNow() }, 1, 1, result.proposedCursor, async () => {}, 2)));
  assert.equal(replay.emitted, 0); assert.equal(replay.held, 1);
  const records = readFileSync(path, "utf8").trim().split("\n").map(line => JSON.parse(line));
  records.push({ ...records[1], candidate: { ...records[1].candidate, mapProjectId: "b" } });
  const forged = Buffer.from(records.map(row => JSON.stringify(row)).join("\n") + "\n");
  const forgedPath = join(dir, "forged"); writeFileSync(forgedPath, forged, { mode: 0o600 });
  await assert.rejects(withColliersPartialSpool(sourcePath, sourceBytes.length, sourceHash, (openSource, verifySource) =>
    withColliersPartialSpool(forgedPath, forged.length, createHash("sha256").update(forged).digest("hex"), (openEvidence, verifyEvidence) =>
      reverifyColliersWindow(openSource, verifySource, openEvidence, verifyEvidence,
        { ...binding, detailFinishedAt: observationNow() }, 1, 2, 3, async () => {}, 2))), /continues after source stop/);
});

test("unsupported control-character token does not block a supported peer", async () => {
  const alteredList = { ...list, html: list.html.replace("pv=a", "pv=a%00") };
  const alteredPair = { ...pair, documents: [{ list: JSON.stringify(alteredList), map: JSON.stringify(map) }] };
  const alteredPage = { ...page, documents: [
    { mapProjectId: "a", detailPv: "a\u0000", detailUrl: "https://my.rcm1.com/handler/modern.aspx?pv=a%00" },
    page.documents[1],
  ] };
  const result = await selectColliersWindow(source([header,alteredPair,alteredPage,hole]), async () => {}, binding, 1, 2);
  assert.deepEqual(result.eligible.map(item => item.mapProjectId), ["b"]);
  assert.equal(result.heldOther, 1);
});

test("replay proves held response classes and exact page identity", async () => {
  const open = source([header,pair,page,hole]);
  const selected = await selectColliersWindow(open, async () => {}, binding, 1, 1);
  const candidate = selected.window[0].candidate;
  const observedAt = new Date().toISOString();
  const full = JSON.stringify({ ProjectSummary: { ProjectId: "a" } });
  const native = (value: string | Uint8Array) => Buffer.from(value).toString("base64");
  const check = async (reason: string, body?: string | Uint8Array) => {
    const record = { kind: "held", candidate, reason, observedAt,
      ...(body === undefined ? {} : { nativeBase64: native(body) }) };
    const data = [ { schema: "colliers-window-evidence/v1", sourceKey: "colliers", generation: "gen", attempt: 1 }, record ];
    return reverifyColliersWindow(open, async () => {}, source(data), async () => {},
      { ...binding, detailFinishedAt: new Date(Date.now() + 1000).toISOString() },
      1, 1, ["detail_http_failure", "detail_timeout", "detail_provider_rejected", "detail_nonjson_html"].includes(reason) ? 1 : 2,
      async () => {}, 1);
  };
  await assert.rejects(check("detail_identity"), /metadata differs/);
  await assert.rejects(check("detail_identity", full), /identity hold classification differs/);
  await assert.rejects(check("detail_invalid_json", full), /held classification differs/);
  await assert.rejects(check("detail_nonjson_html", full), /held classification differs/);
  await assert.rejects(check("detail_provider_rejected", full), /held classification differs/);
  const accepted = await check("detail_identity", JSON.stringify({ ProjectSummary: { ProjectId: "different" } }));
  assert.equal(accepted.held, 1); assert.equal(accepted.emitted, 0);
  for (const [reason, body] of [
    ["detail_empty", ""], ["detail_invalid_json", "{"], ["detail_invalid_response", "[]"],
    ["detail_invalid_utf8", new Uint8Array([255])], ["detail_nonjson_html", "<html>"],
    ["detail_provider_rejected", JSON.stringify({ success: false })],
  ] as const) assert.equal((await check(reason, body)).held, 1);
  await assert.rejects(check("detail_invalid_utf8", full), /encoding differs/);
  await assert.rejects(check("detail_invalid_response", full), /classification differs/);
  await assert.rejects(check("detail_empty", "x"), /bytes differ/);
  await assert.rejects(check("detail_oversized", new Uint8Array(16 * 1024 * 1024)), /bytes differ/);
  assert.equal((await check("detail_oversized", new Uint8Array(16 * 1024 * 1024 + 1))).held, 1);
});

test("native ProjectId accepts exact safe integers but refuses booleans and unsafe numbers", () => {
  assert.doesNotThrow(() => assertPartialNativeIdentity({ ProjectSummary: { ProjectId: 1.0 } }, "1"));
  assert.throws(() => assertPartialNativeIdentity({ ProjectSummary: { ProjectId: true } }, "true"), ColliersIdentityError);
  assert.throws(() => assertPartialNativeIdentity({ ProjectSummary: { ProjectId: 1.5 } }, "1.5"), ColliersIdentityError);
  assert.throws(() => assertPartialNativeIdentity({ ProjectSummary: { ProjectId: 9007199254740992 } }, "9007199254740992"), ColliersIdentityError);
});

test("worker seals an all-held source stop without claiming an admitted listing", async t => {
  const dir = mkdtempSync(join(tmpdir(), "colliers-window-worker-")); chmodSync(dir, 0o700);
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const bytes = Buffer.from([header,pair,page,hole].map(row => JSON.stringify(row)).join("\n") + "\n");
  const sourcePath = join(dir, "source"), artifactPath = join(dir, "artifact");
  writeFileSync(sourcePath, bytes, { mode: 0o600 });
  const result = await buildColliersWindowArtifact({ ...binding, sourcePath, sourceBytes: bytes.length,
    sourceSha256: createHash("sha256").update(bytes).digest("hex"), evidencePath: join(dir, "evidence"),
    artifactPath, cursor: 1, generationStartedAt: "2026-11-04T14:00:00Z", producerSha: "a".repeat(40), targetSha256: "b".repeat(64),
    originalSourceMainSha256: "c".repeat(64) },
    async () => { throw new ColliersSettledSourceStop("detail_http_failure", 503); });
  assert.equal(result.admitted, 0); assert.equal(result.held, 1); assert.equal(result.unattempted, 1);
  const artifact = JSON.parse(readFileSync(artifactPath, "utf8"));
  assert.deepEqual(artifact.listings, []); assert.equal(artifact.sourceOutcome, "source_error");
  assert.equal(artifact.coverage, "unverified"); assert.equal(artifact.targetSha256, "b".repeat(64));
});

test("worker entry refuses a request without its canonical owned lock", async t => {
  const dir = mkdtempSync(join(tmpdir(), "colliers-entry-")); chmodSync(dir, 0o700);
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const runDir = join(dir, "gen"), partialDir = join(runDir, "colliers-partial");
  mkdirSync(runDir, { mode: 0o700 }); mkdirSync(partialDir, { mode: 0o700 });
  const request = { ...binding, generationStartedAt: "2026-11-04T14:00:00Z", runDir,
    sourceBytes: 1, sourceSha256: "a".repeat(64), originalSourceMainSha256: "b".repeat(64),
    producerSha: "c".repeat(40), targetSha256: "d".repeat(64), cursor: 1,
    lockLease: "z".repeat(43), authorityToken: "y".repeat(43), ownerPid: process.pid };
  const path = join(partialDir, "window-1-request.json");
  writeFileSync(path, JSON.stringify(request), { mode: 0o600 });
  const child = spawnSync(process.execPath, ["--import", "tsx", "sources/colliers-partial-window-entry.ts"],
    { cwd: process.cwd(), env: { ...process.env, COLLIERS_PARTIAL_WORKER_REQUEST: path }, encoding: "utf8", timeout: 10000 });
  assert.notEqual(child.status, 0);
  assert.match(child.stderr, /Colliers partial worker failed/);
});

test("default rotating windows admit bounded peers and resume from cursor", async () => {
  const ids = Array.from({length: 120}, (_, i) => "id" + (i + 1));
  const payload = { success: true, total: 121, totalAvail: 121, numProjects: 120,
    html: ids.map(id => `<li class="item"><span class="headline">Example</span><a href="/handler/modern.aspx?pv=${id}">View</a></li>`).join("") };
  const locations = { success: true, total: 121, totalAvail: 121, projectLocations: ids.map(ProjectId => ({ ProjectId })) };
  const paired = { ...pair, pageSize: 120, documents: [{ list: JSON.stringify(payload), map: JSON.stringify(locations) }] };
  const parsed = { ...page, documents: ids.map(id => ({ mapProjectId: id, detailPv: id,
    detailUrl: `https://my.rcm1.com/handler/modern.aspx?pv=${id}` })) };
  const missing = { ...hole, start: 121 };
  const open = source([header, paired, parsed, missing]);
  const first = await selectColliersWindow(open, async () => {}, binding, 1);
  assert.equal(first.selected, 100);
  assert.equal(first.eligible.length, 100);
  assert.equal(first.heldOther, 20);
  const next = await selectColliersWindow(open, async () => {}, binding, 101);
  assert.equal(next.selected, 100);
  assert.equal(next.window[0].candidate.start + next.window[0].candidate.offset, 101);
  assert.equal(next.window[19].candidate.start + next.window[19].candidate.offset, 120);
});

test("worker accepts honest independent source and detail clocks", async t => {
  const dir = mkdtempSync(join(tmpdir(), "colliers-cross-clock-")); chmodSync(dir, 0o700);
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const bytes = Buffer.from([header,pair,page,hole].map(row => JSON.stringify(row)).join("\n") + "\n");
  const sourcePath = join(dir, "source"), artifactPath = join(dir, "artifact");
  writeFileSync(sourcePath, bytes, { mode: 0o600 });
  const sourceFinish = new Date(Date.now() + 30_000).toISOString();
  const result = await buildColliersWindowArtifact({ ...binding, finishedAt: sourceFinish,
    sourcePath, sourceBytes: bytes.length, sourceSha256: createHash("sha256").update(bytes).digest("hex"),
    evidencePath: join(dir, "evidence"), artifactPath, cursor: 1, limit: 1,
    generationStartedAt: "2026-11-04T14:00:00Z", producerSha: "a".repeat(40), targetSha256: "b".repeat(64),
    originalSourceMainSha256: "c".repeat(64) },
    async token => { const decodedText = JSON.stringify({ ProjectSummary: { ProjectId: token } });
      return { detail: JSON.parse(decodedText), decodedText, nativeBytes: Buffer.from(decodedText) }; });
  assert.equal(result.admitted, 1);
  const artifact = JSON.parse(readFileSync(artifactPath, "utf8"));
  assert.equal(artifact.binding.finishedAt, sourceFinish);
  assert.ok(Date.parse(artifact.listings[0].detailObservedAt) < Date.parse(sourceFinish));
});

test("source observation beyond the capture interval has an actionable safe category", async () => {
  await assert.rejects(
    selectColliersWindow(source([header,pair,page,hole]), async () => {},
      { ...binding, finishedAt: "2026-11-04T15:29:59Z" }, 1),
    /partial source observation clock outside capture interval/,
  );
});

test("source-shaped clock step binds pages to genuine child finish", async () => {
  const rows = [header,
    { ...pair, observedAt: "2026-11-04T16:00:00.800Z" },
    { ...page, observedAt: "2026-11-04T16:00:00.801Z" },
    { ...hole, observedAt: "2026-11-04T16:00:00.802Z" }];
  await assert.rejects(selectColliersWindow(source(rows), async () => {}, binding, 1),
    /source observation clock outside capture interval/);
  const result = await selectColliersWindow(source(rows), async () => {},
    { ...binding, finishedAt: "2026-11-04T16:00:01.000Z" }, 1);
  assert.equal(result.selected, 2);
  assert.equal(result.eligible.length, 2);
});
