process.argv = [process.argv[0]!, process.argv[1]!, "--page-cap=0"];
import test from "node:test";
import assert from "node:assert/strict";

const { PAGE_CAP } = await import("../../../lib/config.js");
const { srcJll } = await import("../../../sources/jll.js");
const { firecrawl } = await import("../../../lib/scrape.js");

test("unlimited page cap is serialized as literal zero", () => {
  assert.equal(PAGE_CAP, 0);
  assert.equal(JSON.stringify({ pageCap: PAGE_CAP }), '{"pageCap":0}');
});

test("JLL repeated filter pages stop with incomplete coverage", async (t) => {
  let searchCalls = 0;
  t.mock.method(globalThis, "fetch", async (_input: RequestInfo | URL, init?: RequestInit) => {
    const payload = JSON.parse(String(init?.body));
    assert.ok(payload.variables.skip === 0 || payload.variables.skip === 50);
    searchCalls++;
    return new Response(JSON.stringify({ data: { properties: {
      count: 100,
      items: [{ id: "1", title: "One", pageUrl: "/listings/one" }],
    } } }), { headers: { "content-type": "application/json" } });
  });
  t.mock.method(firecrawl, "scrape", async () => ({
    rawHtml: '<script id="__NEXT_DATA__">{"props":{"pageProps":{"property":{"id":"1","pageUrl":"/listings/one"},"brokers":[]}}}</script>',
    markdown: "", links: [], images: [],
  }) as any);
  const result = await srcJll("sale", Number.POSITIVE_INFINITY, false);
  assert.equal(searchCalls, 18);
  assert.equal(result.listings.length, 1);
  assert.equal(result.truncated, true);
});

test("Colliers unlimited mode reaches provider exhaustion beyond 400 pages", async (t) => {
  const { srcColliers } = await import("../../../sources/colliers.js");
  let listCalls = 0;
  t.mock.method(globalThis, "fetch", async (input: RequestInfo | URL) => {
    const url = new URL(String(input));
    if (url.hostname === "sales.colliers.com") return new Response("<html></html>");
    const start = Number(url.searchParams.get("Start"));
    assert.ok(start >= 1 && start <= 401);
    if (url.pathname.endsWith("GetListingsHtml")) {
      listCalls++;
      return Response.json({ success: true, total: 401, totalAvail: 401, numProjects: 1,
        html: `<ul><li class="item"><a class="headline">Property ${start}</a></li></ul>` });
    }
    assert.ok(url.pathname.endsWith("GetMapData"));
    return Response.json({ success: true, projectLocations: [{ ProjectId: `P${start}`, Latitude: 30, Longitude: -90 }] });
  });
  const result = await srcColliers("sale", Number.POSITIVE_INFINITY, false);
  assert.equal(listCalls, 401);
  assert.equal(result.listings.length, 401);
  assert.equal(result.truncated, false);
});

test("CBRE Deal Flow unlimited mode reaches the declared end beyond sixty pages", async (t) => {
  const { srcCbreDealflow } = await import("../../../sources/cbre-dealflow.js");
  let pages = 0;
  t.mock.method(globalThis, "fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    if (!init?.method || init.method === "GET") return new Response("<html></html>");
    if (url.pathname.endsWith("GetFilters")) return Response.json({});
    assert.ok(url.pathname.endsWith("GetListingsHtml"));
    const start = Number(new URLSearchParams(String(init.body)).get("Start"));
    pages++;
    assert.ok(start <= 61);
    return Response.json({ success: true, total: 61, totalAvail: 61, numProjects: 1,
      html: `<ul class="gridview"><li class="item"><div class="headline">Property ${start}</div><div class="location"><div class="city">Dallas, TX</div></div><div class="details">Investment Sale</div></li></ul>` });
  });
  const result = await srcCbreDealflow("sale", Number.POSITIVE_INFINITY, false);
  assert.equal(pages, 61);
  assert.equal(result.listings.length, 61);
  assert.equal(result.truncated, false);
});

test("CBRE Deal Flow repeated cards stop with incomplete coverage", async (t) => {
  const { srcCbreDealflow } = await import("../../../sources/cbre-dealflow.js");
  let pages = 0;
  t.mock.method(globalThis, "fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    if (!init?.method || init.method === "GET") return new Response("<html></html>");
    if (url.pathname.endsWith("GetFilters")) return Response.json({});
    pages++;
    assert.ok(pages <= 2);
    return Response.json({ success: true, total: 100, totalAvail: 100, numProjects: 1,
      html: '<ul class="gridview"><li class="item"><div class="headline">Repeated</div><div class="location"><div class="city">Dallas, TX</div></div><div class="details">Investment Sale</div></li></ul>' });
  });
  const result = await srcCbreDealflow("sale", Number.POSITIVE_INFINITY, false);
  assert.equal(pages, 2);
  assert.equal(result.listings.length, 1);
  assert.equal(result.truncated, true);
});

test("NAI unlimited mode reaches a short page beyond sixty full pages", async (t) => {
  const nai = await import("../../../sources/nai-global.js");
  const chunk = new URL("/_next/static/chunks/office-config.js", nai.NAI_WIDGET_URL).href;
  const offices = nai.NAI_SOURCE_IDS.map((id, i) => `{id:${id},name:"${i ? "NAI Office" : "NAI Global"}",country:"United States"}`).join(",");
  let calls = 0;
  nai.naiFeedPageCache.clear();
  t.after(() => nai.naiFeedPageCache.clear());
  t.mock.method(globalThis, "fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === nai.NAI_WIDGET_URL) return new Response(`<script src="${chunk}"></script>`);
    if (url === chunk) return new Response(`self.webpackChunk_N_E.push([[1],{1:()=>{let a=[${offices}];}}]);`);
    assert.equal(init?.method, "POST");
    const offset = JSON.parse(String(init?.body)).variables.offset;
    assert.ok(offset <= 6000);
    calls++;
    const count = offset === 6000 ? 1 : 100;
    const rows = Array.from({ length: count }, (_, i) => ({
      id: offset + i + 1, title: `Listing ${offset + i + 1}`,
      contentType: { id: 4, name: "Sale Listings" }, listingStatus: "FOR_SALE_ON_MARKET",
      source: { name: "NAI Test" },
    }));
    return Response.json({ data: { publicPosts: rows } });
  });
  const result = await nai.srcNaiGlobal("sale", 1, false);
  assert.equal(calls, 61);
  assert.equal(result.listings.length, 1);
  assert.equal(result.truncated, true); // finite requested limit and other office batches
});

test("JLL unlimited mode can enumerate beyond sixty pages without claiming missing cards", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async (_input: RequestInfo | URL, init?: RequestInit) => {
    const skip = JSON.parse(String(init?.body)).variables.skip;
    assert.ok(skip <= 3000);
    calls++;
    return Response.json({ data: { properties: { count: 3050,
      items: [{ id: `id-${skip}`, pageUrl: `/listings/id-${skip}` }],
    } } });
  });
  t.mock.method(firecrawl, "scrape", async (url: string) => {
    const id = String(url).split("/").at(-1);
    return { rawHtml: `<script id="__NEXT_DATA__">${JSON.stringify({ props: { pageProps: {
      property: { id, pageUrl: `/listings/${id}` }, brokers: [],
    } } })}</script>`, markdown: "", links: [], images: [] } as any;
  });
  const result = await srcJll("sale", Number.POSITIVE_INFINITY, false);
  assert.equal(calls, 61 * 9);
  assert.equal(result.listings.length, 61);
  assert.equal(result.truncated, true);
});

test("zero-cap source requests still refuse transport and malformed responses", async (t) => {
  const { srcColliers } = await import("../../../sources/colliers.js");
  const { srcCbreDealflow } = await import("../../../sources/cbre-dealflow.js");
  const nai = await import("../../../sources/nai-global.js");

  const collierMock = t.mock.method(globalThis, "fetch", async (input: RequestInfo | URL) => {
    const url = new URL(String(input));
    return url.hostname === "sales.colliers.com" ? new Response("<html></html>") : new Response("denied", { status: 403 });
  });
  await assert.rejects(srcColliers("sale", Number.POSITIVE_INFINITY, false), /HTTP 403/);
  collierMock.mock.restore();

  const cbreMock = t.mock.method(globalThis, "fetch", async (_input: RequestInfo | URL, init?: RequestInit) =>
    !init?.method ? new Response("<html></html>") : new Response("denied", { status: 403 }));
  await assert.rejects(srcCbreDealflow("sale", Number.POSITIVE_INFINITY, false), /HTTP 403/);
  cbreMock.mock.restore();

  const jllMock = t.mock.method(globalThis, "fetch", async () =>
    new Response("{", { headers: { "content-type": "application/json" } }));
  await assert.rejects(srcJll("sale", Number.POSITIVE_INFINITY, false), /malformed JSON/);
  jllMock.mock.restore();

  const chunk = new URL("/_next/static/chunks/office-config.js", nai.NAI_WIDGET_URL).href;
  const offices = nai.NAI_SOURCE_IDS.map((id, i) => `{id:${id},name:"${i ? "NAI Office" : "NAI Global"}",country:"United States"}`).join(",");
  nai.naiFeedPageCache.clear();
  t.after(() => nai.naiFeedPageCache.clear());
  t.mock.method(globalThis, "fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === nai.NAI_WIDGET_URL) return new Response(`<script src="${chunk}"></script>`);
    if (String(input) === chunk) return new Response(`self.webpackChunk_N_E.push([[1],{1:()=>{let a=[${offices}];}}]);`);
    assert.equal(init?.method, "POST");
    return Response.json({ data: {} });
  });
  await assert.rejects(nai.srcNaiGlobal("sale", Number.POSITIVE_INFINITY, false), /lacks a publicPosts array/);
});

test("Colliers repeated IDs fail closed in unlimited mode", async (t) => {
  const { srcColliers } = await import("../../../sources/colliers.js");
  let pages = 0;
  t.mock.method(globalThis, "fetch", async (input: RequestInfo | URL) => {
    const url = new URL(String(input));
    if (url.hostname === "sales.colliers.com") return new Response("<html></html>");
    const start = Number(url.searchParams.get("Start"));
    assert.ok(start <= 2);
    if (url.pathname.endsWith("GetListingsHtml")) {
      pages++;
      return Response.json({ success: true, total: 3, totalAvail: 3, numProjects: 1,
        html: '<ul><li class="item"><a class="headline">Repeated</a></li></ul>' });
    }
    return Response.json({ success: true, projectLocations: [{ ProjectId: "same", Latitude: 30, Longitude: -90 }] });
  });
  await assert.rejects(srcColliers("sale", Number.POSITIVE_INFINITY, false), /duplicate|repeated/i);
  assert.equal(pages, 2);
});

test("NAI full pages without stable IDs stop as incomplete in unlimited mode", async (t) => {
  const nai = await import("../../../sources/nai-global.js");
  const chunk = new URL("/_next/static/chunks/office-config.js", nai.NAI_WIDGET_URL).href;
  const offices = nai.NAI_SOURCE_IDS.map((id, i) => `{id:${id},name:"${i ? "NAI Office" : "NAI Global"}",country:"United States"}`).join(",");
  let calls = 0;
  nai.naiFeedPageCache.clear();
  t.after(() => nai.naiFeedPageCache.clear());
  t.mock.method(globalThis, "fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === nai.NAI_WIDGET_URL) return new Response(`<script src="${chunk}"></script>`);
    if (String(input) === chunk) return new Response(`self.webpackChunk_N_E.push([[1],{1:()=>{let a=[${offices}];}}]);`);
    assert.equal(init?.method, "POST");
    calls++;
    assert.ok(calls <= 3);
    const rows = Array.from({ length: 100 }, (_, i) => ({
      id: i ? i : null, title: `Listing ${i}`,
      contentType: { id: 4, name: "Sale Listings" }, listingStatus: "FOR_SALE_ON_MARKET",
      source: { name: "NAI Test" },
    }));
    return Response.json({ data: { publicPosts: rows } });
  });
  const result = await nai.srcNaiGlobal("sale", Number.POSITIVE_INFINITY, false);
  assert.equal(calls, 3);
  assert.equal(result.truncated, true);
});

test("NAI reordered duplicate pages stop as incomplete in unlimited mode", async (t) => {
  const nai = await import("../../../sources/nai-global.js");
  const chunk = new URL("/_next/static/chunks/office-config.js", nai.NAI_WIDGET_URL).href;
  const offices = nai.NAI_SOURCE_IDS.map((id, i) => `{id:${id},name:"${i ? "NAI Office" : "NAI Global"}",country:"United States"}`).join(",");
  let calls = 0;
  nai.naiFeedPageCache.clear();
  t.after(() => nai.naiFeedPageCache.clear());
  t.mock.method(globalThis, "fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === nai.NAI_WIDGET_URL) return new Response(`<script src="${chunk}"></script>`);
    if (String(input) === chunk) return new Response(`self.webpackChunk_N_E.push([[1],{1:()=>{let a=[${offices}];}}]);`);
    const offset = JSON.parse(String(init?.body)).variables.offset;
    assert.ok(offset <= 100);
    calls++;
    const rows = Array.from({ length: 100 }, (_, i) => ({
      id: i + 1, title: `Listing ${i}`, contentType: { id: 4, name: "Sale Listings" },
      listingStatus: "FOR_SALE_ON_MARKET", source: { name: "NAI Test" },
    }));
    return Response.json({ data: { publicPosts: offset ? rows.reverse() : rows } });
  });
  const result = await nai.srcNaiGlobal("sale", Number.POSITIVE_INFINITY, false);
  assert.equal(calls, 6);
  assert.equal(result.truncated, true);
  assert.equal(result.listings.length, 100);
});
