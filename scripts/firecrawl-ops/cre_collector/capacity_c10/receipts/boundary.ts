import { C10ReceiptError } from "./contracts.js";

const SOURCE_KEYS = new Set([
  "avison-young", "bull-realty", "cbre", "cbre-dealflow", "colliers", "colliers-main",
  "cushman-wakefield", "daum-commercial", "foundry-commercial", "jll", "jll-investor",
  "lee-associates", "marcus-millichap", "matthews", "nai-global", "newmark", "savills",
  "srs", "svn", "transwestern",
]);
const FORBIDDEN_FLAG = /^(--(?:monitor|mark-missing|status|activate-status|cache|performance|model|ocr|scheduler|checkpoint|out|enrich))(?:=|$)/;
const FORBIDDEN_ENV = /(CACHE|MONITOR|MARK_MISSING|STATUS|PERFORMANCE|MODEL|OCR|SCHEDULER|CHECKPOINT|FIRECRAWL|OPENAI)/;

export interface ReceiptInvocation {
  readonly sourceKey: string;
  readonly receiptRoot: string;
}

/** Parse only receipt-lane controls; it deliberately has no collector command path. */
export function parseReceiptInvocation(argv: readonly string[], env: NodeJS.ProcessEnv): ReceiptInvocation {
  for (const [key, value] of Object.entries(env)) {
    if (value !== undefined && FORBIDDEN_ENV.test(key)) {
      throw new C10ReceiptError(`receipt lane rejects ${key}`);
    }
  }
  let sourceKey: string | undefined;
  let receiptRoot: string | undefined;
  for (const arg of argv) {
    if (FORBIDDEN_FLAG.test(arg)) throw new C10ReceiptError("receipt lane rejects collector controls");
    if (arg.startsWith("--source=")) sourceKey = arg.slice("--source=".length);
    else if (arg.startsWith("--receipt-root=")) receiptRoot = arg.slice("--receipt-root=".length);
    else throw new C10ReceiptError("receipt lane accepts only source and private receipt root");
  }
  if (!sourceKey || !SOURCE_KEYS.has(sourceKey) || !receiptRoot || !receiptRoot.startsWith("/")) {
    throw new C10ReceiptError("receipt invocation is incomplete or not allowlisted");
  }
  return Object.freeze({ sourceKey, receiptRoot });
}
