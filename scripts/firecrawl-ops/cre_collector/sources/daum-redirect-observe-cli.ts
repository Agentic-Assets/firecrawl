import { DaumPoliteTransport } from "./daum-transport.js";
import { observeDaumMove } from "./daum-redirect-receipt.js";

// Private, read-only evidence child. The caller must rederive the candidate.
const chunks: Buffer[] = [];
let size = 0;
for await (const chunk of process.stdin) {
  const part = Buffer.from(chunk);
  size += part.length;
  if (size > 32_768) throw new Error("DAUM candidate input too large");
  chunks.push(part);
}
const candidate = JSON.parse(Buffer.concat(chunks).toString("utf8"));
const receipt = await observeDaumMove(candidate, new DaumPoliteTransport());
process.stdout.write(JSON.stringify(receipt));
