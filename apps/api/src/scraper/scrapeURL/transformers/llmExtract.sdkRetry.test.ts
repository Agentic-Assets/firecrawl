import { APICallError, NoObjectGeneratedError } from "ai";
import { MockLanguageModelV3 } from "ai/test";
import { vi } from "vitest";

const fake = vi.hoisted(() => ({ fallback: undefined as unknown }));

vi.mock("../lib/extractSmartScrape", () => ({ extractData: vi.fn() }));
vi.mock("../../../lib/generic-ai", () => ({
  getModel: vi.fn((modelName: string) =>
    modelName === "fallback-model" ? fake.fallback : { modelId: modelName },
  ),
}));

import { generateCompletions } from "./llmExtract";
import { generateSummaryCompletion } from "../lib/structuredOutputFallback";
import { config } from "../../../config";

const usage = {
  inputTokens: { total: 12, noCache: 12, cacheRead: 0, cacheWrite: 0 },
  outputTokens: { total: 5, text: 5, reasoning: 0 },
};

function options(model: MockLanguageModelV3, bounded: boolean) {
  return {
    logger: { debug: vi.fn(), error: vi.fn(), warn: vi.fn(), info: vi.fn() },
    options: {
      schema: {
        type: "object",
        properties: { title: { type: "string" } },
        required: ["title"],
        additionalProperties: false,
      },
    },
    markdown: "# Example",
    model,
    retryModel: model,
    boundedStructuredOutput: bounded,
    costTrackingOptions: { costTracking: { addCall: vi.fn() }, metadata: {} },
    metadata: { teamId: "test-team", functionId: "test-function" },
  } as any;
}

describe("real AI SDK retry and failed-output behavior", () => {
  it.each([429, 503])(
    "bounded generation does not retry provider status %i",
    async status => {
      const model = new MockLanguageModelV3({
        doGenerate: async () => {
          throw new APICallError({
            message: "synthetic provider failure",
            url: "https://example.invalid/",
            requestBodyValues: {},
            statusCode: status,
            isRetryable: true,
          });
        },
      });
      await expect(generateCompletions(options(model, true))).rejects.toThrow();
      expect(model.doGenerateCalls).toHaveLength(1);
    },
    20000,
  );

  it("ordinary generation retains the SDK retry default", async () => {
    let attempts = 0;
    const model = new MockLanguageModelV3({
      doGenerate: async () => {
        if (++attempts === 1)
          throw new APICallError({
            message: "synthetic transient failure",
            url: "https://example.invalid/",
            requestBodyValues: {},
            statusCode: 503,
            isRetryable: true,
          });
        return {
          content: [{ type: "text", text: '{"title":"Example"}' }],
          finishReason: { unified: "stop", raw: "stop" },
          usage,
          warnings: [],
        };
      },
    });
    const result = await generateCompletions(options(model, false));
    expect(result.extract).toEqual({ title: "Example" });
    expect(model.doGenerateCalls).toHaveLength(2);
  }, 20000);

  it("preserves SDK usage on an invalid response error", async () => {
    const model = new MockLanguageModelV3({
      doGenerate: async () => ({
        content: [{ type: "text", text: "{not-json}" }],
        finishReason: { unified: "stop", raw: "stop" },
        usage,
        warnings: [],
      }),
    });
    const opts = options(model, true);
    let caught: unknown;
    try {
      await generateCompletions(opts);
    } catch (error) {
      caught = error;
    }
    expect(NoObjectGeneratedError.isInstance(caught)).toBe(true);
    expect((caught as NoObjectGeneratedError).usage?.inputTokens).toBe(12);
    expect((caught as NoObjectGeneratedError).usage?.outputTokens).toBe(5);
  });
});

describe("real AI SDK structured fallback transaction", () => {
  const originalFallback = config.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK;
  beforeEach(() => {
    config.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK = "fallback-model";
  });
  afterEach(() => {
    config.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK = originalFallback;
    fake.fallback = undefined;
  });

  function model(text: string, reason: "stop" | "length" = "stop") {
    return new MockLanguageModelV3({
      doGenerate: async () => ({
        content: [{ type: "text", text }],
        finishReason: { unified: reason, raw: reason },
        usage,
        warnings: [],
      }),
    });
  }

  function summaryOptions(primary: MockLanguageModelV3) {
    const opts = options(primary, true);
    opts.options.schema = {
      type: "object",
      properties: { summary: { type: "string" } },
      required: ["summary"],
      additionalProperties: false,
    };
    return opts;
  }

  it.each(["invalid", "truncated"])(
    "%s primary uses exactly one successful fallback",
    async kind => {
      const primary = model(
        "{not-json}",
        kind === "truncated" ? "length" : "stop",
      );
      const fallback = model('{"summary":"a useful summary"}');
      fake.fallback = fallback;
      const result = await generateSummaryCompletion(
        generateCompletions,
        summaryOptions(primary),
      );
      expect(result.extract).toEqual({ summary: "a useful summary" });
      expect(primary.doGenerateCalls).toHaveLength(1);
      expect(fallback.doGenerateCalls).toHaveLength(1);
    },
  );

  it("invalid primary and invalid fallback each make one request", async () => {
    const primary = model("{not-json}");
    const fallback = model("{also-not-json}");
    fake.fallback = fallback;
    await expect(
      generateSummaryCompletion(generateCompletions, summaryOptions(primary)),
    ).rejects.toThrow();
    expect(primary.doGenerateCalls).toHaveLength(1);
    expect(fallback.doGenerateCalls).toHaveLength(1);
  });

  it("provider denial does not invoke the fallback", async () => {
    const primary = new MockLanguageModelV3({
      doGenerate: async () => {
        throw new APICallError({
          message: "synthetic provider failure",
          url: "https://example.invalid/",
          requestBodyValues: {},
          statusCode: 429,
          isRetryable: true,
        });
      },
    });
    const fallback = model('{"summary":"unused"}');
    fake.fallback = fallback;
    await expect(
      generateSummaryCompletion(generateCompletions, summaryOptions(primary)),
    ).rejects.toThrow();
    expect(primary.doGenerateCalls).toHaveLength(1);
    expect(fallback.doGenerateCalls).toHaveLength(0);
  });
});
