import assert from "node:assert/strict";
import test from "node:test";
import { captureKidderTeamPage, parseKidderTeamPage } from "../../../sources/kidder-team-evidence.js";
import { srcKidderMathews } from "../../../sources/kidder-mathews.js";
import { RetainedSourceError } from "../../../lib/source-observations.js";
import { NativeEvidenceTerminalError } from "../../../lib/source-evidence/terminal-error.js";

const html = `<html><body><section id="available-properties"><article class="card-post"><a class="card-post__link" href="/same"><h4>A</h4><p>$1</p></a></article><article class="card-post"><a class="card-post__link" href="/same"><h4>B</h4><p>$2</p></a></article><article class="card-post"><div><h4>C</h4></div></article></section></body></html>`;

test("keeps all occurrences and unverified conflicting or missing links", () => {
  const result = parseKidderTeamPage(html, "2026-11-07T03:30:00Z");
  assert.equal(result.coverage, "partial_unknown");
  assert.equal(result.identity, "unverified");
  assert.deepEqual(result.cards.map(card => card.href), ["/same", "/same", null]);
  assert.match(result.cards[1].html, /B/);
  assert.equal(result.decodedHtml, html);
  assert.equal(parseKidderTeamPage("<html><body>changed</body></html>", "now").recognizedSection, false);
  assert.throws(() => parseKidderTeamPage("<title>Just a moment</title>", "now"), /challenge/);
  assert.throws(() => parseKidderTeamPage("<script src=\"/cdn-cgi/challenge-platform/test\"></script>", "now"), /challenge/);
});

test("does not follow denials or redirects and retains no challenge page", async () => {
  for (const response of [new Response("denied", { status: 403 }), new Response(null, { status: 302 }), new Response("<title>Just a moment</title>", { headers: { "content-type": "text/html" } })]) {
    let count = 0;
    await assert.rejects(captureKidderTeamPage(async (_url, options) => { count++; assert.equal(options?.redirect, "manual"); return response; }));
    assert.equal(count, 1);
  }
});

test("settled 403 retains page once while both transactions remain failures", async () => {
  const oldFetch = globalThis.fetch, oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS, oldGeneration = process.env.CRE_REFRESH_GENERATION;
  const calls: string[] = [];
  globalThis.fetch = async input => {
    calls.push(String(input));
    return String(input).includes("services.kidder.com") ? new Response("denied", { status: 403 }) : new Response(html, { headers: { "content-type": "text/html; charset=utf-8" } });
  };
  try {
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    process.env.CRE_REFRESH_GENERATION = "kidder-team-synthetic";
    for (const tx of ["sale", "lease"] as const) {
      await assert.rejects(srcKidderMathews(tx, Infinity, false), (error: unknown) => {
        assert(error instanceof RetainedSourceError);
        assert.match(error.message, /HTTP 403/);
        assert.deepEqual((error.retainedObservations[0] as any).cards.map((c: any) => c.href), ["/same", "/same", null]);
        return true;
      });
    }
    assert.equal(calls.length, 2);
    assert.match(calls[0], /services\.kidder/);
    assert.match(calls[1], /kidder\.com\/teams/);
  } finally {
    globalThis.fetch = oldFetch;
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS; else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
    if (oldGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION; else process.env.CRE_REFRESH_GENERATION = oldGeneration;
  }
});

test("unconfirmed team response cleanup remains terminal", async () => {
  const body = new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(new Uint8Array(1_000_001)); },
    cancel() { return Promise.reject(new Error("cleanup not confirmed")); },
  });
  await assert.rejects(captureKidderTeamPage(async () => new Response(body, {
    headers: { "content-type": "text/html" },
  })), error => error instanceof NativeEvidenceTerminalError);
});
