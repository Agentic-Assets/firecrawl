import assert from "node:assert/strict";
import test from "node:test";

process.argv = [process.argv[0]!, process.argv[1]!];
const { lyonStahlFetchText } = await import("../../../sources/lyon-stahl.js");

for (const status of [503, 302]) {
  for (const mode of ["reject", "throw"] as const) {
    test(`Lyon does not dispatch a successor after HTTP ${status} body cancellation ${mode}s`, { timeout: 3000 }, async () => {
      let requests = 0, cancellations = 0;
      const response = new Response(new ReadableStream<Uint8Array>({
        start(controller) { controller.enqueue(new TextEncoder().encode("discarded fixture")); },
        cancel() {
          cancellations++;
          const error = new Error("synthetic cancellation failure");
          if (mode === "throw") throw error;
          return Promise.reject(error);
        },
      }), {
        status,
        headers: status === 302 ? { location: "/properties/successor/" } : undefined,
      });
      let rejected = false;
      let error: unknown;
      try {
        await lyonStahlFetchText(
          "https://lyonstahl.com/properties/cancel-refusal-fixture/",
          "html",
          async () => {
            requests++;
            return requests === 1 ? response : new Response("<html>safe successor fixture</html>", { headers: { "Content-Type": "text/html" } });
          },
          1000, 1024, 2
        );
      } catch (caught) {
        rejected = true;
        error = caught;
      }
      console.log(JSON.stringify({ status, mode, requests, cancellations, rejected }));
      assert.equal(requests, 1, "failed cancellation must not authorize a retry or redirect request");
      assert.equal(cancellations, 1);
      assert.equal(rejected, true);
      assert.match(String(error), /503|redirect|cancel|settle/i);
      assert.equal(response.body?.locked, false);
    });
  }
}
