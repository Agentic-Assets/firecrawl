import { describe, expect, it, vi } from "vitest";
import type { Response } from "express";
import { agentController } from "../agent";
import type { AgentRequest, AgentResponse, RequestWithAuth } from "../types";

vi.mock("../../../config", () => ({
  config: { EXTRACT_V3_BETA_URL: undefined },
}));

vi.mock("../../../lib/logger", () => ({
  logger: { child: vi.fn(() => ({ error: vi.fn() })), info: vi.fn() },
}));

vi.mock("../../../db/rpc", () => ({ agentConsumeFreeRequestIfLeft: vi.fn() }));
vi.mock("../../../services/logging/log_job", () => ({ logRequest: vi.fn() }));
vi.mock("../../../lib/zdr-helpers", () => ({
  getScrapeZDR: vi.fn(() => "off"),
}));
vi.mock("../../../lib/threat-protection/request", () => ({
  resolveThreatProtection: vi.fn(async () => ({ policy: null })),
  checkUrlsAgainstThreatPolicy: vi.fn(),
}));
vi.mock("../../../services/billing/credit_billing", () => ({
  billTeam: vi.fn(),
}));
vi.mock("../../../lib/siem-logging", () => ({
  emitRejectedScrapeActivityEvents: vi.fn(),
}));

describe("agentController", () => {
  it("returns a stable local configuration response when the agent service is absent", async () => {
    const status = vi.fn();
    const json = vi.fn();
    status.mockReturnValue({ json });
    const req = {
      body: { prompt: "Smoke probe only." },
      auth: { team_id: "local-team" },
      acuc: undefined,
    } as unknown as RequestWithAuth<{}, AgentResponse, AgentRequest>;
    const res = { status } as unknown as Response<AgentResponse>;

    await agentController(req, res);

    expect(status).toHaveBeenCalledWith(503);
    expect(json).toHaveBeenCalledWith({
      success: false,
      error:
        "Agent feature is not configured (EXTRACT_V3_BETA_URL is missing).",
    });
  });
});
