/**
 * Narrow C10 v3 child.  It cannot mint credentials, discover a sidecar, or
 * construct a transport.  The Python host supplies one framed issued request
 * on stdin and independently verifies every field returned here.
 */

const MAX_FRAME_BYTES = 64 * 1024;
const PATH = "/internal/c10/v3/browser-execute";

type Issued = Readonly<{
  endpoint: string;
  authorization: string;
  hostTransportKey: string;
  capability: Record<string, unknown>;
  card: Record<string, unknown>;
}>;

function exact(value: unknown, fields: readonly string[]): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value as Record<string, unknown>).sort().join(",") === [...fields].sort().join(",");
}

function parseIssued(raw: string): Issued {
  if (Buffer.byteLength(raw, "utf8") > MAX_FRAME_BYTES) throw new Error("C10 child frame too large");
  const value: unknown = JSON.parse(raw);
  if (!exact(value, ["authorization", "capability", "card", "endpoint", "hostTransportKey"]) || typeof value.endpoint !== "string" || typeof value.authorization !== "string" || typeof value.hostTransportKey !== "string" || !exact(value.capability, ["binding", "coordinatorKeyId", "expiresAtMs", "nonce", "protocolVersion", "sourceKey"]) || !value.card || typeof value.card !== "object" || Array.isArray(value.card)) throw new Error("C10 child frame is invalid");
  const endpoint = new URL(value.endpoint);
  if (endpoint.protocol !== "http:" || endpoint.hostname !== "127.0.0.1" || endpoint.port === "" || endpoint.pathname !== "/" || endpoint.search || endpoint.hash || endpoint.username || endpoint.password) throw new Error("C10 child endpoint is not issued loopback");
  return value as Issued;
}

async function main(): Promise<void> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) chunks.push(Buffer.from(chunk));
  const issued = parseIssued(Buffer.concat(chunks).toString("utf8").trim());
  const response = await fetch(`${issued.endpoint}${PATH}`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-c10-browser-authorization": issued.authorization,
      "x-firecrawl-host-transport-key": issued.hostTransportKey,
    },
    body: JSON.stringify({ capability: issued.capability, card: issued.card }),
  });
  if (!response.ok) throw new Error("C10 sidecar rejected issued capability");
  const evidence: unknown = await response.json();
  if (!evidence || typeof evidence !== "object" || Array.isArray(evidence)) throw new Error("C10 sidecar returned invalid evidence");
  process.stdout.write(`${JSON.stringify(evidence)}\n`);
}

main().catch(() => process.exitCode = 1);
