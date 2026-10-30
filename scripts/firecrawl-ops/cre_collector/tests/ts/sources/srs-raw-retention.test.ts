import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { mapSrsListing } from "../../../sources/srs.js";
import { preserveSrsRaw } from "../../../sources/pure/srs-raw-retention.js";
import { prune } from "../../../lib/util.js";

function row(): any {
  return { permalink: "/properties/sale/synthetic", apto_data: {
    SRS_Listings_ID__c: "synthetic-id", Name: "Synthetic", Availability__c: "Sale",
    unknown: { nullValue: null, falseValue: false, zero: 0, empty: "", object: {}, array: [],
      ordered: [null, false, 0, "", {}, [], { nested: null }] },
  }};
}

test("parsed source JSON survives both prunes with a bound marker", () => {
  const source = row(); const listing = mapSrsListing(source, "sale");
  assert.deepEqual(listing.rawSrs, source);
  source.apto_data.unknown.ordered[0] = "changed";
  const outer = preserveSrsRaw(prune({ sourceKey: "srs", ...listing }), listing.rawSrs, listing.srsRawRetention);
  assert.deepEqual(outer.rawSrs.apto_data.unknown.ordered, [null, false, 0, "", {}, [], { nested: null }]);
  assert.equal(outer.srsRawRetention.status, "complete");
  assert.equal(outer.srsRawRetention.listingId, outer.id);
  assert.equal(outer.srsRawRetention.listingUrl, outer.url);
  const wrong = preserveSrsRaw({ ...outer, id: "other" }, outer.rawSrs, outer.srsRawRetention);
  assert.equal(wrong.rawSrs, undefined); assert.equal(wrong.srsRawRetention.status, "omitted");
  const legacy = { id: "legacy", rawSrs: { old: true } };
  assert.equal(preserveSrsRaw(legacy, undefined, undefined), legacy);
  assert.deepEqual(legacy.rawSrs, { old: true });
});

test("invalid, cyclic, lossy and oversized input retains the listing with explicit omission", () => {
  const variants: any[] = [];
  const cyclic = row(); cyclic.extra = cyclic; variants.push(cyclic);
  const sparse = row(); sparse.extra = new Array(1); sparse.extra.foo = "extra"; variants.push(sparse);
  const customArray = row(); customArray.extra = []; Object.setPrototypeOf(customArray.extra, Date.prototype); variants.push(customArray);
  const subclass = row(); subclass.extra = new (class extends Array {})(); variants.push(subclass);
  const symbol = row(); symbol.extra = { [Symbol("key")]: 1 }; variants.push(symbol);
  const toJSON = row(); toJSON.extra = { toJSON() { throw Error("executed"); } }; variants.push(toJSON);
  for (const value of [undefined, NaN, Infinity, -0, 1n, "x".repeat(1_000_001)]) {
    const input = row(); input.extra = value; variants.push(input);
  }
  const accessor = row(); Object.defineProperty(accessor, "extra", { enumerable: true, get() { throw Error("accessed"); } }); variants.push(accessor);
  let deep: any = {}; const root = deep; for (let i = 0; i < 130; i++) deep = deep.next = {};
  const nested = row(); nested.extra = root; variants.push(nested);
  for (const input of variants) {
    const listing = mapSrsListing(input, "sale");
    assert.equal(listing.id, "synthetic-id"); assert.equal(listing.rawSrs, undefined);
    assert.equal(listing.srsRawRetention.status, "omitted");
    assert.equal(listing.srsRawRetention.reason, "invalid_or_over_limit");
  }
  const idOnly = row(); delete idOnly.permalink;
  const partial = mapSrsListing(idOnly, "sale");
  assert.deepEqual(partial.rawSrs, idOnly);
  assert.equal(partial.srsRawRetention.status, "complete");
  assert.equal(partial.srsRawRetention.binding, "id_only");
  const restored = preserveSrsRaw(prune(partial), partial.rawSrs, partial.srsRawRetention);
  assert.deepEqual(restored.rawSrs, idOnly);
  const unbound = row(); delete unbound.permalink; delete unbound.apto_data.SRS_Listings_ID__c;
  const unknown = mapSrsListing(unbound, "sale");
  assert.deepEqual(unknown.rawSrs, unbound);
  assert.equal(unknown.srsRawRetention.binding, "unbound");
  assert.equal(unknown.srsRawRetention.listingId, null);
  assert.equal(unknown.srsRawRetention.listingUrl, null);
});

test("normal CLI path retains source JSON after outer prune", () => {
  const dir = mkdtempSync(join(tmpdir(), "srs-retention-test-"));
  try {
    const preload = join(dir, "fetch.mjs"); const out = join(dir, "out.json");
    writeFileSync(preload, `globalThis.fetch = async () => new Response(JSON.stringify(${JSON.stringify({total: 1, properties: [row()]})}), {status: 200});`, {mode: 0o600});
    const result = spawnSync(process.execPath, ["--import", "tsx", "--import", preload, "collect.ts", "--source=srs", "--transaction=sale", `--out=${out}`], {encoding: "utf8"});
    assert.equal(result.status, 0, result.stderr);
    const listing = JSON.parse(readFileSync(out, "utf8")).listings[0];
    assert.deepEqual(listing.rawSrs, row()); assert.equal(listing.srsRawRetention.status, "complete");
  } finally { rmSync(dir, {recursive:true, force:true}); }
});
