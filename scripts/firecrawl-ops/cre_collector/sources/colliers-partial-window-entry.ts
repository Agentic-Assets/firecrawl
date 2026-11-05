/** Internal child of the lock-owning checkpoint. No database authority. */
import { execFileSync } from "node:child_process";
import { closeSync, constants, fstatSync, fsyncSync, lstatSync, openSync, readFileSync, writeSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { buildColliersWindowArtifact } from "./colliers-partial-window-worker.js";
import { fetchColliersPartialDetail } from "./colliers-partial-fetch.js";

function privateText(path: string, maximum: number) {
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const before = fstatSync(fd, { bigint: true });
    if (!before.isFile() || typeof process.getuid !== "function" || before.uid !== BigInt(process.getuid()) ||
        before.nlink !== 1n || (before.mode & 0o077n) !== 0n || before.size > BigInt(maximum))
      throw new Error("private checkpoint input invalid");
    const text = readFileSync(fd, "utf8");
    const after = fstatSync(fd, { bigint: true }), named = lstatSync(path, { bigint: true });
    const identity = (s: typeof before) => [s.dev,s.ino,s.size,s.mtimeNs,s.ctimeNs,s.mode,s.uid,s.nlink].join(":");
    if (identity(before) !== identity(after) || identity(before) !== identity(named)) throw new Error("checkpoint input changed");
    return text;
  } finally { closeSync(fd); }
}

function writeReceipt(path: string, value: unknown) {
  const bytes = Buffer.from(JSON.stringify(value) + "\n");
  if (bytes.length > 16384) throw new Error("worker receipt exceeds bound");
  const fd = openSync(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try {
    const state = fstatSync(fd);
    if (!state.isFile() || state.uid !== process.getuid?.() || state.nlink !== 1 || (state.mode & 0o077) !== 0)
      throw new Error("worker receipt invalid");
    let at = 0;
    while (at < bytes.length) { const n = writeSync(fd, bytes, at, bytes.length - at); if (n <= 0) throw new Error("receipt write failed"); at += n; }
    fsyncSync(fd);
  } finally { closeSync(fd); }
  const dir = openSync(dirname(path), constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
  try { fsyncSync(dir); } finally { closeSync(dir); }
}

async function main() {
  const requestPath = process.env.COLLIERS_PARTIAL_WORKER_REQUEST;
  if (!requestPath || !requestPath.startsWith("/")) throw new Error("worker request missing");
  const request = JSON.parse(privateText(requestPath, 16384));
  const expected = ["attempt", "cursor", "finishedAt", "generation", "generationStartedAt", "lockLease",
    "originalSourceMainSha256", "ownerPid", "producerSha", "runDir", "sourceBytes", "sourceSha256",
    "startedAt", "targetSha256"].sort().join(",");
  if (!request || typeof request !== "object" || Array.isArray(request) || Object.keys(request).sort().join(",") !== expected ||
      !Number.isSafeInteger(request.ownerPid) || request.ownerPid !== process.ppid ||
      !Number.isSafeInteger(request.attempt) || request.attempt < 1 || !request.runDir.startsWith("/") ||
      basename(request.runDir) !== request.generation || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(request.generation) ||
      !/^[A-Za-z0-9_-]{43}$/.test(request.lockLease))
    throw new Error("worker request invalid");
  const runDir = resolve(request.runDir), partialDir = join(runDir, "colliers-partial");
  for (const path of [runDir, partialDir]) {
    const info = lstatSync(path);
    if (!info.isDirectory() || info.isSymbolicLink() || info.uid !== process.getuid?.() ||
        (path === partialDir && (info.mode & 0o077) !== 0)) throw new Error("worker directory invalid");
  }
  if (requestPath !== join(partialDir, `window-${request.attempt}-request.json`)) throw new Error("worker request path differs");
  const root = resolve(process.cwd(), "../../..");
  const common = execFileSync("git", ["rev-parse", "--path-format=absolute", "--git-common-dir"],
    { cwd: root, encoding: "utf8", timeout: 10000 }).trim();
  const lock = join(dirname(common), "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock");
  const owner = () => {
    const dir = lstatSync(lock);
    if (!dir.isDirectory() || dir.isSymbolicLink() || dir.uid !== process.getuid?.() || (dir.mode & 0o077) !== 0 ||
        Number(privateText(join(lock, "pid"), 128).trim().split(/\s+/)[0]) !== request.ownerPid ||
        privateText(join(lock, "lease"), 256).trim() !== request.lockLease || process.ppid !== request.ownerPid)
      throw new Error("checkpoint lock ownership changed");
  };
  const code = () => {
    const sha = execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8", timeout: 10000 }).trim();
    const dirty = execFileSync("git", ["status", "--porcelain", "--untracked-files=normal"],
      { cwd: root, encoding: "utf8", timeout: 10000 });
    if (sha !== request.producerSha || dirty.trim()) throw new Error("collector code changed");
  };
  owner(); code();
  const sourcePath = join(partialDir, `attempt-${request.attempt}.jsonl`);
  const evidencePath = join(partialDir, `window-${request.attempt}-evidence.jsonl`);
  const artifactPath = join(partialDir, `window-${request.attempt}-artifact.json`);
  const result = await buildColliersWindowArtifact({ ...request, sourcePath, evidencePath, artifactPath },
    async token => { owner(); const value = await fetchColliersPartialDetail(token); owner(); return value; });
  owner(); code();
  writeReceipt(join(partialDir, `window-${request.attempt}-receipt.json`),
    { schema: "colliers-window-worker-receipt/v1", generation: request.generation, attempt: request.attempt,
      producerSha: request.producerSha, targetSha256: request.targetSha256, ...result });
}

main().catch(() => { process.stderr.write("Colliers partial worker failed; inspect private checkpoint state.\n"); process.exitCode = 1; });
