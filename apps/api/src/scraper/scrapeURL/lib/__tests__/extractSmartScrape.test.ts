import { vi } from "vitest";

const { structuredOutputConfig, generateCompletionsMock, getModelByNameMock } =
  vi.hoisted(() => ({
    structuredOutputConfig: {} as {
      MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK?: string;
    },
    generateCompletionsMock: vi.fn(),
    getModelByNameMock: vi.fn((modelName: string) => ({ modelId: modelName })),
  }));

vi.mock("../../transformers/llmExtract", async importOriginal => ({
  ...(await importOriginal<typeof import("../../transformers/llmExtract")>()),
  generateCompletions: generateCompletionsMock,
}));

vi.mock("../../../../lib/generic-ai", () => ({
  getModel: vi.fn((modelName: string) => ({ modelId: modelName })),
  getModelByName: getModelByNameMock,
}));

vi.mock("../../../../config", () => ({ config: structuredOutputConfig }));

import { extractData, resolveStructuredResult } from "../extractSmartScrape";

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
    expect(getModelByNameMock).not.toHaveBeenCalled();
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
    expect(getModelByNameMock).toHaveBeenCalledWith(
      "deepseek/deepseek-v4-pro-0813",
      "openai",
    );
    expect(generateCompletionsMock.mock.calls[1][0]).toMatchObject({
      model: { modelId: "deepseek/deepseek-v4-pro-0813" },
      retryModel: undefined,
      disableInternalRateLimitRetry: true,
      disableInternalObjectRepair: true,
      options: { schema },
    });
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
        ([options]) =>
          options.disableInternalRateLimitRetry === true &&
          options.disableInternalObjectRepair === true,
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
    expect(getModelByNameMock).not.toHaveBeenCalled();
    expect(generateCompletionsMock.mock.calls[0][0]).toMatchObject({
      disableInternalRateLimitRetry: true,
      disableInternalObjectRepair: true,
    });
  });
});
