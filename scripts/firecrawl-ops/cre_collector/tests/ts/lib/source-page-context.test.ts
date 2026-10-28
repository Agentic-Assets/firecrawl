import assert from "node:assert/strict";
import test from "node:test";
import { captureSourcePageContext, isSourceChallengeShell } from "../../../lib/source-page-context/index.js";
import { captureFoundryPageContext } from "../../../sources/pure/foundry-observation.js";

const url = "https://example.test/property/one";
const html = `<html><head><title>Property</title><meta name="description" content="native">
  <link rel="canonical" href="/property/one"><script type="application/ld+json">{"x":false}</script>
  </head><body><h1>Example</h1><div class="property-description">Original description</div>
  <div class="custom-description">Other source description</div><a href="/file.pdf">Brochure</a>
  <img src="/photo.jpg" alt="Photo"><table><tr><th>NOI</th><td>$4,000</td></tr></table></body></html>`;

test("shared capture retains source fields and Foundry wrapper keeps legacy representation", () => {
  const options = { sourceUrl: url, detailObservedAt: "2026-10-28T12:00:00Z" };
  const shared = captureSourcePageContext(html, options);
  const foundry = captureFoundryPageContext(html, url, options.detailObservedAt);
  assert.equal(shared.schema, "cre-source-page-context/v1");
  assert.deepEqual(foundry, { ...shared, schema: "foundry-native-page-context/v1" });
  assert.deepEqual(shared.descriptions, ["Original description"]);
  assert.deepEqual(shared.links, [{ href: "/file.pdf", text: "Brochure" }]);
  assert.deepEqual(shared.images, [{ src: "/photo.jpg", alt: "Photo" }]);
  assert.equal(shared.detailObservedAt, options.detailObservedAt);
  assert.deepEqual(captureSourcePageContext(html, { sourceUrl: url, descriptionSelector: ".custom-description" }).descriptions,
    ["Other source description"]);
  assert.equal(captureSourcePageContext(html, { sourceUrl: url }).detailObservedAt, null);
});

test("bounded context records omissions and avoids splitting surrogate pairs", () => {
  const many = Array.from({ length: 1001 }, () => "<a href='/x'>x</a>").join("");
  const body = `<div class="property-description">${"x".repeat(63999)}😀</div>${many}`;
  const result = captureSourcePageContext(body, { sourceUrl: url });
  assert.equal(result.descriptions[0]!.length, 63999);
  assert.equal(result.omissions.characters, 2);
  assert.equal(result.links.length, 1000);
  assert.equal(result.omissions.links, 1);
});

test("challenge detection uses only the existing bounded title heading and body signals", () => {
  assert.equal(isSourceChallengeShell("<title>Access Denied</title>"), true);
  assert.equal(isSourceChallengeShell("<body><h1>Just a moment</h1></body>"), true);
  assert.equal(isSourceChallengeShell("<body>verify you are human</body>"), true);
  assert.equal(isSourceChallengeShell(`<body>${"x".repeat(3001)} verify you are human</body>`), false);
  assert.equal(isSourceChallengeShell("<body><h1>Property details</h1></body>"), false);
});
