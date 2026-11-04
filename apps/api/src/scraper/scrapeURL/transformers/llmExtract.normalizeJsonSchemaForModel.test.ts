import { vi } from "vitest";

// Fork: the schema helper that lib/structuredOutputFallback.ts validates
// provider output against. Kept out of upstream's llmExtract.test.ts.
vi.mock("../lib/extractSmartScrape", () => ({
  extractData: vi.fn(),
}));

import { normalizeJsonSchemaForModel } from "./llmExtract";

describe("normalizeJsonSchemaForModel", () => {
  // Upstream (firecrawl/firecrawl, 2026-09) now normalizes the root array's
  // items like any other schema, so provider-unsupported constraints are
  // dropped; the fork helper must keep matching what generateCompletions sends.
  it("wraps a top-level array under items with the provider-normalized item schema", () => {
    const schema = {
      type: "array",
      minItems: 2,
      items: { type: "string", pattern: "^[A-Z]+$" },
    };

    const normalized = normalizeJsonSchemaForModel(schema);

    expect(normalized).toMatchObject({
      type: "object",
      properties: {
        items: {
          type: "array",
          items: { type: "string" },
        },
      },
      required: ["items"],
      additionalProperties: false,
    });
    expect(normalized.properties.items.items.pattern).toBeUndefined();
  });

  it("turns a bare property map into a strict object schema", () => {
    expect(
      normalizeJsonSchemaForModel({ title: { type: "string" } }),
    ).toMatchObject({
      type: "object",
      properties: { title: { type: "string" } },
      required: ["title"],
      additionalProperties: false,
    });
  });
});
