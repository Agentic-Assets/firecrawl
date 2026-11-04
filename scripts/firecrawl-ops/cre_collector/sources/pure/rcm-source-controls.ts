/** Bounded source-reported flags, represented so collector pruning cannot erase false or null. */
export function rcmSourceControls(value: unknown, keys: readonly string[]): Record<string, { state: string; scalarJson?: string }> {
  const source = value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
  const out: Record<string, { state: string; scalarJson?: string }> = {};
  for (const key of keys) {
    if (!Object.hasOwn(source, key)) { out[key] = { state: "missing" }; continue; }
    const item = source[key];
    if (item === null) { out[key] = { state: "null", scalarJson: "null" }; continue; }
    if (typeof item === "boolean") { out[key] = { state: item ? "true" : "false", scalarJson: JSON.stringify(item) }; continue; }
    if (typeof item === "string" && Buffer.byteLength(item, "utf8") <= 256 &&
        !/(?:authorization|password|passwd|secret|credential|cookie|api[_ -]?key|access[_ -]?token)/i.test(item) &&
        (!/^https?:\/\//i.test(item) || rcmPublicContactUrl(item))) {
      out[key] = { state: "string", scalarJson: JSON.stringify(item) }; continue;
    }
    if (typeof item === "number" && Number.isFinite(item)) {
      out[key] = { state: "number", scalarJson: JSON.stringify(item) }; continue;
    }
    out[key] = { state: "unresolved" };
  }
  return out;
}
export const RCM_CONTACT_FLAGS = ["ShowName", "ShowEmail", "ShowPhone", "ShowTitle", "ShowCompany", "ShowLicense", "ShowProfileImage", "ShowExpertBio"] as const;

/** Reject credentials and nonpublic literal hosts in contact links; this does not fetch them. */
export function rcmPublicContactUrl(value: string | null): string | null {
  if (!value) return null;
  try {
    const u = new URL(value);
    const host = u.hostname.toLowerCase().replace(/\.+$/, "");
    if (u.protocol !== "https:" || u.username || u.password || u.hash || host === "localhost" ||
        host.endsWith(".localhost") || host.endsWith(".local") || /^[0-9.]+$/.test(host) ||
        u.hostname.includes(":") || /%(?![0-9a-f]{2})/i.test(u.search)) return null;
    const secret = /^(?:token|access[_-]?token|api[_-]?key|key|signature|sig|password|secret|session|access[_-]?key(?:[_-]?id)?|x-(?:amz|goog)-(?:signature|credential|security[_-]?token))$/i;
    for (let key of u.searchParams.keys()) {
      for (let n = 0; n < 5; n++) {
        if (secret.test(key)) return null;
        const decoded = decodeURIComponent(key);
        if (decoded === key) break;
        key = decoded;
        if (n === 4) return null;
      }
    }
    return u.href;
  } catch { return null; }
}

/** Permit ordinary source price prose, while rejecting recognized credential-bearing content. */
export function rcmSafePriceText(value: string): boolean {
  if (/(?:authorization|password|passwd|secret|credential|cookie|api[_ -]?key|access[_ -]?token)/i.test(value)) return false;
  return (value.match(/https?:\/\/[^\s<>"']+/gi) ?? []).every(url => rcmPublicContactUrl(url) !== null);
}
