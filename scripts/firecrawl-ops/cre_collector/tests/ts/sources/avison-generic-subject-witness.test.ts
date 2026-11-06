import test from "node:test";
import assert from "node:assert/strict";
import { assertAvisonYoungDetailDoc } from "../../../sources/avison-young.js";

test("fresh generic detail needs a property subject witness, not just its URL", () => {
  const previous = process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS;
  const url = "https://fixture.sharplaunch.com/";
  const base = { id: "9002", name: "Fixture Property", street: "100 Fixture Street", sharpLaunchUrl: url };
  const doc = (rawHtml: string, sourceURL = url) => ({ rawHtml, markdown: "", links: [], metadata: { statusCode: 200, sourceURL } });
  try {
    process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS = "1";
    assert.throws(() => assertAvisonYoungDetailDoc(doc(
      "<main><h1>Unrelated Subject</h1><p>Other building.</p></main>"), url, base), /identity/);
    assert.throws(() => assertAvisonYoungDetailDoc(doc(
      "<main><h1>Unrelated Subject</h1><p>See also Fixture Property.</p></main>"), url, base), /identity/);
    assert.doesNotThrow(() => assertAvisonYoungDetailDoc(doc(
      "<main><h1>Fixture Property</h1><p>Details.</p></main>"), url, base));
    assert.doesNotThrow(() => assertAvisonYoungDetailDoc(doc(
      `<html><head><script type="application/ld+json">${JSON.stringify({
        "@type": "RealEstateListing", name: "Fixture Property", url,
      })}</script></head><body><main><h1>Available now</h1></main></body></html>`), url, base));
    assert.throws(() => assertAvisonYoungDetailDoc(doc(
      "<main><h1>Fixture Property</h1></main>", "https://other.sharplaunch.com/"), url, base), /identity/);
    const redirect = doc(`<html><head><script type="application/ld+json">${JSON.stringify({
      "@type": "RealEstateListing", name: "Fixture Property", url,
    })}</script></head><body><main><h1>Fixture Property</h1></main></body></html>`);
    assert.throws(() => assertAvisonYoungDetailDoc({ ...redirect, metadata: {
      ...redirect.metadata, url: "https://fixture.sharplaunch.com/other-property?private=1",
    } }, url, base), /final URL is not bound/);
    assert.doesNotThrow(() => assertAvisonYoungDetailDoc({ ...redirect, metadata: {
      ...redirect.metadata, url: "https://fixture.sharplaunch.com",
    } }, url, base));
  } finally {
    if (previous === undefined) delete process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS = previous;
  }
});
