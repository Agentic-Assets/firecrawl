/** Bounded native page context; no source identity or listing qualification. */
import * as cheerio from "cheerio";
import { captureDetailTables } from "../../lib/detail-tables/index.js";

const MAX_ITEMS = 1000;
const MAX_TEXT = 250_000;
const MAX_ITEM_TEXT = 64_000;

export function captureFoundryPageContext(html: string, sourceUrl: string, detailObservedAt?: string) {
  const $ = cheerio.load(html);
  let remaining = MAX_TEXT;
  let omittedCharacters = 0;
  const bounded = (text: string): string => {
    let size = Math.min(text.length, MAX_ITEM_TEXT, remaining);
    if (size > 0 && size < text.length && /[\uD800-\uDBFF]/.test(text[size - 1]!) && /[\uDC00-\uDFFF]/.test(text[size]!)) size--;
    remaining -= size;
    omittedCharacters += text.length - size;
    return text.slice(0, size);
  };
  const capture = <T>(selector: string, map: (node: cheerio.Cheerio<any>) => T) => {
    const nodes = $(selector).toArray();
    return { values: nodes.slice(0, MAX_ITEMS).map((node) => map($(node))), omitted: Math.max(0, nodes.length - MAX_ITEMS) };
  };
  const scripts = capture('script[type="application/ld+json"], script[type="application/json"]', (node) => ({
    type: node.attr("type") === undefined ? null : bounded(node.attr("type")!), text: bounded(node.text()),
  }));
  const descriptions = capture(".property-description", (node) => bounded(node.text()));
  const headings = capture("h1,h2,h3", (node) => bounded(node.text()));
  const documentLinks = capture("link[rel]", (node) => ({
    rel: node.attr("rel") === undefined ? null : bounded(node.attr("rel")!),
    href: node.attr("href") === undefined ? null : bounded(node.attr("href")!),
  }));
  const metadata = capture("meta", (node) => ({
    name: node.attr("name") === undefined ? null : bounded(node.attr("name")!),
    property: node.attr("property") === undefined ? null : bounded(node.attr("property")!),
    content: node.attr("content") === undefined ? null : bounded(node.attr("content")!),
  }));
  const links = capture("a", (node) => ({ href: node.attr("href") === undefined ? null : bounded(node.attr("href")!), text: bounded(node.text()) }));
  const images = capture("img", (node) => ({ src: node.attr("src") === undefined ? null : bounded(node.attr("src")!), alt: node.attr("alt") === undefined ? null : bounded(node.attr("alt")!) }));
  return {
    schema: "foundry-native-page-context/v1" as const,
    sourceUrl,
    detailObservedAt: detailObservedAt ?? null,
    identity: "unverified" as const,
    visibility: "unverified" as const,
    scripts: scripts.values,
    descriptions: descriptions.values,
    headings: headings.values,
    documentLinks: documentLinks.values,
    metadata: metadata.values,
    links: links.values,
    images: images.values,
    tables: captureDetailTables(html, { sourceUrl, observedAt: detailObservedAt ?? "" }),
    omissions: { scripts: scripts.omitted, descriptions: descriptions.omitted, headings: headings.omitted, documentLinks: documentLinks.omitted,
      metadata: metadata.omitted, links: links.omitted,
      images: images.omitted, characters: omittedCharacters },
  };
}
