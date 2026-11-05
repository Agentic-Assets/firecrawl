/** Bind partial processing to an owner-settled private attempt spool. */
import { constants, closeSync, fstatSync, lstatSync, openSync, readSync } from "node:fs";
import { createHash } from "node:crypto";
import { Readable } from "node:stream";

type OpenStream = () => Readable;

/** Caller must independently validate the checkpoint receipt and exact expected path. */
export async function withColliersPartialSpool<T>(path: string, bytes: number, sha256: string,
  work: (open: OpenStream, verify: () => Promise<void>) => Promise<T>): Promise<T> {
  if (!Number.isSafeInteger(bytes) || bytes < 1 || !/^[0-9a-f]{64}$/.test(sha256) || typeof process.getuid !== "function") throw new Error("invalid spool binding");
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const initial = fstatSync(fd, { bigint: true });
    const identity = (s: typeof initial) => [s.dev, s.ino, s.size, s.mtimeNs, s.ctimeNs, s.mode, s.uid, s.nlink].join(":");
    const expected = identity(initial);
    if (!initial.isFile() || initial.uid !== BigInt(process.getuid()) || initial.nlink !== 1n ||
        (initial.mode & 0o777n) !== 0o600n || initial.size !== BigInt(bytes)) throw new Error("invalid spool file");
    // Each reader uses explicit positional reads on the owner-held descriptor.
    // Destroying a consumer stream must not close the descriptor used by
    // final identity verification or mask the original failure.
    const open: OpenStream = () => Readable.from((async function* () {
      let position = 0;
      for (;;) {
        const buffer = Buffer.allocUnsafe(64 * 1024);
        const count = readSync(fd, buffer, 0, buffer.length, position);
        if (count === 0) return;
        position += count;
        yield buffer.subarray(0, count);
      }
    })());
    const verify = async () => {
      const before = fstatSync(fd, { bigint: true }), named = lstatSync(path, { bigint: true });
      if (!named.isFile() || identity(before) !== expected || identity(named) !== expected) throw new Error("spool identity changed");
      const digest = createHash("sha256"); let count = 0;
      for await (const chunk of open()) { digest.update(chunk); count += chunk.length; }
      const after = fstatSync(fd, { bigint: true }), latest = lstatSync(path, { bigint: true });
      if (identity(after) !== expected || identity(latest) !== expected || count !== bytes || digest.digest("hex") !== sha256)
        throw new Error("spool binding changed");
    };
    await verify();
    const result = await work(open, verify);
    await verify();
    return result;
  } finally { closeSync(fd); }
}
