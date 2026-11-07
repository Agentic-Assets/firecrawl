import assert from "node:assert/strict";
import { test } from "node:test";
import { spawnSync } from "node:child_process";
import { brokers, resetBrokerStateForTests } from "../../../lib/broker.js";
import { mapCbrePartialProjection } from "../../../sources/cbre-partial-map.js";

function candidate(id: string, document: unknown = { "Common.PrimaryKey": id }) {
  return { source_id: id, observations: [{ document, observed_at: "2026-01-01T00:00:00Z",
    request_transaction: "sale" as const }] };
}
function projection(candidates: ReturnType<typeof candidate>[]) {
  return { source_key: "cbre" as const, generation: "run-1", attempt: 1, sidecar_sha256: "a".repeat(64),
    whole_source_coverage: false as const, write_authorized: false as const, candidates, held: [] };
}

test("maps a healthy peer while retaining a malformed document and unknown tenure", () => {
  resetBrokerStateForTests();
  const good = candidate("good", { "Common.PrimaryKey": "good", "Common.Aspects": null,
    "Common.Agents": [{ "Common.AgentName": "Agent" }] });
  const bad = candidate("bad", null);
  const result = mapCbrePartialProjection(projection([good, bad]));
  assert.equal(result.mapped.length, 1);
  assert.equal(result.mapped[0].source_id, "good");
  assert.equal(result.mapped[0].row.transactionType, null);
  assert.equal(result.mapped[0].row.url, null);
  assert.equal(result.mapped[0].row.canonicalUrl, null);
  assert.equal(result.mapped[0].row.freshnessProvenance, undefined);
  assert.deepEqual(result.mapped[0].observations, good.observations);
  assert.equal(result.held.length, 1);
  assert.deepEqual((result.held[0] as any).candidate, bad);
  assert.equal(result.write_authorized, false);
  assert.equal(result.whole_source_coverage, false);
  assert.deepEqual(brokers, []);
});

test("holds duplicate, mismatched and unsafe IDs without losing the source observations", () => {
  const duplicate = candidate("duplicate");
  const mismatch = candidate("expected", { "Common.PrimaryKey": "other" });
  const unsafe = candidate("bad/path");
  const result = mapCbrePartialProjection(projection([candidate("good"), duplicate, duplicate, mismatch, unsafe]));
  assert.deepEqual(result.mapped.map((item) => item.source_id), ["good"]);
  assert.equal(result.held.length, 4);
  assert.deepEqual((result.held[2] as any).candidate, mismatch);
  assert.deepEqual((result.held[3] as any).candidate, unsafe);
});

test("refuses any claim of write or whole-source authority", () => {
  assert.throws(() => mapCbrePartialProjection({ ...projection([]), write_authorized: true } as any));
  assert.throws(() => mapCbrePartialProjection({ ...projection([]), whole_source_coverage: true } as any));
});


test("maps a real offline Python sidecar partition and keeps invalid peers held", () => {
  const script = `import hashlib,json
from cbre_partial_projection import project
header={"schema":"cbre-partial-pages/v1","sourceKey":"cbre","generation":"run-1","attempt":1}
page={"kind":"cbre_rejected_page","transaction":"sale","pass":1,"page":1,"pageSize":2,"observedAt":"2026-01-01T00:00:00Z","qualification":"unverified_page","rejection":"document_validation","rejectedDocumentsIndexBasis":"documents_flat_1","rejectedDocuments":[1],"documents":[{"DocumentCount":2,"Documents":[{"Common.PrimaryKey":"good","Common.Aspects":None},None]}]}
raw=(json.dumps(header)+"\\n"+json.dumps(page)+"\\n").encode()
custody={"state":"sealed_local","coverage":"unverified","outcome":"source_error","bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest(),"observationRecords":1}
print(json.dumps(project(raw,generation="run-1",attempt=1,custody=custody,expected_page_size=2)))`;
  const child = spawnSync("python3", ["-c", script], { encoding: "utf8", timeout: 10000 });
  assert.equal(child.status, 0, child.stderr);
  const result = mapCbrePartialProjection(JSON.parse(child.stdout));
  assert.equal(result.mapped[0].source_id, "good");
  assert.equal(result.mapped[0].row.transactionType, null);
  assert.equal((result.held[0] as any).reason, "missing_identity");
  assert.equal(result.write_authorized, false);
});


test("held and mismatched native identities taint otherwise valid peers", () => {
  const p = projection([candidate("good"), candidate("expected", { "Common.PrimaryKey": "good" }), candidate("other")]);
  p.held = [{ reason: "missing_identity", document: { "Common.PrimaryKey": " other " } }] as any;
  const result = mapCbrePartialProjection(p);
  assert.equal(result.mapped.length, 0);
  assert.equal(result.held.length, 4);
});

test("later observation metadata must also be valid", () => {
  const item = candidate("x");
  item.observations.push({ ...item.observations[0], observed_at: "" });
  const result = mapCbrePartialProjection(projection([item, candidate("good")]));
  assert.deepEqual(result.mapped.map((row) => row.source_id), ["good"]);
  assert.equal(result.held.length, 1);
});
