process.argv = [process.argv[0]!, process.argv[1]!];
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import * as cheerio from "cheerio";
import test from "node:test";
import { parseMatthewsDetail } from "../../../sources/matthews.js";

const fixture = readFileSync(new URL("../../fixtures/matthews-leasing-spaces.html", import.meta.url), "utf8");
const url = "https://www.matthews.com/properties/leasing-12961-w-chapman-ave-garden-grove-ca";
const expected = [
  ["Suite 12847/12849 – Both Units Combined", "$2.45 PSF", "1,920"],
  ["Suite 12849 – Individual Space", "$2.65 PSF", "960"],
  ["Suite 12891", "$2.45 PSF", "2,100"],
].map(([name, rate, area]) => [
  { label: "Space name", value: name },
  { label: "Rental rate", value: rate },
  { label: "Space available", value: area },
]);
function parse(html = fixture) {
  const row = parseMatthewsDetail(html, url, "lease", { strict: true });
  assert.ok(row);
  return row;
}
function mutate(change: ($: cheerio.CheerioAPI) => void) {
  const $ = cheerio.load(fixture);
  change($);
  return $.html();
}

test("Matthews retains three observed suite/rate/area tuples without aggregation", () => {
  const row = parse();
  assert.deepEqual(row.extraFacts?.matthewsLeasingSpaces, expected);
  assert.equal(row.leaseRateText, "CONTACT BROKER");
  assert.equal(row.buildingSizeSqft, undefined);
  assert.equal(row.sizeText, undefined);
  assert.equal(row.salePriceUsd, undefined);
  assert.equal(row.id, "leasing-12961-w-chapman-ave-garden-grove-ca");
  assert.equal(row.transactionType, "Lease");
});

test("additional literal space facts survive without coercing units or periodicity", () => {
  const row = parse(mutate(($) => {
    $("h3").next().find("article").last().append('<div><div><span class="key-info-title">Lease term</span></div><div><span class="key-info-value">Negotiable; NNN</span></div></div>');
  }));
  assert.deepEqual(row.extraFacts.matthewsLeasingSpaces[0], [...expected[0]!, { label: "Lease term", value: "Negotiable; NNN" }]);
});

for (const [name, change] of [
  ["blank name", ($: cheerio.CheerioAPI) => $(".key-info-value").first().text(" ")],
  ["missing value", ($: cheerio.CheerioAPI) => $(".key-info-value").eq(1).remove()],
  ["duplicate conflicting label", ($: cheerio.CheerioAPI) => $("h3").next().append('<div><div><span class="key-info-title">Rental rate</span></div><div><span class="key-info-value">Conflicting</span></div></div>')],
  ["orphan value", ($: cheerio.CheerioAPI) => $("h3").next().append('<span class="key-info-value">Orphan fact</span>')],
  ["two names", ($: cheerio.CheerioAPI) => $("h3").next().append('<div><div><span class="key-info-title">Space name</span></div><div><span class="key-info-value">Different suite</span></div></div>')],
] as const) {
  test(`Matthews omits only the ambiguous ${name} card without mixing neighboring facts`, () => {
    const row = parse(mutate(change));
    assert.deepEqual(row.extraFacts?.matthewsLeasingSpaces, expected.slice(1));
    assert.equal(row.extraFacts?.matthewsUnparsedLeasingSpaces.length, 1);
    assert.match(row.extraFacts.matthewsUnparsedLeasingSpaces[0], /Space name/);

  });
}

test("unrelated recommendation spaces outside the owned property section are ignored", () => {
  const row = parse(mutate(($) => {
    $("body").append('<section><h3>1 Space Available</h3><div><div><span class="key-info-title">Space name</span></div><div><span class="key-info-value">Wrong property</span></div></div></section>');
  }));
  assert.deepEqual(row.extraFacts.matthewsLeasingSpaces, expected);
});

test("ambiguous groups or missing owned section do not invent space associations", () => {
  for (const html of [
    mutate(($) => { $("h3").parent().clone().appendTo($("#propertyTitle").parent()); }),
    fixture.replace(/<section>/g, "<div>").replace(/<\/section>/g, "</div>"),
  ]) assert.equal(parse(html).extraFacts?.matthewsLeasingSpaces, undefined);
});

test("space retention does not change unrelated mapped output", () => {
  const withSpaces = parse();
  const withoutSpaces = parse(mutate(($) => { $("h3").parent().remove(); }));
  delete withSpaces.extraFacts;
  assert.deepEqual(withSpaces, withoutSpaces);
  assert.equal(parseMatthewsDetail(fixture, url, "sale", { strict: true }), null);
  assert.equal(parseMatthewsDetail(fixture.replace('rel="canonical"', 'rel="untrusted"'), url, "lease", { strict: true }), null);
});

test("actual ordinary collect CLI serialization and Python staging preserve the suite facts", () => {
  const root = fileURLToPath(new URL("../../../", import.meta.url));
  const dir = mkdtempSync(join(tmpdir(), "matthews-space-cli-"));
  try {
    const body = join(dir, "body.html"), preload = join(dir, "preload.mts"), output = join(dir, "output.json");
    writeFileSync(body, fixture);
    writeFileSync(preload, `
import { readFileSync } from "node:fs";
await import(${JSON.stringify(new URL("../../../sources/matthews.ts", import.meta.url).href)});
const oldTimeout = globalThis.setTimeout;
globalThis.setTimeout = (callback, delay, ...args) => oldTimeout(callback, delay === 30000 ? delay : 0, ...args);
globalThis.fetch = async (input) => {
  const target = String(input);
  if (target === "https://www.matthews.com/sitemap.xml") return new Response(${JSON.stringify(`<urlset><url><loc>${url}</loc></url></urlset>`)});
  if (target !== ${JSON.stringify(url)}) throw new Error("Unexpected mocked URL");
  return new Response(readFileSync(${JSON.stringify(body)}, "utf8"));
};`);
    const child = spawnSync(process.execPath, ["--import", "tsx", "--import", preload, "collect.ts", "--source", "matthews", "--transaction", "lease", "--out", output, "--concurrency", "1"], {
      cwd: root, encoding: "utf8", timeout: 15000, maxBuffer: 256 * 1024,
      env: { ...process.env, CRE_REQUIRE_FRESH_DETAILS: "1" },
    });
    assert.equal(child.status, 0, child.stderr);
    const artifact = JSON.parse(readFileSync(output, "utf8"));
    assert.equal(artifact.listings.length, 1);
    assert.deepEqual(artifact.listings[0].extraFacts.matthewsLeasingSpaces, expected);
    const staged = spawnSync("python3", ["-c", [
      "import json,sys,cre_ingest as ci",
      "listing=json.load(sys.stdin)",
      "row=ci.to_row(listing,{},'2026-10-17T12:00:00Z')",
      "assert row['transaction_type']=='lease' and row['sale_price_usd'] is None",
      "print(json.dumps(row['extra_facts']))",
    ].join("; ")], { cwd: root, input: JSON.stringify(artifact.listings[0]), encoding: "utf8", timeout: 10000 });
    assert.equal(staged.status, 0, staged.stderr);
    assert.deepEqual(JSON.parse(staged.stdout).matthewsLeasingSpaces, expected);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
