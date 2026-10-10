import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import test from "node:test";

// AGENTIC-3758 (a): the unhandledRejection policy must be installed by the
// production entrypoint, not just exist as an exported function. Running
// api.ts as the main module with an invalid SCRAPE_START_INTERVAL_MS makes
// runService throw during configuration, after it has registered the process
// handlers and before it starts a proxy, browser, or listener. A preload
// reports the registered listeners from the exit event.
test("runService registers onUnhandledRejection before any service starts", () => {
  const preload = [
    'process.on("exit", () => {',
    '  const names = process.listeners("unhandledRejection").map((fn) => fn.name);',
    '  console.log("LISTENERS:" + JSON.stringify(names));',
    "});",
  ].join("\n");
  const child = spawnSync(
    process.execPath,
    [
      "--import",
      `data:text/javascript,${encodeURIComponent(preload)}`,
      "--import",
      "tsx",
      "./api.ts",
    ],
    {
      cwd: __dirname,
      encoding: "utf8",
      timeout: 60_000,
      env: { ...process.env, SCRAPE_START_INTERVAL_MS: "-1" },
    },
  );
  assert.notEqual(child.status, 0, "invalid config must stop the entrypoint");
  assert.match(child.stderr, /SCRAPE_START_INTERVAL_MS must be an integer/);
  assert.doesNotMatch(child.stdout, /Server is running/);
  assert.match(child.stdout, /LISTENERS:\["onUnhandledRejection"\]/);
});
