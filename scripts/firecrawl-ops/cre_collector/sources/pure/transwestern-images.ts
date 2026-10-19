import { canonicalTranswesternUrl } from "./transwestern-identity.js";

type ExcludedImage = {
  url: string;
  role: "broker_portrait" | "template_resource";
  basis: "parsed_contact_avatar" | "existing_source_template_policy";
};

export function isTranswesternTemplateImage(url: string): boolean {
  const canonical = canonicalTranswesternUrl(url);
  if (!canonical) return false;
  const parsed = new URL(canonical);
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
  let pathname = parsed.pathname;
  if (parsed.hostname === "images.transwestern.com") {
    pathname = pathname.replace(/^\/f[0-9]+\/transwestern\.com(?=\/assets\/images\/)/, "");
  } else if (parsed.hostname !== "transwestern.com") {
    return false;
  }
  return /^\/assets\/images\/(?:tw_gl\.png|connect-image\.png|transwestern-mapmarker\.png|mail-3\.png)$/.test(pathname);
}

export function filterTranswesternImages(
  images: readonly string[],
  { nativePropertyUrls, brokerAvatarUrls }: { nativePropertyUrls: readonly string[]; brokerAvatarUrls: readonly string[] },
): { images: string[]; excluded: ExcludedImage[] } {
  const native = new Set(nativePropertyUrls.map(canonicalTranswesternUrl));
  const avatars = new Set(brokerAvatarUrls.map(canonicalTranswesternUrl));
  const retained: string[] = [];
  const excluded: ExcludedImage[] = [];
  const recorded = new Set<string>();
  for (const url of images) {
    const canonical = canonicalTranswesternUrl(url);
    if (canonical && native.has(canonical)) {
      retained.push(url);
      continue;
    }
    const role = canonical && avatars.has(canonical)
      ? "broker_portrait"
      : isTranswesternTemplateImage(url) ? "template_resource" : null;
    if (!role) {
      retained.push(url);
      continue;
    }
    const key = canonical ?? url;
    if (!recorded.has(key)) {
      recorded.add(key);
      excluded.push({
        url,
        role,
        basis: role === "broker_portrait" ? "parsed_contact_avatar" : "existing_source_template_policy",
      });
    }
  }
  return { images: retained, excluded };
}
