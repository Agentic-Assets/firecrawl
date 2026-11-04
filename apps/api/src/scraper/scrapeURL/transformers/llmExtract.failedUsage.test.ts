import { MockLanguageModelV3 } from "ai/test";
import { APICallError } from "ai";
import { vi } from "vitest";

const fake = vi.hoisted(() => ({ fallback: undefined as unknown }));
vi.mock("../lib/extractSmartScrape", () => ({ extractData: vi.fn() }));
vi.mock("../../../lib/generic-ai", () => ({
  getModel: vi.fn((name: string) =>
    name === "fallback-model" ? fake.fallback : { modelId: name },
  ),
}));
import { generateCompletions, calculateCost } from "./llmExtract";
import { generateSummaryCompletion } from "../lib/structuredOutputFallback";
import { config } from "../../../config";
import {
  CostTracking,
  CostLimitExceededError,
} from "../../../lib/cost-tracking";

const usage = {
  inputTokens: { total: 12, noCache: 12, cacheRead: 0, cacheWrite: 0 },
  outputTokens: { total: 5, text: 5, reasoning: 0 },
};
function model(id: string, text: string, finish: "stop" | "length" = "stop") {
  return new MockLanguageModelV3({
    modelId: id,
    doGenerate: async () => ({
      content: [{ type: "text", text }],
      finishReason: { unified: finish, raw: finish },
      usage,
      warnings: [],
    }),
  });
}
function options(primary: MockLanguageModelV3, cost: CostTracking) {
  return {
    logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    options: {
      schema: {
        type: "object",
        properties: { summary: { type: "string" } },
        required: ["summary"],
        additionalProperties: false,
      },
    },
    markdown: "# fixture",
    model: primary,
    costTrackingOptions: { costTracking: cost, metadata: {} },
    metadata: { teamId: "test", functionId: "test" },
  } as any;
}
describe("known failed structured generation usage", () => {
  const prior = config.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK;
  beforeEach(() => {
    config.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK = "fallback-model";
  });
  afterEach(() => {
    config.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK = prior;
    fake.fallback = undefined;
  });
  it.each(["stop", "length"] as const)(
    "records %s primary and successful fallback once",
    async reason => {
      const primary = model("gpt-4o-mini", "{invalid", reason);
      const fallback = model("gpt-4.1-mini", '{"summary":"ok"}');
      fake.fallback = fallback;
      const cost = new CostTracking();
      const out = await generateSummaryCompletion(
        generateCompletions,
        options(primary, cost),
      );
      expect(out.extract.summary).toBe("ok");
      expect(cost.calls.map(x => [x.model, x.tokens, x.cost])).toEqual([
        [
          "gpt-4o-mini",
          { input: 12, output: 5 },
          calculateCost("gpt-4o-mini", 12, 5),
        ],
        [
          "gpt-4.1-mini",
          { input: 12, output: 5 },
          calculateCost("gpt-4.1-mini", 12, 5),
        ],
      ]);
    },
  );
  it("counts each failed attempt when fallback also fails", async () => {
    const primary = model("gpt-4o-mini", "{bad"),
      fallback = model("gpt-4.1-mini", "{bad");
    fake.fallback = fallback;
    const cost = new CostTracking();
    await expect(
      generateSummaryCompletion(generateCompletions, options(primary, cost)),
    ).rejects.toThrow();
    expect(cost.calls.map(x => x.model)).toEqual([
      "gpt-4o-mini",
      "gpt-4.1-mini",
    ]);
  });
  it("counts a salvaged fenced response once", async () => {
    const primary = model(
      "gpt-4o-mini",
      '\x60\x60\x60json\n{"summary":"ok"}\n\x60\x60\x60',
    );
    const cost = new CostTracking();
    const out = await generateSummaryCompletion(
      generateCompletions,
      options(primary, cost),
    );
    expect(out.extract.summary).toBe("ok");
    expect(cost.calls).toHaveLength(1);
    expect(cost.calls[0].tokens).toEqual({ input: 12, output: 5 });
  });
  it("records unparseable fenced output once before fallback", async () => {
    const primary = model(
      "gpt-4o-mini",
      "`" + "`" + "`" + "json\n{bad}\n" + "`" + "`" + "`",
    );
    const fallback = model("gpt-4.1-mini", '{"summary":"ok"}');
    fake.fallback = fallback;
    const cost = new CostTracking();
    await generateSummaryCompletion(
      generateCompletions,
      options(primary, cost),
    );
    expect(cost.calls.map(x => x.model)).toEqual([
      "gpt-4o-mini",
      "gpt-4.1-mini",
    ]);
  });
  it("records retry-model output with the retry model price", async () => {
    config.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK = undefined;
    const primary = new MockLanguageModelV3({
      modelId: "gpt-4o-mini",
      doGenerate: async () => {
        throw new APICallError({
          message: "rate limit",
          url: "https://example.invalid",
          requestBodyValues: {},
          statusCode: 429,
          isRetryable: false,
        });
      },
    });
    const retry = model("gpt-4.1-mini", "{", "length");
    const cost = new CostTracking();
    const opts = options(primary, cost);
    opts.retryModel = retry;
    await expect(generateCompletions(opts)).rejects.toThrow();
    expect(cost.calls.map(x => [x.model, x.tokens, x.cost])).toEqual([
      [
        "gpt-4.1-mini",
        { input: 12, output: 5 },
        calculateCost("gpt-4.1-mini", 12, 5),
      ],
    ]);
  });
  it("does not fabricate missing token usage", async () => {
    const primary = new MockLanguageModelV3({
      modelId: "gpt-4o-mini",
      doGenerate: async () => ({
        content: [{ type: "text", text: "{bad" }],
        finishReason: { unified: "stop", raw: "stop" },
        usage: {
          inputTokens: {
            total: 12,
            noCache: undefined,
            cacheRead: undefined,
            cacheWrite: undefined,
          },
          outputTokens: {
            total: undefined,
            text: undefined,
            reasoning: undefined,
          },
        },
        warnings: [],
      }),
    });
    const cost = new CostTracking();
    const opts = options(primary, cost);
    opts.boundedStructuredOutput = true;
    await expect(generateCompletions(opts)).rejects.toThrow();
    expect(cost.calls).toHaveLength(0);
    expect(opts.logger.warn).toHaveBeenCalledWith(
      "Structured generation failed without complete token usage",
      {
        model: "gpt-4o-mini",
        knownInputTokens: 12,
        knownOutputTokens: undefined,
      },
    );
  });
  it("checks primary known cost before fallback", async () => {
    const primary = model("gpt-4o-mini", "{bad"),
      fallback = model("gpt-4.1-mini", '{"summary":"ok"}');
    fake.fallback = fallback;
    const cost = new CostTracking(0);
    await expect(
      generateSummaryCompletion(generateCompletions, options(primary, cost)),
    ).rejects.toBeInstanceOf(CostLimitExceededError);
    expect(cost.calls).toHaveLength(1);
    expect(fallback.doGenerateCalls).toHaveLength(0);
  });
  it("checks fallback cost after it is incurred", async () => {
    const primary = model("gpt-4o-mini", "{bad"),
      fallback = model("gpt-4.1-mini", '{"summary":"ok"}');
    fake.fallback = fallback;
    const cost = new CostTracking(calculateCost("gpt-4o-mini", 12, 5));
    await expect(
      generateSummaryCompletion(generateCompletions, options(primary, cost)),
    ).rejects.toBeInstanceOf(CostLimitExceededError);
    expect(cost.calls.map(x => x.model)).toEqual([
      "gpt-4o-mini",
      "gpt-4.1-mini",
    ]);
  });
});
