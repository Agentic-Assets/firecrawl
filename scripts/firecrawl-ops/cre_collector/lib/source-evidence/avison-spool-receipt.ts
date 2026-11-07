/** Private source-owned receipt for a settled Avison observation spool.
 * Receipt publication is evidence only; the owner must independently verify
 * process settlement, source attempt, code, target and unknown-write fences.
 */
import { randomBytes } from "node:crypto";
import { closeSync, constants, fchmodSync, fstatSync, fsyncSync, linkSync, lstatSync,
  openSync, unlinkSync, writeFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";

export type AvisonSpoolReceipt = {
  state: "sealed" | "sealed_partial"; bytes: number; observationRecords: number; sha256: string;
};

export function publishAvisonSpoolReceipt(
  spoolPath: string, generation: string, attempt: number, receipt: AvisonSpoolReceipt,
  sourceStartedAt: string, sourceFinishedAt: string,
  sourceOutcome: "record_local_error" | "unclassified_source_error" | "terminal_detail_not_found"
    | "mixed_source_errors",
): string {
  const dir = dirname(spoolPath);
  const filename = `attempt-${attempt}.jsonl`;
  if (basename(spoolPath) !== filename || !generation || !Number.isSafeInteger(attempt) || attempt < 1
      || !["sealed", "sealed_partial"].includes(receipt.state)
      || !Number.isSafeInteger(receipt.bytes) || receipt.bytes < 1
      || !Number.isSafeInteger(receipt.observationRecords) || receipt.observationRecords < 1
      || !/^[a-f0-9]{64}$/.test(receipt.sha256)
      || !["record_local_error", "unclassified_source_error", "terminal_detail_not_found",
        "mixed_source_errors"].includes(sourceOutcome)
      || !Number.isFinite(Date.parse(sourceStartedAt)) || !Number.isFinite(Date.parse(sourceFinishedAt))
      || Date.parse(sourceStartedAt) > Date.parse(sourceFinishedAt)) throw new Error("invalid source receipt");
  const owner = process.getuid?.();
  const parentBefore = lstatSync(dir);
  const spool = lstatSync(spoolPath);
  if (owner === undefined || !parentBefore.isDirectory() || parentBefore.isSymbolicLink()
      || parentBefore.uid !== owner || (parentBefore.mode & 0o777) !== 0o700
      || !spool.isFile() || spool.isSymbolicLink() || spool.uid !== owner || spool.nlink !== 1
      || (spool.mode & 0o777) !== 0o600 || spool.size !== receipt.bytes) throw new Error("invalid source custody");
  const output = join(dir, `attempt-${attempt}.receipt.json`);
  const temporary = join(dir, `.receipt-${randomBytes(16).toString("hex")}.tmp`);
  const payload = Buffer.from(JSON.stringify({
    schema: "avison-young-settled-spool/v1", sourceKey: "avison-young", generation, attempt,
    sourceOutcome, coverage: "unverified",
    sourceStartedAt, sourceFinishedAt, spool: receipt,
  }) + "\n", "utf8");
  let owned = false;
  try {
    const fd = openSync(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
    owned = true;
    try {
      fchmodSync(fd, 0o600);
      const file = fstatSync(fd);
      if (!file.isFile() || file.uid !== owner || file.nlink !== 1 || (file.mode & 0o777) !== 0o600) throw new Error();
      writeFileSync(fd, payload);
      fsyncSync(fd);
    } finally { closeSync(fd); }
    const parentAfter = lstatSync(dir);
    if (parentBefore.dev !== parentAfter.dev || parentBefore.ino !== parentAfter.ino
        || parentBefore.uid !== parentAfter.uid || parentBefore.mode !== parentAfter.mode) throw new Error();
    linkSync(temporary, output);
    unlinkSync(temporary);
    owned = false;
    const directory = openSync(dir, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
    try { fsyncSync(directory); } finally { closeSync(directory); }
    return output;
  } finally {
    if (owned) { try { unlinkSync(temporary); } catch { /* retain uncertain private file */ } }
  }
}
