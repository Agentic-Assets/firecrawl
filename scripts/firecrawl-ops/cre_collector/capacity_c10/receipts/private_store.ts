import { constants as fsConstants } from "node:fs";
import {
  chmod,
  link,
  lstat,
  mkdir,
  open,
  unlink,
} from "node:fs/promises";
import { basename, isAbsolute, join } from "node:path";

import { C10ReceiptError, canonicalJson, sha256 } from "./contracts.js";

const ROOT_MODE = 0o700;
const FILE_MODE = 0o600;
const MAX_ARTIFACT_BYTES = 2 * 1024 * 1024;

export interface SealedArtifact {
  readonly name: string;
  readonly sha256: string;
  readonly bytes: number;
}

function safeStem(stem: string): string {
  if (!/^[a-z0-9][a-z0-9-]{0,80}$/.test(stem)) {
    throw new C10ReceiptError("receipt artifact stem is invalid");
  }
  return stem;
}

async function fsyncDirectory(path: string): Promise<void> {
  const directory = await open(path, fsConstants.O_RDONLY | fsConstants.O_DIRECTORY);
  try {
    await directory.sync();
  } finally {
    await directory.close();
  }
}

/** A private, append-only artifact root. It never emits artifact contents. */
export class PrivateReceiptStore {
  private constructor(readonly root: string) {}

  static async create(root: string): Promise<PrivateReceiptStore> {
    if (!isAbsolute(root)) throw new C10ReceiptError("receipt root must be absolute");
    await mkdir(root, { recursive: true, mode: ROOT_MODE });
    let metadata = await lstat(root);
    if (!metadata.isDirectory() || metadata.isSymbolicLink()) {
      throw new C10ReceiptError("receipt root must be a real directory");
    }
    await chmod(root, ROOT_MODE);
    metadata = await lstat(root);
    if (!metadata.isDirectory() || metadata.isSymbolicLink() || (metadata.mode & 0o777) !== ROOT_MODE) {
      throw new C10ReceiptError("receipt root must be mode 0700");
    }
    return new PrivateReceiptStore(root);
  }

  async sealJson(stem: string, value: unknown): Promise<SealedArtifact> {
    return this.sealBytes(stem, Buffer.from(canonicalJson(value), "utf8"));
  }

  async sealBytes(stem: string, value: Uint8Array): Promise<SealedArtifact> {
    const normalizedStem = safeStem(stem);
    if (value.byteLength > MAX_ARTIFACT_BYTES) {
      throw new C10ReceiptError("receipt artifact size is invalid");
    }
    if (typeof fsConstants.O_NOFOLLOW !== "number") {
      throw new C10ReceiptError("receipt store requires O_NOFOLLOW");
    }
    const digest = sha256(value);
    const name = `${normalizedStem}-${digest}.sealed`;
    if (basename(name) !== name) throw new C10ReceiptError("receipt artifact path is invalid");
    const finalPath = join(this.root, name);
    const temporaryPath = join(this.root, `.${name}.tmp-${process.pid}`);
    const flags = fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_EXCL | fsConstants.O_NOFOLLOW;
    let handle: Awaited<ReturnType<typeof open>> | undefined;
    try {
      handle = await open(temporaryPath, flags, FILE_MODE);
      await handle.writeFile(value);
      await handle.sync();
      const metadata = await handle.stat();
      if (!metadata.isFile() || (metadata.mode & 0o777) !== FILE_MODE) {
        throw new C10ReceiptError("receipt artifact mode is invalid");
      }
      await handle.close();
      handle = undefined;
      await link(temporaryPath, finalPath);
      await unlink(temporaryPath);
      await fsyncDirectory(this.root);
      const sealed = await lstat(finalPath);
      if (!sealed.isFile() || sealed.isSymbolicLink() || (sealed.mode & 0o777) !== FILE_MODE) {
        throw new C10ReceiptError("sealed artifact is invalid");
      }
      return Object.freeze({ name, sha256: digest, bytes: value.byteLength });
    } catch (error) {
      await handle?.close().catch(() => undefined);
      await unlink(temporaryPath).catch(() => undefined);
      if (error instanceof C10ReceiptError) throw error;
      throw new C10ReceiptError("private receipt artifact could not be sealed");
    }
  }
}
