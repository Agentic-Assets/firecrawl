import assert from "node:assert/strict";
import test from "node:test";

import { c10TerminalEvidence } from "./c10_browser_response";

test("C10 route withholds success until cleanup is confirmed", () => {
  const evidence = { protocolVersion: 3, bodyBase64: "e30=" };
  assert.equal(c10TerminalEvidence(evidence, false, false), null);
  assert.equal(c10TerminalEvidence(evidence, true, true), null);
  assert.equal(c10TerminalEvidence(evidence, false, true), evidence);
});
