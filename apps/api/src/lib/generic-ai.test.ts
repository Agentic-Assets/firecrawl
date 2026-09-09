import { describe, expect, it, vi } from "vitest";

vi.mock("../config", () => ({
  config: {
    MODEL_NAME: "configured-primary",
    MODEL_EMBEDDING_NAME: undefined,
    OLLAMA_BASE_URL: undefined,
    OPENAI_API_KEY: undefined,
    OPENAI_BASE_URL: undefined,
    OPENROUTER_API_KEY: undefined,
    VERTEX_CREDENTIALS: undefined,
  },
}));

import { getModel, getModelByName } from "./generic-ai";

describe("getModelByName", () => {
  it("bypasses the process-wide model override for compatibility fallbacks", () => {
    expect(getModel("caller-requested", "openai").modelId).toBe(
      "configured-primary",
    );
    expect(getModelByName("configured-fallback", "openai").modelId).toBe(
      "configured-fallback",
    );
  });
});
