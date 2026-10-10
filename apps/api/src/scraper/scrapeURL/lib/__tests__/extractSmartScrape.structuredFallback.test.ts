import { NoObjectGeneratedError } from "ai";
import { vi } from "vitest";

// Fork: drives the real extractData -> generateCompletions path with only the
// AI SDK's generateObject mocked, so the errors generateCompletions itself
// raises (output limit, fenced-but-unparseable JSON) reach the one-time
// structured-output fallback exactly as in production.
const { generateObjectMock, structuredOutputConfig, getModelMock } = vi.hoisted(
  () => ({
    generateObjectMock: vi.fn(),
    structuredOutputConfig: {} as {
      MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK?: string;
    },
    getModelMock: vi.fn((modelName: string) => ({ modelId: modelName })),
  }),
);

vi.mock("ai", async importOriginal => ({
  ...(await importOriginal<typeof import("ai")>()),
  generateObject: generateObjectMock,
}));

vi.mock("../../../../lib/generic-ai", () => ({
  getModel: getModelMock,
}));

vi.mock("../../../../config", () => ({ config: structuredOutputConfig }));

import { extractData } from "../extractSmartScrape";
import { CostLimitExceededError } from "../../../../lib/cost-tracking";

const FALLBACK_MODEL = "deepseek/deepseek-v4-pro-0813";
const OUTPUT_LIMIT =
  "the extracted data exceeded the model's maximum output length, so nothing was returned. Try a schema or prompt that asks for fewer items.";

const schema = {
  type: "object",
  properties: { title: { type: "string" } },
  required: ["title"],
  additionalProperties: false,
};

function objectResult(object: unknown) {
  return {
    object,
    usage: { inputTokens: 1, outputTokens: 1, totalTokens: 2 },
  };
}

function noObjectError(finishReason: "length" | "stop", text: string) {
  return new NoObjectGeneratedError({
    response: {} as any,
    usage: {} as any,
    finishReason: finishReason as any,
    text,
  });
}

function runExtraction(addCall: () => void = vi.fn()) {
  const logger = {
    child: vi.fn(function () {
      return this;
    }),
    debug: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
  };
  return extractData({
    extractOptions: {
      logger,
      options: { schema },
      markdown: "# Example Domain",
      model: { modelId: "deepseek/deepseek-v4-flash-0731" },
      retryModel: { modelId: "deepseek/deepseek-v4-flash-0731" },
      costTrackingOptions: {
        costTracking: { addCall },
        metadata: {},
      },
      metadata: { teamId: "test-team", scrapeId: "test-scrape" },
    } as any,
    urls: ["https://example.com"],
    useAgent: false,
    scrapeId: "test-scrape",
    metadata: { teamId: "test-team", functionId: "test" },
  });
}

describe("extractData fallback for errors raised inside generateCompletions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    generateObjectMock.mockReset();
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      FALLBACK_MODEL;
  });

  it("retries a truncated (output-limit) primary once and returns the fallback's data", async () => {
    generateObjectMock
      .mockRejectedValueOnce(noObjectError("length", '{"title": "Exam'))
      .mockResolvedValueOnce(objectResult({ title: "Example Domain" }));

    const result = await runExtraction();

    expect(result.extractedDataArray).toEqual([{ title: "Example Domain" }]);
    expect(result.warning).toBeUndefined();
    expect(generateObjectMock).toHaveBeenCalledTimes(2);
    expect(generateObjectMock.mock.calls[1][0].model).toMatchObject({
      modelId: FALLBACK_MODEL,
    });
  });

  it("stops after one fallback with the output-limit warning when both truncate", async () => {
    generateObjectMock
      .mockRejectedValueOnce(noObjectError("length", '{"title": "Exam'))
      .mockRejectedValueOnce(noObjectError("length", '{"title": "Exam'));

    const result = await runExtraction();

    expect(generateObjectMock).toHaveBeenCalledTimes(2);
    expect(result.extractedDataArray).toEqual([undefined]);
    expect(result.warning).toContain("JSON extraction fallback failed");
    expect(result.warning).toContain(
      "exceeded the model's maximum output length",
    );
  });

  it("retries code-fenced but unparseable primary JSON once with the fallback", async () => {
    generateObjectMock
      .mockRejectedValueOnce(
        noObjectError("stop", '```json\n{"title": "Example\n```'),
      )
      .mockResolvedValueOnce(objectResult({ title: "Example Domain" }));

    const result = await runExtraction();

    expect(result.extractedDataArray).toEqual([{ title: "Example Domain" }]);
    expect(generateObjectMock).toHaveBeenCalledTimes(2);
    expect(generateObjectMock.mock.calls[1][0].model).toMatchObject({
      modelId: FALLBACK_MODEL,
    });
  });

  it("keeps the output-limit reason when the fallback returns a schema-invalid object", async () => {
    generateObjectMock
      .mockRejectedValueOnce(noObjectError("length", '{"title": "Exam'))
      .mockResolvedValueOnce(objectResult({ nope: 1 }));

    const result = await runExtraction();

    expect(generateObjectMock).toHaveBeenCalledTimes(2);
    expect(result.extractedDataArray).toEqual([undefined]);
    expect(result.warning).toContain("JSON extraction failed");
    expect(result.warning).toContain(
      "exceeded the model's maximum output length",
    );
  });

  it("reports both failures when code-fenced JSON is unparseable twice", async () => {
    const fenced = '```json\n{"title": "Example\n```';
    generateObjectMock
      .mockRejectedValueOnce(noObjectError("stop", fenced))
      .mockRejectedValueOnce(noObjectError("stop", fenced));

    const result = await runExtraction();

    expect(generateObjectMock).toHaveBeenCalledTimes(2);
    expect(result.extractedDataArray).toEqual([undefined]);
    expect(result.warning).toContain("JSON extraction fallback failed");
    expect(result.warning).toContain("No object generated");
  });

  describe("without a configured fallback", () => {
    beforeEach(() => {
      structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK = undefined;
    });

    it("keeps the upstream output-limit warning after a single call", async () => {
      generateObjectMock.mockRejectedValueOnce(
        noObjectError("length", '{"title": "Exam'),
      );

      const result = await runExtraction();

      expect(generateObjectMock).toHaveBeenCalledTimes(1);
      expect(getModelMock).not.toHaveBeenCalledWith(
        expect.anything(),
        "openai",
        { ignoreModelOverride: true },
      );
      expect(result.extractedDataArray).toEqual([undefined]);
      expect(result.warning).toContain("JSON extraction failed");
      expect(result.warning).toContain(
        "exceeded the model's maximum output length",
      );
    });

    it("keeps the upstream parse failure for code-fenced but unparseable JSON", async () => {
      generateObjectMock.mockRejectedValueOnce(
        noObjectError("stop", '```json\n{"title": "Example\n```'),
      );

      const result = await runExtraction();

      expect(generateObjectMock).toHaveBeenCalledTimes(1);
      expect(getModelMock).not.toHaveBeenCalledWith(
        expect.anything(),
        "openai",
        { ignoreModelOverride: true },
      );
      expect(result.extractedDataArray).toEqual([undefined]);
      expect(result.warning).toContain("JSON extraction failed");
      expect(result.warning).not.toContain("fallback");
    });
  });
});

// Pins exact warnings and the primary-plus-one-fallback bound through the real
// generateCompletions, so refactors of the fallback module are proven
// behavior-preserving.
describe("extractData structured-output fallback bounds through generateCompletions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    generateObjectMock.mockReset();
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      FALLBACK_MODEL;
  });

  it("disables AI SDK repair on the primary and the fallback", async () => {
    generateObjectMock
      .mockResolvedValueOnce(objectResult({ nope: 1 }))
      .mockResolvedValueOnce(objectResult({ title: "Example Domain" }));

    const result = await runExtraction();

    expect(result.extractedDataArray).toEqual([{ title: "Example Domain" }]);
    expect(generateObjectMock).toHaveBeenCalledTimes(2);
    for (const [config] of generateObjectMock.mock.calls) {
      expect(config.experimental_repairText).toBeUndefined();
    }
  });

  it("keeps the exact output-limit warnings when both attempts truncate", async () => {
    generateObjectMock
      .mockRejectedValueOnce(noObjectError("length", '{"title": "Exam'))
      .mockRejectedValueOnce(noObjectError("length", '{"title": "Exam'));

    const result = await runExtraction();

    expect(generateObjectMock).toHaveBeenCalledTimes(2);
    expect(result.warning).toBe(
      `JSON extraction failed: ${OUTPUT_LIMIT} JSON extraction fallback failed: ${OUTPUT_LIMIT}`,
    );
  });

  it("keeps exactly the primary output-limit warning when the fallback is schema-invalid", async () => {
    generateObjectMock
      .mockRejectedValueOnce(noObjectError("length", '{"title": "Exam'))
      .mockResolvedValueOnce(objectResult({ nope: 1 }));

    const result = await runExtraction();

    expect(generateObjectMock).toHaveBeenCalledTimes(2);
    expect(result.extractedDataArray).toEqual([undefined]);
    expect(result.warning).toBe(`JSON extraction failed: ${OUTPUT_LIMIT}`);
  });

  it("keeps the exact warnings when code-fenced JSON is unparseable twice", async () => {
    const fenced = '```json\n{"title": "Example\n```';
    const reason = noObjectError("stop", fenced).message;
    generateObjectMock
      .mockRejectedValueOnce(noObjectError("stop", fenced))
      .mockRejectedValueOnce(noObjectError("stop", fenced));

    const result = await runExtraction();

    expect(generateObjectMock).toHaveBeenCalledTimes(2);
    expect(result.warning).toBe(
      `JSON extraction failed: ${reason} JSON extraction fallback failed: ${reason}`,
    );
  });

  it("makes no internal retry or fallback call for a rate-limited primary", async () => {
    generateObjectMock.mockRejectedValueOnce(new Error("rate limit"));

    const result = await runExtraction();

    expect(generateObjectMock).toHaveBeenCalledTimes(1);
    expect(result.extractedDataArray).toEqual([undefined]);
    expect(result.warning).toBe("JSON extraction failed: rate limit");
  });

  it("makes no internal retry for a rate-limited fallback", async () => {
    generateObjectMock
      .mockRejectedValueOnce(noObjectError("length", '{"title": "Exam'))
      .mockRejectedValueOnce(new Error("rate limit"));

    const result = await runExtraction();

    expect(generateObjectMock).toHaveBeenCalledTimes(2);
    expect(result.warning).toBe(
      `JSON extraction failed: ${OUTPUT_LIMIT} JSON extraction fallback failed: rate limit`,
    );
  });

  it("propagates a cost-limit failure on the primary without a fallback call", async () => {
    generateObjectMock.mockResolvedValueOnce(
      objectResult({ title: "Example Domain" }),
    );

    await expect(
      runExtraction(() => {
        throw new CostLimitExceededError();
      }),
    ).rejects.toBeInstanceOf(CostLimitExceededError);
    expect(generateObjectMock).toHaveBeenCalledTimes(1);
  });

  it("propagates a cost-limit failure on the fallback", async () => {
    generateObjectMock
      .mockRejectedValueOnce(noObjectError("length", '{"title": "Exam'))
      .mockResolvedValueOnce(objectResult({ title: "Example Domain" }));

    await expect(
      runExtraction(() => {
        throw new CostLimitExceededError();
      }),
    ).rejects.toBeInstanceOf(CostLimitExceededError);
    expect(generateObjectMock).toHaveBeenCalledTimes(2);
  });

  describe("without a configured fallback", () => {
    beforeEach(() => {
      structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK = undefined;
    });

    it("keeps the upstream repair callback and rate-limit retry", async () => {
      generateObjectMock
        .mockRejectedValueOnce(new Error("rate limit"))
        .mockResolvedValueOnce(objectResult({ title: "Example Domain" }));

      const result = await runExtraction();

      expect(result.extractedDataArray).toEqual([{ title: "Example Domain" }]);
      expect(generateObjectMock).toHaveBeenCalledTimes(2);
      expect(
        generateObjectMock.mock.calls[0][0].experimental_repairText,
      ).toBeTypeOf("function");
    });

    it("keeps exactly the upstream output-limit warning", async () => {
      generateObjectMock.mockRejectedValueOnce(
        noObjectError("length", '{"title": "Exam'),
      );

      const result = await runExtraction();

      expect(generateObjectMock).toHaveBeenCalledTimes(1);
      expect(result.warning).toBe(`JSON extraction failed: ${OUTPUT_LIMIT}`);
    });
  });
});
