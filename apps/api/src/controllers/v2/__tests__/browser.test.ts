import type { Response } from "express";
import { describe, expect, it, vi } from "vitest";

const { mockConfig, mockListBrowserSessions } = vi.hoisted(() => ({
  mockConfig: { BROWSER_SERVICE_URL: undefined as string | undefined },
  mockListBrowserSessions: vi.fn(),
}));

vi.mock("../../../config", () => ({ config: mockConfig }));
vi.mock("../../../lib/logger", () => ({
  logger: { child: vi.fn(() => ({ info: vi.fn() })) },
}));
vi.mock("../../../lib/browser-sessions", () => ({
  insertBrowserSession: vi.fn(),
  getBrowserSession: vi.fn(),
  getBrowserSessionByBrowserId: vi.fn(),
  listBrowserSessions: mockListBrowserSessions,
  updateBrowserSessionActivity: vi.fn(),
  updateBrowserSessionStatus: vi.fn(),
  updateBrowserSessionCreditsUsed: vi.fn(),
  claimBrowserSessionDestroyed: vi.fn(),
  invalidateActiveBrowserSessionCount: vi.fn(),
  didBrowserSessionUsePrompt: vi.fn(),
  clearBrowserSessionPromptFlag: vi.fn(),
}));
vi.mock("../../../services/worker/nuq-router", () => ({
  getCombinedTeamActiveCount: vi.fn(),
  mirrorExternalSlotAcquire: vi.fn(),
  mirrorExternalSlotRelease: vi.fn(),
}));
vi.mock("../../../lib/concurrency-limit", () => ({
  getEffectiveConcurrencyLimit: vi.fn(),
}));
vi.mock("../../../services/billing/credit_billing", () => ({
  billTeam: vi.fn(),
}));
vi.mock("../../../lib/browser-session-activity", () => ({
  enqueueBrowserSessionActivity: vi.fn(),
}));
vi.mock("../../../services/logging/log_job", () => ({ logRequest: vi.fn() }));
vi.mock("../../../lib/browser-billing", () => ({
  BROWSER_CREDITS_PER_HOUR: 120,
  INTERACT_CREDITS_PER_HOUR: 420,
  calculateBrowserSessionCredits: vi.fn(),
}));
vi.mock("../../../services/autumn/autumn.service", () => ({
  autumnService: { checkCredits: vi.fn() },
}));

import { browserListController } from "../browser";
import type { RequestWithAuth } from "../types";

function buildResponse() {
  const json = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status } as unknown as Response, status, json };
}

describe("browserListController", () => {
  it("returns the same stable configuration response as browser creation when browser service is absent", async () => {
    mockConfig.BROWSER_SERVICE_URL = undefined;
    const { res, status, json } = buildResponse();
    const req = {
      auth: { team_id: "local-team" },
      query: {},
    } as RequestWithAuth<{}, any, undefined>;

    await browserListController(req, res);

    expect(mockListBrowserSessions).not.toHaveBeenCalled();
    expect(status).toHaveBeenCalledWith(503);
    expect(json).toHaveBeenCalledWith({
      success: false,
      error:
        "Browser feature is not configured (BROWSER_SERVICE_URL is missing).",
    });
  });

  it("lists persisted browser sessions when browser service is configured", async () => {
    mockConfig.BROWSER_SERVICE_URL = "http://browser-service";
    mockListBrowserSessions.mockResolvedValueOnce([
      {
        id: "session-123",
        status: "active",
        cdp_url: "ws://browser/session-123",
        cdp_path: "https://view/session-123",
        cdp_interactive_path: "https://interactive/session-123",
        stream_web_view: true,
        created_at: "2026-08-16T00:00:00.000Z",
        updated_at: "2026-08-16T00:01:00.000Z",
      },
    ]);
    const { res, status, json } = buildResponse();
    const req = {
      auth: { team_id: "team-123" },
      query: {},
    } as RequestWithAuth<{}, any, undefined>;

    await browserListController(req, res);

    expect(mockListBrowserSessions).toHaveBeenCalledWith("team-123", {
      status: undefined,
    });
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({
      success: true,
      sessions: [
        {
          id: "session-123",
          status: "active",
          cdpUrl: "ws://browser/session-123",
          liveViewUrl: "https://view/session-123",
          interactiveLiveViewUrl: "https://interactive/session-123",
          streamWebView: true,
          createdAt: "2026-08-16T00:00:00.000Z",
          lastActivity: "2026-08-16T00:01:00.000Z",
        },
      ],
    });
  });
});
