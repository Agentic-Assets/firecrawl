import test from "node:test";
import assert from "node:assert/strict";
import {
  buildoutInventoryUrl,
  envBool,
  envInt,
  buildoutCacheSlug,
  buildoutPageCachePath,
  buildoutPageWindow,
} from "../../../sources/buildout.js";

const ENV_KEYS = ["BUILDOUT_PAGE_START", "BUILDOUT_PAGE_END", "BUILDOUT_CACHE_DIR"] as const;

function clearEnv(keys: readonly string[]): void {
  for (const key of keys) delete process.env[key];
}

test("buildoutInventoryUrl includes plugin key and page", () => {
  assert.equal(
    buildoutInventoryUrl("abc123plugin", 7),
    "https://buildout.com/plugins/abc123plugin/inventory.json?page=7"
  );
});

test("envBool recognizes truthy string values", () => {
  process.env.TEST_BUILDOUT_BOOL = "true";
  assert.equal(envBool("TEST_BUILDOUT_BOOL"), true);
  process.env.TEST_BUILDOUT_BOOL = "YES";
  assert.equal(envBool("TEST_BUILDOUT_BOOL"), true);
  process.env.TEST_BUILDOUT_BOOL = "0";
  assert.equal(envBool("TEST_BUILDOUT_BOOL"), false);
  delete process.env.TEST_BUILDOUT_BOOL;
});

test("envInt parses integers and rejects invalid input", () => {
  process.env.TEST_BUILDOUT_INT = "42";
  assert.equal(envInt("TEST_BUILDOUT_INT"), 42);
  process.env.TEST_BUILDOUT_INT = "-3";
  assert.equal(envInt("TEST_BUILDOUT_INT"), 0);
  process.env.TEST_BUILDOUT_INT = "nope";
  assert.equal(envInt("TEST_BUILDOUT_INT"), null);
  delete process.env.TEST_BUILDOUT_INT;
  assert.equal(envInt("TEST_BUILDOUT_INT"), null);
});

test("buildoutCacheSlug slugifies company or uses override", () => {
  assert.equal(buildoutCacheSlug("Lee & Associates", "plugin-key", {}), "lee-and-associates");
  assert.equal(buildoutCacheSlug("SVN", "plugin-key", { cacheSlug: "svn-custom" }), "svn-custom");
});

test("buildoutPageCachePath uses cache dir and padded page", () => {
  process.env.BUILDOUT_CACHE_DIR = "/tmp/buildout-cache-test";
  const path = buildoutPageCachePath("SVN", "plugin-key", 3, {});
  assert.equal(path, "/tmp/buildout-cache-test/svn/page-0003.json");
  delete process.env.BUILDOUT_CACHE_DIR;
});

test("buildoutPageWindow returns null when env unset", () => {
  clearEnv(ENV_KEYS);
  assert.equal(buildoutPageWindow(100), null);
});

test("buildoutPageWindow clamps to page bounds", () => {
  clearEnv(ENV_KEYS);
  process.env.BUILDOUT_PAGE_START = "5";
  process.env.BUILDOUT_PAGE_END = "12";
  assert.deepEqual(buildoutPageWindow(20), { start: 5, end: 12 });
  clearEnv(ENV_KEYS);
});

test("buildoutPageWindow throws on inverted range", () => {
  clearEnv(ENV_KEYS);
  process.env.BUILDOUT_PAGE_START = "10";
  process.env.BUILDOUT_PAGE_END = "2";
  assert.throws(() => buildoutPageWindow(20), /invalid Buildout page window/);
  clearEnv(ENV_KEYS);
});
