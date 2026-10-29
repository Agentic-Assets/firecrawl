/** Share collector output custody without changing ordinary listing output. */
import { closeSync, fsyncSync, lstatSync, mkdirSync, openSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

/** Reserve a private output before acquisition, without replacing an existing file. */
export function reservePrivateCollectorArtifact(path: string): number {
  const parent = dirname(path);
  mkdirSync(parent, { recursive: true, mode: 0o700 });
  const info = lstatSync(parent);
  if (!info.isDirectory() || (info.mode & 0o077) !== 0 ||
      (typeof process.getuid === "function" && info.uid !== process.getuid())) {
    throw new Error("observation output directory must be owner-private");
  }
  return openSync(path, "wx", 0o600);
}

export function finishPrivateCollectorArtifact(descriptor: number, json: string): void {
  try { writeFileSync(descriptor, json); fsyncSync(descriptor); }
  finally { closeSync(descriptor); }
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
