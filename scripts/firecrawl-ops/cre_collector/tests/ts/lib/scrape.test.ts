// Isolate argv before scrape.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { parseJsonBody, repairUnescapedJsonStringQuotes, withRequestDeadline } from "../../../lib/scrape.js";

describe("withRequestDeadline", () => {
  it("returns a settled request result", async () => {
    assert.equal(await withRequestDeadline(Promise.resolve("ok"), 25), "ok");
  });

  it("rejects a request that does not settle within its client deadline", async () => {
    await assert.rejects(
      () => withRequestDeadline(new Promise<never>(() => undefined), 5),
      /timed out after 5ms/
    );
  });
});

describe("parseJsonBody", () => {
  it("parses plain JSON object", () => {
    assert.deepEqual(parseJsonBody('{"count":42,"ok":true}'), { count: 42, ok: true });
  });

  it("parses plain JSON array", () => {
    assert.deepEqual(parseJsonBody("[1,2,3]"), [1, 2, 3]);
  });

  it("parses HTML-wrapped JSON (Chrome viewer style)", () => {
    const wrapped =
      '<html><body><pre id="json">{"name":&quot;Tower&quot;,&quot;id&quot;:7}</pre></body></html>';
    assert.deepEqual(parseJsonBody(wrapped), { name: "Tower", id: 7 });
  });

  it("parses JSON embedded in HTML with entity escapes", () => {
    const wrapped = "<div>&lt;ignored&gt;</div>{&quot;items&quot;:[&quot;a&quot;,&quot;b&quot;]}";
    assert.deepEqual(parseJsonBody(wrapped), { items: ["a", "b"] });
  });

  it("repairs unescaped inner quotes in string values", () => {
    const broken = '{"title":"12" Main St"}';
    assert.deepEqual(parseJsonBody(broken), { title: '12" Main St' });
  });

  it("returns null for malformed JSON with no recoverable span", () => {
    assert.equal(parseJsonBody("not json at all"), null);
    assert.equal(parseJsonBody("{broken"), null);
    assert.equal(parseJsonBody(""), null);
  });
});

describe("repairUnescapedJsonStringQuotes", () => {
  it("escapes inner quotes that are not string terminators", () => {
    const input = '{"note":"said "hello" there"}';
    const repaired = repairUnescapedJsonStringQuotes(input);
    assert.equal(repaired, '{"note":"said \\"hello\\" there"}');
    assert.deepEqual(JSON.parse(repaired), { note: 'said "hello" there' });
  });

  it("leaves valid JSON string boundaries unchanged", () => {
    const input = '{"a":"x","b":"y"}';
    assert.equal(repairUnescapedJsonStringQuotes(input), input);
  });

  it("preserves already-escaped quotes", () => {
    const input = '{"msg":"already \\"fine\\""}';
    assert.equal(repairUnescapedJsonStringQuotes(input), input);
  });
});
