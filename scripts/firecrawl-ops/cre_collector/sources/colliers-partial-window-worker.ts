/** Owned-worker building block. The checkpoint must bind owner, path and target. */
import { createHash } from "node:crypto";
import { closeSync, constants, fchmodSync, fstatSync, fsyncSync, lstatSync, openSync, statfsSync, writeSync } from "node:fs";
import { dirname } from "node:path";
import { observationNow } from "../lib/observation-clock.js";
import { withColliersPartialSpool } from "./colliers-partial-custody.js";
import { ColliersWindowEvidence } from "./colliers-partial-window-evidence.js";
import { produceColliersWindow } from "./colliers-partial-window-produce.js";
import { reverifyColliersWindow } from "./colliers-partial-window-reverify.js";

type Native = { detail: any; decodedText: string; nativeBytes: Uint8Array };
type Input = { sourcePath: string; sourceBytes: number; sourceSha256: string; evidencePath: string; artifactPath: string;
  generation: string; generationStartedAt: string; attempt: number; startedAt: string; finishedAt: string; cursor: number;
  producerSha: string; targetSha256: string; originalSourceMainSha256: string; softBytes?: number; limit?: number;
  stopBeforePosition?: number };
const MAX_ARTIFACT_BYTES = 128 * 1024 * 1024;

/** Stream a new private artifact; a failed file is never a sealed receipt. */
class PrivateArtifact {
  private fd: number | null;
  private bytes = 0;
  private hash = createHash("sha256");
  constructor(readonly path: string) {
    const directory = lstatSync(dirname(path));
    if (!directory.isDirectory() || directory.isSymbolicLink() || typeof process.getuid !== "function" ||
        directory.uid !== process.getuid() || (directory.mode & 0o077) !== 0) throw new Error("partial artifact owner invalid");
    this.fd = openSync(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
    try {
      fchmodSync(this.fd, 0o600);
      const file = fstatSync(this.fd);
      if (!file.isFile() || file.uid !== process.getuid() || file.nlink !== 1 || (file.mode & 0o777) !== 0o600)
        throw new Error("partial artifact file invalid");
    } catch (error) { closeSync(this.fd); this.fd = null; throw error; }
  }
  write(text: string) {
    if (this.fd === null) throw new Error("partial artifact unavailable");
    const bytes = Buffer.from(text);
    if (this.bytes + bytes.length > MAX_ARTIFACT_BYTES) throw new Error("partial artifact exceeds bound");
    const space = statfsSync(dirname(this.path));
    if (space.bavail * space.bsize - bytes.length < 1024 ** 3) throw new Error("partial artifact below disk reserve");
    let position = 0;
    while (position < bytes.length) {
      const count = writeSync(this.fd, bytes, position, bytes.length - position);
      if (count <= 0) throw new Error("partial artifact write failed");
      position += count;
    }
    this.hash.update(bytes); this.bytes += bytes.length;
  }
  finish() {
    if (this.fd === null) throw new Error("partial artifact unavailable");
    try {
      fsyncSync(this.fd); closeSync(this.fd); this.fd = null;
      const parent = openSync(dirname(this.path), constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
      try { fsyncSync(parent); } finally { closeSync(parent); }
      return { bytes: this.bytes, sha256: this.hash.digest("hex") };
    } catch (error) { this.abort(); throw error; }
  }
  abort() { if (this.fd !== null) { try { closeSync(this.fd); } finally { this.fd = null; } } }
}

/** No output here grants write authority or a complete-source verdict. */
export async function buildColliersWindowArtifact(input: Input, fetchDetail?: (token: string) => Promise<Native>) {
  if (!/^[0-9a-f]{40}$/.test(input.producerSha) || !/^[0-9a-f]{64}$/.test(input.targetSha256) ||
      !/^[0-9a-f]{64}$/.test(input.originalSourceMainSha256)) throw new Error("partial checkpoint binding invalid");
  const binding = { generation: input.generation, attempt: input.attempt, startedAt: input.startedAt, finishedAt: input.finishedAt };
  return withColliersPartialSpool(input.sourcePath, input.sourceBytes, input.sourceSha256, async (openSource, verifySource) => {
    const evidence = new ColliersWindowEvidence(input.evidencePath, input.generation, input.attempt, input.softBytes);
    let result: Awaited<ReturnType<typeof produceColliersWindow>>;
    try {
      result = await produceColliersWindow(openSource, verifySource, binding, input.cursor,
        async item => evidence.detail(item), async item => evidence.hold(item), fetchDetail, input.limit, input.stopBeforePosition);
    } catch (error) { evidence.abort(); throw error; }
    const receipt = evidence.finish(), detailFinishedAt = observationNow();
    const artifact = new PrivateArtifact(input.artifactPath);
    let emitted = 0, replay: Awaited<ReturnType<typeof reverifyColliersWindow>>;
    try {
      const header = { schema: "colliers-partial-window/v1", sourceKey: "colliers", binding, detailFinishedAt,
        producerSha: input.producerSha, targetSha256: input.targetSha256,
        runMeta: { mode: "partial", transactions: ["sale", "lease"], startedAt: input.startedAt,
          finishedAt: detailFinishedAt, freshness: { generationId: input.generation,
            generationStartedAt: input.generationStartedAt, requireFreshDetails: true } },
        source: { bytes: input.sourceBytes, sha256: input.sourceSha256, mainSha256: input.originalSourceMainSha256 },
        sourceOutcome: "source_error", coverage: "unverified", cursor: input.cursor, proposedCursor: result.proposedCursor,
        selected: result.selection.selected, total: result.selection.total, heldOther: result.selection.heldOther,
        unattempted: result.unattempted, evidence: receipt };
      artifact.write(JSON.stringify(header).slice(0, -1) + ',"listings":[');
      replay = await withColliersPartialSpool(input.evidencePath, receipt.bytes, receipt.sha256, (openEvidence, verifyEvidence) =>
        reverifyColliersWindow(openSource, verifySource, openEvidence, verifyEvidence,
          { ...binding, detailFinishedAt }, input.cursor, receipt.records, result.proposedCursor,
          async item => {
            if (Object.hasOwn(item.listing, "colliersPartialRecord")) throw new Error("partial listing key collision");
            const { listing, ...record } = item;
            artifact.write((emitted ? "," : "") + JSON.stringify({ ...listing, colliersPartialRecord: record }));
            emitted++;
          }, input.limit, input.stopBeforePosition));
      if (replay.emitted !== emitted || replay.sourceStopped !== result.sourceStopped ||
          replay.rotationComplete !== result.rotationComplete || replay.firstPosition !== result.firstPosition)
        throw new Error("partial emitted partition differs");
      const sources = [
        { sourceKey: "colliers", transaction: "sale", supported: true, truncated: true,
          listingsCollected: emitted, error: "original source pass failed" },
        { sourceKey: "colliers", transaction: "lease", supported: true, truncated: false, listingsCollected: 0 },
      ];
      artifact.write('],"held":' + replay.held + ',"sources":' + JSON.stringify(sources) +
        ',"brokers":[],"totalListings":' + emitted + "}\n");
    } catch (error) { artifact.abort(); throw error; }
    const sealed = artifact.finish();
    await withColliersPartialSpool(input.artifactPath, sealed.bytes, sealed.sha256, async () => {});
    return { artifact: sealed, evidence: receipt, detailFinishedAt, proposedCursor: result.proposedCursor,
      admitted: replay.emitted, held: replay.held, heldOther: result.selection.heldOther, unattempted: result.unattempted,
      sourceStopped: replay.sourceStopped, rotationComplete: replay.rotationComplete, firstPosition: replay.firstPosition };
  });
}
