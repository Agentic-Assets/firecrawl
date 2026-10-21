import assert from "node:assert/strict";
import test from "node:test";
import { jllDirectHtmlDoc } from "../../sources/jll-direct.js";
import { withOfflineJllProjection } from "../../sources/jll-direct-projection.js";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const context = {
  url: "https://property.jll.com/listings/example",
  statusCode: 202,
  contentType: "text/html; charset=utf-8",
};
// Synthetic primitive coverage, not a captured property or acquisition claim.
const html = `<!doctype html><html lang="en"><head>
<base href="https://source.example/assets/"><title>Fixture title</title>
<meta name="description" content="Complete description"><meta name="statusCode" content="999">
<meta name="contentType" content="invented"><meta name="sourceURL" content="https://wrong.example/">
<meta name="url" content="https://wrong.example/second"><meta name="scrapeId" content="page-claim">
<meta name="proxyUsed" content="page-proxy"><meta name="creditsUsed" content="page-credits">
<meta name="keywords" content="one"><meta name="keywords" content="two"><link rel="icon" href="/favicon.ico">
<script>secretScript()</script></head><body><h1>Fixture</h1>
<div component="video" url="https://vimeo.com/824804225"></div>
<iframe src="players/tour.html?autoplay=0"></iframe>
<a href="docs/offering.pdf">Offering memorandum</a><a href="../brokers/jane">Broker profile</a>
<a href="mailto:broker@example.com">Email</a><a href="#fragment">Section</a>
<video><source src="media/tour.mp4" type="video/mp4"></video><div data-video-url="media/second.mp4"></div>
<table><thead><tr><th>Area</th><th>Rent</th></tr></thead><tbody><tr><td>10,000 SF</td><td>$20</td></tr></tbody></table>
<p><del>Old price</del></p><img src="https://images.example/one.jpg"><img src="https://images.example/two.jpg">
</body></html>`;

test("native reconstruction preserves all five attribute blocks, links, GFM, raw HTML and HTML metadata", async () => {
  const doc = await jllDirectHtmlDoc(html, context);
  assert.equal(doc.rawHtml, html);
  assert.deepEqual(doc.attributes, [
    {
      selector: "div[component=video]",
      attribute: "url",
      values: ["https://vimeo.com/824804225"],
    },
    {
      selector: "iframe",
      attribute: "src",
      values: ["players/tour.html?autoplay=0"],
    },
    {
      selector: "a",
      attribute: "href",
      values: [
        "https://source.example/assets/docs/offering.pdf",
        "https://source.example/brokers/jane",
        "mailto:broker@example.com",
        "https://source.example/assets/#fragment",
      ],
    },
    { selector: "video source", attribute: "src", values: ["media/tour.mp4"] },
    {
      selector: "[data-video-url]",
      attribute: "data-video-url",
      values: ["media/second.mp4"],
    },
  ]);
  assert.deepEqual(doc.links, [
    "https://source.example/assets/docs/offering.pdf",
    "https://source.example/brokers/jane",
    "mailto:broker@example.com",
    "https://source.example/assets/#fragment",
  ]);
  assert.match(doc.markdown, /\| Area\s*\| Rent/);
  assert.match(doc.markdown, /~+Old price~+/);
  assert.doesNotMatch(doc.markdown, /secretScript/);
  assert.equal(doc.metadata?.title, "Fixture title");
  assert.equal(doc.metadata?.description, "Complete description");
  assert.equal(
    doc.metadata?.favicon,
    "https://property.jll.com/favicon.ico",
    "favicon uses actual page URL, not HTML base",
  );
  assert.deepEqual(doc.metadata?.keywords, ["one", "two"]);
  assert.equal(doc.metadata?.statusCode, context.statusCode);
  assert.equal(doc.metadata?.contentType, context.contentType);
  assert.equal(doc.metadata?.sourceURL, context.url);
  assert.equal(doc.metadata?.url, context.url);
  assert.equal(doc.metadata?.scrapeId, "page-claim");
  assert.equal(doc.metadata?.proxyUsed, "page-proxy");
  assert.equal(doc.metadata?.creditsUsed, "page-credits");
  assert.equal(doc.metadata?.transport, undefined);
  assert.equal(doc.detailObservation, undefined);
  assert.equal(doc.images?.length, 2);
  assert.ok(doc.images?.includes("https://images.example/one.jpg"));
  assert.ok(doc.images?.includes("https://images.example/two.jpg"));
});

const one = "https://images.example/one.jpg";
const two = "https://images.example/two.jpg";
const member = {
  sampleId: "synthetic",
  providerId: "123",
  canonicalUrl: context.url,
  baseRow: { id: "123", url: context.url, transactionType: "lease" },
};
function propertyHtml(images: string[]): string {
  const property = {
    id: "123",
    pageUrl: "/listings/example",
    title: "Example",
    images,
    hidePrice: false,
    description: "Useful description",
  };
  return `<html><body><h1>Example</h1><p>Useful description</p><img src="${one}"><img src="${two}"><script id="__NEXT_DATA__">${JSON.stringify({ props: { pageProps: { property, brokers: [{ name: "Example Broker", email: "broker@jll.com" }] } } })}</script></body></html>`;
}
async function isolatedProjection(
  run: Parameters<typeof withOfflineJllProjection>[0],
) {
  const cache = mkdtempSync(join(tmpdir(), "jll-native-projection-"));
  const variables = {
    JLL_DETAIL_CACHE_DIR: cache,
    CRE_REFRESH_GENERATION: "synthetic-native-contract",
    CRE_REQUIRE_FRESH_DETAILS: "1",
    CRE_REFRESH_STARTED_AT: new Date(Date.now() - 2000).toISOString(),
  };
  const previous = Object.fromEntries(
    Object.keys(variables).map((key) => [key, process.env[key]]),
  );
  Object.assign(process.env, variables);
  try {
    await withOfflineJllProjection(run);
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    rmSync(cache, { recursive: true, force: true });
  }
}
async function observedDoc(images: string[]) {
  const doc = await jllDirectHtmlDoc(propertyHtml(images), {
    ...context,
    statusCode: 200,
  });
  // Synthetic parser input only, not evidence of a new acquisition.
  doc.detailObservation = {
    method: "jll_direct_candidate",
    cacheDisposition: "live",
    generationId: "synthetic-native-contract",
    observedAt: new Date().toISOString(),
  };
  return doc;
}

test("anchored native gallery retains exact final source photo order and primary", async () => {
  await isolatedProjection(async (project) => {
    for (let repetition = 0; repetition < 3; repetition++) {
      const result = await project(member, await observedDoc([two, one]));
      assert.deepEqual(result.listing.photos, [two, one]);
      assert.equal(result.listing.photos[0], two);
      assert.equal(result.listing.name, "Example");
      assert.ok(result.listing.brokerIds.length > 0);
    }
  });
});

test("unanchored native assets survive and source retains supplied order; no DOM or repeat-order parity credit", async () => {
  await isolatedProjection(async (project) => {
    const doc = await observedDoc([]);
    assert.equal(doc.images?.length, 2);
    assert.ok(doc.images?.includes(one));
    assert.ok(doc.images?.includes(two));
    const suppliedOrder = [...doc.images!];
    const result = await project(member, doc);
    assert.deepEqual(result.listing.photos, suppliedOrder);
    assert.equal(result.listing.photos[0], suppliedOrder[0]);
    // The saved native-final.log parity failure remains an unmet gate. This
    // assertion does not establish DOM order or stable order across native calls.
  });
});

test("native empty gallery stays empty and an empty page is refused", async () => {
  const doc = await jllDirectHtmlDoc(
    "<html><body><h1>No gallery</h1></body></html>",
    context,
  );
  assert.deepEqual(doc.images, []);
  assert.deepEqual(doc.links, []);
  await assert.rejects(
    jllDirectHtmlDoc(
      "<html><head><script>only()</script></head><body></body></html>",
      context,
    ),
    /no durable page Markdown/,
  );
});
