import { vi } from "vitest";

const { generateObjectMock } = vi.hoisted(() => ({
  generateObjectMock: vi.fn(),
}));

vi.mock("ai", async importOriginal => ({
  ...(await importOriginal<typeof import("ai")>()),
  generateObject: generateObjectMock,
}));

vi.mock("../lib/extractSmartScrape", () => ({
  extractData: vi.fn(),
}));

vi.mock("../../../lib/generic-ai", () => ({
  getModel: vi.fn((modelName: string) => ({ modelId: modelName })),
}));

import { generateCompletions } from "./llmExtract";

const primaryModel = { modelId: "primary-model" };
const retryModel = { modelId: "ordinary-retry-model" };

function completionOptions(
  disableInternalRateLimitRetry: boolean,
  disableInternalObjectRepair = false,
) {
  return {
    logger: {
      debug: vi.fn(),
      error: vi.fn(),
      warn: vi.fn(),
    },
    options: {
      schema: {
        type: "object",
        properties: { title: { type: "string" } },
        required: ["title"],
        additionalProperties: false,
      },
    },
    markdown: "# Example Domain",
    model: primaryModel,
    retryModel,
    disableInternalRateLimitRetry,
    disableInternalObjectRepair,
    costTrackingOptions: {
      costTracking: { addCall: vi.fn() },
      metadata: {},
    },
    metadata: { teamId: "test-team", functionId: "test-function" },
  } as any;
}

describe("generateCompletions rate-limit retry control", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("does not issue an internal retry when an explicit compatibility fallback owns the transaction", async () => {
    generateObjectMock.mockRejectedValueOnce(new Error("rate limit"));

    await expect(generateCompletions(completionOptions(true))).rejects.toThrow(
      "rate limit",
    );

    expect(generateObjectMock).toHaveBeenCalledTimes(1);
    expect(generateObjectMock.mock.calls[0][0].model).toBe(primaryModel);
  });

  it("keeps the ordinary one-time rate-limit retry behavior", async () => {
    generateObjectMock
      .mockRejectedValueOnce(new Error("rate limit"))
      .mockResolvedValueOnce({
        object: { title: "Example Domain" },
        usage: { inputTokens: 1, outputTokens: 1, totalTokens: 2 },
      });

    const result = await generateCompletions(completionOptions(false));

    expect(result.extract).toEqual({ title: "Example Domain" });
    expect(generateObjectMock).toHaveBeenCalledTimes(2);
    expect(generateObjectMock.mock.calls[0][0].model).toBe(primaryModel);
    expect(generateObjectMock.mock.calls[1][0].model).toBe(retryModel);
  });

  it("omits the AI SDK repair callback in a bounded compatibility transaction", async () => {
    generateObjectMock.mockResolvedValueOnce({
      object: { title: "Example Domain" },
      usage: { inputTokens: 1, outputTokens: 1, totalTokens: 2 },
    });

    await generateCompletions(completionOptions(true, true));

    expect(generateObjectMock).toHaveBeenCalledTimes(1);
    expect(
      generateObjectMock.mock.calls[0][0].experimental_repairText,
    ).toBeUndefined();
  });
});
