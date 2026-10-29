/** Share collector output custody without changing ordinary listing output. */
import { closeSync, fsyncSync, lstatSync, mkdirSync, openSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

/** Reserve a private output directory before acquisition, without replacing it. */
export function reservePrivateCollectorDirectory(path: string): void {
  const parent = dirname(path);
  mkdirSync(parent, { recursive: true, mode: 0o700 });
  const info = lstatSync(parent);
  if (!info.isDirectory() || (info.mode & 0o077) !== 0 ||
      (typeof process.getuid === "function" && info.uid !== process.getuid())) {
    throw new Error("observation output directory must be owner-private");
  }
  mkdirSync(path, { mode: 0o700 });
}

export function writeCollectorArtifact(path: string | null | undefined, json: string, privateEvidence: boolean): void {
  if (!path) {
    if (privateEvidence) throw new Error("Private collector artifacts require a private --out file");
    process.stdout.write(json);
    return;
  }
  mkdirSync(dirname(path), privateEvidence ? { recursive: true, mode: 0o700 } : { recursive: true });
  if (privateEvidence) {
    const descriptor = openSync(path, "wx", 0o600);
    try {
      writeFileSync(descriptor, json);
      fsyncSync(descriptor);
    } finally { closeSync(descriptor); }
  } else writeFileSync(path, json);
}
