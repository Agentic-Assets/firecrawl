/** Private bounded NDJSON evidence for a single partial source window. */
import { createHash } from "node:crypto";
import { closeSync, constants, fchmodSync, fstatSync, fsyncSync, lstatSync, openSync, statfsSync, writeSync } from "node:fs";
import { dirname } from "node:path";

const MAX_NATIVE_BYTES = 16 * 1024 * 1024;
const MAX_METADATA_BYTES = 16 * 1024;
const DEFAULT_SOFT_BYTES = 16 * 1024 * 1024;

export class ColliersWindowEvidence {
  private fd: number | null;
  private digest = createHash("sha256");
  private bytes = 0;
  private records = 0;
  private failed = false;

  constructor(readonly path: string, generation: string, attempt: number, private softBytes = DEFAULT_SOFT_BYTES) {
    const directory = lstatSync(dirname(path));
    if (!directory.isDirectory() || directory.isSymbolicLink() || typeof process.getuid !== "function" ||
        directory.uid !== process.getuid() || (directory.mode & 0o077) !== 0 ||
        !generation || !Number.isSafeInteger(attempt) || attempt < 1 ||
        !Number.isSafeInteger(softBytes) || softBytes < 1024 || softBytes > 64 * 1024 * 1024)
      throw new Error("invalid partial evidence owner");
    this.fd = openSync(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
    try {
      fchmodSync(this.fd, 0o600);
      const info = fstatSync(this.fd);
      if (!info.isFile() || info.uid !== process.getuid() || info.nlink !== 1 || (info.mode & 0o777) !== 0o600)
        throw new Error("invalid partial evidence file");
      this.line({ schema: "colliers-window-evidence/v1", sourceKey: "colliers", generation, attempt });
    } catch (error) { closeSync(this.fd); this.fd = null; throw error; }
  }

  private line(metadata: unknown, native?: Uint8Array, maximum = MAX_NATIVE_BYTES) {
    if (this.fd === null || this.failed) throw new Error("partial evidence unavailable");
    try {
      const encoded = JSON.stringify(metadata);
      if (Buffer.byteLength(encoded) > MAX_METADATA_BYTES || (native && native.length > maximum))
        throw new Error("partial evidence record exceeds bound");
      const suffix = native === undefined ? "" : ',"nativeBase64":"' + Buffer.from(native).toString("base64") + '"';
      const bytes = Buffer.from(encoded.slice(0, -1) + suffix + "}\n");
      const space = statfsSync(dirname(this.path));
      if (space.bavail * space.bsize - bytes.length < 1024 ** 3) throw new Error("partial evidence below disk reserve");
      let at = 0;
      while (at < bytes.length) {
        const written = writeSync(this.fd, bytes, at, bytes.length - at);
        if (written <= 0) throw new Error("partial evidence write failed");
        at += written;
      }
      this.digest.update(bytes); this.bytes += bytes.length;
    } catch (error) { this.failed = true; throw error; }
  }

  detail(item: { source: unknown; nativeBytes: Uint8Array }): "continue" | "stop" {
    if (!(item.nativeBytes instanceof Uint8Array)) throw new TypeError("native evidence missing");
    this.line({ kind: "detail", source: item.source }, item.nativeBytes);
    this.records++;
    return this.bytes >= this.softBytes ? "stop" : "continue";
  }

  hold(item: { candidate: unknown; reason: string; observedAt?: string; evidence?: Uint8Array; status?: number }) {
    this.line({ kind: "held", candidate: item.candidate, reason: item.reason,
      ...(item.status === undefined ? {} : { status: item.status }),
      ...(item.observedAt === undefined ? {} : { observedAt: item.observedAt }) }, item.evidence,
      item.reason === "detail_oversized" ? MAX_NATIVE_BYTES + 1 : MAX_NATIVE_BYTES);
    this.records++;
    return this.bytes >= this.softBytes ? "stop" as const : "continue" as const;
  }

  finish(): { bytes: number; records: number; sha256: string } {
    if (this.fd === null || this.failed) throw new Error("partial evidence failed");
    try {
      fsyncSync(this.fd); closeSync(this.fd); this.fd = null;
      const directory = openSync(dirname(this.path), constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
      try { fsyncSync(directory); } finally { closeSync(directory); }
      return { bytes: this.bytes, records: this.records, sha256: this.digest.digest("hex") };
    } catch (error) { if (this.fd !== null) { try { closeSync(this.fd); } finally { this.fd = null; } } this.failed = true; throw error; }
  }

  abort() {
    this.failed = true;
    if (this.fd !== null) { try { closeSync(this.fd); } finally { this.fd = null; } }
  }
}
