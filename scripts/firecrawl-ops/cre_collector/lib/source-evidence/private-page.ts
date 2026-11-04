import { randomBytes } from "node:crypto";
import { constants, lstatSync, opendirSync, openSync, closeSync, fchmodSync, fstatSync,
  writeFileSync, readFileSync, fsyncSync, linkSync, unlinkSync } from "node:fs";
import { join, resolve } from "node:path";

/** Synchronous bounded publication in a private single-writer attempt directory. */
export function publishPrivatePage(directory: string, filename: string, payload: Buffer,
  namePattern: RegExp, maxEntryBytes: number, maxPages: number, maxBytes: number): string {
  let temp: string | undefined;
  let tempOwned = false;
  try {
    const dir = resolve(directory);
    const stat = lstatSync(dir);
    if (!stat.isDirectory() || stat.isSymbolicLink() || typeof process.getuid !== "function" ||
        stat.uid !== process.getuid() || (stat.mode & 0o077) !== 0 || !namePattern.test(filename) ||
        payload.length > maxEntryBytes) throw new Error();
    const target = join(dir, filename);
    // All quota checks and publication below are synchronous, so concurrent
    // async callers in this worker cannot interleave a check and a write.
    let usedBytes = 0;
    let pageCount = 0;
    let targetExists = false;
    const entries = opendirSync(dir);
    try {
      for (let item = entries.readSync(); item !== null; item = entries.readSync()) {
        const name = item.name;
        // Unknown, temporary and unsafe entries are refused, not ignored.
        if (!namePattern.test(name)) throw new Error();
        const file = lstatSync(join(dir, name));
        if (!file.isFile() || file.isSymbolicLink() || file.uid !== process.getuid() ||
            (file.mode & 0o777) !== 0o600 || file.nlink !== 1 || file.size > maxEntryBytes) throw new Error();
        usedBytes += file.size;
        pageCount++;
        if (!Number.isSafeInteger(usedBytes) || pageCount > maxPages || usedBytes > maxBytes) throw new Error();
        if (name === target.slice(dir.length + 1)) targetExists = true;
      }
    } finally { entries.closeSync(); }
    if (targetExists) {
      const fd = openSync(target, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      try {
        const before = fstatSync(fd);
        if (!before.isFile() || before.uid !== process.getuid() || (before.mode & 0o777) !== 0o600 || before.nlink !== 1 || before.size !== payload.length) throw new Error();
        const bytes = readFileSync(fd); const after = fstatSync(fd); const named = lstatSync(target);
        const identity = (value: typeof before) => [value.dev, value.ino, value.size, value.mtimeMs, value.ctimeMs, value.mode, value.uid, value.nlink].join(":");
        if (identity(before) !== identity(after) || identity(after) !== identity(named) || !bytes.equals(payload)) throw new Error();
      } finally { closeSync(fd); }
      return target;
    }
    if (pageCount >= maxPages || payload.length > maxBytes - usedBytes) throw new Error();
    const current = lstatSync(dir);
    if (!current.isDirectory() || current.dev !== stat.dev || current.ino !== stat.ino ||
        current.uid !== stat.uid || current.mode !== stat.mode) throw new Error();
    temp = join(dir, `.page-${randomBytes(16).toString("hex")}.tmp`);
    const fd = openSync(temp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
    tempOwned = true;
    try { fchmodSync(fd, 0o600); writeFileSync(fd, payload); fsyncSync(fd); } finally { closeSync(fd); }
    linkSync(temp, target);
    unlinkSync(temp); tempOwned = false; temp = undefined;
    const parent = openSync(dir, constants.O_RDONLY);
    try { fsyncSync(parent); } finally { closeSync(parent); }
    return target;
  } catch {
    if (temp && tempOwned) { try { unlinkSync(temp); } catch { /* preserve private failure */ } }
    throw new Error("native capture publication failed");
  }
}
