import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {rentEvidence} from "../../../lib/rent-evidence.js";

// Shared with tests/test_rent_evidence.py so the Python mirror cannot drift.
const PARITY_VECTORS: Array<{input: string; label: string | null; note: string; expected: unknown}> = JSON.parse(
  readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), "../../fixtures/rent_evidence_parity_vectors.json"), "utf8"),
);

test("rent amount is not lease term, supports leading decimals and flags basis conflict", () => {
  assert.equal(rentEvidence("USD 1.125/SF/year").annual_psf_min, 1.13);
  assert.equal(rentEvidence("USD .75/SF/month NNN").annual_psf_min, 9);
  assert.equal(rentEvidence("5-year lease at USD 12/SF/year NNN").annual_psf_min, 12);
  assert.equal(rentEvidence("5-year lease at USD .75/SF/month NNN").annual_psf_min, 9);
  const result = rentEvidence("USD 12/SF/year NNN or gross");
  assert.equal(result.lease_basis, null);
  assert.ok(result.anomalies.includes("lease_basis_conflict"));
});

test("evidence retains high bounds and original source labels", () => {
  const result = rentEvidence("2.50 - 250", "USD/SF/month NNN");
  assert.equal(result.annual_psf_min, 30);
  assert.equal(result.annual_psf_max, 3000);
  assert.deepEqual(result.anomalies, ["unusually_high_annual_psf", "wide_range"]);
  assert.equal(result.source_field_label, "USD/SF/month NNN");
});

test("source units, period conflicts and negative amounts remain unresolved", () => {
  for (const text of ["$20/month", "USD -20/SF/year", "USD 20/SF/month/year", "USD 20/year building 1000 SF"])
    assert.equal(rentEvidence(text).annual_psf_min, null);
  assert.equal(rentEvidence("USD 20/SF/year", "per sqm").denominator, "conflict");
});

for (const vector of PARITY_VECTORS) {
  test(`shared rent evidence parity vector: ${vector.note}`, () => {
    assert.deepEqual(rentEvidence(vector.input, vector.label), vector.expected);
  });
}
