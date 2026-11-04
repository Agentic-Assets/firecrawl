import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

// Every child installs its fake transport before the source/config imports.
// A fresh child also exercises the real process-local cache boundary.
const fixture = `
import assert from "node:assert/strict";
process.argv = [process.execPath, "--concurrency", process.env.TEST_CONCURRENCY];
const {CONCURRENCY} = await import("./lib/config.js");
assert.equal(CONCURRENCY, Number(process.env.TEST_CONCURRENCY));
process.env.CRE_REFRESH_GENERATION = "pyramid-attempt-one";
process.env.CRE_REFRESH_STARTED_AT = "2026-10-19T00:00:00Z";
process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS = "1";
const row = (id) => ({
  id, status: "publish", type: "pbc-listings", slug: "property-" + id,
  link: "https://www.pyramidbrokerage.com/listings/property-" + id + "/",
  modified_gmt: "2026-10-19T00:00:00", title: {rendered: "100 Main St-Albany-NY-X" + id},
  content: {rendered: "<p>Current property</p>"}, "sale-type": [id % 2 ? 246 : 249],
  "property-use": [250], city: [263], county: [264], state: [108],
  _embedded: {"wp:term": [[{taxonomy:"property-use",name:"Industrial"}],
    [{taxonomy:"city",name:"Albany"}], [{taxonomy:"county",name:"Albany"}],
    [{taxonomy:"state",name:"NY"}]]}
});
const html = (id) => '<html><head><link rel="canonical" href="' + row(id).link
  + '"><link rel="shortlink" href="https://www.pyramidbrokerage.com/?p=' + id
  + '"></head><body><section class="property-details"><ul><li><span class="name">Sale/Lease:</span>'
  + '<span class="value">' + (id % 2 ? 'Sale' : 'Lease')
  + '</span></li><li><span class="name">Price:</span><span class="value">Contact Agent</span>'
  + '</li></ul></section></body></html>';
const inventory = (page, total) => Response.json(
  Array.from({length: Math.min(100, total - (page - 1) * 100)}, (_, n) => row((page - 1) * 100 + n + 1)),
  {headers:{"X-WP-Total":String(total),"X-WP-TotalPages":String(Math.ceil(total / 100))}}
);
const delay = (ms) => new Promise(resolve => setTimeout(resolve, ms));
`;

function run(code: string, concurrency = 2) {
  const child = spawnSync(process.execPath, ["--import", "tsx", "--input-type=module", "-e", fixture + code], {
    cwd: fileURLToPath(new URL("../../../", import.meta.url)),
    env: { ...process.env, TEST_CONCURRENCY: String(concurrency) },
    encoding: "utf8", timeout: 20_000, maxBuffer: 128 * 1024,
  });
  assert.equal(child.error, undefined);
  assert.equal(child.status, 0, child.stderr + child.stdout);
}

const failedInventory = `
const calls = new Map();
let terminal = false, lateStarts = 0, inFlightSettled = false;
globalThis.fetch = async (input) => {
  const url = new URL(String(input));
  assert.equal(url.pathname, "/wp-json/wp/v2/pbc-listings", "no detail after failed inventory");
  const page = Number(url.searchParams.get("page"));
  calls.set(page, (calls.get(page) ?? 0) + 1);
  if (terminal && page !== 7) lateStarts++;
  if (page === 7) {
    if (calls.get(7) % 2 === 0) terminal = true;
    return new Response("bad gateway", {status:502});
  }
  if (page === 8) {
    await delay(650);
    inFlightSettled = true;
  }
  return inventory(page, 1400);
};
const {srcPyramidBrokerage} = await import("./sources/pyramid-brokerage.js");
const rejected = async (tx) => {
  try { await srcPyramidBrokerage(tx, Infinity, false); assert.fail("expected HTTP502"); }
  catch (error) { assert.match(String(error), /Pyramid.*HTTP 502/); return error; }
};
`;

test("Pyramid reuses the settled page7 HTTP502 attempt across sale/lease without a second inventory walk", () => {
  run(failedInventory + `
const sale = await rejected("sale");
assert.equal(inFlightSettled, true, "existing worker must settle before source rejection");
assert.equal(lateStarts, 0, "no new item dispatched after terminal callback failure");
const callsAfterSale = [...calls];
const lease = await rejected("lease");
assert.deepEqual([...calls], callsAfterSale, "lease must not restart failed inventory");
assert.strictEqual(lease, sale);
assert.equal(calls.get(7), 2, "retain existing two HTTP attempts, not blanket retries");
terminal = false;
process.env.CRE_REFRESH_GENERATION = "pyramid-attempt-two";
await rejected("sale");
assert.equal(calls.get(1), 2, "new generation independently retries");
assert.equal(calls.get(7), 4);
terminal = false;
process.env.CRE_REFRESH_STARTED_AT = "2026-10-19T00:00:01Z";
await rejected("lease");
assert.equal(calls.get(1), 3, "changed generation start cannot inherit old outcome");
`);
});

test("Pyramid shares one in-flight failure and a fresh process retries the same generation", () => {
  const control = failedInventory + `
const [sale, lease] = await Promise.all([rejected("sale"), rejected("lease")]);
assert.strictEqual(sale, lease);
assert.equal(calls.get(1), 1);
assert.equal(calls.get(7), 2);
assert.equal(inFlightSettled, true);
assert.equal(lateStarts, 0);
`;
  run(control);
  run(control); // Same generation/start, genuinely separate process attempt.
});

test("Pyramid retains only complete immutable inventory success and fresh transaction details", () => {
  run(`
let pages = 0, details = 0;
globalThis.fetch = async (input) => {
  const url = new URL(String(input));
  if (url.pathname.startsWith("/wp-json/")) { pages++; return inventory(1, 6); }
  const id = Number(url.pathname.match(/property-(\\d+)/)?.[1]);
  assert.ok(id, "unexpected offline URL"); details++;
  return new Response(html(id));
};
const {srcPyramidBrokerage} = await import("./sources/pyramid-brokerage.js");
const sale = await srcPyramidBrokerage("sale", Infinity, false);
const lease = await srcPyramidBrokerage("lease", Infinity, false);
assert.equal(pages, 2, "two complete matching passes, shared across transactions");
assert.equal(details, 6);
assert.equal(sale.listings.length, 3); assert.equal(lease.listings.length, 3);
assert.equal(sale.currentInventoryProof.passes.length, 2);
assert.deepEqual(sale.currentInventoryProof.passes, lease.currentInventoryProof.passes);
assert.ok(Object.isFrozen(sale.currentInventoryProof.passes));
assert.ok(Object.isFrozen(sale.currentInventoryProof.passes[0].pages));
assert.throws(() => sale.currentInventoryProof.passes[0].pages.push([]), TypeError);
await srcPyramidBrokerage("sale", Infinity, false);
assert.equal(pages, 2); assert.equal(details, 9, "details remain freshly acquired");
process.env.CRE_REFRESH_GENERATION = "pyramid-success-two";
await srcPyramidBrokerage("lease", Infinity, false);
assert.equal(pages, 4);
`);
});

test("Pyramid does not cache a partially completed or nonconvergent inventory as success", () => {
  run(`
let firstPages = 0;
globalThis.fetch = async (input) => {
  const page = Number(new URL(String(input)).searchParams.get("page"));
  assert.equal(page, 1); firstPages++;
  return Response.json([{...row(1), modified_gmt:"revision-" + firstPages}],
    {headers:{"X-WP-Total":"1","X-WP-TotalPages":"1"}});
};
const {srcPyramidBrokerage} = await import("./sources/pyramid-brokerage.js");
for (const tx of ["sale", "lease"]) {
  await assert.rejects(srcPyramidBrokerage(tx, Infinity, false), /did not converge/);
}
assert.equal(firstPages, 3, "one failed convergence attempt across both transactions");
process.env.CRE_REFRESH_GENERATION = "pyramid-nonconvergent-two";
await assert.rejects(srcPyramidBrokerage("sale", Infinity, false), /did not converge/);
assert.equal(firstPages, 6);
`);
});

for (const mode of ["monitor", "limited", "unbound"]) {
  test(`Pyramid ${mode} calls do not inherit a strict failed-attempt cache`, () => {
    run(`
let calls = 0;
globalThis.fetch = async () => { calls++; return new Response("bad gateway", {status:502}); };
const {srcPyramidBrokerage} = await import("./sources/pyramid-brokerage.js");
await assert.rejects(srcPyramidBrokerage("sale", Infinity, false), /Pyramid.*HTTP 502/);
assert.equal(calls, 2, "populate the strict failed-attempt cache first");
process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS = "0";
if ("${mode}" === "unbound") delete process.env.CRE_REFRESH_GENERATION;
for (const tx of ["sale", "lease"]) {
  await assert.rejects(srcPyramidBrokerage(tx, "${mode}" === "limited" ? 1 : Infinity,
    "${mode}" === "monitor"), /Pyramid.*HTTP 502/);
}
assert.equal(calls, 6, "separate non-proof calls bypass the populated strict cache");
`);
  });
}

for (const concurrency of [1, 2, 6]) {
  test(`Pyramid honors configured C${concurrency} within inventory2/detail4 ceilings`, () => {
    run(`
let inventoryActive = 0, inventoryMax = 0, detailActive = 0, detailMax = 0;
globalThis.fetch = async (input) => {
  const url = new URL(String(input));
  if (url.pathname.startsWith("/wp-json/")) {
    inventoryMax = Math.max(inventoryMax, ++inventoryActive);
    await delay(5); inventoryActive--;
    return inventory(Number(url.searchParams.get("page")), 301);
  }
  detailMax = Math.max(detailMax, ++detailActive);
  await delay(2); detailActive--;
  const id = Number(url.pathname.match(/property-(\\d+)/)?.[1]);
  assert.ok(id, "unexpected offline URL"); return new Response(html(id));
};
const {srcPyramidBrokerage} = await import("./sources/pyramid-brokerage.js");
const result = await srcPyramidBrokerage("sale", Infinity, false);
assert.equal(result.listings.length, 151);
assert.equal(inventoryMax, Math.min(${concurrency}, 2));
assert.equal(detailMax, Math.min(${concurrency}, 4));
`, concurrency);
  });
}
