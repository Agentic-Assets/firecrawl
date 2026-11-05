process.argv = [process.argv[0]!, process.argv[1]!, "--page-cap=1"];
import test from "node:test";
import assert from "node:assert/strict";

test("Colliers finite page cap retains its truncated status", async (t) => {
  const { srcColliers } = await import("../../../sources/colliers.js");
  let listCalls = 0;
  t.mock.method(globalThis, "fetch", async (input: RequestInfo | URL) => {
    const url = new URL(String(input));
    if (url.hostname === "sales.colliers.com") return new Response("<html></html>");
    assert.equal(url.searchParams.get("Start"), "1");
    if (url.pathname.endsWith("GetListingsHtml")) {
      listCalls++;
      return Response.json({ success: true, total: 2, totalAvail: 2, numProjects: 1,
        html: '<ul><li class="item"><a class="headline">Property one</a></li></ul>' });
    }
    return Response.json({ success: true, projectLocations: [{ ProjectId: "P1", Latitude: 30, Longitude: -90 }] });
  });
  const result = await srcColliers("sale", Number.POSITIVE_INFINITY, false);
  assert.equal(listCalls, 1);
  assert.equal(result.listings.length, 1);
  assert.equal(result.truncated, true);
});
