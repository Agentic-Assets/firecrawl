import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, chmodSync, readFileSync, writeFileSync, statSync, realpathSync, openSync, closeSync, utimesSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { auditBuildoutPage, serializePrivateAudit, readStableInput } from "../../buildout-page-audit.js";
import { buildoutInventoryUrl, buildoutQueryFingerprint } from "../../sources/buildout.js";
import { registeredBuildoutFirmFor } from "../../sources/buildout-registry.js";

const firm = registeredBuildoutFirmFor("faris-lee")!;
const when = "2026-10-28T12:00:00.000Z";
const start = "2026-10-28T11:00:00.000Z";
const end = "2026-10-28T13:00:00.000Z";
const generation = "fixture-generation";
const digest = (v: unknown) => createHash("sha256").update(JSON.stringify(v)).digest("hex");
function fixture(rows: unknown[] = [{ id: 1, show_link: "https://farislee.com/listing-one", sale: true, closed: false, under_contract: false, also_for_sale_or_lease: false }]) {
  const data = { meta: { total: rows.length, limit: 100 }, inventory: rows };
  return { pluginKey: firm.pluginKey, page: 0, queryFingerprint: buildoutQueryFingerprint(firm.inventoryOpts), cachedAt: when, generationId: generation, data, observation: { observedAt: when, generationId: generation, cacheDisposition: "live", endpoint: buildoutInventoryUrl(firm.pluginKey, 0, firm.inventoryOpts.inventorySort), transport: "direct_json", parsedJsonSha256: digest(data) } };
}
const run = (data: unknown) => auditBuildoutPage(Buffer.from(JSON.stringify(data)), "faris-lee", generation, start, end, when);

test("valid page is internally consistent, never coverage or publisher authority", () => {
  const original = fixture() as any;
  original.data.extra_page_field = { arbitrary: ["preserved"] };
  original.observation.extra_context = "preserved";
  original.observation.parsedJsonSha256 = digest(original.data);
  const out = run(original);
  assert.equal(out.status, "parsed_json_consistent_fidelity_unverified");
  assert.equal(out.coverage, "unknown");
  assert.equal(out.authority, "none");
  assert.equal(out.publisher_authenticated, false);
  assert.deepEqual(out.rows[0].row, original.data.inventory[0]);
  assert.deepEqual(out.unreviewedSourceWrapper, original);
  assert.equal(out.input_byte_authority, "originalCacheBytesBase64");
  assert.equal(Buffer.from(out.originalCacheBytesBase64, "base64").toString(), JSON.stringify(original));
  assert.equal(out.private_unreviewed, true);
});

test("missing and contradictory row facts remain fully retained with unresolved reasons", () => {
  const rows = [ { id: null, name: "retain", sale: null }, { id: 2, show_link: "https://farislee.com/p", sale: true, closed: false, under_contract: false, also_for_sale_or_lease: false }, { id: 2, show_link: "https://farislee.com/p", sale: false, closed: false, under_contract: false, also_for_sale_or_lease: false }, null ];
  const result = run(fixture(rows));
  assert.deepEqual(result.rows.map(x => x.row), rows);
  assert.equal(result.observedRows, 4);
  assert.ok(result.rows[0].reasons.includes("native_id_missing_or_invalid"));
  assert.ok(result.rows[0].reasons.includes("lifecycle_or_tenure_flags_missing_or_invalid"));
  assert.ok(result.rows[1].reasons.includes("duplicate_native_id"));
  assert.ok(result.rows[1].reasons.includes("duplicate_listing_url"));
  assert.equal(result.rows[3].disposition, "unresolved");
});

test("null, mismatched and stale generation retain all rows without authority", () => {
  for (const changed of [null, "different"]) {
    const f = fixture(); f.generationId = changed as any; f.observation.generationId = changed as any;
    const out = run(f);
    assert.equal(out.status, "provenance_incomplete");
    assert.ok(out.provenance_reasons.includes("generation_missing_or_mismatch"));
    assert.equal(out.rows.length, 1);
    assert.equal(out.authority, "none");
  }
  const f = fixture(); f.cachedAt = f.observation.observedAt = "2026-10-26T12:00:00.000Z";
  assert.ok(run(f).provenance_reasons.includes("observation_outside_window"));
});

test("tampered hash, query, source, endpoint and invalid page are explicit incomplete provenance without data loss", () => {
  const f = fixture(); f.pluginKey = "other"; f.queryFingerprint = "other"; f.page = 9; f.observation.endpoint = "https://example.test/"; f.observation.parsedJsonSha256 = "bad";
  const out = run(f);
  for (const reason of ["plugin_mismatch", "query_mismatch", "endpoint_mismatch", "parsed_data_hash_mismatch", "page_shape_invalid"]) assert.ok(out.provenance_reasons.includes(reason));
  assert.equal(out.rows.length, 1);
});

test("unsafe integer collision cannot claim complete fidelity and original bytes remain authority", () => {
  const f = fixture() as any;
  f.data.inventory[0].largeValue = 9007199254740992;
  f.observation.parsedJsonSha256 = digest(f.data);
  const a = Buffer.from(JSON.stringify(f));
  const b = Buffer.from(a.toString().replace("9007199254740992", "9007199254740993"));
  const first = auditBuildoutPage(a, "faris-lee", generation, start, end, when);
  const second = auditBuildoutPage(b, "faris-lee", generation, start, end, when);
  assert.notEqual(first.cacheFileSha256, second.cacheFileSha256);
  assert.equal(first.observation.parsedJsonSha256, second.observation.parsedJsonSha256);
  for (const result of [first, second]) {
    assert.ok(result.provenance_reasons.includes("unsafe_numeric_values"));
    assert.equal(result.status, "provenance_incomplete");
    assert.equal(result.input_byte_authority, "originalCacheBytesBase64");
  }
  assert.deepEqual(Buffer.from(second.originalCacheBytesBase64, "base64"), b);
});

test("duplicate keys retain original bytes and never claim lossless parsing", () => {
  const f = fixture();
  const raw = Buffer.from(JSON.stringify(f).replace('"id":1', '"id":999,"id":1'));
  const out = auditBuildoutPage(raw, "faris-lee", generation, start, end, when);
  assert.deepEqual(Buffer.from(out.originalCacheBytesBase64, "base64"), raw);
  assert.equal(out.status, "parsed_json_consistent_fidelity_unverified");
  assert.ok(!out.provenance_reasons.includes("parsed_data_hash_mismatch"));
  assert.match(out.data_fidelity.parsed_representation, /duplicate keys.*not verified/);
});

test("invalid calendar times and malformed JSON are rejected without echoing input", () => {
  const invalid = fixture();
  invalid.cachedAt = invalid.observation.observedAt = "2026-02-30T12:00:00Z";
  assert.ok(run(invalid).provenance_reasons.includes("observation_outside_window"));
  assert.throws(() => auditBuildoutPage(Buffer.from('{"secret-fake-marker"'), "faris-lee", generation, start, end, when), (e: any) => !String(e).includes("secret-fake-marker"));
  assert.throws(() => auditBuildoutPage(Buffer.from(JSON.stringify(fixture())), "faris-lee", generation, "2026-02-30T12:00:00Z", end, when), /valid generation/);
});

test("oversized serialized evidence is refused", () => {
  assert.throws(() => serializePrivateAudit({ value: "x".repeat(100) }, 20), /output exceeds/);
});

test("overdeep parsed data is refused with an explicit bounded error", () => {
  const f = fixture() as any;
  let value: unknown = "leaf";
  for (let i = 0; i < 300; i++) value = [value];
  f.data.nested = value;
  f.observation.parsedJsonSha256 = digest(f.data);
  assert.throws(() => run(f), /cache structure exceeds bounded evidence limits/);
});

test("same-size in-place rewrite during descriptor read refuses before audit output", () => {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "buildout-page-race-")));
  const input = join(dir, "cache.json"), output = join(dir, "audit.json");
  writeFileSync(input, "A".repeat(128 * 1024));
  const fd = openSync(input, "r");
  try {
    assert.throws(() => {
      const bytes = readStableInput(fd, () => {
        writeFileSync(input, "B".repeat(128 * 1024));
        utimesSync(input, new Date("2020-01-01T00:00:00Z"), new Date("2020-01-01T00:00:00Z"));
      });
      writeFileSync(output, bytes);
    }, /input changed during read/);
    assert.throws(() => statSync(output), { code: "ENOENT" });
  } finally { closeSync(fd); }
});

test("CLI exclusively creates private output and refuses overwrite", () => {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "buildout-page-audit-"))); chmodSync(dir, 0o700);
  const input = join(dir, "input.json"), output = join(dir, "result.json");
  writeFileSync(input, JSON.stringify(fixture()));
  const invoke = () => spawnSync(process.execPath, ["--import", "tsx", resolve("buildout-page-audit.ts")], { encoding: "utf8", env: { ...process.env, BUILDOUT_AUDIT_SOURCE: "faris-lee", BUILDOUT_AUDIT_INPUT: input, BUILDOUT_AUDIT_OUTPUT: output, BUILDOUT_AUDIT_GENERATION: generation, BUILDOUT_AUDIT_RUN_START: start, BUILDOUT_AUDIT_RUN_END: end, BUILDOUT_AUDIT_NOW: when } });
  const first = invoke(); assert.equal(first.status, 0, first.stderr);
  assert.equal(statSync(output).mode & 0o777, 0o600);
  const before = readFileSync(output, "utf8");
  const second = invoke(); assert.notEqual(second.status, 0);
  assert.equal(readFileSync(output, "utf8"), before);
});
