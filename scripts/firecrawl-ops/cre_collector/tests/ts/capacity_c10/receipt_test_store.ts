import {
  canonicalJson,
  sha256,
  type ReceiptArtifactStore,
  type SealedArtifact,
} from "../../../capacity_c10/receipts/index.js";

/** Test-only artifact store: transport tests must not weaken production FS gates. */
export class MemoryReceiptStore implements ReceiptArtifactStore {
  readonly artifacts = new Map<string, Uint8Array>();

  async sealJson(stem: string, value: unknown): Promise<SealedArtifact> {
    return this.sealBytes(stem, Buffer.from(canonicalJson(value), "utf8"));
  }

  async sealBytes(stem: string, value: Uint8Array): Promise<SealedArtifact> {
    const digest = sha256(value);
    const name = `${stem}-${digest}.sealed`;
    if (this.artifacts.has(name)) throw new Error("test artifact was sealed twice");
    this.artifacts.set(name, new Uint8Array(value));
    return Object.freeze({ name, sha256: digest, bytes: value.byteLength });
  }

  jsonFor(artifactSha256: string): Readonly<Record<string, unknown>> {
    const body = [...this.artifacts.entries()].find(([name]) => name.includes(artifactSha256))?.[1];
    if (!body) throw new Error("test artifact was not found");
    return JSON.parse(Buffer.from(body).toString("utf8")) as Readonly<Record<string, unknown>>;
  }
}
