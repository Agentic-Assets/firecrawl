import { randomUUID } from "node:crypto";
import { constants as fsConstants, existsSync } from "node:fs";
import { link, lstat, mkdir, open, unlink } from "node:fs/promises";
import { basename, isAbsolute } from "node:path";

import { C10ReceiptError, canonicalJson, sha256 } from "./contracts.js";

const ROOT_MODE = 0o700;
const FILE_MODE = 0o600;
const MAX_ARTIFACT_BYTES = 2 * 1024 * 1024;
const LINUX_DESCRIPTOR_ROOT = "/proc/self/fd";

export interface SealedArtifact {
  readonly name: string;
  readonly sha256: string;
  readonly bytes: number;
}

/** Minimal append-only artifact boundary shared by the transport and producers. */
export interface ReceiptArtifactStore {
  sealJson(stem: string, value: unknown): Promise<SealedArtifact>;
  sealBytes(stem: string, value: Uint8Array): Promise<SealedArtifact>;
}

interface DirectoryIdentity {
  readonly dev: number;
  readonly ino: number;
}

function safeStem(stem: string): string {
  if (!/^[a-z0-9][a-z0-9-]{0,80}$/.test(stem)) {
    throw new C10ReceiptError("receipt artifact stem is invalid");
  }
  return stem;
}

function sameIdentity(left: DirectoryIdentity, right: DirectoryIdentity): boolean {
  return left.dev === right.dev && left.ino === right.ino;
}

function identity(metadata: { dev: number; ino: number }): DirectoryIdentity {
  return Object.freeze({ dev: metadata.dev, ino: metadata.ino });
}

function supportedFdRelativeRuntime(): boolean {
  return process.platform === "linux"
    && existsSync(LINUX_DESCRIPTOR_ROOT)
    && typeof fsConstants.O_DIRECTORY === "number"
    && typeof fsConstants.O_NOFOLLOW === "number";
}

/**
 * A private append-only artifact root using a retained directory descriptor.
 *
 * Node does not expose openat/linkat/unlinkat. On Linux, procfs descriptor
 * paths preserve the retained directory object across path replacement. Other
 * hosts fail closed: a path-only fallback would make the sealing claim false.
 */
export class PrivateReceiptStore implements ReceiptArtifactStore {
  private closed = false;

  private constructor(
    readonly root: string,
    private readonly directory: Awaited<ReturnType<typeof open>>,
    private readonly directoryIdentity: DirectoryIdentity,
  ) {}

  static safeRuntimeAvailable(): boolean {
    return supportedFdRelativeRuntime();
  }

  static async create(root: string): Promise<PrivateReceiptStore> {
    if (!isAbsolute(root)) throw new C10ReceiptError("receipt root must be absolute");
    if (!supportedFdRelativeRuntime()) {
      throw new C10ReceiptError("receipt store requires fd-relative filesystem primitives");
    }
    await mkdir(root, { recursive: true, mode: ROOT_MODE });
    const directory = await open(root, fsConstants.O_RDONLY | fsConstants.O_DIRECTORY | fsConstants.O_NOFOLLOW);
    try {
      await directory.chmod(ROOT_MODE);
      const metadata = await directory.stat();
      if (!metadata.isDirectory() || (metadata.mode & 0o777) !== ROOT_MODE) {
        throw new C10ReceiptError("receipt root must be a real mode 0700 directory");
      }
      const store = new PrivateReceiptStore(root, directory, identity(metadata));
      await store.assertRootIdentity();
      return store;
    } catch (error) {
      await directory.close().catch(() => undefined);
      if (error instanceof C10ReceiptError) throw error;
      throw new C10ReceiptError("receipt root could not retain a verified directory descriptor");
    }
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    await this.directory.close();
  }

  async sealJson(stem: string, value: unknown): Promise<SealedArtifact> {
    return this.sealBytes(stem, Buffer.from(canonicalJson(value), "utf8"));
  }

  async sealBytes(stem: string, value: Uint8Array): Promise<SealedArtifact> {
    const normalizedStem = safeStem(stem);
    if (value.byteLength > MAX_ARTIFACT_BYTES) {
      throw new C10ReceiptError("receipt artifact size is invalid");
    }
    await this.assertRootIdentity();
    const digest = sha256(value);
    const name = `${normalizedStem}-${digest}.sealed`;
    if (basename(name) !== name) throw new C10ReceiptError("receipt artifact path is invalid");
    const temporaryName = `.${name}.tmp-${randomUUID()}`;
    const temporaryPath = this.relativePath(temporaryName);
    const finalPath = this.relativePath(name);
    const flags = fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_EXCL | fsConstants.O_NOFOLLOW;
    let handle: Awaited<ReturnType<typeof open>> | undefined;
    try {
      handle = await open(temporaryPath, flags, FILE_MODE);
      await handle.writeFile(value);
      await handle.sync();
      const metadata = await handle.stat();
      if (!metadata.isFile() || metadata.nlink !== 1 || (metadata.mode & 0o777) !== FILE_MODE) {
        throw new C10ReceiptError("temporary receipt artifact is invalid");
      }
      await handle.close();
      handle = undefined;
      await this.assertArtifact(temporaryName, digest, value.byteLength);
      await link(temporaryPath, finalPath);
      await unlink(temporaryPath);
      await this.directory.sync();
      await this.assertArtifact(name, digest, value.byteLength);
      await this.assertRootIdentity();
      return Object.freeze({ name, sha256: digest, bytes: value.byteLength });
    } catch (error) {
      await handle?.close().catch(() => undefined);
      await unlink(temporaryPath).catch(() => undefined);
      if (error instanceof C10ReceiptError) throw error;
      throw new C10ReceiptError("private receipt artifact could not be sealed");
    }
  }

  /** Verify a sealed artifact without exposing its private body. */
  async verifySealed(artifact: SealedArtifact): Promise<void> {
    if (basename(artifact.name) !== artifact.name || !artifact.name.endsWith(".sealed")) {
      throw new C10ReceiptError("sealed artifact name is invalid");
    }
    await this.assertRootIdentity();
    await this.assertArtifact(artifact.name, artifact.sha256, artifact.bytes);
    await this.assertRootIdentity();
  }

  private relativePath(name: string): string {
    if (this.closed) throw new C10ReceiptError("receipt store is closed");
    if (basename(name) !== name || name.includes("/")) {
      throw new C10ReceiptError("receipt artifact path is invalid");
    }
    return `${LINUX_DESCRIPTOR_ROOT}/${this.directory.fd}/${name}`;
  }

  private async assertRootIdentity(): Promise<void> {
    if (this.closed) throw new C10ReceiptError("receipt store is closed");
    const retained = await this.directory.stat();
    const named = await lstat(this.root);
    if (
      !retained.isDirectory()
      || !named.isDirectory()
      || named.isSymbolicLink()
      || (named.mode & 0o777) !== ROOT_MODE
      || !sameIdentity(identity(retained), this.directoryIdentity)
      || !sameIdentity(identity(named), this.directoryIdentity)
    ) {
      throw new C10ReceiptError("receipt root identity changed");
    }
  }

  private async assertArtifact(name: string, digest: string, bytes: number): Promise<void> {
    const handle = await open(this.relativePath(name), fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW);
    try {
      const metadata = await handle.stat();
      if (!metadata.isFile() || metadata.nlink !== 1 || (metadata.mode & 0o777) !== FILE_MODE) {
        throw new C10ReceiptError("sealed artifact is invalid");
      }
      const body = await handle.readFile();
      if (body.byteLength !== bytes || sha256(body) !== digest) {
        throw new C10ReceiptError("sealed artifact readback hash is invalid");
      }
    } finally {
      await handle.close();
    }
  }
}
