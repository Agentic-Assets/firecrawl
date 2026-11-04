// Draft regressions. Do not execute while the operator's checks hold is active.
process.argv = [process.argv[0]!, process.argv[1]!];
import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import { srcMatthews } from "../../../sources/matthews.js";
import { RetainedSourceError } from "../../../lib/source-observations.js";

const host = "https://www.matthews.com";
const detail = (index: number) => `${host}/properties/partial-${index}`;
const page = (index: number, canonical = detail(index)) => `<html><head>
  <link rel="canonical" href="${canonical}"><script type="application/ld+json">{"unknown":null}</script>
  </head><body><section><h1 id="propertyTitle">Partial ${index}</h1>
  <div id="propertyPrice">Contact broker</div><div class="prose">Literal ${index}</div>
  <div class="key-info-title">Unknown units</div><div class="key-info-value">0</div>
  <table><tr><th>Rate</th><td></td></tr><tr><th>Rate</th><td>Original units</td></tr></table>
  <a href="/brochure.pdf">Brochure</a><img src="/photo.jpg" alt="Literal image"></section></body></html>`;

function provider(t: TestContext, bodies: string[], strict: boolean) {
  const oldFetch = globalThis.fetch, oldTimer = globalThis.setTimeout;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  process.env.CRE_REQUIRE_FRESH_DETAILS = strict ? "1" : "0";
  const requests: string[] = [];
  globalThis.fetch = async (input) => {
    const url = String(input);
    requests.push(url);
    if (url === `${host}/sitemap.xml`) return new Response(`<urlset>${bodies.map((_, i) =>
      `<url><loc>${detail(i)}</loc></url>`).join("")}</urlset>`);
    const index = bodies.findIndex((_, i) => detail(i) === url);
    assert.ok(index >= 0, `unexpected provider request ${url}`);
    return new Response(bodies[index]);
  };
  // Only provider pacing is accelerated; bounded request deadlines stay intact.
  globalThis.setTimeout = ((callback: (...args: any[]) => void, delay?: number, ...args: any[]) =>
    oldTimer(callback, typeof delay === "number" && delay < 10_000 ? 0 : delay, ...args)) as typeof setTimeout;
  t.after(() => {
    globalThis.fetch = oldFetch; globalThis.setTimeout = oldTimer;
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
  });
  return requests;
}

test("78 accessible Matthews identity failures retain ordered context without canonical rows", async (t) => {
  const bodies = Array.from({ length: 78 }, (_, index) => page(index, `${host}/properties/conflict-${index}`));
  const requests = provider(t, bodies, false);
  await assert.rejects(srcMatthews("sale", Infinity, false), (error: unknown) => {
    assert.ok(error instanceof RetainedSourceError);
    assert.match(error.message, /none parsed/);
    assert.equal(error.retainedObservations.length, 78);
    error.retainedObservations.forEach((value, index) => {
      const observation = value as any;
      assert.equal(observation.requestedUrl, detail(index));
      assert.equal(observation.httpStatus, 200);
      assert.equal(observation.identity, "unverified");
      assert.equal(observation.pageContext.scripts[0].text, '{"unknown":null}');
      assert.equal(observation.pageContext.links[0].href, "/brochure.pdf");
      assert.equal(observation.pageContext.tables.status, "observed");
    });
    return true;
  });
  assert.equal(requests.length, 79);
});

test("strict failure retains settled Matthews page observations and keeps the failure", async (t) => {
  provider(t, [page(0), page(1, `${host}/properties/conflict`), page(2)], true);
  await assert.rejects(srcMatthews("sale", Infinity, false), (error: unknown) => {
    assert.ok(error instanceof RetainedSourceError);
    assert.match(error.message, /failed identity or detail validation/);
    const observations = error.retainedObservations as any[];
    assert.ok(observations.some((value) => value.requestedUrl === detail(1)));
    assert.ok(observations.some((value) => value.schema === "matthews-completed-parse-observation/v1"));
    return true;
  });
});

test("Matthews mixed results keep accepted counts and omit challenge-shell context", async (t) => {
  const shell = `<html><head><link rel="canonical" href="${host}/properties/conflict-shell"></head><body><h1>Access Denied</h1></body></html>`;
  provider(t, [page(0), page(1, `${host}/properties/conflict`), shell], false);
  const result = await srcMatthews("sale", Infinity, false);
  assert.equal(result.listings.length, 1);
  assert.equal(result.truncated, true);
  assert.equal(result.retainedObservations?.length, 1);
  assert.equal((result.retainedObservations![0] as any).requestedUrl, detail(1));
});
