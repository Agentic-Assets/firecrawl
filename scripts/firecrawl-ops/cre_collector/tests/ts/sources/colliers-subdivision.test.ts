import assert from "node:assert/strict";
import { test } from "node:test";
import { srcColliers } from "../../../sources/colliers.js";

function page(start: number, size: number, total = 6) {
  const ids = Array.from({ length: size }, (_, i) => `P${start + i}`);
  return {
    list: { success: true, total, totalAvail: total, numProjects: size,
      html: `<ul>${ids.map(id => `<li class="item"><a class="headline">Property ${id}</a></li>`).join("")}</ul>` },
    map: { success: true, projectLocations: ids.map((ProjectId, i) => ({ ProjectId, Latitude: 30 + i, Longitude: -90 - i })) },
  };
}

function install(t: { after(fn: () => void): void }, handler: (url: URL) => Response) {
  const prior = globalThis.fetch;
  t.after(() => { globalThis.fetch = prior; });
  globalThis.fetch = (async (input: RequestInfo | URL) => handler(new URL(String(input)))) as typeof fetch;
}

function response(value: unknown, status = 200, headers?: Record<string, string>) {
  return new Response(JSON.stringify(value), { status, headers });
}

test("retains verified subdivisions and one explicit hole, preserving the original failure", async t => {
  const parsed: any[] = [], raw: any[] = [], holes: any[] = [], calls: string[] = [];
  install(t, url => {
    if (url.hostname === "sales.colliers.com") return new Response("<html></html>");
    const start = Number(url.searchParams.get("Start"));
    const size = Number(url.searchParams.get("PageSize"));
    const list = url.pathname.endsWith("GetListingsHtml");
    calls.push(`${start}:${size}:${list ? "list" : "map"}`);
    if (start === 1) return response(page(1, 2)[list ? "list" : "map"]);
    if (start <= 4 && start + size > 4) return response({ success: false, error: list ? "Failure retrieving listing HTML." : "Failure retrieving map." });
    return response(page(start, size)[list ? "list" : "map"]);
  });
  await assert.rejects(srcColliers("sale", 6, false,
    (page, start, cards, size) => parsed.push({ page, start, size, cards }),
    (page, start, attempt, bodies, size) => raw.push({ page, start, attempt, bodies, size }),
    (page, start, size, reason) => holes.push({ page, start, size, reason }), () => true), /success=false/);
  assert.deepEqual(parsed.map(x => [x.start, x.cards.length]), [[1, 2], [5, 2], [3, 1]]);
  assert.deepEqual(holes.map(x => [x.start, x.size, x.reason]), [[4, 1, "provider_failure"]]);
  assert.ok(raw.some(x => x.start === 3 && x.size === 2));
  assert.ok(raw.some(x => x.start === 4 && x.size === 1));
  assert.equal(calls.length, 12);
  assert.ok(parsed.every(x => x.cards.every((card: any) => card.inventoryOnly.reason === "detail_not_attempted")));
});

test("recovery stops on an observed denial, changing total, or duplicate identity", async t => {
  let mode: "denial" | "rate" | "retry-after" | "total" | "available" | "duplicate" | "count" | "parity" | "capture" = "denial";
  let requests = 0;
  install(t, url => {
      if (url.hostname === "sales.colliers.com") return new Response("<html></html>");
      const start = Number(url.searchParams.get("Start")), size = Number(url.searchParams.get("PageSize"));
      const list = url.pathname.endsWith("GetListingsHtml"); requests++;
      if (start === 1) return response(page(1, 2)[list ? "list" : "map"]);
      if (size === 4) return response({ success: false, error: list ? "Failure retrieving listing HTML." : "Failure retrieving map." });
      if (mode === "denial") return response({}, 403);
      if (mode === "rate") return response({}, 429);
      if (mode === "retry-after") return response({ success: false, error: "Failure retrieving listing HTML." }, 200, { "retry-after": "120" });
      const result = page(mode === "duplicate" ? 1 : start, size, mode === "total" ? 7 : 6);
      if (mode === "available") result.list.totalAvail = 7;
      if (mode === "count") result.list.numProjects = size - 1;
      if (mode === "parity") result.map.projectLocations.pop();
      return response(result[list ? "list" : "map"]);
  });
  for (mode of ["denial", "rate", "retry-after", "total", "available", "duplicate", "count", "parity", "capture"] as const) {
    requests = 0;
    const records: unknown[] = [];
    let writable = true;
    await assert.rejects(srcColliers("sale", 6, false, () => {}, (page) => { if (mode === "capture" && page === 2 && requests >= 6) writable = false; }, (...x) => records.push(x), () => writable), /success=false; recovery stopped/);
    assert.equal(requests, 6, `${mode} must not issue a further pair`);
    assert.equal(records.length, 0);
  }
});

test("a bounded all-failure page records unexamined ranges without claiming completeness", async t => {
  let requests = 0;
  install(t, url => {
    if (url.hostname === "sales.colliers.com") return new Response("<html></html>");
    const start = Number(url.searchParams.get("Start"));
    const list = url.pathname.endsWith("GetListingsHtml"); requests++;
    if (start === 1) return response(page(1, 1, 101)[list ? "list" : "map"]);
    return response({ success: false, error: list ? "Failure retrieving listing HTML." : "Failure retrieving map." });
  });
  const holes: Array<{ size: number; reason: string }> = [];
  await assert.rejects(srcColliers("sale", 101, false, () => {}, () => {}, (_page, _start, size, reason) => holes.push({ size, reason }), () => true), /success=false/);
  assert.equal(requests, 44); // initial success, original failure, and at most 20 recovery pairs
  assert.equal(holes.reduce((n, hole) => n + hole.size, 0), 100);
  assert.ok(holes.some(hole => hole.reason === "budget_exhausted"));
});

test("initial page denial or unrecognized application failure never triggers subdivision", async t => {
  let mode: "mixed-denial" | "unknown" = "mixed-denial", requests = 0;
  install(t, url => {
    if (url.hostname === "sales.colliers.com") return new Response("<html></html>");
    const start = Number(url.searchParams.get("Start"));
    const list = url.pathname.endsWith("GetListingsHtml"); requests++;
    if (start === 1) return response(page(1, 2)[list ? "list" : "map"]);
    if (mode === "mixed-denial" && !list) return response({}, 403);
    return response({ success: false, error: mode === "unknown" ? "unknown provider failure" : "Failure retrieving listing HTML." });
  });
  for (mode of ["mixed-denial", "unknown"] as const) {
    requests = 0;
    await assert.rejects(srcColliers("sale", 6, false, () => {}, () => {}, () => {}, () => true), mode === "mixed-denial" ? /HTTP 403/ : /success=false/);
    assert.equal(requests, 4);
  }
});
