import { clean } from "../../lib/util.js";

export function newmarkNimCanonicalIdentity(record: any): {
  id: string | null;
  url: string | null;
} {
  const slug = clean(record?.slug);
  const safeSlug =
    slug
    && slug !== "."
    && slug !== ".."
    && /^[A-Za-z0-9._~-]+$/.test(slug)
      ? slug
      : null;
  const providerUrl = clean(record?.externalWebsiteUrl);
  if (!providerUrl) {
    if (slug && !safeSlug) {
      throw new Error("Newmark NIM fallback slug identity is unsafe");
    }
    return safeSlug
      ? {
          id: safeSlug,
          url: `https://www.nmrk.com/properties/${encodeURIComponent(safeSlug)}`,
        }
      : { id: null, url: null };
  }
  let parsed: URL;
  try {
    parsed = new URL(providerUrl);
  } catch {
    throw new Error("Newmark NIM externalWebsiteUrl is not a valid URL");
  }
  const hostname = parsed.hostname.toLowerCase();
  if (
    parsed.protocol !== "https:"
    || parsed.username
    || parsed.password
    || parsed.port
  ) {
    throw new Error("Newmark NIM externalWebsiteUrl is not a safe HTTPS URL");
  }
  if (
    hostname === "my.rcm1.com"
    || hostname === "properties.nmrk.com"
  ) {
    if (slug && !safeSlug) {
      throw new Error("Newmark NIM fallback slug identity is unsafe");
    }
    return safeSlug
      ? {
          id: safeSlug,
          url: `https://www.nmrk.com/properties/${encodeURIComponent(safeSlug)}`,
        }
      : { id: null, url: null };
  }
  if (
    !["www.nmrk.com", "nmrk.com"].includes(hostname)
  ) {
    throw new Error(
      `Newmark NIM externalWebsiteUrl has an unsupported host: ${hostname}`
    );
  }
  const match = parsed.pathname.match(/^\/properties\/([^/]+)\/?$/);
  if (!match) {
    throw new Error("Newmark NIM externalWebsiteUrl has an unexpected path");
  }
  let id: string;
  try {
    id = decodeURIComponent(match[1]);
  } catch (error) {
    throw new Error(
      `Newmark NIM canonical URL has an invalid encoded identity: ${
        error instanceof Error ? error.message : String(error)
      }`
    );
  }
  if (
    id === "."
    || id === ".."
    || !/^[A-Za-z0-9._~-]+$/.test(id)
  ) {
    throw new Error("Newmark NIM canonical identity is unsafe");
  }
  return {
    id,
    url: `https://www.nmrk.com/properties/${match[1]}`,
  };
}
