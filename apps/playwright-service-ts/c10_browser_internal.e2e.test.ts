/** Local-only integration: launches the real private Express listener and Chromium. */
import assert from "node:assert/strict";
import { createHash, createHmac } from "node:crypto";
import { execFileSync, spawn } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { createServer } from "node:https";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { issueC10SidecarCapability, type C10SidecarInput } from "./c10_browser_internal";

const secret = "c10-e2e-secret-material-that-is-more-than-thirty-two-bytes";
const canonical = (value: unknown): unknown => {
  if (value === null || typeof value === "string" || typeof value === "boolean" || typeof value === "number") return value;
  if (Array.isArray(value)) return value.map(canonical);
  const result: Record<string, unknown> = {};
  for (const key of Object.keys(value as Record<string, unknown>).sort()) result[key] = canonical((value as Record<string, unknown>)[key]);
  return result;
};
const sha = (value: unknown) => createHash("sha256").update(JSON.stringify(canonical(value))).digest("hex");
const tokenSha = (arm: string, id: string) => createHash("sha256").update(createHmac("sha256", secret).update(["cre-capacity-c10-browser-arm-v1", "jll", arm, id].join("\0")).digest("hex")).digest("hex");
const port = () => 39000 + Math.floor(Math.random() * 1000);

test("private C10 route consumes one signed capability, enforces SSRF/body caps/deadline, and cleans pages", { timeout: 90_000 }, async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "c10-e2e-"));
  const cert = join(directory, "cert.pem"), key = join(directory, "key.pem");
  execFileSync("openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-subj", "/CN=127.0.0.1", "-days", "1"], { stdio: "ignore" });
  let targetCalls = 0;
  const target = createServer({ key: await readFile(key), cert: await readFile(cert) }, (req, res) => {
    targetCalls += 1;
    if (req.url === "/large") return res.end("x".repeat(4096));
    if (req.url === "/slow") return setTimeout(() => res.end('{"slow":true}'), 250);
    res.setHeader("content-type", "application/json"); res.end('{"ok":true}');
  });
  await new Promise<void>((resolve) => target.listen(0, "127.0.0.1", resolve));
  const targetAddress = target.address();
  if (!targetAddress || typeof targetAddress === "string") throw new Error("target unavailable");
  const privatePort = port(), publicPort = port();
  const child = spawn(process.execPath, ["--import", "tsx", "api.ts"], { cwd: process.cwd(), env: {
    ...process.env, NODE_ENV: "test", PORT: String(publicPort), C10_BROWSER_INTERNAL_PORT: String(privatePort),
    C10_BROWSER_INTERNAL_SECRET: secret, C10_BROWSER_INTERNAL_ALLOW_TEST_LOCAL_TARGETS: "true", ALLOW_LOCAL_WEBHOOKS: "True",
  }, stdio: "ignore" });
  t.after(async () => { child.kill("SIGINT"); await new Promise((resolve) => child.once("exit", resolve)); await new Promise<void>((resolve) => target.close(() => resolve())); await rm(directory, { recursive: true, force: true }); });
  const privateBase = `http://127.0.0.1:${privatePort}`;
  for (let tries = 0; tries < 100; tries += 1) { try { if ((await fetch(`${privateBase}/health`)).ok) break; } catch {} await new Promise((resolve) => setTimeout(resolve, 100)); }
  const host = `127.0.0.1:${targetAddress.port}`;
  const card = { id: "card-1", sourceKey: "jll", stage: "member" as const, method: "GET" as const, url: `https://${host}/`, allowedHost: host, headers: {}, contentType: null, body: null, browserBootstrapUrl: `https://${host}/`, cacheMode: "no-store" as const, timeoutMs: 10_000, maxBytes: 1024, bodySha256: null };
  const input: C10SidecarInput = { sourceKey: "jll", armSha256: "a".repeat(64), tokenId: "e2e-token", tokenSha256: tokenSha("a".repeat(64), "e2e-token"), cardSha256: sha(card), card };
  const capability = issueC10SidecarCapability(secret, input);
  const call = (body: C10SidecarInput, authorization: string) => fetch(`${privateBase}/internal/c10/browser-execute`, { method: "POST", headers: { "content-type": "application/json", "x-c10-browser-authorization": authorization }, body: JSON.stringify(body) });
  const first = await call(input, capability);
  assert.equal(first.status, 200);
  const evidence = await first.json() as { cacheRead: boolean; cacheWrite: boolean; engineAttempts: number; evidenceSignature: string };
  assert.equal(evidence.engineAttempts, 1); assert.equal(evidence.cacheRead, false); assert.equal(evidence.cacheWrite, false); assert.match(evidence.evidenceSignature, /^[0-9a-f]{64}$/);
  assert.equal((await call(input, capability)).status, 404);
  assert.equal(targetCalls, 2); // one bootstrap navigation plus the one browser fetch operation
  const publicHealth = await fetch(`http://127.0.0.1:${publicPort}/health`);
  assert.equal((await publicHealth.json() as { activePages: number }).activePages, 0);
  const largeCard = { ...card, id: "card-2", url: `https://${host}/large`, browserBootstrapUrl: `https://${host}/`, maxBytes: 64 };
  const large = { ...input, card: largeCard, cardSha256: sha(largeCard) };
  assert.equal((await call(large, issueC10SidecarCapability(secret, large))).status, 502);
  const slowCard = { ...card, id: "card-3", url: `https://${host}/slow`, browserBootstrapUrl: `https://${host}/`, timeoutMs: 50 };
  const slow = { ...input, card: slowCard, cardSha256: sha(slowCard) };
  assert.equal((await call(slow, issueC10SidecarCapability(secret, slow))).status, 502);
  // Test-only local capability permits the fixture target; a production start lacks this env gate.
  assert.equal((await fetch(`http://127.0.0.1:${publicPort}/internal/c10/browser-execute`, { method: "POST" })).status, 404);
  const health = await fetch(`${privateBase}/health`); assert.equal(health.status, 200);
});
