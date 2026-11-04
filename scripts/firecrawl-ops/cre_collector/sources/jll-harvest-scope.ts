// AGENTIC-4342: property ownership for harvested JLL children, not URL identity.
import * as cheerio from "cheerio";
import type { AnyNode } from "domhandler";
import type { ScrapedDoc } from "../types.js";

export type JllHarvestScope = "recommendations_scoped" | "unknown";

type Srcset = { candidates: string[]; fragments: string[] };

// Descriptors delimit candidates; commas inside a Cloudinary URL are URL data.
// Ambiguous/unsupported srcsets provide no exclusion authority.
function srcsetUrls(value: string, resolve: (value: string) => string | null): Srcset {
  const candidates: string[] = [];
  let rest = value.trim();
  while (rest) {
    const match = /^(\S+)\s+(?:\d+w|\d+(?:\.\d+)?x)(?:\s*,\s*|$)/.exec(rest);
    if (!match) return { candidates: [], fragments: [] };
    const url = resolve(match[1]!);
    if (!url) return { candidates: [], fragments: [] };
    candidates.push(url);
    rest = rest.slice(match[0].length);
  }
  const fragments = candidates.some(url => url.includes(","))
    ? value.split(",").map(part => resolve(part.trim().split(/\s+/)[0] ?? ""))
      .filter((url): url is string => url !== null && !candidates.includes(url))
    : [];
  return { candidates, fragments };
}

export function jllPropertyHarvestDoc(
  doc: ScrapedDoc,
  baseUrl: string,
  nativeAssets: readonly string[],
  brokerNames: readonly string[] = [],
): { doc: ScrapedDoc; scope: JllHarvestScope } {
  const resolve = (value: string): string | null => {
    try {
      const url = new URL(value, baseUrl);
      return value.trim() && ["http:", "https:"].includes(url.protocol) ? url.href : null;
    } catch { return null; }
  };
  const $ = cheerio.load(doc.rawHtml ?? "");
  const baseOrigin = new URL(baseUrl).origin;
  const headings = $("h2").filter((_, node) => $(node).text().trim() === "Related properties");
  const excluded = new Set<string>();
  const protectedUrls = new Set(nativeAssets.map(resolve).filter((url): url is string => url !== null));
  const fragments = new Set<string>();
  const retainedSrcsetImages = new Set<string>();
  const collect = (nodes: cheerio.Cheerio<AnyNode>, urls: Set<string>) => {
    nodes.find("[href], [src], [data-src], [data-lazy-src], [poster], [srcset], [style]").addBack().each((_, node) => {
      const element = $(node);
      for (const attribute of ["href", "src", "data-src", "data-lazy-src", "poster"]) {
        const value = element.attr(attribute);
        const url = value && resolve(value);
        if (url) urls.add(url);
      }
      // A document cover may be a CSS background rather than an img. Parse
      // only this image-bearing property, not arbitrary style text or JSON.
      const background = element.css("background-image") ?? "";
      const backgrounds = background.matchAll(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^'"\s)]+))\s*\)/gi);
      for (const match of backgrounds) {
        const url = resolve(match[1] ?? match[2] ?? match[3] ?? "");
        if (url) urls.add(url);
      }
      const srcset = srcsetUrls(element.attr("srcset") ?? "", resolve);
      for (const url of srcset.candidates) urls.add(url);
      for (const url of srcset.fragments) fragments.add(url);
    });
  };
  // Flat API image arrays can contain malformed pieces from srcset.split(',').
  // Derive only pieces actually present in this observation, never guess a path.
  collect($.root(), new Set());
  let scope: JllHarvestScope = "unknown";
  if (headings.length === 1) {
    const carousel = headings.next("div").children(".embla");
    const links = carousel.find("a[href]");
    const own = new URL(baseUrl);
    const allOtherProperties = links.length > 0 && links.toArray().every(node => {
      const value = resolve($(node).attr("href") ?? "");
      if (!value) return false;
      const url = new URL(value);
      return url.origin === own.origin && /^\/listings\/[^/]+\/?$/.test(url.pathname)
        && url.pathname.replace(/\/+$/, "") !== own.pathname.replace(/\/+$/, "");
    });
    if (carousel.length === 1 && allOtherProperties) {
      collect(carousel, excluded);
      carousel.remove();
      scope = "recommendations_scoped";
    }
  }
  // Observed city taxonomy cards are not property galleries. Require their
  // heading, carousel/card, route, unavailable label and static asset together;
  // a placeholder basename or a generic carousel never supplies authority.
  $("h2").each((_, node) => {
    const heading = $(node);
    const match = /^Similar (Industrials|Offices) in .+$/.exec(heading.text().trim());
    if (!match) return;
    const carousel = heading.next("div").children(".embla");
    if (carousel.length !== 1) return;
    const route = match[1] === "Industrials" ? "rent-industrial" : "rent-office";
    carousel.find('.embla-container > .embla-slide a[href] img[alt="Image Unavailable"]').each((_, node) => {
      const image = $(node);
      const href = resolve(image.closest("a[href]").attr("href") ?? "");
      if (!href) return;
      const link = new URL(href);
      if (link.origin !== baseOrigin || !new RegExp(`^/${route}/[^/]+/?$`).test(link.pathname)) return;
      if (!image.siblings("div").toArray().some(label => $(label).text().trim() === "Image Unavailable")) return;
      const srcset = image.attr("srcset") ?? "";
      if (srcset.trim() && !srcsetUrls(srcset, resolve).candidates.length) return;
      const urls = new Set<string>();
      collect(image, urls);
      if (!urls.size || ![...urls].every(value => {
        const asset = new URL(value);
        return asset.origin === baseOrigin && asset.pathname === "/_next/image"
          && /^\/_next\/static\/media\/default-img-small@2x\.[a-f0-9]+\.jpg$/.test(asset.searchParams.get("url") ?? "");
      })) return;
      for (const value of urls) excluded.add(value);
      image.remove();
    });
  });
  // These images have explicit non-gallery roles. Broker portraits remain in
  // contacts; consent UI stays in the original capture. Do not classify by CDN
  // basename, a person's name alone, or a generic logo/face heuristic.
  $("img").each((_, node) => {
    const image = $(node);
    const card = image.closest('[data-slot="button"]');
    const brokerName = image.attr("alt");
    const brokerPortrait = Boolean(brokerName && brokerNames.includes(brokerName))
      && card.find("span").toArray().some(span => $(span).text().trim() === "Broker details");
    if (brokerPortrait || image.closest("#onetrust-consent-sdk").length > 0) {
      collect(image, excluded);
      image.remove();
    }
  });
  // Native fields and explicit retained DOM references take precedence over
  // recommendation membership, including shared documents and image URLs.
  collect($.root(), protectedUrls);
  $("img[srcset], source[srcset]").each((_, node) => {
    for (const url of srcsetUrls($(node).attr("srcset") ?? "", resolve).candidates) retainedSrcsetImages.add(url);
  });
  const reject = (value: string): boolean => {
    const url = resolve(value);
    return url !== null && !protectedUrls.has(url) && (excluded.has(url) || fragments.has(url));
  };
  const tile = (value: string): boolean => {
    const url = resolve(value);
    if (!url || protectedUrls.has(url) && nativeAssets.some(asset => resolve(asset) === url)) return false;
    const parsed = new URL(url);
    return parsed.hostname === "maps.googleapis.com" && parsed.pathname === "/maps/vt";
  };
  $("img[src]").each((_, node) => { if (tile($(node).attr("src")!)) $(node).remove(); });
  const filtered: ScrapedDoc = {
    ...doc,
    rawHtml: $.html(),
    ...(Array.isArray(doc.images) ? { images: [...new Set([...doc.images, ...retainedSrcsetImages])].filter(value => !reject(value) && !tile(value)) } : {}),
    ...(Array.isArray(doc.links) ? { links: doc.links.filter(value => !reject(value)) } : {}),
    ...(Array.isArray(doc.attributes) ? { attributes: doc.attributes.map(block => ({
      ...block,
      values: Array.isArray(block.values) ? block.values.filter(value => !reject(value)) : block.values,
    })) } : {}),
  };
  return { doc: filtered, scope };
}
