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
  cbreDealflowDetailUnavailableReason,
  cbreDealflowUnavailableCard,
  cbreDealflowUnlinkedCardId,
  parseCbreDealflowCards,
  enrichCbreDealflowCard,
  cbreDealflowNumProjects,
  cbreDealflowAssertPageCount,
  cbreDealflowAssertHtmlOnlyMix,
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

test("CBRE Deal Flow recognizes a current card with no configured public landing detail", () => {
  const html = `
    <div>
      The landing page executive summary has been enabled,
      but the landing page has not been setup.
    </div>
  `;
  assert.equal(cbreDealflowDetailUnavailableReason(html), "landing_not_setup");
  assert.equal(
    cbreDealflowDetailUnavailableReason(`
      <html>
        <head><title>State of Florida Surplus Lands | CBRE | Powered by LightBox</title></head>
        <body>
          <div id="ProjectNameAndAddress">
            <div id="ProjectName">State of Florida Surplus Lands</div>
          </div>
          <div id="Content">
            <div class="TabContent">
              <p>This public property landing page is available without an embedded data object.</p>
              <p>Contact the listed brokerage team for current offering information.</p>
            </div>
          </div>
        </body>
      </html>
    `, "State of Florida Surplus Lands"),
    "public_html_only"
  );
  assert.equal(
    cbreDealflowDetailUnavailableReason(`
      <html>
        <head><title>Maintenance | CBRE | Powered by LightBox</title></head>
        <body>
          <script>${"provider shell ".repeat(20)}</script>
          <p>Temporarily unavailable. Please try again later.</p>
        </body>
      </html>
    `, "Maintenance"),
    null
  );
  assert.equal(
    cbreDealflowDetailUnavailableReason(`
      <html>
        <head><title>Different Property | CBRE | Powered by LightBox</title></head>
        <body>
          <div id="ProjectNameAndAddress">
            <div id="ProjectName">Different Property</div>
          </div>
          <div id="Content">
            <div class="TabContent">
              <p>This is a substantive public property page with enough visible body text to pass the length floor.</p>
            </div>
          </div>
        </body>
      </html>
    `, "Expected Property"),
    null
  );
  assert.equal(cbreDealflowDetailUnavailableReason("<html>unexpected empty page</html>"), null);
});

test("CBRE Deal Flow HTML-only classification fails closed on a layout-wide anomaly", () => {
  const listing = (reason: string | null) => ({
    detailUnavailable: reason ? { reason } : undefined,
  });
  assert.equal(
    cbreDealflowAssertHtmlOnlyMix([
      ...Array.from({ length: 5 }, () => listing("public_html_only")),
      ...Array.from({ length: 95 }, () => listing(null)),
    ]),
    5
  );
  assert.throws(
    () =>
      cbreDealflowAssertHtmlOnlyMix([
        ...Array.from({ length: 6 }, () => listing("public_html_only")),
        ...Array.from({ length: 94 }, () => listing(null)),
      ]),
    /public_html_only anomaly/
  );
});

test("CBRE Deal Flow unavailable detail preserves prior children and keeps fresh card fields", () => {
  const row = cbreDealflowUnavailableCard(
    {
      id: "public-card-token",
      url: "https://www.cbredealflow.com/handler/landing.aspx?pv=public-card-token",
      urlKind: "detail",
      listingPv: "public-card-token",
      name: "Current public card",
      transactionType: "Investment Sale",
      assetType: "Multifamily",
      description: "Fresh card description",
      city: "New York",
      state: "NY",
      country: "United States",
      sizeText: "9,766 sf",
      status: "Available",
      brokerIds: [],
      contactsDetailed: [{ name: "Current Broker" }],
      brochures: [],
      photos: ["https://example.test/current.jpg"],
      cbreDealflowCard: { projectType: "Investment Sale" },
    },
    "landing_not_setup"
  );
  assert.equal(row.detailUnavailable.reason, "landing_not_setup");
  assert.equal(row.detailUnavailable.publicPageObserved, true);
  assert.equal(row.preserveChildCollections, true);
  assert.equal(row.statusBadge, "Available");
  assert.deepEqual(row.extraFacts, { project_type: "Investment Sale" });
  assert.equal(row.name, "Current public card");

  const htmlOnly = cbreDealflowUnavailableCard(
    {
      ...row,
      urlKind: "detail",
      cbreDealflowCard: { projectType: "Investment Sale" },
    },
    "public_html_only"
  );
  assert.equal(htmlOnly.detailUnavailable.publicPageObserved, true);
});

test("CBRE Deal Flow retains agreement-gated and unlinked provider cards", () => {
  const html = `
    <ul class="gridview">
      <li class="item">
        <div class="card">
          <div class="img"><a class="summary" href="/buyer/agreement?pv=agreement-token"><p>Agreement listing</p></a></div>
          <div class="headline">Agreement listing</div>
          <div class="location"><div class="city">Dallas, TX</div></div>
          <span class="asset">Retail</span><span class="status">Available</span>
          <div class="details">Investment Sale | 10,000 sq ft</div>
        </div>
      </li>
      <li class="item">
        <div class="card">
          <div class="img"><a class="summary"><p>Coming soon listing</p></a></div>
          <div class="headline">Coming soon listing</div>
          <div class="location"><div class="city">Austin, TX</div></div>
          <span class="asset">Industrial</span><span class="status">Coming Soon</span>
          <div class="details">Investment Sale | 20,000 sq ft</div>
        </div>
      </li>
    </ul>
  `;
  const cards = parseCbreDealflowCards(html, "sale");
  assert.equal(cards.length, 2);
  assert.equal(cards[0]?.urlKind, "agreement");
  assert.equal(cards[0]?.listingPv, "agreement-token");
  assert.equal(cards[1]?.urlKind, "unlinked");
  assert.match(cards[1]?.id ?? "", /^card:[0-9a-f]{24}$/);
  assert.equal(cards[1]?.cbreDealflowCard.cardIdentity, cards[1]?.id);
  assert.match(cards[0]?.cbreDealflowCard.cardIdentity ?? "", /^card:[0-9a-f]{24}$/);
  assert.equal(cards[1]?.url, null);
});

test("CBRE Deal Flow unlinked-card identity is deterministic and rejects nameless cards", () => {
  const fields = {
    name: "Coming Soon Listing",
    city: "Austin",
    state: "TX",
    assetType: "Industrial",
  };
  assert.equal(cbreDealflowUnlinkedCardId(fields), cbreDealflowUnlinkedCardId(fields));
  assert.notEqual(
    cbreDealflowUnlinkedCardId(fields),
    cbreDealflowUnlinkedCardId({ ...fields, city: "Dallas" })
  );
  assert.equal(cbreDealflowUnlinkedCardId({ ...fields, name: null }), null);
});

test("CBRE Deal Flow pagination count fails closed when malformed", () => {
  assert.equal(cbreDealflowNumProjects(200, 1), 200);
  assert.equal(cbreDealflowNumProjects("0", 2001), 0);
  for (const invalid of [undefined, null, "not-a-number", -1, 1.5, Number.NaN]) {
    assert.throws(() => cbreDealflowNumProjects(invalid, 1), /invalid numProjects/);
  }
  assert.equal(cbreDealflowAssertPageCount(200, 200, 1), 200);
  assert.throws(() => cbreDealflowAssertPageCount(0, 200, 1), /parity failed/);
  assert.throws(() => cbreDealflowAssertPageCount(201, 200, 1), /parity failed/);
});

test("CBRE Deal Flow classifies agreement and unlinked cards without a failing detail request", async () => {
  const base = {
    id: "card-id",
    url: "https://www.cbredealflow.com/",
    listingPv: null,
    name: "Current card",
    transactionType: "Investment Sale",
    assetType: "Office",
    description: null,
    city: "Dallas",
    state: "TX",
    country: "United States",
    sizeText: null,
    status: "Available",
    brokerIds: [],
    photos: [],
    cbreDealflowCard: { projectType: "Investment Sale" },
  };
  const agreement = await enrichCbreDealflowCard(
    { ...base, urlKind: "agreement", url: "https://www.cbredealflow.com/buyer/agreement?pv=card-id" },
    "sale"
  );
  const unlinked = await enrichCbreDealflowCard({ ...base, urlKind: "unlinked", url: null }, "sale");
  const brochure = await enrichCbreDealflowCard(
    {
      ...base,
      urlKind: "brochure",
      url: "https://www.cbredealflow.com/buyer/brochure?pv=card-id",
      brochures: [{ name: "Public brochure", url: "https://www.cbredealflow.com/buyer/brochure?pv=card-id" }],
    },
    "sale"
  );
  assert.equal(agreement.detailUnavailable.reason, "gated_agreement");
  assert.equal(unlinked.detailUnavailable.reason, "card_not_linked");
  assert.equal(unlinked.provisionalIdentity.historyContinuity, "not_guaranteed");
  assert.equal(unlinked.inventoryOnly.reason, "no_provider_id_or_listing_url");
  assert.equal(unlinked.inventoryOnly.indexUrl, "https://www.cbredealflow.com/");
  assert.equal(brochure.detailUnavailable.reason, "public_brochure_only");
  assert.equal(brochure.brochures.length, 1);
  assert.equal(agreement.detailUnavailable.publicCardObserved, true);
  assert.equal(agreement.detailUnavailable.publicPageObserved, undefined);
  assert.equal(unlinked.detailUnavailable.publicPageObserved, undefined);
  assert.equal(agreement.preserveChildCollections, true);
  assert.equal(unlinked.preserveChildCollections, true);
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
