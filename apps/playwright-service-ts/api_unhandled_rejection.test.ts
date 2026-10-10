import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import test from "node:test";

// AGENTIC-3758 (a): playwright-extra's stealth shim fires CDP calls it never
// awaits. When a page closes mid-call the rejection is unhandled, and Node's
// default policy exits the whole sidecar. Each case runs the entrypoint's
// handler in a child process, the only place a real unhandled rejection can
// be observed without taking the test runner down.
function runWithHandler(rejection: string) {
  const script = [
    'const { onUnhandledRejection } = require("./api");',
    'process.on("unhandledRejection", onUnhandledRejection);',
    `Promise.reject(${rejection});`,
    'setTimeout(() => console.log("sidecar-still-alive"), 100);',
  ].join("\n");
  return spawnSync(process.execPath, ["--import", "tsx", "-e", script], {
    cwd: __dirname,
    encoding: "utf8",
    timeout: 60_000,
  });
}

test("a closed-target rejection from the stealth shim is logged and survived", () => {
  const child = runWithHandler(
    'new Error("cdpSession.send: Target page, context or browser has been closed")',
  );
  assert.equal(child.status, 0, child.stderr);
  assert.match(child.stdout, /sidecar-still-alive/);
  assert.match(child.stderr, /Target page, context or browser has been closed/);
});

test("any other unhandled rejection still crashes the process", () => {
  const child = runWithHandler('new Error("unexpected failure")');
  assert.notEqual(child.status, 0);
  assert.doesNotMatch(child.stdout, /sidecar-still-alive/);
  assert.match(child.stderr, /unexpected failure/);
});
