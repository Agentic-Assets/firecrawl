/** Share collector output custody without changing ordinary listing output. */
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

export function writeCollectorArtifact(path: string | undefined, json: string, privateEvidence: boolean): void {
  if (!path) {
    if (privateEvidence) throw new Error("Retained source observations require a private --out file");
    process.stdout.write(json);
    return;
  }
  mkdirSync(dirname(path), privateEvidence ? { recursive: true, mode: 0o700 } : { recursive: true });
  if (privateEvidence) writeFileSync(path, json, { flag: "wx", mode: 0o600 });
  else writeFileSync(path, json);
}
