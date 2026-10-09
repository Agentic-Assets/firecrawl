import type { Response } from "express";
import { describe, expect, it, vi } from "vitest";

const { mockConfig, mockListBrowserSessions } = vi.hoisted(() => ({
  mockConfig: { HANGAR_URL: undefined as string | undefined },
  mockListBrowserSessions: vi.fn(),
}));

vi.mock("../../../config", () => ({ config: mockConfig }));
vi.mock("../../../lib/browser-sessions", () => ({
  deleteBrowserProfile: vi.fn(),
  getBrowserSession: vi.fn(),
  listBrowserSessions: mockListBrowserSessions,
  updateBrowserSessionActivity: vi.fn(),
}));
vi.mock("../../../lib/hangar", () => ({
  deleteHangarProfile: vi.fn(),
  executeHangarBrowser: vi.fn(),
  getHangarBrowser: vi.fn(),
  getHangarRecording: vi.fn(),
  HangarError: class HangarError extends Error {},
}));
vi.mock("../../../lib/browser-lifecycle", () => ({
  createBrowserSession: vi.fn(),
  getBrowserZDR: vi.fn(),
  BrowserSessionError: class BrowserSessionError extends Error {},
  browserSessionLinks: (session: {
    cdp_url: string;
    cdp_path: string | null;
    cdp_interactive_path: string | null;
  }) => ({
    cdpUrl: session.cdp_url,
    liveViewUrl: session.cdp_path ?? "",
    interactiveLiveViewUrl: session.cdp_interactive_path ?? "",
  }),
  stopBrowserSession: vi.fn(),
  settleBrowserSession: vi.fn(),
}));
vi.mock("../../../lib/browser-session-activity", () => ({
  enqueueBrowserSessionActivity: vi.fn(),
}));
vi.mock("../../../lib/keyless", () => ({
  KEYLESS_FREE_TIER_LIMIT_MESSAGE: "keyless limit",
  keylessLimitPromptForTeam: vi.fn(),
}));
vi.mock("../../../lib/agent-interop", () => ({
  isAgentInteropSecretValid: vi.fn(),
}));

import { browserListController } from "../browser";
import type { RequestWithAuth } from "../types";

function buildResponse() {
  const json = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status, json } as unknown as Response, status, json };
}

describe("browserListController", () => {
  it("returns the same stable configuration response as browser creation when the browser service is absent", async () => {
    mockConfig.HANGAR_URL = undefined;
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
      error: "Browser feature is not configured (HANGAR_URL is missing).",
    });
  });

  it("lists persisted browser sessions when the browser service is configured", async () => {
    mockConfig.HANGAR_URL = "http://hangar";
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
    expect(status).not.toHaveBeenCalled();
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
