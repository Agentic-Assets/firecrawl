// The reduced identity and boolean are from saved native property 95605.
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

let networkCalls = 0;
globalThis.fetch = async () => {
  networkCalls++;
  throw new Error("JLL sublease tests prohibit network requests");
};
const { enrichJllListing, preserveJllSubleaseFact, writeJllDetailCache } = await import("../../../sources/jll.js");

const { prune } = await import("../../../lib/util.js");

const pageUrl = "/listings/4660-boca-raton-innovation-campus-big-blue-4660-communication-ave-boca-raton-north";
const base = {
  id: "95605",
  url: `https://property.jll.com${pageUrl}`,
  name: "4660 Boca Raton Innovation Campus Big Blue",
  transactionType: "Lease",
  street: "4660 Communication Ave",
  city: "Boca Raton",
  state: "FL",
  postalCode: "33431",
  jllSearchResult: { priceWithholdingControl: "visible" },
};

async function enrich(property: Record<string, unknown>) {
  const cacheDir = mkdtempSync(join(tmpdir(), "jll-sublease-"));
  const oldDir = process.env.JLL_DETAIL_CACHE_DIR;
  process.env.JLL_DETAIL_CACHE_DIR = cacheDir;
  try {
    writeJllDetailCache(base.url, {
      rawHtml: '<script id="__NEXT_DATA__" type="application/json">' +
        JSON.stringify({ props: { pageProps: { property: {
          id: base.id, pageUrl, title: base.name,
          tenureTypes: ["rent"], labels: ["Sublease"],
          hidePrice: null, salePrice: null, rentPrice: null,
          ...property,
        } } } }) + "</script>",
      markdown: "Sublease. Please contact us for price.",
      links: [],
    });
    return await enrichJllListing(base);
  } finally {
    if (oldDir === undefined) delete process.env.JLL_DETAIL_CACHE_DIR;
    else process.env.JLL_DETAIL_CACHE_DIR = oldDir;
    rmSync(cacheDir, { recursive: true, force: true });
  }
}

const collectSource = readFileSync(new URL("../../../collect.ts", import.meta.url), "utf8");
const collectBody = collectSource.match(/for \(const l of res.listings\) \{([\s\S]*?)\n        \}/)?.[1];
assert.ok(collectBody, "ordinary collector emission loop must be found");
const collectListing = new Function("key", "res", "tx", "prune", "preserveJllSubleaseFact",
  `const listings = []; for (const l of res.listings) {${collectBody}} return listings[0];`);

function stage(listing: Record<string, any>) {
  // Run the actual ordinary collector emission body without executing the CLI.
  listing = collectListing("jll", { company: "JLL", listings: [listing] }, "lease",
    prune, preserveJllSubleaseFact);
  const result = spawnSync("python3", ["-c", [
    "import json,sys,cre_ingest as ci",
    "row=ci.to_row(json.load(sys.stdin),{},'2026-10-17T05:18:26.241Z')",
    "print(json.dumps(row, default=list))",
  ].join(";")], {
    cwd: process.cwd(),
    input: JSON.stringify({ ...listing, sourceKey: "jll", transactionMode: "lease" }),
    encoding: "utf8",
    timeout: 10_000,
    env: { ...process.env, PYTHONPATH: "." },
  });
  assert.equal(result.status, 0, result.stderr);
  const row = JSON.parse(result.stdout);
  assert.equal(row.transaction_type, "lease");
  return row;
}

for (const hidePrice of [false, true, null]) {
  for (const isSubleaseOnly of [true, false]) {
    test(`JLL retains boolean ${isSubleaseOnly} through staging with hidePrice=${hidePrice}`, async () => {
      const listing = await enrich({ isSubleaseOnly, hidePrice, rentPrice: "$987654" });
      assert.equal(listing.detailError, undefined);
      assert.equal(listing.jllDetail.isSubleaseOnly, isSubleaseOnly);
      const row = stage(listing);
      assert.equal(row.raw_data.jllDetail.isSubleaseOnly, isSubleaseOnly);
      assert.equal(row.lease_rate_type, null);
      if (hidePrice !== false) {
        assert.equal(row.lease_rate_min, null);
        assert.equal(row.lease_rate_max, null);
        assert.equal(JSON.stringify(row).includes("987654"), false);
      }
      assert.equal(networkCalls, 0);
    });
  }
}

for (const value of [undefined, null, "true", "false", 1, 0, {}, []]) {
  test(`JLL omits absent or malformed sublease value ${JSON.stringify(value)}`, async () => {
    const listing = await enrich({ isSubleaseOnly: value });
    assert.equal(listing.detailError, undefined);
    assert.equal(Object.hasOwn(listing.jllDetail, "isSubleaseOnly"), false);
    assert.equal(Object.hasOwn(stage(listing).raw_data.jllDetail, "isSubleaseOnly"), false);
    const malformed = { ...listing, jllDetail: { ...listing.jllDetail, isSubleaseOnly: value } };
    assert.equal(Object.hasOwn(stage(malformed).raw_data.jllDetail, "isSubleaseOnly"), false);
    assert.equal(networkCalls, 0);
  });
}

for (const identity of [{ id: "other" }, { pageUrl: "/listings/other" }]) {
  test(`JLL does not promote sublease from a mismatched detail ${JSON.stringify(identity)}`, async () => {
    const listing = await enrich({ isSubleaseOnly: true, ...identity });
    assert.match(listing.detailError, /provider id mismatch|does not match enumerated/);
    assert.equal(listing.jllDetail?.isSubleaseOnly, undefined);
    assert.equal(networkCalls, 0);
  });
}

test("collector preservation hook remains scoped to JLL", () => {
  const listing = { jllDetail: { isSubleaseOnly: false } };
  const other = collectListing("other", { company: "Other", listings: [listing] }, "lease",
    prune, preserveJllSubleaseFact);
  assert.equal(other?.jllDetail?.isSubleaseOnly, undefined);
});
