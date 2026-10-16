import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { observationNow } from "../../../lib/observation-clock.js";
import { srcEssexRealty } from "../../../sources/essex-realty.js";

const base = "https://essexrealtygroup.com/properties/";
const template = readFileSync(new URL("../../fixtures/essex-detail.html", import.meta.url), "utf8");
function transport(drift = false) {
  const calls: string[] = [];
  let currentReads = 0;
  return { calls, async getText(url: string) {
    calls.push(url);
    if (url === base) {
      currentReads++;
      return `<a class="allposts">ALL [1]</a><a class="property-card" href="${base}EXISTING_PROPERTY/"><span class="property-card__label">ON MARKET</span><span class="property-info__address--address-1">${drift && currentReads === 2 ? "Changed" : "Current"}</span></a>`;
    }
    if (url === base + "?sale_deal_status_id=3") return `<a class="allposts">ALL [1]</a><a class="property-card" href="${base}CLOSED_PROPERTY/"><span class="property-card__label">CLOSED</span><span class="property-info__address--address-1">Closed</span></a>`;
    if (url === "https://essexrealtygroup.com/properties-sitemap.xml") return `<urlset><url><loc>${base}EXISTING_PROPERTY/</loc></url><url><loc>${base}CLOSED_PROPERTY/</loc></url></urlset>`;
    assert.equal(url, base + "EXISTING_PROPERTY/");
    return template;
  } };
}

test("Essex proof cache is immutable and limited to the exact successful full generation", async (t) => {
  const keys = ["CRE_REQUIRE_FRESH_DETAILS", "CRE_REFRESH_GENERATION", "CRE_REFRESH_STARTED_AT"];
  const previous = keys.map(key => process.env[key]);
  t.after(() => keys.forEach((key, i) => { if (previous[i] === undefined) delete process.env[key]; else process.env[key] = previous[i]; }));
  process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
  process.env.CRE_REFRESH_GENERATION = "essex-proof-cache";
  process.env.CRE_REFRESH_STARTED_AT = observationNow();
  const own = transport();
  const sale = await srcEssexRealty("sale", Infinity, false, own);
  assert.equal(own.calls.length, 7);
  const proof = sale.currentInventoryProof!;
  assert.equal(proof.schema, "essex-strict-detail-v1");
  if (proof.schema !== "essex-strict-detail-v1") return;
  assert.equal(proof.cohort.length, 1);
  proof.passes[0].current.rows[0][0] = "https://evil.test/";
  const lease = await srcEssexRealty("lease", Infinity, false, own);
  assert.equal(own.calls.length, 7);
  const empty = lease.currentInventoryProof!;
  if (empty.schema !== "essex-strict-detail-v1") return;
  assert.equal(empty.passes[0].current.rows[0][0], base + "EXISTING_PROPERTY/");
  assert.equal(empty.kind, "known_empty_transaction");
  assert.deepEqual(empty.details, []);
  assert.equal((await srcEssexRealty("lease", 1, false, own)).currentInventoryProof, undefined);
  assert.equal((await srcEssexRealty("lease", Infinity, true, own)).currentInventoryProof, undefined);
  process.env.CRE_REFRESH_GENERATION = "another-generation";
  assert.equal((await srcEssexRealty("lease", Infinity, false, own)).currentInventoryProof, undefined);
  process.env.CRE_REFRESH_GENERATION = "essex-proof-cache";
  process.env.CRE_REFRESH_STARTED_AT = "2026-01-01T00:00:00Z";
  assert.equal((await srcEssexRealty("lease", Infinity, false, own)).currentInventoryProof, undefined);
  process.env.CRE_REFRESH_STARTED_AT = observationNow();
  const failed = transport(true);
  await assert.rejects(() => srcEssexRealty("sale", Infinity, false, failed), /passes drifted/);
  assert.equal(failed.calls.length, 6);
  assert.equal((await srcEssexRealty("lease", Infinity, false, failed)).currentInventoryProof, undefined);
  const capped = await srcEssexRealty("sale", 1, false, transport());
  assert.equal(capped.currentInventoryProof, undefined);
});
