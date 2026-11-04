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
  let mode: "denial" | "rate" | "retry-after" | "transient" | "total" | "available" | "duplicate" | "count" | "parity" | "capture" = "denial";
  let requests = 0;
  install(t, url => {
      if (url.hostname === "sales.colliers.com") return new Response("<html></html>");
      const start = Number(url.searchParams.get("Start")), size = Number(url.searchParams.get("PageSize"));
      const list = url.pathname.endsWith("GetListingsHtml"); requests++;
      if (start === 1) return response(page(1, 2)[list ? "list" : "map"]);
      if (size === 4) return response({ success: false, error: list ? "Failure retrieving listing HTML." : "Failure retrieving map." });
      if (mode === "denial") return response({}, 403);
      if (mode === "rate") return response({}, 429);
      if (mode === "transient") return response({}, 503);
      if (mode === "retry-after") return response({ success: false, error: "Failure retrieving listing HTML." }, 200, { "retry-after": "120" });
      const result = page(mode === "duplicate" ? 1 : start, size, mode === "total" ? 7 : 6);
      if (mode === "available") result.list.totalAvail = 7;
      if (mode === "count") result.list.numProjects = size - 1;
      if (mode === "parity") result.map.projectLocations.pop();
      return response(result[list ? "list" : "map"]);
  });
  for (mode of ["denial", "rate", "retry-after", "transient", "total", "available", "duplicate", "count", "parity", "capture"] as const) {
    requests = 0;
    const records: unknown[] = [];
    let writable = true;
    await assert.rejects(srcColliers("sale", 6, false, () => {}, (page) => { if (mode === "capture" && page === 2 && requests >= 6) writable = false; }, (...x) => records.push(x), () => writable), mode === "capture" ? /success=false; recovery stopped:.*capture failed and evidence is unverified/ : /success=false; recovery stopped/);
    assert.equal(requests, 6, `${mode} must not issue a further pair`);
    if (mode === "capture") assert.equal(records.length, 0);
    else assert.deepEqual(records.map((row: any) => [row[1], row[2], row[3]]),
      [[3, 2, "recovery_stopped"], [5, 2, "recovery_stopped"]]);
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

test("semantic failure waits for delayed sibling denial or Retry-After before deciding", async t => {
  const prior = globalThis.fetch; t.after(() => { globalThis.fetch = prior; });
  let mode: "403" | "429" | "retry-after" = "403", requests = 0;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    if (url.hostname === "sales.colliers.com") return new Response("<html></html>");
    const start = Number(url.searchParams.get("Start")), list = url.pathname.endsWith("GetListingsHtml"); requests++;
    if (start === 1) return response(page(1, 2)[list ? "list" : "map"]);
    if (list) return response({ success: false, error: "Failure retrieving listing HTML." });
    await new Promise<void>(resolve => setTimeout(resolve, 50));
    assert.equal(init?.signal?.aborted, false, "semantic failure must not cancel the sibling");
    if (mode === "retry-after") return response({ success: true, projectLocations: [] }, 200, { "retry-after": "60" });
    return response({}, Number(mode));
  }) as typeof fetch;
  for (mode of ["403", "429", "retry-after"] as const) {
    requests = 0;
    await assert.rejects(srcColliers("sale", 6, false, () => {}, () => {}, () => {}, () => true), mode === "retry-after" ? /Retry-After/ : new RegExp(`HTTP ${mode}`));
    assert.equal(requests, 4, "no recovery pair follows a delayed stop signal");
  }
});


test("retains later pages after a settled provider hole without admitting the source", async t => {
  const parsed: any[] = [], holes: any[] = [], calls: string[] = [];
  let active = 0, peak = 0;
  const prior = globalThis.fetch; t.after(() => { globalThis.fetch = prior; });
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = new URL(String(input));
    if (url.hostname === "sales.colliers.com") return new Response("<html></html>");
    assert.ok(url.pathname.endsWith("GetListingsHtml") || url.pathname.endsWith("GetMapData"), "no detail request");
    const start = Number(url.searchParams.get("Start")), size = Number(url.searchParams.get("PageSize"));
    const list = url.pathname.endsWith("GetListingsHtml");
    calls.push(`${start}:${size}:${list ? "list" : "map"}`);
    active++; peak = Math.max(peak, active);
    await new Promise(resolve => setTimeout(resolve, 2));
    active--;
    if (start <= 101 && start + size > 101 && start !== 1)
      return response({ success: false, error: list ? "Failure retrieving listing HTML." : "Failure retrieving map." });
    return response(page(start, size, 203)[list ? "list" : "map"]);
  }) as typeof fetch;
  await assert.rejects(srcColliers("sale", 203, false,
    (pageNo, start, cards, size) => parsed.push({ pageNo, start, size, cards }),
    () => {}, (pageNo, start, size, reason) => holes.push({ pageNo, start, size, reason }),
    () => true), /success=false/);
  assert.ok(peak <= 2);
  assert.deepEqual(holes.map(x => [x.start, x.size, x.reason]), [[101, 1, "provider_failure"]]);
  assert.equal(parsed.reduce((sum, x) => sum + x.cards.length, 0), 202);
  assert.ok(parsed.some(x => x.start === 201 && x.cards.length === 3));
  assert.deepEqual(calls.slice(-2).sort(), ["201:3:list", "201:3:map"]);
  assert.ok(parsed.every(x => x.cards.every((card: any) => card.inventoryOnly.reason === "detail_not_attempted")));
  calls.length = 0;
  await assert.rejects(srcColliers("sale", 203, false), /success=false/);
  assert.equal(calls.length, 4, "without a private spool no recovery or tail requests run");
});

test("a tail denial or total drift stops immediately and records the unexamined range", async t => {
  const holes: any[] = [], calls: string[] = [];
  let mode: "denial" | "total" = "denial";
  install(t, url => {
    if (url.hostname === "sales.colliers.com") return new Response("<html></html>");
    const start = Number(url.searchParams.get("Start")), size = Number(url.searchParams.get("PageSize"));
    const list = url.pathname.endsWith("GetListingsHtml"); calls.push(`${start}:${size}`);
    if (start === 201) return mode === "denial" ? response({}, 403) : response(page(start, size, 304)[list ? "list" : "map"]);
    if (start <= 101 && start + size > 101 && start !== 1)
      return response({ success: false, error: list ? "Failure retrieving listing HTML." : "Failure retrieving map." });
    return response(page(start, size, 303)[list ? "list" : "map"]);
  });
  for (mode of ["denial", "total"] as const) {
    holes.length = 0; calls.length = 0;
    await assert.rejects(srcColliers("sale", 303, false, () => {}, () => {},
      (pageNo, start, size, reason) => holes.push({ pageNo, start, size, reason }), () => true),
      mode === "denial" ? /success=false; recovery stopped:.*403/ : /success=false; recovery stopped:.*total changed/);
    assert.ok(holes.some(x => x.start === 101 && x.size === 1 && x.reason === "provider_failure"));
    assert.ok(holes.some(x => x.start === 201 && x.size === 103 && x.reason === "recovery_stopped"));
    assert.ok(!calls.some(x => x.startsWith("301:")), "no page follows a stop signal");
  }
});


test("tail stops on rate limits, map drift, duplicates, capture loss, or a second failed page", async t => {
  const priorTimer = globalThis.setTimeout;
  (globalThis as any).setTimeout = (callback: (...args: any[]) => void, ms?: number, ...args: any[]) =>
    priorTimer(callback, ms === 1000 ? 0 : ms, ...args);
  t.after(() => { globalThis.setTimeout = priorTimer; });
  let mode: "429" | "retry-after" | "map-total" | "duplicate" | "capture" | "semantic" | "accounting" = "429";
  let writable = true;
  const calls: string[] = [], holes: any[] = [];
  install(t, url => {
    if (url.hostname === "sales.colliers.com") return new Response("<html></html>");
    const start = Number(url.searchParams.get("Start")), size = Number(url.searchParams.get("PageSize"));
    const list = url.pathname.endsWith("GetListingsHtml"); calls.push(`${start}:${size}`);
    if (start !== 1 && start <= 101 && start + size > 101)
      return response({ success: false, error: list ? "Failure retrieving listing HTML." : "Failure retrieving map." });
    if (start === 201) {
      if (mode === "429") return response({}, 429);
      if (mode === "accounting") return response({}, 403);
      if (mode === "retry-after" && list) return response({}, 200, { "retry-after": "60" });
      if (mode === "semantic") return response({ success: false, error: list ? "Failure retrieving listing HTML." : "Failure retrieving map." });
      const result = page(mode === "duplicate" ? 1 : start, size, 303);
      if (mode === "map-total") (result.map as any).total = 304;
      return response(result[list ? "list" : "map"]);
    }
    return response(page(start, size, 303)[list ? "list" : "map"]);
  });
  for (mode of ["429", "retry-after", "map-total", "duplicate", "capture", "semantic", "accounting"] as const) {
    calls.length = 0; holes.length = 0; writable = true;
    await assert.rejects(srcColliers("sale", mode === "accounting" ? 250 : 303, false, () => {},
      (_page, start) => { if (mode === "capture" && start === 201) writable = false; },
      (pageNo, start, size, reason) => {
        holes.push({ pageNo, start, size, reason });
        if (mode === "accounting" && reason === "requested_limit") writable = false;
      }, () => writable), mode === "accounting" ? /success=false; recovery stopped:.*403.*evidence is unverified/ : /success=false; recovery stopped/);
    assert.ok(!calls.some(value => value.startsWith("301:")), mode);
    if (mode === "capture" || mode === "accounting") assert.equal(writable, false);
    else if (mode === "semantic") {
      assert.ok(holes.some(x => x.start === 201 && x.size === 100 && x.reason === "provider_failure"));
      assert.ok(holes.some(x => x.start === 301 && x.size === 3 && x.reason === "recovery_stopped"));
    } else {
      assert.ok(holes.some(x => x.start === 201 && x.size === 103 && x.reason === "recovery_stopped"), mode);
    }
  }
});

test("finite maximum uses absolute offsets and records the unrequested remainder", async t => {
  const priorTimer = globalThis.setTimeout;
  (globalThis as any).setTimeout = (callback: (...args: any[]) => void, ms?: number, ...args: any[]) =>
    priorTimer(callback, ms === 1000 ? 0 : ms, ...args);
  t.after(() => { globalThis.setTimeout = priorTimer; });
  const holes: any[] = [], calls: string[] = [];
  install(t, url => {
    if (url.hostname === "sales.colliers.com") return new Response("<html></html>");
    const start = Number(url.searchParams.get("Start")), size = Number(url.searchParams.get("PageSize"));
    const list = url.pathname.endsWith("GetListingsHtml"); calls.push(`${start}:${size}`);
    if (start !== 1 && start <= 101 && start + size > 101)
      return response({ success: false, error: list ? "Failure retrieving listing HTML." : "Failure retrieving map." });
    return response(page(start, size, 203)[list ? "list" : "map"]);
  });
  await assert.rejects(srcColliers("sale", 150, false, () => {}, () => {},
    (pageNo, start, size, reason) => holes.push({ pageNo, start, size, reason }), () => true), /success=false/);
  assert.ok(holes.some(x => x.start === 101 && x.size === 1 && x.reason === "provider_failure"));
  assert.ok(holes.some(x => x.start === 151 && x.size === 53 && x.reason === "requested_limit"));
  assert.ok(!calls.some(value => Number(value.split(":")[0]) > 150));
});
