/** Private, attempt-bound observations. They do not authorize ingestion. */
import { createHash } from "node:crypto";
import { closeSync, constants, fchmodSync, fstatSync, fsyncSync, ftruncateSync, lstatSync, openSync, statfsSync, writeSync } from "node:fs";
import { dirname } from "node:path";

export const PARTIAL_PAGE_SOURCES = ["cbre", "colliers", "avison-young", "lyon-stahl", "kidder-mathews"] as const;
export type PartialPageSource = (typeof PARTIAL_PAGE_SOURCES)[number];

export function partialPageEnvName(source: PartialPageSource, field: "PATH" | "ATTEMPT"): string {
  return `CRE_${source.toUpperCase().replaceAll("-", "_")}_PARTIAL_${field}`;
}

export type PartialPageObservation = {
  transaction: "sale" | "lease";
  pass: number;
  page: number;
  observedAt: string;
  documents: unknown[];
  start?: number;
  pageSize?: number;
  kind?: "unqualified_response_pair" | "recovery_hole" | "validated_detail" | "detail_outcomes" | "lyon_search_response" | "kidder_team_observation";
  reason?: "provider_failure" | "budget_exhausted" | "recovery_stopped" | "count_drift" | "requested_limit" | "page_cap";
  pairAttempt?: number;
};

export class PartialPageSpool {
  private fd: number | null = null;
  private hash = createHash("sha256");
  private bytes = 0;
  // Counts JSONL observations, including retries and raw plus parsed records, not unique pages.
  private observationRecords = 0;
  private failed = false;

  get writable(): boolean { return this.fd !== null && !this.failed; }

  constructor(
    readonly path: string,
    readonly generation: string,
    readonly attempt: number,
    private readonly io: {
      write: (fd: number, buffer: Buffer, offset: number, length: number) => number;
      sync: (fd: number) => void;
    } = { write: (fd, buffer, offset, length) => writeSync(fd, buffer, offset, length),
      sync: (fd) => fsyncSync(fd) },
    readonly sourceKey: PartialPageSource = "cbre",
  ) {
    try {
      const directory = lstatSync(dirname(path));
      if (!directory.isDirectory() || directory.isSymbolicLink()
          || typeof process.getuid !== "function" || directory.uid !== process.getuid()
          || (directory.mode & 0o077) !== 0 || !generation
          || !Number.isSafeInteger(attempt) || attempt < 1) throw new Error();
      this.fd = openSync(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
      fchmodSync(this.fd, 0o600);
      const file = fstatSync(this.fd);
      if (!file.isFile() || file.uid !== process.getuid() || file.nlink !== 1
          || (file.mode & 0o777) !== 0o600) throw new Error();
      this.write({ schema: `${sourceKey}-partial-pages/v1`, sourceKey, generation, attempt });
    } catch { this.fail(); }
  }

  private write(value: unknown): void {
    if (this.fd === null || this.failed) return;
    try {
      const bytes = Buffer.from(JSON.stringify(value) + "\n", "utf8");
      // Match the pipeline's existing minimum free-space guard.
      const space = statfsSync(dirname(this.path));
      if (space.bavail * space.bsize - bytes.length < 1024 ** 3) throw new Error();
      let offset = 0;
      while (offset < bytes.length) {
        const written = this.io.write(this.fd, bytes, offset, bytes.length - offset);
        if (written <= 0) throw new Error();
        offset += written;
      }
      this.hash.update(bytes);
      this.bytes += bytes.length;
    } catch { this.fail(); }
  }

  record(page: PartialPageObservation): void {
    if (this.failed) return;
    this.write({ ...page, kind: page.kind ?? (this.sourceKey === "colliers" ? "parsed_page" : "validated_page") });
    if (!this.failed) this.observationRecords++;
  }

  markFailure(): void { this.failed = true; }

  private fail(): void { this.markFailure(); }

  finish(): { state: "sealed" | "sealed_partial"; bytes: number; observationRecords: number; sha256: string } | { state: "failed" } {
    if (this.fd === null) return { state: "failed" };
    let partial = this.failed;
    try {
      if (partial) {
        if (this.observationRecords === 0) throw new Error();
        ftruncateSync(this.fd, this.bytes);
      }
      try { this.io.sync(this.fd); }
      catch {
        if (partial || this.observationRecords === 0) throw new Error();
        partial = true;
        ftruncateSync(this.fd, this.bytes);
        this.io.sync(this.fd);
      }
      closeSync(this.fd);
      this.fd = null;
      const directory = openSync(dirname(this.path), constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
      try { fsyncSync(directory); } finally { closeSync(directory); }
      return { state: partial ? "sealed_partial" : "sealed", bytes: this.bytes,
        observationRecords: this.observationRecords, sha256: this.hash.digest("hex") };
    } catch {
      if (this.fd !== null) {
        try { closeSync(this.fd); } catch { /* leave unverified bytes in place */ }
        this.fd = null;
      }
      this.fail();
      return { state: "failed" };
    }
  }
}
