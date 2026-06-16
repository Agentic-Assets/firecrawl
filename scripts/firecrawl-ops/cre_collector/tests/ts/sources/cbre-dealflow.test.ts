import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  parseCbreDealflowLocation,
  listingPvFromCbreDealflowUrl,
  cbreDealflowUrl,
  extractCbreDealflowEngineKey,
  CBRE_DEALFLOW_FALLBACK_ENGINE_KEY,
  cbreDealflowHarvestHtml,
  cbreDealflowStrandedStructured,
  cbreDealflowNewFieldsFromRawData,
} from "../../../sources/cbre-dealflow.js";
import { harvestDetail } from "../../../lib/harvest.js";

// ---------------------------------------------------------------------------
// Fixture helpers
// ---------------------------------------------------------------------------

const __filename = fileURLToPath(import.meta.url);
const __dir = dirname(__filename);
const FIXTURE_PATH = join(__dir, "../../fixtures/raw_data/cbre.json");

function loadFixture(): Array<{ _comment?: string; external_id: string; sourceKey: string; raw_data: any }> {
  return JSON.parse(readFileSync(FIXTURE_PATH, "utf8"));
}

test("parseCbreDealflowLocation extracts city and state", () => {
  assert.deepEqual(parseCbreDealflowLocation("Dallas, TX"), {
    city: "Dallas",
    state: "TX",
  });
  assert.deepEqual(parseCbreDealflowLocation("Austin\u201A TX"), {
    city: "Austin",
    state: "TX",
  });
  assert.deepEqual(parseCbreDealflowLocation("Houston, TX 77002"), {
    city: "Houston",
    state: "TX",
  });
});

test("listingPvFromCbreDealflowUrl reads pv query param", () => {
  assert.equal(
    listingPvFromCbreDealflowUrl("https://www.cbredealflow.com/listing?pv=abc123token"),
    "abc123token"
  );
  assert.equal(listingPvFromCbreDealflowUrl("https://www.cbredealflow.com/listing"), null);
  assert.equal(listingPvFromCbreDealflowUrl(null), null);
});

test("cbreDealflowUrl resolves relative links and rejects unsafe schemes", () => {
  assert.equal(
    cbreDealflowUrl("/properties/us-tx-dallas"),
    "https://www.cbredealflow.com/properties/us-tx-dallas"
  );
  assert.equal(cbreDealflowUrl("javascript:alert(1)"), null);
  assert.equal(cbreDealflowUrl("mailto:broker@example.com"), null);
});

test("extractCbreDealflowEngineKey reads ListingEngine key from HTML", () => {
  const html = `
    <script>
      const engine = new ListingEngine({ key: "engine-key-from-script-012345678901234567890" });
    </script>
  `;
  assert.equal(extractCbreDealflowEngineKey(html), "engine-key-from-script-012345678901234567890");
});

test("extractCbreDealflowEngineKey falls back to pv token or default", () => {
  const html = `<a href="/x?pv=${"A".repeat(32)}">link</a>`;
  assert.equal(extractCbreDealflowEngineKey(html), "A".repeat(32));
  assert.equal(extractCbreDealflowEngineKey("<html></html>"), CBRE_DEALFLOW_FALLBACK_ENGINE_KEY);
});

test("cbreDealflowHarvestHtml concatenates page html + section content fragments", () => {
  const data = {
    sections: [
      { contents: [{ content: '<iframe src="https://player.vimeo.com/video/999"></iframe>' }] },
      { contents: [{ content: "<p>no media here</p>" }] },
    ],
  };
  const html = cbreDealflowHarvestHtml("<html><body>page</body></html>", data);
  assert.match(html, /player\.vimeo\.com\/video\/999/);
  // The harvester picks the embedded iframe up as media.
  const out = harvestDetail({ rawHtml: html } as any, {});
  assert.ok(out.media.some((m) => m.provider === "vimeo"));
});

test("cbreDealflowStrandedStructured lifts caprate/noi/occupancy/year/units; empty for sparse", () => {
  const out = cbreDealflowStrandedStructured({
    projectfields: {
      caprate: "6.75%",
      noi: "$1,250,000",
      occupancy: "92%",
      yearbuilt: "1995",
      units: "150",
      zoning: "MU-1",
    },
  });
  assert.equal(out.capRatePct, 6.75);
  assert.equal(out.noi, 1250000);
  assert.equal(out.occupancyRate, 92);
  assert.equal(out.yearBuilt, 1995);
  assert.equal(out.units, 150);
  assert.equal(out.zoning, "MU-1");
  assert.deepEqual(cbreDealflowStrandedStructured({}), {});
});

// ---------------------------------------------------------------------------
// WS1: cbreDealflowNewFieldsFromRawData - fixture-driven tests
// ---------------------------------------------------------------------------

test("cbreDealflowNewFieldsFromRawData: dealflow fixture row yields statusBadge, contacts phone/title, extraFacts", () => {
  const fixture = loadFixture();
  const row = fixture.find((r) => r.external_id === "dealflow:150532")!;
  assert.ok(row, "fixture row dealflow:150532 must exist");
  const out = cbreDealflowNewFieldsFromRawData(row.raw_data);
  // statusBadge from cbreDealflowDetail.status (preferred) or card status
  assert.equal(out.statusBadge, "Available");
  // contactsDetailed with phone and title
  assert.ok(out.contactsDetailedWithPhoneAndTitle.length >= 2, "must have at least 2 contacts");
  const firstContact = out.contactsDetailedWithPhoneAndTitle[0]!;
  assert.equal(firstContact.name, "Ben Galles");
  assert.equal(firstContact.phone, "775 750 6429");
  assert.equal(firstContact.title, "Senior Vice President");
  const secondContact = out.contactsDetailedWithPhoneAndTitle[1]!;
  assert.equal(secondContact.name, "Katie Galles");
  assert.equal(secondContact.phone, "+1 775 772 6181");
  assert.equal(secondContact.title, "Senior Associate");
  // extraFacts from cbreDealflowDetail.projectType
  assert.ok(out.extraFacts !== null, "extraFacts must be non-null when projectType is present");
  assert.equal(out.extraFacts?.project_type, "Value Add");
});

test("cbreDealflowNewFieldsFromRawData: absent cbreDealflowDetail yields nulls", () => {
  const out = cbreDealflowNewFieldsFromRawData({
    status: "Available",
    contactsDetailed: [{ name: "Jane Smith", phone: "555-1234", title: null }],
  });
  // statusBadge falls back to card-level status when cbreDealflowDetail is absent
  assert.equal(out.statusBadge, "Available");
  assert.equal(out.contactsDetailedWithPhoneAndTitle[0]?.phone, "555-1234");
  assert.equal(out.contactsDetailedWithPhoneAndTitle[0]?.title, null);
  // extraFacts null when no projectType
  assert.equal(out.extraFacts, null);
});

test("cbreDealflowNewFieldsFromRawData: no status, no contacts, no projectType -> all null", () => {
  const out = cbreDealflowNewFieldsFromRawData({});
  assert.equal(out.statusBadge, null);
  assert.deepEqual(out.contactsDetailedWithPhoneAndTitle, []);
  assert.equal(out.extraFacts, null);
});

test("cbreDealflowNewFieldsFromRawData: null input does not throw", () => {
  assert.doesNotThrow(() => {
    const out = cbreDealflowNewFieldsFromRawData(null);
    assert.equal(out.statusBadge, null);
    assert.deepEqual(out.contactsDetailedWithPhoneAndTitle, []);
    assert.equal(out.extraFacts, null);
  });
});

test("cbreDealflowNewFieldsFromRawData: statusBadge is NOT written to status field (routes through OPT-IN gate)", () => {
  // statusBadge must NOT be 'status'; it is the statusBadge camelCase key that routes through
  // the OPT-IN activation gate in cre_ingest.py and never auto-activates.
  const out = cbreDealflowNewFieldsFromRawData({ cbreDealflowDetail: { status: "Sold" }, contactsDetailed: [] });
  assert.equal(out.statusBadge, "Sold");
  // Confirm the returned shape has no 'status' key (that would bypass the gate)
  assert.ok(!("status" in out), "returned shape must not contain a direct 'status' key");
});
