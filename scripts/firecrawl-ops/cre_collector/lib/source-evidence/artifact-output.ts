/** Share collector output custody without changing ordinary listing output. */
import { closeSync, fsyncSync, mkdirSync, openSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

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
