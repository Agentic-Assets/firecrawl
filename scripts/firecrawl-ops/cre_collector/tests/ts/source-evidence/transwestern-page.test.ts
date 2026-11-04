import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, readFileSync, chmodSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { srcTranswestern } from "../../../sources/transwestern.js";
import { publishTranswesternParsedPage } from "../../../lib/source-evidence/transwestern-page.js";
const prefix = "CRE_TRANSWESTERN_NATIVE_CAPTURE";
const keys = ["DIR", "SINGLE_WRITER", "MAX_PAGES", "MAX_BYTES"].map((x) => `${prefix}_${x}`);
function clear() { for (const key of keys) delete process.env[key]; }
function setup() { clear(); const dir = mkdtempSync(join(tmpdir(), "tw-native-")); chmodSync(dir, 0o700);
  process.env[`${prefix}_DIR`] = dir; process.env[`${prefix}_SINGLE_WRITER`] = "1"; return dir; }
function bodies(dir: string) { return readdirSync(dir).map((n) => {
  const entry = JSON.parse(readFileSync(join(dir, n), "utf8"));
  return { ...entry, body: JSON.parse(Buffer.from(entry.responseBodyBase64, "base64").toString()) };
}); }
test("disabled capture has no side effect, scope and limits fail closed", () => {
  clear(); publishTranswesternParsedPage([], "invalid", "invalid", 0, false);
  const dir = setup(); try {
    assert.throws(() => publishTranswesternParsedPage([], "sale", "Lease", 0, false));
    process.env[`${prefix}_MAX_PAGES`] = "1";
    publishTranswesternParsedPage([null], "sale", "Sale", 0, false);
    assert.throws(() => publishTranswesternParsedPage([], "sale", "Sale or Lease", 1, false));
    assert.equal(readdirSync(dir).length, 1);
  } finally { clear(); rmSync(dir, { recursive: true, force: true }); }
});
test("identity failure and later non-array failure keep acquired buckets", async () => {
  const dir = setup(); try {
    await assert.rejects(srcTranswestern("sale", 1, true, async () => [{ PageUrl: "" }, { unknown: 9 }]));
    assert.equal(bodies(dir).length, 1); assert.equal(bodies(dir)[0].body.length, 2);
    rmSync(dir, { recursive: true, force: true });
  } finally { clear(); rmSync(dir, { recursive: true, force: true }); }
  const dir2 = setup(); let calls = 0;
  try {
    await assert.rejects(srcTranswestern("sale", 1, true, async () => ++calls === 1
      ? [{ PageUrl: "valid-path", extra: 1 }, { PageUrl: "valid-path", extra: 2 }] : { unexpected: [null, false] }));
    const captured = bodies(dir2).sort((a,b) => a.receipt.occurrence - b.receipt.occurrence);
    assert.equal(captured.length, 2); assert.equal(captured[0].body.length, 2);
    assert.deepEqual(captured[1].body, { unexpected: [null, false] });
    assert.equal(captured[0].receipt.freshRequested, false);
  } finally { clear(); rmSync(dir2, { recursive: true, force: true }); }
});
test("unsafe JSON values fail without mutating or deleting an earlier bucket", async () => {
  for (const invalid of [JSON.parse('1e309'), JSON.parse('-0'), { missing: undefined }]) {
    const dir = setup(); let calls = 0;
    try {
      await assert.rejects(srcTranswestern("sale", 1, true, async () => ++calls === 1
        ? [{ PageUrl: "valid-path", zero: 0 }] : [invalid]), /native capture failed/);
      const captured = bodies(dir);
      assert.equal(captured.length, 1);
      assert.equal(captured[0].body[0].zero, 0);
      assert.equal(Object.is(captured[0].body[0].zero, -0), false);
    } finally { clear(); rmSync(dir, { recursive: true, force: true }); }
  }
});
