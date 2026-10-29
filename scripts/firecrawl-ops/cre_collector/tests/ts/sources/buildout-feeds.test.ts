import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { allBuildoutFeeds, buildoutFeedFor, registeredBuildoutTuples } from "../../../sources/buildout-feeds/index.js";
import { franklinStreetBuildoutConfig } from "../../../sources/franklin-street.js";

test("shared registry preserves 25 registered tuples and exposes 29 distinct feeds", () => {
  const original = JSON.parse(readFileSync(new URL("../../../data/cre-buildout-sources.json", import.meta.url), "utf8"));
  assert.deepEqual(registeredBuildoutTuples, original);
  assert.equal(allBuildoutFeeds.length, 29);
  assert.equal(new Set(allBuildoutFeeds.map((feed) => feed.sourceKey)).size, 28);
  assert.equal(new Set(allBuildoutFeeds.map((feed) => feed.pluginKey)).size, 29);
  assert.equal(new Set(allBuildoutFeeds.map((feed) => feed.feedKey)).size, 29);
  assert.equal(buildoutFeedFor("franklin-street"), null);
  assert.equal(buildoutFeedFor("svn", "lee-associates"), null);
  assert.equal(buildoutFeedFor("unknown"), null);
});

test("core SVN and Lee bindings exactly match the existing collection routes", () => {
  assert.deepEqual(buildoutFeedFor("svn"), {
    sourceKey: "svn", company: "SVN", pluginKey: "b933480474026c41d248b77156c84aef37dcac68",
    listingsPage: "https://svn.com/properties/", detailHost: "svn.com", feedKey: "svn",
  });
  assert.deepEqual(buildoutFeedFor("lee-associates"), {
    sourceKey: "lee-associates", company: "Lee & Associates", pluginKey: "9a64a93980aeae8db347e72cdfa8ca61017acc9a",
    listingsPage: "https://www.lee-associates.com/properties/", detailHost: "www.lee-associates.com", feedKey: "lee-associates",
  });
});

test("Franklin sale and lease maintain distinct public feeds and original pacing", () => {
  for (const [tx, key] of [["sale", "a234450b432b2b2bebc1ace7e6f692e4489bde70"],
    ["lease", "2f82fcd26667c4b0126d0084938ffa265f05fa4a"]] as const) {
    const feedKey = `franklin-street-${tx}`;
    const feed = buildoutFeedFor("franklin-street", feedKey);
    assert.equal(feed?.pluginKey, key);
    assert.equal(feed?.feedKey, feedKey);
    const config = franklinStreetBuildoutConfig(tx);
    assert.deepEqual(config, {
      company: "Franklin Street", pluginKey: key, listingsPage: "https://www.franklinst.com/properties/",
      opts: { preferDirectJson: true, inventorySort: "created_at asc, id asc", directReferer: "https://www.franklinst.com/properties/",
        pageConcurrency: 1, requireCompletePages: true, cacheSlug: feedKey, usePageCache: true,
        recoveryPasses: 1, recoveryCooldownMs: 15000, maxRecoveryPages: 30 },
    });
  }
});
