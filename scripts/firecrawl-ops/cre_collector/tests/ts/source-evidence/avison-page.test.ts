import { test } from "node:test";
import assert from "node:assert/strict";
import { chmod, mkdtemp, readFile, readdir, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fetchAvisonYoungEntity } from "../../../sources/avison-young.js";
import { publishAvisonNativePage } from "../../../lib/source-evidence/avison-page.js";

const secret = "INTEGRATION_KEY_SAMPLE_X123";
const start = "2026-10-28T12:00:00.000Z";
const end = "2026-10-28T12:00:01.000Z";

test("captures website data before parsing and never records integration credential", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "avison-native-"));
  const oldFetch = globalThis.fetch;
  const keys = ["CRE_AVISON_NATIVE_CAPTURE_DIR", "CRE_AVISON_NATIVE_CAPTURE_SINGLE_WRITER"] as const;
  const previous = keys.map((key) => process.env[key]);
  t.after(() => {
    globalThis.fetch = oldFetch;
    keys.forEach((key, i) => { if (previous[i] === undefined) delete process.env[key]; else process.env[key] = previous[i]; });
  });
  process.env.CRE_AVISON_NATIVE_CAPTURE_DIR = dir;
  process.env.CRE_AVISON_NATIVE_CAPTURE_SINGLE_WRITER = "1";
  const rows = [{ id: 1, unknown: [null, 4], hide_price: 1 }, { id: 1, name: "No address" }];
  const text = JSON.stringify({ items: rows, errors: [{ detail: "partial" }], extra: true });
  globalThis.fetch = async () => new Response(text, { status: 200 });
  assert.deepEqual(await fetchAvisonYoungEntity("website", secret), rows);
  let names = await readdir(dir);
  assert.equal(names.length, 1);
  const entry = JSON.parse(await readFile(join(dir, names[0]!), "utf8"));
  assert.equal(Buffer.from(entry.responseBodyBase64, "base64").toString("utf8"), text);
  assert.equal(entry.receipt.httpOutcome, "success_status_unobserved");
  assert.equal(entry.provenance.bodyKind, "collector_decoded_text_reencoded_utf8_not_fetch_or_wire_bytes");
  assert.ok(!JSON.stringify(entry).includes(secret));
  assert.ok(!names[0]!.includes(secret));
  globalThis.fetch = async () => new Response("{invalid", { status: 200 });
  await assert.rejects(fetchAvisonYoungEntity("website", secret), SyntaxError);
  names = await readdir(dir);
  assert.equal(names.length, 2);
  globalThis.fetch = async () => new Response('{"items":[]}', { status: 200 });
  await assert.rejects(fetchAvisonYoungEntity("website", secret), /returned no items/);
  assert.equal((await readdir(dir)).length, 3);
  await fetchAvisonYoungEntity("team_member", secret).catch(() => {});
  assert.equal((await readdir(dir)).length, 3);
});

test("private capture refuses unsafe custody, credential-bearing body and oversize", async () => {
  const dir = await mkdtemp(join(tmpdir(), "avison-native-"));
  const old = process.env.CRE_AVISON_NATIVE_CAPTURE_SINGLE_WRITER;
  process.env.CRE_AVISON_NATIVE_CAPTURE_SINGLE_WRITER = "1";
  try {
    assert.throws(() => publishAvisonNativePage(dir, secret, secret, start, end), (error: unknown) => {
      assert.equal((error as Error).message, "Avison Young native capture failed");
      assert.ok(!(error as Error).message.includes(secret));
      return true;
    });
    const escaped = Array.from(secret, (char) => "\\u" + char.charCodeAt(0).toString(16).padStart(4, "0")).join("");
    assert.throws(() => publishAvisonNativePage(dir, escaped, secret, start, end), /capture failed/);
    const mixed = secret.slice(0, 4) + "\\u" + secret.charCodeAt(4).toString(16).padStart(4, "0") + secret.slice(5);
    assert.throws(() => publishAvisonNativePage(dir, mixed, secret, start, end), /capture failed/);
    assert.throws(() => publishAvisonNativePage(dir, "a".repeat(16 * 1024 * 1024 + 1), secret, start, end), /capture failed/);
    const alias = dir + "-alias"; await symlink(dir, alias);
    assert.throws(() => publishAvisonNativePage(alias, "{}", secret, start, end), /capture failed/);
    await chmod(dir, 0o755);
    assert.throws(() => publishAvisonNativePage(dir, "{}", secret, start, end), /capture failed/);
    await chmod(dir, 0o700);
    assert.deepEqual(await readdir(dir), []);
  } finally {
    if (old === undefined) delete process.env.CRE_AVISON_NATIVE_CAPTURE_SINGLE_WRITER;
    else process.env.CRE_AVISON_NATIVE_CAPTURE_SINGLE_WRITER = old;
  }
});

test("quota refusal preserves a previous complete capture and exact replay", async () => {
  const dir = await mkdtemp(join(tmpdir(), "avison-native-"));
  const keys = ["CRE_AVISON_NATIVE_CAPTURE_SINGLE_WRITER", "CRE_AVISON_NATIVE_CAPTURE_MAX_PAGES"] as const;
  const old = keys.map((key) => process.env[key]);
  process.env.CRE_AVISON_NATIVE_CAPTURE_SINGLE_WRITER = "1";
  process.env.CRE_AVISON_NATIVE_CAPTURE_MAX_PAGES = "1";
  try {
    const first = publishAvisonNativePage(dir, '{"items":[{"id":1}]}', secret, start, end);
    assert.equal(publishAvisonNativePage(dir, '{"items":[{"id":1}]}', secret, start, end), first);
    assert.throws(() => publishAvisonNativePage(dir, '{"items":[{"id":2}]}', secret, start, end), /capture failed/);
    assert.equal((await readdir(dir)).length, 1);
  } finally {
    keys.forEach((key, i) => { if (old[i] === undefined) delete process.env[key]; else process.env[key] = old[i]; });
  }
});
