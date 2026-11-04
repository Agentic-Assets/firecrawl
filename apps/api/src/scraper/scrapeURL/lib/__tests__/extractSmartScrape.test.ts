import { NoObjectGeneratedError } from "ai";
import { CostLimitExceededError } from "../../../../lib/cost-tracking";
import { vi } from "vitest";

const { structuredOutputConfig, generateCompletionsMock, getModelMock } =
  vi.hoisted(() => ({
    structuredOutputConfig: {} as {
      MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK?: string;
    },
    generateCompletionsMock: vi.fn(),
    getModelMock: vi.fn((modelName: string) => ({ modelId: modelName })),
  }));

vi.mock("../../transformers/llmExtract", async importOriginal => ({
  ...(await importOriginal<typeof import("../../transformers/llmExtract")>()),
  generateCompletions: generateCompletionsMock,
}));

vi.mock("../../../../lib/generic-ai", () => ({
  getModel: getModelMock,
}));

vi.mock("../../../../config", () => ({ config: structuredOutputConfig }));

import { extractData } from "../extractSmartScrape";
import { resolveStructuredResult } from "../structuredOutputFallback";

const schema = {
  type: "object",
  properties: {
    title: { type: "string" },
    domain: { type: "string" },
  },
  required: ["title", "domain"],
  additionalProperties: false,
};

const directResult = { title: "Example Domain", domain: "example.com" };

function completion(extract: unknown, warning?: string) {
  return {
    extract,
    warning,
    totalUsage: { promptTokens: 1, completionTokens: 1, totalTokens: 2 },
  };
}

function invalidStructuredOutputError() {
  return new NoObjectGeneratedError({
    response: {} as any,
    usage: {} as any,
    finishReason: "stop" as any,
    text: "{invalid JSON",
  });
}

function extractOptions(optionsSchema: any = schema) {
  const logger = {
    child: vi.fn(function () {
      return this;
    }),
    debug: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
  };

  return {
    logger,
    options: { schema: optionsSchema },
    markdown: "# Example Domain",
    model: { modelId: "deepseek/deepseek-v4-flash-0731" },
    retryModel: { modelId: "deepseek/deepseek-v4-flash-0731" },
    costTrackingOptions: { costTracking: {}, metadata: {} },
    metadata: { teamId: "test-team", scrapeId: "test-scrape" },
  } as any;
}

async function runExtraction(optionsSchema: any = schema, useAgent = false) {
  return extractData({
    extractOptions: extractOptions(optionsSchema),
    urls: ["https://example.com"],
    useAgent,
    scrapeId: "test-scrape",
    metadata: { teamId: "test-team", functionId: "test" },
  });
}

describe("resolveStructuredResult", () => {
  it("preserves a SmartScrape envelope when a provider omits the optional agent hint", () => {
    expect(
      resolveStructuredResult({ extractedData: directResult }, schema),
    ).toEqual({ extractedData: directResult, wasDirectSchemaResult: false });
  });

  it("prefers a valid direct result when the user schema has a root extractedData field", () => {
    const rootExtractedDataSchema = {
      type: "object",
      properties: {
        extractedData: {
          type: "object",
          properties: { title: { type: "string" } },
          required: ["title"],
          additionalProperties: false,
        },
      },
      required: ["extractedData"],
      additionalProperties: false,
    };
    const directRootResult = { extractedData: { title: "Example Domain" } };

    expect(
      resolveStructuredResult(directRootResult, rootExtractedDataSchema),
    ).toEqual({
      extractedData: directRootResult,
      wasDirectSchemaResult: true,
    });
  });

  it("normalizes only a direct result that satisfies the user schema", () => {
    expect(resolveStructuredResult(directResult, schema)).toEqual({
      extractedData: directResult,
      wasDirectSchemaResult: true,
    });
    expect(
      resolveStructuredResult({ title: "Example Domain" }, schema),
    ).toBeUndefined();
  });

  it("removes provider-added fields that the user schema forbids", () => {
    const titleSchema = {
      type: "object",
      properties: { title: { type: "string" } },
      required: ["title"],
      additionalProperties: false,
    };

    expect(
      resolveStructuredResult(
        { title: "Example Domain", description: "Unrequested" },
        titleSchema,
      ),
    ).toEqual({
      extractedData: { title: "Example Domain" },
      wasDirectSchemaResult: true,
    });
  });
});

describe("extractData structured-output compatibility", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK = undefined;
  });

  it("accepts a schema-valid direct provider result without a second completion", async () => {
    generateCompletionsMock.mockResolvedValueOnce(completion(directResult));

    const result = await runExtraction();

    expect(result.extractedDataArray).toEqual([directResult]);
    expect(result.warning).toBeUndefined();
    expect(generateCompletionsMock).toHaveBeenCalledTimes(1);
    expect(getModelMock).not.toHaveBeenCalledWith(expect.anything(), "openai", {
      ignoreModelOverride: true,
    });
  });

  it("retries once with the configured explicit fallback after an invalid result", async () => {
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      "deepseek/deepseek-v4-pro-0813";
    generateCompletionsMock
      .mockResolvedValueOnce(completion({ title: "Example Domain" }))
      .mockResolvedValueOnce(
        completion({
          extractedData: directResult,
          shouldUseSmartscrape: false,
        }),
      );

    const result = await runExtraction();

    expect(result.extractedDataArray).toEqual([directResult]);
    expect(result.warning).toBeUndefined();
    expect(generateCompletionsMock).toHaveBeenCalledTimes(2);
    expect(getModelMock).toHaveBeenCalledWith(
      "deepseek/deepseek-v4-pro-0813",
      "openai",
      { ignoreModelOverride: true },
    );
    expect(generateCompletionsMock.mock.calls[1][0]).toMatchObject({
      model: { modelId: "deepseek/deepseek-v4-pro-0813" },
      boundedStructuredOutput: true,
      options: { schema },
    });
  });

  it("retries once when the primary reports AI SDK schema-invalid output", async () => {
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      "deepseek/deepseek-v4-pro-0813";
    generateCompletionsMock
      .mockRejectedValueOnce(invalidStructuredOutputError())
      .mockResolvedValueOnce(completion(directResult));

    const result = await runExtraction();

    expect(result.extractedDataArray).toEqual([directResult]);
    expect(result.warning).toBeUndefined();
    expect(generateCompletionsMock).toHaveBeenCalledTimes(2);
    expect(getModelMock).toHaveBeenCalledWith(
      "deepseek/deepseek-v4-pro-0813",
      "openai",
      { ignoreModelOverride: true },
    );
  });

  it("uses the provider-normalized schema for a valid direct result", async () => {
    const constrainedSchema = {
      type: "object",
      properties: {
        label: { type: "string", pattern: "^[A-Z]+$" },
      },
      required: ["label"],
      additionalProperties: false,
    };
    generateCompletionsMock.mockResolvedValueOnce(
      completion({ label: "lowercase" }),
    );

    const result = await runExtraction(constrainedSchema);

    expect(result.extractedDataArray).toEqual([{ label: "lowercase" }]);
    expect(generateCompletionsMock).toHaveBeenCalledTimes(1);
  });

  it("retries after a malformed SmartScrape envelope", async () => {
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      "deepseek/deepseek-v4-pro-0813";
    generateCompletionsMock
      .mockResolvedValueOnce(
        completion({
          extractedData: { title: 7, domain: "example.com" },
          shouldUseSmartscrape: "false",
        }),
      )
      .mockResolvedValueOnce(completion(directResult));

    const result = await runExtraction();

    expect(result.extractedDataArray).toEqual([directResult]);
    expect(generateCompletionsMock).toHaveBeenCalledTimes(2);
  });

  it("keeps the SmartScrape wrapper for an agent-enabled fallback", async () => {
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      "deepseek/deepseek-v4-pro-0813";
    generateCompletionsMock
      .mockResolvedValueOnce(completion({ title: "Example Domain" }))
      .mockResolvedValueOnce(
        completion({
          extractedData: directResult,
          shouldUseSmartscrape: false,
        }),
      );

    const result = await runExtraction(schema, true);

    expect(result.extractedDataArray).toEqual([directResult]);
    expect(
      generateCompletionsMock.mock.calls[1][0].options.schema,
    ).toMatchObject({
      properties: { extractedData: schema },
    });
  });

  it("caps a compatibility transaction at a primary plus one fallback attempt", async () => {
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      "deepseek/deepseek-v4-pro-0813";
    generateCompletionsMock
      .mockResolvedValueOnce(completion({ title: "Example Domain" }))
      .mockResolvedValueOnce(completion({ title: "Still incomplete" }));

    const result = await runExtraction();

    expect(result.extractedDataArray).toEqual([undefined]);
    expect(generateCompletionsMock).toHaveBeenCalledTimes(2);
    expect(
      generateCompletionsMock.mock.calls.every(
        ([options]) => options.boundedStructuredOutput === true,
      ),
    ).toBe(true);
  });

  it("does not retry a failed provider request with the structured-output fallback", async () => {
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      "deepseek/deepseek-v4-pro-0813";
    generateCompletionsMock.mockRejectedValueOnce(new Error("rate limit"));

    const result = await runExtraction();

    expect(result.extractedDataArray).toEqual([undefined]);
    expect(generateCompletionsMock).toHaveBeenCalledTimes(1);
    expect(getModelMock).not.toHaveBeenCalledWith(expect.anything(), "openai", {
      ignoreModelOverride: true,
    });
    expect(generateCompletionsMock.mock.calls[0][0]).toMatchObject({
      boundedStructuredOutput: true,
    });
  });
});

// Pins the exact user-facing warnings and call bounds of the one-time
// structured-output fallback, so refactors of the fallback module are proven
// behavior-preserving.
describe("extractData structured-output fallback warnings", () => {
  const FALLBACK_MODEL = "deepseek/deepseek-v4-pro-0813";
  const invalid = { title: "Example Domain" };

  beforeEach(() => {
    vi.clearAllMocks();
    generateCompletionsMock.mockReset();
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      FALLBACK_MODEL;
  });

  it("keeps the accepted primary's warning", async () => {
    generateCompletionsMock.mockResolvedValueOnce(
      completion(directResult, "primary warning"),
    );

    const result = await runExtraction();

    expect(result.extractedDataArray).toEqual([directResult]);
    expect(result.warning).toBe("primary warning");
    expect(generateCompletionsMock).toHaveBeenCalledTimes(1);
  });

  it("uses only the accepted fallback's warning", async () => {
    generateCompletionsMock
      .mockResolvedValueOnce(completion(invalid, "primary warning"))
      .mockResolvedValueOnce(completion(directResult, "fallback warning"));

    const result = await runExtraction();

    expect(result.extractedDataArray).toEqual([directResult]);
    expect(result.warning).toBe("fallback warning");
    expect(generateCompletionsMock).toHaveBeenCalledTimes(2);
  });

  it("joins both warnings when the fallback is also invalid", async () => {
    generateCompletionsMock
      .mockResolvedValueOnce(completion(invalid, "primary warning"))
      .mockResolvedValueOnce(completion(invalid, "fallback warning"));

    const result = await runExtraction();

    expect(result.extractedDataArray).toEqual([undefined]);
    expect(result.warning).toBe("primary warning fallback warning");
    expect(generateCompletionsMock).toHaveBeenCalledTimes(2);
  });

  it("keeps the primary warning when an invalid fallback has none", async () => {
    generateCompletionsMock
      .mockResolvedValueOnce(completion(invalid, "primary warning"))
      .mockResolvedValueOnce(completion(invalid));

    const result = await runExtraction();

    expect(result.extractedDataArray).toEqual([undefined]);
    expect(result.warning).toBe("primary warning");
  });

  it("returns no warning when neither invalid attempt has one", async () => {
    generateCompletionsMock
      .mockResolvedValueOnce(completion(invalid))
      .mockResolvedValueOnce(completion(invalid));

    const result = await runExtraction();

    expect(result.extractedDataArray).toEqual([undefined]);
    expect(result.warning).toBeUndefined();
  });

  it("appends a thrown fallback failure to the primary warning", async () => {
    generateCompletionsMock
      .mockResolvedValueOnce(completion(invalid, "primary warning"))
      .mockRejectedValueOnce(new Error("fallback unavailable"));

    const result = await runExtraction();

    expect(result.extractedDataArray).toEqual([undefined]);
    expect(result.warning).toBe(
      "primary warning JSON extraction fallback failed: fallback unavailable",
    );
    expect(generateCompletionsMock).toHaveBeenCalledTimes(2);
  });

  it("reports a thrown invalid primary with the fallback's warning", async () => {
    const primaryError = invalidStructuredOutputError();
    generateCompletionsMock
      .mockRejectedValueOnce(primaryError)
      .mockResolvedValueOnce(completion(invalid, "fallback warning"));

    const result = await runExtraction();

    expect(result.extractedDataArray).toEqual([undefined]);
    expect(result.warning).toBe(
      `JSON extraction failed: ${primaryError.message} fallback warning`,
    );
  });

  it("caps each failure reason at 300 characters", async () => {
    const longReason = "x".repeat(400);
    generateCompletionsMock
      .mockResolvedValueOnce(completion(invalid))
      .mockRejectedValueOnce(new Error(longReason));

    const result = await runExtraction();

    expect(result.warning).toBe(
      `JSON extraction fallback failed: ${"x".repeat(300)}`,
    );
  });

  it("reports a provider failure on the primary without a fallback call", async () => {
    generateCompletionsMock.mockRejectedValueOnce(new Error("rate limit"));

    const result = await runExtraction();

    expect(result.extractedDataArray).toEqual([undefined]);
    expect(result.warning).toBe("JSON extraction failed: rate limit");
    expect(generateCompletionsMock).toHaveBeenCalledTimes(1);
  });

  it("propagates a cost-limit failure on the primary without a fallback call", async () => {
    generateCompletionsMock.mockRejectedValueOnce(new CostLimitExceededError());

    await expect(runExtraction()).rejects.toBeInstanceOf(
      CostLimitExceededError,
    );
    expect(generateCompletionsMock).toHaveBeenCalledTimes(1);
  });

  it("propagates a cost-limit failure on the fallback", async () => {
    generateCompletionsMock
      .mockResolvedValueOnce(completion(invalid))
      .mockRejectedValueOnce(new CostLimitExceededError());

    await expect(runExtraction()).rejects.toBeInstanceOf(
      CostLimitExceededError,
    );
    expect(generateCompletionsMock).toHaveBeenCalledTimes(2);
  });

  it("rejects an invalid SmartScrape envelope after one fallback", async () => {
    generateCompletionsMock
      .mockResolvedValueOnce(
        completion({ extractedData: invalid, shouldUseSmartscrape: false }),
      )
      .mockResolvedValueOnce(
        completion({ extractedData: invalid, shouldUseSmartscrape: false }),
      );

    const result = await runExtraction(schema, true);

    expect(result.extractedDataArray).toEqual([undefined]);
    expect(generateCompletionsMock).toHaveBeenCalledTimes(2);
  });

  describe("without a configured fallback", () => {
    beforeEach(() => {
      structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK = undefined;
    });

    it("keeps the primary warning of a schema-invalid result after one call", async () => {
      generateCompletionsMock.mockResolvedValueOnce(
        completion(invalid, "primary warning"),
      );

      const result = await runExtraction();

      expect(result.extractedDataArray).toEqual([undefined]);
      expect(result.warning).toBe("primary warning");
      expect(generateCompletionsMock).toHaveBeenCalledTimes(1);
    });

    it("reports a thrown invalid primary after one call", async () => {
      const primaryError = invalidStructuredOutputError();
      generateCompletionsMock.mockRejectedValueOnce(primaryError);

      const result = await runExtraction();

      expect(result.extractedDataArray).toEqual([undefined]);
      expect(result.warning).toBe(
        `JSON extraction failed: ${primaryError.message}`,
      );
      expect(generateCompletionsMock).toHaveBeenCalledTimes(1);
    });

    it("passes a schema-less result through unvalidated", async () => {
      generateCompletionsMock.mockResolvedValueOnce(
        completion({ anything: 1 }, "primary warning"),
      );

      const result = await runExtraction(null);

      expect(result.extractedDataArray).toEqual([{ anything: 1 }]);
      expect(result.warning).toBe("primary warning");
      expect(generateCompletionsMock).toHaveBeenCalledTimes(1);
    });
  });
});
