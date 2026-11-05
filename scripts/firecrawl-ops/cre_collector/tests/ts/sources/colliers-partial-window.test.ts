import { test } from "node:test";
import assert from "node:assert/strict";
import { Readable } from "node:stream";
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
