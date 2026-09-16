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

/**
 * Golden-vector parity for the GraphQL `errors` "no errors" contract, shared
 * with the Python host (`_graphql_errors_absent`, `select_jll_admission_members`,
 * `_C10HostTransport._verify_evidence`) and the playwright sidecar
 * (`hasNoC10GraphqlErrors`). An absent or empty `errors` array means no
 * errors; any other value (non-empty array, null, object, string, number,
 * boolean) is a failure. Vectors: tests/fixtures/c10_graphql_errors_vectors.json.
 */
interface GraphqlErrorsVector {
  name: string;
  errors: { present: boolean; value: unknown };
  accepted: boolean;
}

const graphqlErrorsFixtureUrl = new URL(
  "../../fixtures/c10_graphql_errors_vectors.json",
  import.meta.url,
);
const graphqlErrorsVectors: GraphqlErrorsVector[] = JSON.parse(
  readFileSync(graphqlErrorsFixtureUrl, "utf-8"),
);

// Sixteen canonical JLL enumeration candidates, matching the Python test's
// base envelope so both suites exercise the identical shape.
const canonicalGraphqlErrorsItems = Array.from({ length: 16 }, (_, index) => ({
  id: `900${index + 1}`,
  pageUrl: `/listings/member-${index + 1}`,
}));

function graphqlErrorsEnvelope(vector: GraphqlErrorsVector): unknown {
  const payload: Record<string, unknown> = {
    data: {
      properties: {
        count: canonicalGraphqlErrorsItems.length,
        items: canonicalGraphqlErrorsItems,
      },
    },
  };
  if (vector.errors.present) {
    payload.errors = vector.errors.value;
  }
  return payload;
}

test("graphql errors fixture is non-empty and covers both dispositions", () => {
  assert.ok(graphqlErrorsVectors.length > 0);
  assert.ok(graphqlErrorsVectors.some((vector) => vector.accepted));
  assert.ok(graphqlErrorsVectors.some((vector) => !vector.accepted));
});

for (const vector of graphqlErrorsVectors) {
  test(`selectJllAdmissionMembers graphql errors parity: ${vector.name}`, () => {
    const payload = graphqlErrorsEnvelope(vector);
    if (vector.accepted) {
      const result = selectJllAdmissionMembers(payload);
      assert.equal(result.candidateCount, 16);
      assert.equal(result.selectedMembers.length, 16);
    } else {
      assert.throws(
        () => selectJllAdmissionMembers(payload),
        (err: unknown) => {
          assert.ok(err instanceof C10ReceiptError, `expected C10ReceiptError, got ${String(err)}`);
          assert.ok(
            (err as Error).message.includes("envelope is invalid"),
            `expected envelope-invalid message, got ${JSON.stringify((err as Error).message)}`,
          );
          return true;
        },
      );
    }
  });
}
