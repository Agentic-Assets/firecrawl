import test from "node:test";
import assert from "node:assert/strict";
import {
  parseCbreDealflowLocation,
  listingPvFromCbreDealflowUrl,
  cbreDealflowUrl,
  extractCbreDealflowEngineKey,
  CBRE_DEALFLOW_FALLBACK_ENGINE_KEY,
} from "../../../sources/cbre-dealflow.js";

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
