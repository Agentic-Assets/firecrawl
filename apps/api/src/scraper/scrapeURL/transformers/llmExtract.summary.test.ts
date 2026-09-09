import { NoObjectGeneratedError } from "ai";
import { vi } from "vitest";

const {
  generateObjectMock,
  getModelMock,
  getModelByNameMock,
  structuredOutputConfig,
} = vi.hoisted(() => ({
  generateObjectMock: vi.fn(),
  getModelMock: vi.fn(() => ({
    modelId: "deepseek/deepseek-v4-flash-0731",
  })),
  getModelByNameMock: vi.fn((modelName: string) => ({ modelId: modelName })),
  structuredOutputConfig: {} as {
    MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK?: string;
  },
}));

vi.mock("ai", async importOriginal => ({
  ...(await importOriginal<typeof import("ai")>()),
  generateObject: generateObjectMock,
}));

vi.mock("../lib/extractSmartScrape", () => ({
  extractData: vi.fn(),
}));

vi.mock("../../../config", () => ({ config: structuredOutputConfig }));

vi.mock("../../../lib/generic-ai", () => ({
  getModel: getModelMock,
  getModelByName: getModelByNameMock,
}));

import { performSummary } from "./llmExtract";

function completion(object: unknown) {
  return {
    object,
    usage: { inputTokens: 1, outputTokens: 1, totalTokens: 2 },
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

function summaryMeta() {
  const childLogger = {
    debug: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
  };
  const logger = {
    child: vi.fn(() => childLogger),
    debug: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
  };

  return {
    options: { formats: [{ type: "summary" }] },
    internalOptions: { zeroDataRetention: false, teamId: "test-team" },
    logger,
    costTracking: { addCall: vi.fn() },
    id: "test-scrape",
  } as any;
}

describe("performSummary structured-output compatibility", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK = undefined;
  });

  it("accepts a valid primary summary without a fallback call", async () => {
    generateObjectMock.mockResolvedValueOnce(
      completion({ summary: "Example Domain is for documentation examples." }),
    );

    const result = await performSummary(summaryMeta(), {
      markdown: "# Example Domain",
    } as any);

    expect(result.summary).toBe(
      "Example Domain is for documentation examples.",
    );
    expect(generateObjectMock).toHaveBeenCalledTimes(1);
    expect(getModelByNameMock).not.toHaveBeenCalled();
  });

  it("retries an invalid primary result once with the configured explicit fallback", async () => {
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      "deepseek/deepseek-v4-pro-0813";
    generateObjectMock
      .mockResolvedValueOnce(completion({ type: "object" }))
      .mockResolvedValueOnce(
        completion({
          summary: "Example Domain is for documentation examples.",
        }),
      );

    const result = await performSummary(summaryMeta(), {
      markdown: "# Example Domain",
    } as any);

    expect(result.summary).toBe(
      "Example Domain is for documentation examples.",
    );
    expect(generateObjectMock).toHaveBeenCalledTimes(2);
    expect(getModelByNameMock).toHaveBeenCalledWith(
      "deepseek/deepseek-v4-pro-0813",
      "openai",
    );
    expect(generateObjectMock.mock.calls[1][0].model).toMatchObject({
      modelId: "deepseek/deepseek-v4-pro-0813",
    });
  });

  it("retries an AI SDK schema-invalid primary result once with the configured fallback", async () => {
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      "deepseek/deepseek-v4-pro-0813";
    generateObjectMock
      .mockRejectedValueOnce(invalidStructuredOutputError())
      .mockResolvedValueOnce(
        completion({
          summary: "Example Domain is for documentation examples.",
        }),
      );

    const result = await performSummary(summaryMeta(), {
      markdown: "# Example Domain",
    } as any);

    expect(result.summary).toBe(
      "Example Domain is for documentation examples.",
    );
    expect(generateObjectMock).toHaveBeenCalledTimes(2);
    expect(getModelByNameMock).toHaveBeenCalledWith(
      "deepseek/deepseek-v4-pro-0813",
      "openai",
    );
  });

  it("does not fabricate a summary when the fallback is also invalid", async () => {
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      "deepseek/deepseek-v4-pro-0813";
    generateObjectMock
      .mockResolvedValueOnce(completion({ type: "object" }))
      .mockResolvedValueOnce(completion({ summary: "   " }));

    const result = await performSummary(summaryMeta(), {
      markdown: "# Example Domain",
    } as any);

    expect(result.summary).toBeUndefined();
    expect(generateObjectMock).toHaveBeenCalledTimes(2);
  });

  it("does not retry a failed provider request with the structured-output fallback", async () => {
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      "deepseek/deepseek-v4-pro-0813";

    generateObjectMock.mockRejectedValueOnce(new Error("rate limit"));

    await expect(
      performSummary(summaryMeta(), { markdown: "# Example Domain" } as any),
    ).rejects.toThrow("rate limit");

    expect(generateObjectMock).toHaveBeenCalledTimes(1);
    expect(getModelByNameMock).not.toHaveBeenCalled();
  });

  it("propagates a configured fallback failure", async () => {
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      "deepseek/deepseek-v4-pro-0813";
    generateObjectMock
      .mockResolvedValueOnce(completion({ type: "object" }))
      .mockRejectedValueOnce(new Error("fallback unavailable"));

    await expect(
      performSummary(summaryMeta(), { markdown: "# Example Domain" } as any),
    ).rejects.toThrow("fallback unavailable");

    expect(generateObjectMock).toHaveBeenCalledTimes(2);
  });

  it("keeps the ordinary one-primary-call behavior when no fallback is configured", async () => {
    generateObjectMock.mockResolvedValueOnce(completion({ type: "object" }));

    const result = await performSummary(summaryMeta(), {
      markdown: "# Example Domain",
    } as any);

    expect(result.summary).toBeUndefined();
    expect(generateObjectMock).toHaveBeenCalledTimes(1);
    expect(getModelByNameMock).not.toHaveBeenCalled();
  });

  it("keeps the ordinary rate-limit retry when no fallback is configured", async () => {
    generateObjectMock
      .mockRejectedValueOnce(new Error("rate limit"))
      .mockResolvedValueOnce(
        completion({
          summary: "Example Domain is for documentation examples.",
        }),
      );

    const result = await performSummary(summaryMeta(), {
      markdown: "# Example Domain",
    } as any);

    expect(result.summary).toBe(
      "Example Domain is for documentation examples.",
    );
    expect(generateObjectMock).toHaveBeenCalledTimes(2);
    expect(getModelByNameMock).not.toHaveBeenCalled();
  });
});
