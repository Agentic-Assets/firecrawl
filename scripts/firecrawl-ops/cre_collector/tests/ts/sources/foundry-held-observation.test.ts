import assert from "node:assert/strict";
import test from "node:test";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

process.argv = [process.argv[0]!, process.argv[1]!];
const { parseFoundryCommercialDetail, srcFoundryCommercial } = await import("../../../sources/foundry-commercial.js");

// Entirely synthetic parser/transport fixtures; no historical Foundry body is recreated.
const host = "https://www.foundrycommercial.com";
const detailUrl = (slug: string) => `${host}/property/${slug}/`;
function page(slug: string, id: string, notes: string[]): string {
  const url = detailUrl(slug);
  return `<html><head><link rel="canonical" href="${url}">
    <link rel="shortlink" href="${host}/?p=${id}">
    <script type="application/ld+json">${JSON.stringify({
      "@type": "RealEstateListing", url, name: `Synthetic ${slug}`,
      mainEntity: { additionalType: "Office", floorSize: { value: 4000 } },
    })}</script></head><body class="postid-${id}"><h1>Synthetic ${slug}</h1>
    <ul class="property-notes">${notes.map((note) => `<li>${note}</li>`).join("")}</ul>
    <table><tr><th>Price</th><td>$900,000</td></tr></table></body></html>`;
}
type Observation = {
  id: string; foundryPostId: string; url: string; propertyNotes: string[];
  status: string | null; reason: string; requestedTransaction: "sale" | "lease";
  inventoryObservedAt?: string; detailObservedAt?: string;
};
function held(html: string, slug: string, tx: "sale" | "lease", context = {}) {
  const outcome = parseFoundryCommercialDetail(html, detailUrl(slug), tx, { strict: true, ...context });
  assert.equal(outcome.kind, "held");
  if (outcome.kind !== "held") throw new Error("expected synthetic held outcome");
  // Baseline type intentionally compiles; RED must fail for missing retention, not a new export.
  const observation = (outcome as typeof outcome & { observation?: Observation }).observation;
  assert.ok(observation, "held outcome must retain the parsed observation");
  return { outcome, observation };
}

test("synthetic held unknown status retains all parsed notes, exact association and supplied clocks", () => {
  const notes = ["Temporarily Paused", "Office / Flex", "123 Synthetic Road", "For Lease"];
  const context = { inventoryObservedAt: "2026-10-16T01:00:00Z", detailObservedAt: "2026-10-16T01:01:00Z" };
  const { outcome, observation } = held(page("held", "70001", notes), "held", "sale", context);
  assert.equal(outcome.status, "temporarily paused");
  assert.equal(outcome.reason, "unknown Foundry property status: temporarily paused");
  assert.deepEqual(observation, {
    id: "property/held", foundryPostId: "70001", url: detailUrl("held"), propertyNotes: notes,
    status: outcome.status, reason: outcome.reason, requestedTransaction: "sale", ...context,
  });
  assert.deepEqual(JSON.parse(JSON.stringify(observation)), observation);
  assert.equal("transactionType" in observation, false);
  assert.equal("freshnessProvenance" in observation, false);
  assert.equal("listing" in outcome, false);
});

test("synthetic missing status preserves null and empty notes without invented clocks", () => {
  const { outcome, observation } = held(page("empty", "70002", []), "empty", "lease");
  assert.equal(outcome.status, null);
  assert.equal(outcome.reason, "missing explicit Foundry property status");
  assert.deepEqual(observation, {
    id: "property/empty", foundryPostId: "70002", url: detailUrl("empty"),
    propertyNotes: [], status: null, reason: outcome.reason, requestedTransaction: "lease",
  });
  assert.deepEqual(JSON.parse(JSON.stringify(observation)).propertyNotes, []);
});

test("synthetic unresolved tenure remains held for both request contexts without accepted enrichment", () => {
  for (const tx of ["sale", "lease"] as const) {
    const { outcome, observation } = held(page("ambiguous", "70003", ["Available", "Office"]), "ambiguous", tx, { detailObservedAt: "2026-10-16T01:02:00Z" });
    assert.equal(outcome.reason, "active status available requires a separate explicit transaction token; no explicit sale or lease tenure found");
    assert.equal(observation.requestedTransaction, tx);
    assert.deepEqual(observation.propertyNotes, ["Available", "Office"]);
    assert.equal(observation.status, "available");
    assert.equal("inventoryObservedAt" in observation, false);
    assert.equal("salePriceUsd" in observation, false);
    assert.equal("buildingSizeSqft" in observation, false);
  }
});

function responsesFor(rows: Array<{ slug: string; id: string; notes: string[]; html?: string }>) {
  return new Map([
    [`${host}/sitemap.xml`, `<sitemapindex><sitemap><loc>${host}/property-sitemap.xml</loc></sitemap></sitemapindex>`],
    [`${host}/property-sitemap.xml`, `<urlset>${rows.map(({ slug }) => `<url><loc>${detailUrl(slug)}</loc></url>`).join("")}</urlset>`],
    ...rows.map((row): [string, string] => [detailUrl(row.slug), row.html ?? page(row.slug, row.id, row.notes)]),
  ]);
}
function events(lines: string[]) {
  return lines.filter((line) => line.startsWith("{")).map((line) => JSON.parse(line))
    .filter((event) => event.event === "foundry_held_observation");
}

test("synthetic mixed producer preserves accepted rows, fetches, order and all nonheld dispositions", async (t) => {
  const rows = [
    { slug: "first", id: "71001", notes: ["For Sale"] },
    { slug: "unknown", id: "71002", notes: ["Paused", "Office", "Location"] },
    { slug: "terminal", id: "71003", notes: ["Closed"] },
    { slug: "lease", id: "71004", notes: ["For Lease"] },
    { slug: "ambiguous", id: "71005", notes: ["Available", "Office"] },
    { slug: "challenge", id: "71006", notes: ["Paused"], html: page("challenge", "71006", ["Paused"]).replace("<h1>Synthetic challenge</h1>", "<h1>Access Denied</h1>") },
    { slug: "conflict", id: "71007", notes: ["Paused"], html: page("conflict", "71007", ["Paused"]).replace(`href="${detailUrl("conflict")}"`, `href="${detailUrl("foreign")}"`) },
    { slug: "last", id: "71008", notes: ["For Sale"] },
  ];
  const responses = responsesFor(rows);
  const calls: string[] = [], lines: string[] = [];
  const oldFetch = globalThis.fetch, oldError = console.error;
  t.after(() => { globalThis.fetch = oldFetch; console.error = oldError; });
  globalThis.fetch = async (input) => {
    const url = String(input); calls.push(url);
    assert.ok(responses.has(url), `unexpected synthetic transport URL ${url}`);
    return new Response(responses.get(url), { headers: { "content-type": url.endsWith(".xml") ? "application/xml" : "text/html" } });
  };
  console.error = (...args: unknown[]) => { lines.push(args.map(String).join(" ")); };
  const result = await srcFoundryCommercial("sale", Infinity, false);
  assert.equal(result.totalAvailable, rows.length);
  assert.equal(result.truncated, true);
  assert.equal(calls.length, rows.length + 2);
  assert.deepEqual(calls.slice(0, 2), [`${host}/sitemap.xml`, `${host}/property-sitemap.xml`]);
  assert.deepEqual(calls.slice(2).sort(), rows.map(({ slug }) => detailUrl(slug)).sort());
  assert.deepEqual(result.listings.map((row) => row.id), ["property/first", "property/last"]);
  for (const [index, slug] of ["first", "last"].entries()) {
    const actual = result.listings[index]!;
    const expected = parseFoundryCommercialDetail(page(slug, index ? "71008" : "71001", ["For Sale"]), detailUrl(slug), "sale", {
      inventoryObservedAt: actual.inventoryObservedAt, detailObservedAt: actual.detailObservedAt,
    });
    assert.equal(expected.kind, "accepted");
    if (expected.kind === "accepted") assert.deepEqual(actual, expected.listing);
    assert.equal("observation" in actual, false);
  }
  assert.equal(result.note, "2 detail page(s) held for unknown or ambiguous status; 1 detail page(s) belong only to the other transaction; 1 terminal detail page(s) excluded; 2 detail page(s) failed identity or structure validation");
  const retained = events(lines);
  assert.equal(retained.length, 2);
  assert.deepEqual(retained.map((event) => event.observation.id).sort(), ["property/ambiguous", "property/unknown"]);
  for (const event of retained) {
    assert.equal(event.schema, "foundry-held-observation-v1");
    const fixture = rows.find((row) => detailUrl(row.slug) === event.observation.url)!;
    assert.deepEqual(event.observation.propertyNotes, fixture.notes);
    assert.equal(event.observation.foundryPostId, fixture.id);
    assert.equal(event.observation.requestedTransaction, "sale");
    assert.ok(event.observation.inventoryObservedAt);
    assert.ok(event.observation.detailObservedAt);
  }
});

test("synthetic completed held event survives a later fatal accepted-ID uniqueness assertion", async (t) => {
  const responses = responsesFor([
    { slug: "held-before-failure", id: "73001", notes: ["Paused", "Office", "Location"] },
    { slug: "duplicate-one", id: "73002", notes: ["For Sale"] },
    { slug: "duplicate-two", id: "73002", notes: ["For Sale"] },
  ]);
  const lines: string[] = [], calls: string[] = [];
  const oldFetch = globalThis.fetch, oldError = console.error;
  t.after(() => { globalThis.fetch = oldFetch; console.error = oldError; });
  globalThis.fetch = async (input) => {
    const url = String(input); calls.push(url);
    assert.ok(responses.has(url), `unexpected synthetic transport URL ${url}`);
    return new Response(responses.get(url), { headers: { "content-type": url.endsWith(".xml") ? "application/xml" : "text/html" } });
  };
  console.error = (...args: unknown[]) => { lines.push(args.map(String).join(" ")); };
  await assert.rejects(() => srcFoundryCommercial("sale", Infinity, false), /duplicate provider ID 73002/);
  assert.equal(calls.length, 5);
  const retained = events(lines);
  assert.equal(retained.length, 1, "completed held event must precede the fatal uniqueness assertion");
  assert.equal(retained[0].schema, "foundry-held-observation-v1");
  assert.equal(retained[0].observation.id, "property/held-before-failure");
  assert.equal(retained[0].observation.foundryPostId, "73001");
  assert.deepEqual(retained[0].observation.propertyNotes, ["Paused", "Office", "Location"]);
});

test("synthetic real all-held CLI fails without artifact and retains stderr observations", (t) => {
  const dir = mkdtempSync(join(tmpdir(), "foundry-held-cli-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const preload = join(dir, "transport.mjs"), callsPath = join(dir, "calls.jsonl"), out = join(dir, "artifact.json");
  const responses = responsesFor([{ slug: "empty", id: "72001", notes: [] }, { slug: "paused", id: "72002", notes: ["Paused", "Office", "Location"] }]);
  writeFileSync(preload, `
    import { appendFileSync } from "node:fs";
    import net from "node:net";
    import tls from "node:tls";
    import http from "node:http";
    import https from "node:https";
    const deny = () => { throw new Error("NETWORK_DENIED_BY_SYNTHETIC_FIXTURE"); };
    net.Socket.prototype.connect = deny; tls.connect = deny;
    http.request = deny; http.get = deny; https.request = deny; https.get = deny;
    const responses = new Map(${JSON.stringify([...responses])});
    globalThis.fetch = async (input) => {
      const url = String(input);
      if (!responses.has(url)) throw new Error("UNEXPECTED_SYNTHETIC_FETCH");
      appendFileSync(${JSON.stringify(callsPath)}, JSON.stringify(url) + "\\n");
      return new Response(responses.get(url), {headers:{"content-type":url.endsWith(".xml")?"application/xml":"text/html"}});
    };
    console.error("SYNTHETIC_TRANSPORT_INSTALLED");
  `);
  const child = spawnSync(process.execPath, ["--import", "tsx", "--import", pathToFileURL(preload).href, "collect.ts", "--source=foundry-commercial", "--transaction=sale", `--out=${out}`], {
    cwd: fileURLToPath(new URL("../../../", import.meta.url)), encoding: "utf8", timeout: 30_000, maxBuffer: 1024 * 1024,
  });
  assert.ifError(child.error);
  assert.notEqual(child.status, 0);
  assert.match(child.stderr, /SYNTHETIC_TRANSPORT_INSTALLED/);
  assert.doesNotMatch(child.stderr, /NETWORK_DENIED|UNEXPECTED_SYNTHETIC_FETCH/);
  assert.match(child.stderr, /no listings collected from any source/);
  assert.equal(existsSync(out), false);
  assert.deepEqual(readFileSync(callsPath, "utf8").trim().split("\n").map((line) => JSON.parse(line)).sort(), [...responses.keys()].sort());
  const retained = events(child.stderr.split(/\r?\n/));
  assert.equal(retained.length, 2, "all-held zero-row failure must retain both completed observations");
  const empty = retained.find((event) => event.observation.id === "property/empty")!;
  assert.ok(empty);
  assert.equal(empty.observation.status, null);
  assert.deepEqual(empty.observation.propertyNotes, []);
  assert.deepEqual(retained.find((event) => event.observation.id === "property/paused")?.observation.propertyNotes, ["Paused", "Office", "Location"]);
});
