import { clean } from "../../lib/util.js";

/** Pure Infabode asset-channel normalization; no fetch, cache, or collector state. */
export function naiImageUrls(row: any, detail: any): string[] {
  const urls = [...(detail?.postImages ?? []), ...(row?.postImages ?? [])]
    .map((img: any) => clean(img?.url))
    .filter((url: string | null): url is string => !!url && /^https?:\/\//i.test(url));
  return [...new Set(urls)];
}

export function naiDocumentUrls(detail: any): string[] {
  const urls = [detail?.urlDocument, detail?.documentPreview]
    .map((url) => clean(url))
    .filter((url: string | null): url is string => !!url && /^https?:\/\//i.test(url));
  return [...new Set(urls)];
}
