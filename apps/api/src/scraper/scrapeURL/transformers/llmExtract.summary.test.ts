import { NoObjectGeneratedError } from "ai";
import { vi } from "vitest";

const { generateObjectMock, getModelMock, structuredOutputConfig } = vi.hoisted(
  () => ({
    generateObjectMock: vi.fn(),
    // The configured fallback is the only call that bypasses MODEL_NAME.
    getModelMock: vi.fn(
      (
        modelName: string,
        _provider?: string,
        options?: { ignoreModelOverride?: boolean },
      ) => ({
        modelId: options?.ignoreModelOverride
          ? modelName
          : "deepseek/deepseek-v4-flash-0731",
      }),
    ),
    structuredOutputConfig: {} as {
      MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK?: string;
    },
  }),
);

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
}));

import { performSummary } from "./llmExtract";
import { CostLimitExceededError } from "../../../lib/cost-tracking";

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

function truncatedStructuredOutputError() {
  return new NoObjectGeneratedError({
    response: {} as any,
    usage: {} as any,
    finishReason: "length" as any,
    text: '{"summary": "Example Domain is',
  });
}

function fencedUnparseableOutputError() {
  return new NoObjectGeneratedError({
    response: {} as any,
    usage: {} as any,
    finishReason: "stop" as any,
    text: '```json\n{"summary": "Example Domain is\n```',
  });
}

function summaryMeta(addCall: () => void = vi.fn()) {
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
    costTracking: { addCall },
    id: "test-scrape",
  } as any;
}

describe("performSummary structured-output compatibility", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Drop queued once-responses a failing test may leave behind.
    generateObjectMock.mockReset();
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
    expect(getModelMock).not.toHaveBeenCalledWith(expect.anything(), "openai", {
      ignoreModelOverride: true,
    });
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
    expect(getModelMock).toHaveBeenCalledWith(
      "deepseek/deepseek-v4-pro-0813",
      "openai",
      { ignoreModelOverride: true },
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
    expect(getModelMock).toHaveBeenCalledWith(
      "deepseek/deepseek-v4-pro-0813",
      "openai",
      { ignoreModelOverride: true },
    );
  });

  it("retries a truncated (output-limit) primary once with the configured fallback", async () => {
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      "deepseek/deepseek-v4-pro-0813";
    generateObjectMock
      .mockRejectedValueOnce(truncatedStructuredOutputError())
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
    expect(generateObjectMock.mock.calls[1][0].model).toMatchObject({
      modelId: "deepseek/deepseek-v4-pro-0813",
    });
  });

  it("fails cleanly after one fallback when primary and fallback both truncate", async () => {
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      "deepseek/deepseek-v4-pro-0813";
    generateObjectMock
      .mockRejectedValueOnce(truncatedStructuredOutputError())
      .mockRejectedValueOnce(truncatedStructuredOutputError());

    await expect(
      performSummary(summaryMeta(), { markdown: "# Example Domain" } as any),
    ).rejects.toThrow("exceeded the model's maximum output length");

    expect(generateObjectMock).toHaveBeenCalledTimes(2);
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

  it("retries a code-fenced but unparseable primary summary once with the configured fallback", async () => {
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      "deepseek/deepseek-v4-pro-0813";
    generateObjectMock
      .mockRejectedValueOnce(fencedUnparseableOutputError())
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
    expect(generateObjectMock.mock.calls[1][0].model).toMatchObject({
      modelId: "deepseek/deepseek-v4-pro-0813",
    });
  });

  it("fails cleanly after one fallback when code-fenced output is unparseable twice", async () => {
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      "deepseek/deepseek-v4-pro-0813";
    generateObjectMock
      .mockRejectedValueOnce(fencedUnparseableOutputError())
      .mockRejectedValueOnce(fencedUnparseableOutputError());

    await expect(
      performSummary(summaryMeta(), { markdown: "# Example Domain" } as any),
    ).rejects.toBeInstanceOf(NoObjectGeneratedError);

    expect(generateObjectMock).toHaveBeenCalledTimes(2);
  });

  it("keeps the upstream output-limit error when no fallback is configured", async () => {
    generateObjectMock.mockRejectedValueOnce(truncatedStructuredOutputError());

    const rejection = performSummary(summaryMeta(), {
      markdown: "# Example Domain",
    } as any);

    await expect(rejection).rejects.toBeInstanceOf(Error);
    await expect(rejection).rejects.toThrow(
      "exceeded the model's maximum output length",
    );
    expect(generateObjectMock).toHaveBeenCalledTimes(1);
    expect(getModelMock).not.toHaveBeenCalledWith(expect.anything(), "openai", {
      ignoreModelOverride: true,
    });
  });

  it("keeps the upstream SyntaxError for code-fenced output when no fallback is configured", async () => {
    generateObjectMock.mockRejectedValueOnce(fencedUnparseableOutputError());

    await expect(
      performSummary(summaryMeta(), { markdown: "# Example Domain" } as any),
    ).rejects.toBeInstanceOf(SyntaxError);

    expect(generateObjectMock).toHaveBeenCalledTimes(1);
    expect(getModelMock).not.toHaveBeenCalledWith(expect.anything(), "openai", {
      ignoreModelOverride: true,
    });
  });

  it("does not retry a failed provider request with the structured-output fallback", async () => {
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      "deepseek/deepseek-v4-pro-0813";

    generateObjectMock.mockRejectedValueOnce(new Error("rate limit"));

    await expect(
      performSummary(summaryMeta(), { markdown: "# Example Domain" } as any),
    ).rejects.toThrow("rate limit");

    expect(generateObjectMock).toHaveBeenCalledTimes(1);
    expect(getModelMock).not.toHaveBeenCalledWith(expect.anything(), "openai", {
      ignoreModelOverride: true,
    });
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
    expect(getModelMock).not.toHaveBeenCalledWith(expect.anything(), "openai", {
      ignoreModelOverride: true,
    });
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
    expect(getModelMock).not.toHaveBeenCalledWith(expect.anything(), "openai", {
      ignoreModelOverride: true,
    });
  });
});

// Pins key presence, error propagation, and the primary-plus-one-fallback
// bound, so refactors of the fallback module are proven behavior-preserving.
describe("performSummary structured-output fallback bounds", () => {
  const FALLBACK_MODEL = "deepseek/deepseek-v4-pro-0813";
  const throwCostLimit = () => {
    throw new CostLimitExceededError();
  };

  beforeEach(() => {
    vi.clearAllMocks();
    generateObjectMock.mockReset();
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK =
      FALLBACK_MODEL;
  });

  it("leaves summary unset when the fallback is also unusable", async () => {
    generateObjectMock
      .mockResolvedValueOnce(completion({ type: "object" }))
      .mockResolvedValueOnce(completion({ summary: "   " }));

    const result = await performSummary(summaryMeta(), {
      markdown: "# Example Domain",
    } as any);

    expect(Object.hasOwn(result, "summary")).toBe(false);
  });

  it("leaves summary unset for an unusable primary without a fallback", async () => {
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK = undefined;
    generateObjectMock.mockResolvedValueOnce(completion({ summary: "" }));

    const result = await performSummary(summaryMeta(), {
      markdown: "# Example Domain",
    } as any);

    expect(Object.hasOwn(result, "summary")).toBe(false);
    expect(generateObjectMock).toHaveBeenCalledTimes(1);
  });

  it("disables AI SDK repair on the primary and the fallback", async () => {
    generateObjectMock
      .mockResolvedValueOnce(completion({ type: "object" }))
      .mockResolvedValueOnce(completion({ summary: "Example summary." }));

    const result = await performSummary(summaryMeta(), {
      markdown: "# Example Domain",
    } as any);

    expect(result.summary).toBe("Example summary.");
    expect(generateObjectMock).toHaveBeenCalledTimes(2);
    for (const [config] of generateObjectMock.mock.calls) {
      expect(config.experimental_repairText).toBeUndefined();
    }
  });

  it("keeps the upstream repair callback without a fallback", async () => {
    structuredOutputConfig.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK = undefined;
    generateObjectMock.mockResolvedValueOnce(
      completion({ summary: "Example summary." }),
    );

    await performSummary(summaryMeta(), {
      markdown: "# Example Domain",
    } as any);

    expect(
      generateObjectMock.mock.calls[0][0].experimental_repairText,
    ).toBeTypeOf("function");
  });

  it("makes no internal retry for a rate-limited fallback", async () => {
    generateObjectMock
      .mockResolvedValueOnce(completion({ type: "object" }))
      .mockRejectedValueOnce(new Error("rate limit"));

    await expect(
      performSummary(summaryMeta(), { markdown: "# Example Domain" } as any),
    ).rejects.toThrow("rate limit");
    expect(generateObjectMock).toHaveBeenCalledTimes(2);
  });

  it("propagates a cost-limit failure on the primary without a fallback call", async () => {
    generateObjectMock.mockResolvedValueOnce(
      completion({ summary: "Example summary." }),
    );

    await expect(
      performSummary(summaryMeta(throwCostLimit), {
        markdown: "# Example Domain",
      } as any),
    ).rejects.toBeInstanceOf(CostLimitExceededError);
    expect(generateObjectMock).toHaveBeenCalledTimes(1);
  });

  it("propagates a cost-limit failure on the fallback", async () => {
    generateObjectMock
      .mockRejectedValueOnce(truncatedStructuredOutputError())
      .mockResolvedValueOnce(completion({ summary: "Example summary." }));

    await expect(
      performSummary(summaryMeta(throwCostLimit), {
        markdown: "# Example Domain",
      } as any),
    ).rejects.toBeInstanceOf(CostLimitExceededError);
    expect(generateObjectMock).toHaveBeenCalledTimes(2);
  });
});
