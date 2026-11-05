import { test } from "node:test";
import assert from "node:assert/strict";
import { Readable } from "node:stream";
import { createHash } from "node:crypto";
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { withColliersPartialSpool } from "../../../sources/colliers-partial-custody.js";
import { ColliersWindowEvidence } from "../../../sources/colliers-partial-window-evidence.js";
import { reverifyColliersWindow } from "../../../sources/colliers-partial-window-reverify.js";
import { produceColliersWindow } from "../../../sources/colliers-partial-window-produce.js";
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
  assert.equal(result.unattempted, 1); assert.equal(result.proposedCursor, 2);
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
        { ...binding, detailFinishedAt: new Date().toISOString() }, 1, 1, result.proposedCursor, async () => {}, 2)));
  assert.equal(replay.emitted, 0); assert.equal(replay.held, 1);
  const records = readFileSync(path, "utf8").trim().split("\n").map(line => JSON.parse(line));
  records.push({ ...records[1], candidate: { ...records[1].candidate, mapProjectId: "b" } });
  const forged = Buffer.from(records.map(row => JSON.stringify(row)).join("\n") + "\n");
  const forgedPath = join(dir, "forged"); writeFileSync(forgedPath, forged, { mode: 0o600 });
  await assert.rejects(withColliersPartialSpool(sourcePath, sourceBytes.length, sourceHash, (openSource, verifySource) =>
    withColliersPartialSpool(forgedPath, forged.length, createHash("sha256").update(forged).digest("hex"), (openEvidence, verifyEvidence) =>
      reverifyColliersWindow(openSource, verifySource, openEvidence, verifyEvidence,
        { ...binding, detailFinishedAt: new Date().toISOString() }, 1, 2, 3, async () => {}, 2))), /continues after source stop/);
});
