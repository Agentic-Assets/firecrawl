import { clean } from "../../lib/util.js";

/** Infabode publicPosts identity is a positive decimal provider id. */
export function naiPublicPostId(value: unknown): string | null {
  const raw = typeof value === "string" || typeof value === "number" ? clean(String(value)) : null;
  return raw && /^[1-9]\d*$/.test(raw) ? raw : null;
}
