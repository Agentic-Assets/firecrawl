import { strict as assert } from "node:assert";
import { test } from "node:test";
import { spawnSync } from "node:child_process";
import { resolveNewmarkDetailLink } from "../../../sources/pure/newmark-detail-link.js";
import { newmarkNimCanonicalIdentity as sourceIdentity } from "../../../sources/newmark.js";
import { newmarkNimCanonicalIdentity as leafIdentity } from "../../../sources/pure/newmark-nim-identity.js";

function claim(index = 1) {
  const slug = `property-${index}`;
  return { sourceKey: "newmark", externalId: slug, url: `https://www.nmrk.com/properties/${slug}`,
    sourcePublishedDetail: { status: "observed", schema: "newmark-nim-link-witness/v1", sourceKey: "newmark",
      sourcePath: "raw_data.rawNewmarkNimRecord", recordId: `native-${index}`, slug,
      url: `https://my.rcm1.com/handler/modern.aspx?pv=opaque_${index}`,
      inventoryObservedAt: "2026-10-29T12:34:56.123456Z", sourceName: {state:"value",value:"Sample"},
      sourceProperty: {selection:"unresolved",fields:{}} } };
}

test("more than 50 independent witnesses retain literal route and unverified source clock", () => {
  for (let i = 0; i < 80; i++) {
    const item = claim(i); const resolved = resolveNewmarkDetailLink(item);
    assert.deepEqual(resolved, { state: "resolved", pageUrl: item.sourcePublishedDetail.url,
      claimUrl: item.url, sourceClock: item.sourcePublishedDetail.inventoryObservedAt,
      identity: "unverified", sourceClockStatus: "unverified" });
  }
  const item = claim(); item.sourcePublishedDetail.inventoryObservedAt = null as any;
  const result = resolveNewmarkDetailLink(item);
  assert.equal(result.state, "resolved");
  if (result.state === "resolved") assert.equal(result.sourceClock, null);
});

test("untrusted or mismatched witness stays unresolved", () => {
  const original = claim();
  const mutations: unknown[] = [
    { ...original, sourceKey: "other" }, { ...original, externalId: "other" },
    { ...original, url: original.url + "/" }, { ...original, sourcePublishedDetail: undefined },
    { ...original, sourcePublishedDetail: { status: "unresolved", reason: "unsupported_source_fields" } },
    ...[
      { sourceKey: "other" }, { status: "unresolved" }, { schema: "other" }, { sourcePath: "other" },
      { recordId: "" }, { recordId: "x".repeat(257) }, { recordId: 1 }, { slug: "other" },
      { slug: ".." }, { url: "" }, { url: "https://my.rcm1.com/handler/modern.aspx?pv=a&pv=b" },
      { url: "https://my.rcm1.com/handler/modern.aspx?pv=a&token=b" },
      { url: "https://user@my.rcm1.com/handler/modern.aspx?pv=a" },
      { url: "https://my.rcm1.com:443/handler/modern.aspx?pv=a" },
      { url: "https://my.rcm1.com/handler/modern.aspx?pv=a#fragment" },
      { url: "https://my.rcm1.com/buyer/agreement?pv=a" },
      { url: "https://my.rcm1.com/handler/modern.aspx?pv=%61" },
      { url: "https://my.rcm1.com/handler/modern.aspx?pv=" + "a".repeat(513) },
      { url: "https://127.0.0.1/handler/modern.aspx?pv=a" },
      { inventoryObservedAt: "2026-02-30T00:00:00Z" },
      { inventoryObservedAt: "2026-10-29T12:00:00+01:00" },
      { inventoryObservedAt: undefined }, { unexpected: false },
    ].map(change => ({ ...original, sourcePublishedDetail: { ...original.sourcePublishedDetail, ...change } })),
  ];
  for (const item of mutations) assert.deepEqual(resolveNewmarkDetailLink(item as any), { state: "unresolved" });
});

test("identity leaf preserves the existing source export and vectors", () => {
  assert.equal(sourceIdentity, leafIdentity);
  for (const row of [
    {slug:"alpha", externalWebsiteUrl:"https://my.rcm1.com/handler/modern.aspx?pv=opaque"},
    {slug:"alpha", externalWebsiteUrl:"https://www.nmrk.com/properties/alpha"},
    {slug:"alpha", externalWebsiteUrl:null}, {slug:null, externalWebsiteUrl:null},
  ]) assert.deepEqual(sourceIdentity(row), leafIdentity(row));
  for (const row of [{slug:"..",externalWebsiteUrl:null}, {slug:"alpha",externalWebsiteUrl:"https://foreign.example/a"}]) {
    assert.throws(() => leafIdentity(row));
    assert.throws(() => sourceIdentity(row));
  }
});

test("pure resolver and identity leaf import without config parsing or network", () => {
  const code = `globalThis.fetch = () => { throw new Error('unexpected fetch'); };
    await import('./sources/pure/newmark-detail-link.ts');
    await import('./sources/pure/newmark-nim-identity.ts');`;
  const child = spawnSync(process.execPath, ["--import", "tsx", "--input-type=module", "-e", code, "--", "placeholder", "--invalid-collector-flag"],
    { cwd: process.cwd(), encoding: "utf8", timeout: 15000 });
  assert.equal(child.status, 0, child.stderr);
});
