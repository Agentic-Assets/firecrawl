import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright-extra";
import type { Browser, BrowserContext, Request, Route } from "playwright";
import { createBrowserPool } from "./browser_context";
import { assertSafeTargetUrl } from "./target_guard";

// Exact production factory registrations, with only Chromium I/O mocked.
// Playwright 1.58.1: latest registered matching route runs first; continue
// ends dispatch, fallback visits earlier matching handlers. No network exists.
async function registered(blockMedia: boolean) {
  const routes: Array<{
    pattern: string;
    handler: (route: Route, request: Request) => unknown;
  }> = [];
  const checked: string[] = [];
  const context = {
    addInitScript: async () => {},
    route: async (
      pattern: string,
      handler: (route: Route, request: Request) => unknown,
    ) => { routes.push({ pattern, handler }); },
    close: async () => {},
  };
  const browser = {
    newContext: async () => context as unknown as BrowserContext,
    close: async () => {},
  };
  const original = chromium.launch;
  chromium.launch = async () => browser as unknown as Browser;
  const pool = createBrowserPool({
    blockMedia,
    ssrfProxyPort: 1,
    assertSafeTargetUrl: async (url, allow) => {
      checked.push(url);
      await assertSafeTargetUrl(url, allow);
    },
  });
  try {
    await pool.initializeBrowser();
    const bundle = await pool.createContext({allowLocalTargets: false});
    return {routes, checked, bundle, pool};
  } finally { chromium.launch = original; }
}

async function dispatch(state: Awaited<ReturnType<typeof registered>>, url: string) {
  let result: string | undefined;
  const visited: string[] = [];
  const request = {
    url: () => url,
    isNavigationRequest: () => true,
    frame: () => ({ parentFrame: () => null }),
  };
  const run = async (index: number): Promise<void> => {
    if (index < 0) { result = "continued"; return; }
    const {pattern, handler} = state.routes[index];
    // Fixture URLs deliberately exercise exact **/* and *.png matches only.
    const matches = pattern === "**/*"
      || (pattern.includes("png") && url.endsWith(".png"));
    if (!matches) return run(index - 1);
    visited.push(pattern);
    await handler({
      abort: async () => { result = "aborted"; },
      continue: async () => { result = "continued"; },
      fallback: async () => run(index - 1),
    } as unknown as Route, request as unknown as Request);
  };
  await run(state.routes.length - 1);
  return {result, visited};
}

test("BLOCK_MEDIA=true aborts the registered image route after SSRF validation", async () => {
  const state = await registered(true);
  try {
    assert.equal(state.routes.length, 2);
    assert.equal(state.routes[1].pattern, "**/*");
    const url = "https://8.8.8.8/owned-fixture.png";
    const result = await dispatch(state, url);
    assert.deepEqual(state.checked, [url]);
    assert.equal(result.result, "aborted", JSON.stringify(result));
  } finally { await state.pool.close(); }
});

test("default media-off behavior permits an image through the actual guard", async () => {
  const state = await registered(false);
  try {
    assert.equal(state.routes.length, 1);
    assert.equal(
      (await dispatch(state, "https://8.8.8.8/owned-fixture.png")).result,
      "continued",
    );
    assert.equal(state.checked.length, 1);
  } finally { await state.pool.close(); }
});

test("media-on retains ordinary safe document/script dispatch", async () => {
  const state = await registered(true);
  try {
    for (const name of ["document.html", "script.js"]) {
      assert.equal((await dispatch(state, `https://8.8.8.8/${name}`)).result, "continued");
    }
    assert.equal(state.checked.length, 2);
  } finally { await state.pool.close(); }
});

test("actual SSRF guard still rejects private image and navigation", async () => {
  const state = await registered(true);
  try {
    for (const name of ["image.png", "document.html"]) {
      assert.equal((await dispatch(state, `http://127.0.0.1/${name}`)).result, "aborted");
    }
    assert.equal(state.checked.length, 2);
    assert.equal(
      state.bundle.securityState.blockedNavigationRequestUrl,
      "http://127.0.0.1/document.html",
    );
  } finally { await state.pool.close(); }
});
