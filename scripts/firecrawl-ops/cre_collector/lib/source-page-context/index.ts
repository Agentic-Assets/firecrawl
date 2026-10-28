/** Bounded native page context; no source identity or listing qualification. */
import * as cheerio from "cheerio";
import { captureDetailTables } from "../detail-tables/index.js";
import { clean } from "../util.js";

const MAX_ITEMS = 1000;
const MAX_TEXT = 250_000;
const MAX_ITEM_TEXT = 64_000;

export function captureSourcePageContext(html: string, options: {
  sourceUrl: string; detailObservedAt?: string; descriptionSelector?: string;
}) {
  const { sourceUrl, detailObservedAt, descriptionSelector = ".property-description" } = options;
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
  const nativeAttributes = <Name extends string>(node: cheerio.Cheerio<any>, names: readonly Name[]) => {
    const attributes: Partial<Record<Name, string>> = {};
    for (const name of names) {
      const value = node.attr(name);
      if (value !== undefined) attributes[name] = bounded(value);
    }
    return attributes;
  };
  const scripts = capture('script[type="application/ld+json"], script[type="application/json"]', (node) => ({
    type: node.attr("type") === undefined ? null : bounded(node.attr("type")!), text: bounded(node.text()),
  }));
  const descriptions = capture(descriptionSelector, (node) => bounded(node.text()));
  const headings = capture("h1,h2,h3", (node) => bounded(node.text()));
  const documentLinks = capture("link[rel]", (node) => ({
    rel: node.attr("rel") === undefined ? null : bounded(node.attr("rel")!),
    href: node.attr("href") === undefined ? null : bounded(node.attr("href")!),
    ...nativeAttributes(node, ["type", "title"]),
  }));
  const metadata = capture("meta", (node) => ({
    name: node.attr("name") === undefined ? null : bounded(node.attr("name")!),
    property: node.attr("property") === undefined ? null : bounded(node.attr("property")!),
    content: node.attr("content") === undefined ? null : bounded(node.attr("content")!),
  }));
  const links = capture("a", (node) => ({
    href: node.attr("href") === undefined ? null : bounded(node.attr("href")!), text: bounded(node.text()),
    ...nativeAttributes(node, ["title", "aria-label", "download", "type"]),
  }));
  const images = capture("img", (node) => ({ src: node.attr("src") === undefined ? null : bounded(node.attr("src")!), alt: node.attr("alt") === undefined ? null : bounded(node.attr("alt")!) }));
  return {
    schema: "cre-source-page-context/v1" as const,
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

/** Match only the existing Foundry title, heading and bounded body shell signals. */
export function isSourceChallengeShell(html: string): boolean {
  const $ = cheerio.load(html);
  const heading = [clean($("title").first().text()), clean($("h1").first().text())].filter(Boolean).join(" ");
  return /\b(?:404|not found|access denied|forbidden|captcha|just a moment|service unavailable)\b/i.test(heading) ||
    /\b(?:verify you are human|checking your browser|challenge-platform|cf-chl-)\b/i.test($("body").text().slice(0, 3000));
}
