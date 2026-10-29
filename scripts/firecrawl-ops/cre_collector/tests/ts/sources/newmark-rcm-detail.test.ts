import { strict as assert } from "node:assert";
import { test } from "node:test";
import { extractNewmarkRcmDetail, matchNewmarkRcmProperty } from "../../../sources/pure/newmark-rcm-detail.js";

const value = (s: string) => ({ state: "value" as const, value: s });
const witness = { sourceName: value("Example Property"), sourceProperty: { selection: "single" as const,
  fields: { address: value("10 Main Street"), city: value("Example"), stateAbbreviation: value("FL"), zip: value("12345") } } };
const literal = '{"name":"Example Property","addresses":{"street":"10 Main Street","city":"Example","state":"FL","zip":"12345"},"number":1.0000000000000001}';

test("RCM extraction retains original numeric text and compares property-local witnesses", () => {
  const data = extractNewmarkRcmDetail(`<script>var data = ${literal}</script>`);
  assert.equal(data.status, "observed"); assert.equal(data.literal, literal);
  assert.equal(data.numericFidelity, "literal_only");
  assert.equal(matchNewmarkRcmProperty(witness, data.parsed), "matched");
});

test("ambiguous, missing, contradictory and invalid data remain unresolved", () => {
  const script = `<script>var data = ${literal}</script>`;
  assert.equal(extractNewmarkRcmDetail(script + script).status, "ambiguous");
  assert.equal(extractNewmarkRcmDetail("<script>var data = {bad}</script>").status, "invalid_json");
  assert.equal(extractNewmarkRcmDetail('<script>var data = {"name":"one","name":"two"}</script>').status, "invalid_json");
  assert.equal(extractNewmarkRcmDetail('<script>var data = {"name":"one","\\u006eame":"two"}</script>').status, "invalid_json");
  assert.equal(extractNewmarkRcmDetail('<script>var data = {"x":1,}</script>').status, "invalid_json");
  assert.equal(extractNewmarkRcmDetail('<script>var data = {/*comment*/"x":1}</script>').status, "invalid_json");
  assert.equal(extractNewmarkRcmDetail(`<script>var data = {"${"k".repeat(8193)}":1}</script>`).status, "invalid_json");
  assert.equal(extractNewmarkRcmDetail(`<script>var data = {"x":${"[".repeat(130)}0${"]".repeat(130)}}</script>`).status, "invalid_json");
  assert.equal(extractNewmarkRcmDetail("x".repeat(2_000_001)).status, "input_limit");
  const data = extractNewmarkRcmDetail(script).parsed;
  assert.equal(matchNewmarkRcmProperty({ ...witness, sourceName: value("Wrong") }, data), "unresolved");
  assert.equal(matchNewmarkRcmProperty({ ...witness, sourceProperty: { selection: "unresolved", fields: {} } }, data), "unresolved");
  assert.equal(matchNewmarkRcmProperty({ ...witness, sourceProperty: { selection: "single", fields: {} } }, data), "unresolved");
  assert.equal(matchNewmarkRcmProperty(witness, {name:"Example Property",addresses:{street:"Wrong",city:"Example",state:"FL",zip:"12345"}}), "unresolved");
});
