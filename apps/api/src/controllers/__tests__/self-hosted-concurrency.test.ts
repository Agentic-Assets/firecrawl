import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { authenticateUser, getACUCTeam } from "../auth";
import { config } from "../../config";
import { selfHostedConcurrencyLimit } from "../../lib/self-hosted-concurrency";
import { RateLimiterMode } from "../../types";
import { getValue } from "../../services/redis";

// Exercise the real auth entrypoints and withAuth wrapper without opening services.
vi.mock("../../config", () => ({
  config: { USE_DB_AUTHENTICATION: false, PREVIEW_TOKEN: "preview-test" },
}));
vi.mock("../../lib/logger", () => ({
  logger: { warn: vi.fn(), error: vi.fn(), info: vi.fn() },
}));
vi.mock("../../services/agent-sponsor", () => ({
  getAgentSponsorStatus: vi.fn(),
}));
vi.mock("../../services/rate-limiter", () => ({
  getRateLimiter: vi.fn(() => ({ consume: vi.fn() })),
  getAutumnRateLimiter: vi.fn(() => ({ consume: vi.fn() })),
  getRateLimitOverride: vi.fn(),
  HOBBY_RATE_LIMIT_MULTIPLIER: 1,
}));
vi.mock("../../lib/agent-interop", () => ({
  isTrustedAgentInteropRequest: vi.fn(() => false),
}));
vi.mock("../../lib/keyless", () => ({}));
vi.mock("../../lib/keyless-signup-link", () => ({}));
vi.mock("../../lib/spur", () => ({}));
vi.mock("../../lib/keyless-metrics", () => ({}));
vi.mock("../../lib/ip-restriction", () => ({}));
vi.mock("../../lib/key-restriction", () => ({}));
vi.mock("../../lib/auth-denied-log", () => ({ logAuthDenied: vi.fn() }));
vi.mock("../../services/redis", () => ({
  getValue: vi.fn(),
  setValue: vi.fn(),
  deleteKey: vi.fn(),
}));
vi.mock("../../services/redlock", () => ({ redlock: {} }));
vi.mock("../../db/connection", () => ({ db: {}, dbRr: {} }));
vi.mock("../../db/rpc", () => ({
  authCreditUsageChunk: vi.fn(),
  authCreditUsageChunkFromTeam: vi.fn(),
}));
vi.mock("../../services/oauth-token-introspection", () => ({}));
vi.mock("../../lib/mcp-delegated-credential", () => ({}));
vi.mock("../../services/autumn/autumn.service", () => ({
  DEFAULT_TEAM_LIMITS: {
    concurrency_limit: 2,
    rate_limit_multiplier: 1,
    is_paid_plan: false,
  },
  autumnService: {},
}));

describe("fork self-hosted concurrency", () => {
  const originalUseDbAuth = config.USE_DB_AUTHENTICATION;

  beforeEach(() => {
    config.USE_DB_AUTHENTICATION = false;
    vi.stubEnv("SELF_HOSTED_CONCURRENCY_LIMIT", undefined);
  });

  afterEach(() => {
    config.USE_DB_AUTHENTICATION = originalUseDbAuth;
    vi.unstubAllEnvs();
    vi.clearAllMocks();
  });

  it.each([
    undefined,
    "",
    "0",
    "33",
    "-1",
    "1.5",
    "8.0",
    "1e1",
    "NaN",
    "Infinity",
    "8junk",
  ])("retains the default for absent or invalid setting %s", rawValue => {
    expect(selfHostedConcurrencyLimit(false, 2, rawValue)).toBe(2);
  });

  it.each(["1", "8", "32"])("accepts bounded integer %s", rawValue => {
    expect(selfHostedConcurrencyLimit(false, 2, rawValue)).toBe(
      Number(rawValue),
    );
  });

  it.each([true, undefined])(
    "requires explicitly disabled DB auth (%s)",
    auth => {
      expect(selfHostedConcurrencyLimit(auth, 2, "8")).toBe(2);
    },
  );

  it.each([undefined, "8", "33"])(
    "applies the setting consistently through auth and team lookup (%s)",
    async rawValue => {
      vi.stubEnv("SELF_HOSTED_CONCURRENCY_LIMIT", rawValue);
      const expected = rawValue === "8" ? 8 : 2;
      const auth = await authenticateUser(
        { headers: {}, socket: {} },
        {},
        RateLimiterMode.Scrape,
      );
      expect(auth.success).toBe(true);
      if (!auth.success) throw new Error("expected bypass auth");
      expect(auth.chunk).toMatchObject({
        team_id: "bypass",
        concurrency_limit: expected,
      });
      expect(await getACUCTeam("bypass")).toMatchObject({
        concurrency_limit: expected,
      });
      expect(getValue).not.toHaveBeenCalled();
    },
  );

  it("keeps preview team limits at the default", async () => {
    vi.stubEnv("SELF_HOSTED_CONCURRENCY_LIMIT", "8");
    expect(await getACUCTeam("preview_example")).toMatchObject({
      concurrency_limit: 2,
    });
  });

  it("preserves hosted team limits through auth and team lookup", async () => {
    config.USE_DB_AUTHENTICATION = true;
    vi.stubEnv("SELF_HOSTED_CONCURRENCY_LIMIT", "8");
    vi.mocked(getValue).mockResolvedValue(
      JSON.stringify({
        api_key: "11111111-1111-4111-8111-111111111111",
        api_key_id: 0,
        team_id: "hosted-team",
        org_id: "hosted-org",
        flags: null,
        is_banned: false,
        concurrency_limit: 5,
        rate_limit_multiplier: 1,
        is_paid_plan: true,
      }),
    );
    const auth = await authenticateUser(
      {
        headers: {
          authorization: "Bearer fc-11111111111141118111111111111111",
        },
        socket: {},
      },
      {},
      RateLimiterMode.Scrape,
    );
    expect(auth.success).toBe(true);
    if (!auth.success) throw new Error("expected hosted auth");
    expect(auth.chunk).toMatchObject({
      team_id: "hosted-team",
      concurrency_limit: 5,
    });
    expect(await getACUCTeam("hosted-team")).toMatchObject({
      concurrency_limit: 5,
    });
    expect(getValue).toHaveBeenCalledTimes(2);
  });
});
