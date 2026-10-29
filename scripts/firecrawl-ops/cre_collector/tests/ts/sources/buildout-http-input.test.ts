process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { buildoutDetailDocIsUsable, buildoutDetailIframeUrl, buildoutSlugFromUrl, enrichBuildoutDetail, enrichBuildoutDetailObserved } from "../../../sources/buildout.js";
import { REGISTERED_BUILDOUT_FIRMS } from "../../../sources/buildout-registry.js";
import { firecrawl } from "../../../lib/scrape.js";
import { buildoutEnricherFor } from "../../../lib/enrich.js";

const leibsohn = REGISTERED_BUILDOUT_FIRMS.find(firm => firm.sourceKey === "leibsohn")!.detailConfig!;
const url = "http://www.leibsohn.com/properties/?propertyId=example-sale";

test("HTTP source input is scoped and the constructed route uses HTTPS", () => {
  assert.equal(REGISTERED_BUILDOUT_FIRMS.filter(firm => firm.detailConfig?.allowHttpListingInput).length, 1);
  const result = buildoutDetailIframeUrl("leibsohn", url, leibsohn)!;
  const parsed = new URL(result);
  assert.equal(parsed.protocol, "https:");
  assert.equal(parsed.hostname, "buildout.com");
  assert(parsed.pathname.endsWith("/inventory/example-sale"));
  assert(new URL(buildoutDetailIframeUrl("leibsohn", url.replace("example-sale", "example-lease"), leibsohn)!).pathname.endsWith("/inventory/example-lease"));
  assert.equal(buildoutDetailIframeUrl("leibsohn", url, { ...leibsohn, allowHttpListingInput: undefined }), null);
  assert.equal(buildoutDetailIframeUrl("faris-lee", url, leibsohn), null);
  assert(buildoutDetailIframeUrl("leibsohn", `${url}&propertyId=`, leibsohn));
});

test("literal detail routes and JavaScript identity agree with Python staging", () => {
  const values = ["parcel-sale", "parcel-lease", "parcel-SALE", "parcel-Lease", "pre-sale-label", "parcel"];
  const urls = values.map(value => `http://www.leibsohn.com/properties/?propertyId=${value}`);
  const script = `import json,sys\nfrom cre_ingest import to_row\nurls=json.load(sys.stdin)\nprint(json.dumps([to_row({'sourceKey':'leibsohn','id':'native-fixture','url':url}, {}, '2026-10-29T00:00:00Z')['external_id'] for url in urls]))`;
  const result = spawnSync("python3", ["-c", script], { input: JSON.stringify(urls), encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), ["parcel", "parcel", "parcel-SALE", "parcel-Lease", "pre-sale-label", "parcel"]);
  assert.deepEqual(urls.map(value => buildoutSlugFromUrl(value)), JSON.parse(result.stdout));
  for (let index = 0; index < urls.length; index++) {
    assert(new URL(buildoutDetailIframeUrl("leibsohn", urls[index], leibsohn)!).pathname.endsWith(`/inventory/${values[index]}`));
  }
});

test("rejects ambiguous property IDs and unsafe authorities", () => {
  for (const candidate of [
    `${url}&propertyId=other`, `${url}&propertyId=example-sale`,
    url.replace("example-sale", "%65xample-sale"), url.replace("propertyId", "%70ropertyId"),
    `${url}&%70ropertyId=`,
    url.replace("example-sale", "-sale"), url.replace("example-sale", "bad%20slug"),
    url.replace("www.leibsohn.com", "attacker.example"),
    url.replace("www.leibsohn.com", "www.leibsohn.com.attacker.example"),
    url.replace("www.leibsohn.com", "user@www.leibsohn.com"),
    url.replace("www.leibsohn.com", "www.leibsohn.com:80"), `${url}#fragment`, ` ${url}`,
  ]) assert.equal(buildoutDetailIframeUrl("leibsohn", candidate, leibsohn), null);
});

test("enrichment requests only the HTTPS iframe and echoes source URL; failure yields no replacement", async () => {
  const original = firecrawl.scrape;
  const targets: string[] = [];
  try {
    (firecrawl as any).scrape = async (target: string) => {
      targets.push(target);
      return { rawHtml: "<h1>Detail</h1>", markdown: "Detail", links: [] };
    };
    const result = await enrichBuildoutDetail("leibsohn", url, leibsohn);
    assert.equal(result.url, url);
    assert.equal(result.id, "example");
    assert.equal(targets.length, 1);
    assert.equal(new URL(targets[0]).origin, "https://buildout.com");
    (firecrawl as any).scrape = async () => { throw new Error("fixture failure"); };
    assert.equal(await enrichBuildoutDetail("leibsohn", url, leibsohn), null);
  } finally { (firecrawl as any).scrape = original; }
});

test("empty and loading shells remain observations without a canonical detail", async () => {
  const original = firecrawl.scrape;
  const shells = [
    { rawHtml: "<html><head><title>Buildout</title></head><body></body></html>", markdown: "" },
    { rawHtml: "<html><body><script>loadListing()</script><p>Loading...</p></body></html>", markdown: "Loading..." },
    { rawHtml: '<body><img src="photo.jpg"><a href="doc.pdf">Brochure</a></body>', markdown: "[Brochure](doc.pdf)" },
    { rawHtml: '<body><img src="photo.jpg"></body>', markdown: "![Photo][1]\n\n[1]: https://example.test/photo.jpg" },
    { rawHtml: "<body></body>", markdown: "[Brochure][]\n\n[Brochure]: https://example.test/file.pdf" },
    { rawHtml: "<body></body>", markdown: "[Brochure]\n\n[Brochure]: https://example.test/file.pdf" },
    { rawHtml: "<body></body>", markdown: '<img src="photo.jpg"><a href="doc.pdf">Brochure</a>' },
    { rawHtml: "<body></body>", markdown: "[Brochure](doc.pdf). !?" },
    { rawHtml: "<body></body>", markdown: "![photo](https://example.test/a_(1).png)" },
    { rawHtml: "<body></body>", markdown: "![photo](<https://example.test/a_(1).png>)" },
    { rawHtml: "<body></body>", markdown: "![photo](https://example.test/a_((1)).png)" },
    { rawHtml: "<body></body>", markdown: "![photo](https://example.test/a_\\(1\\).png)" },
  ];
  try {
    for (const doc of shells) {
      assert.equal(buildoutDetailDocIsUsable(doc), false);
      (firecrawl as any).scrape = async () => doc;
      let observation: { saved: { rawHtml?: string; markdown?: string }; qualification: string } | undefined;
      const result = await enrichBuildoutDetailObserved("leibsohn", url, leibsohn,
        (saved, _page, qualification) => { observation = { saved, qualification }; });
      assert.equal(result, null);
      assert.equal(observation?.qualification, "rejected_unqualified_detail");
      assert.equal(observation?.saved.rawHtml, doc.rawHtml);
      assert.equal(observation?.saved.markdown, doc.markdown);
    }
    assert.equal(buildoutDetailDocIsUsable({ rawHtml: "<body><p>Available space: 2,000 SF</p></body>" }), true);
    assert.equal(buildoutDetailDocIsUsable({ markdown: "Partial property description" }), true);
    assert.equal(buildoutDetailDocIsUsable({ markdown: "[2,000 SF]" }), true);
    assert.equal(buildoutDetailDocIsUsable({ markdown: "[Unresolved][reference]" }), true);
    assert.equal(buildoutDetailDocIsUsable({ markdown: "Available space: [details](https://example.test)." }), true);
    (firecrawl as any).scrape = async () => shells[2];
    const captured = await buildoutEnricherFor(leibsohn).enrichWithEvidence!([{ sourceKey: "leibsohn", externalId: "fixture", url }]);
    assert.deepEqual(captured.listings, []);
    assert.equal((captured.retainedObservations[0] as any).qualification, "rejected_unqualified_detail");
  } finally { (firecrawl as any).scrape = original; }
});

test("malformed response fields retain the original observation without completing detail", async () => {
  const original = firecrawl.scrape;
  try {
    for (const doc of [{ rawHtml: 42, markdown: "partial" }, { rawHtml: "<p>partial</p>", markdown: true }]) {
      (firecrawl as any).scrape = async () => doc;
      let observation: any;
      const result = await enrichBuildoutDetailObserved("leibsohn", url, leibsohn,
        (saved, _page, qualification) => { observation = { saved, qualification }; });
      assert.equal(result, null);
      assert.equal(observation.qualification, "rejected_unqualified_detail");
      assert.equal(observation.saved.rawHtml, doc.rawHtml);
      assert.equal(observation.saved.markdown, doc.markdown);
    }
  } finally { (firecrawl as any).scrape = original; }
});
