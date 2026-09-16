/**
 * Golden-vector parity for the JLL admission selection rule
 * `jll-canonical-url-lexicographic-v1`, implemented twice and required to be
 * byte-identical: TypeScript `selectJllAdmissionMembers`
 * (capacity_c10/receipts/strict_detail/jll_admission.ts) and Python
 * `jll_admission.select_jll_admission_members`. Both sides read the same
 * fixture, tests/fixtures/c10_jll_selection_vectors.json.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { C10ReceiptError } from "../../../capacity_c10/receipts/index.js";
import { selectJllAdmissionMembers } from "../../../capacity_c10/receipts/strict_detail/jll_admission.js";

const CATEGORY_PHRASES: Record<string, string> = {
  envelope: "envelope is invalid",
  noncanonical: "noncanonical candidate",
  duplicate: "duplicate candidates",
  insufficient: "insufficient canonical candidates",
};

interface ExpectedMember {
  key: string;
  provider_id: string;
  canonical_url: string;
}

interface Vector {
  name: string;
  payload: unknown;
  expect:
    | { error: keyof typeof CATEGORY_PHRASES }
    | { members: ExpectedMember[]; candidate_count: number; digest: string; rule: string };
}

const fixtureUrl = new URL("../../fixtures/c10_jll_selection_vectors.json", import.meta.url);
const vectors: Vector[] = JSON.parse(readFileSync(fixtureUrl, "utf-8"));

test("fixture is non-empty and covers every category", () => {
  assert.ok(vectors.length > 0);
  const categories = new Set(
    vectors.map((vector) => ("error" in vector.expect ? vector.expect.error : "success")),
  );
  assert.deepEqual(
    [...categories].sort(),
    ["duplicate", "envelope", "insufficient", "noncanonical", "success"],
  );
});

test("fixture vector names are unique", () => {
  const names = vectors.map((vector) => vector.name);
  assert.equal(new Set(names).size, names.length);
});

for (const vector of vectors) {
  test(`selectJllAdmissionMembers: ${vector.name}`, () => {
    if ("error" in vector.expect) {
      const phrase = CATEGORY_PHRASES[vector.expect.error];
      assert.ok(phrase, `unknown category ${vector.expect.error}`);
      assert.throws(
        () => selectJllAdmissionMembers(vector.payload),
        (err: unknown) => {
          assert.ok(err instanceof C10ReceiptError, `expected C10ReceiptError, got ${String(err)}`);
          assert.ok(
            (err as Error).message.includes(phrase),
            `expected message to include ${JSON.stringify(phrase)}, got ${JSON.stringify((err as Error).message)}`,
          );
          return true;
        },
      );
      return;
    }

    const result = selectJllAdmissionMembers(vector.payload);
    const expected = vector.expect;
    assert.equal(result.rule, expected.rule);
    assert.equal(result.candidateCount, expected.candidate_count);
    assert.equal(result.digest, expected.digest);
    assert.deepEqual(
      result.selectedMembers.map((member) => ({
        key: member.key,
        provider_id: member.providerId,
        canonical_url: member.canonicalUrl,
      })),
      expected.members,
    );
  });
}
