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
  const reference = (node: cheerio.Cheerio<any>, name: "href" | "src") => {
    const original = node.attr(name);
    if (original === undefined) return { value: null, truncated: false, prefix: undefined };
    const value = bounded(original);
    return value.length === original.length
      ? { value, truncated: false, prefix: undefined }
      : { value: null, truncated: true, prefix: value };
  };
  const scripts = capture('script[type="application/ld+json"], script[type="application/json"]', (node) => {
    const type = node.attr("type") === undefined ? null : bounded(node.attr("type")!);
    const original = node.text();
    const text = bounded(original);
    return { type, text,
      ...(text.length === original.length ? {} : { textComplete: false }) };
  });
  const descriptions = capture(descriptionSelector, (node) => bounded(node.text()));
  const headings = capture("h1,h2,h3", (node) => bounded(node.text()));
  const documentLinks = capture("link[rel]", (node) => {
    const rel = node.attr("rel") === undefined ? null : bounded(node.attr("rel")!);
    const href = reference(node, "href");
    return { rel, href: href.value,
      ...(href.truncated ? { referenceIntegrity: { href: "truncated" as const }, referencePrefix: href.prefix } : {}),
      ...nativeAttributes(node, ["type", "title"]) };
  });
  const metadata = capture("meta", (node) => ({
    name: node.attr("name") === undefined ? null : bounded(node.attr("name")!),
    property: node.attr("property") === undefined ? null : bounded(node.attr("property")!),
    content: node.attr("content") === undefined ? null : bounded(node.attr("content")!),
  }));
  const links = capture("a", (node) => {
    const href = reference(node, "href");
    return { href: href.value, text: bounded(node.text()),
      ...(href.truncated ? { referenceIntegrity: { href: "truncated" as const }, referencePrefix: href.prefix } : {}),
      ...nativeAttributes(node, ["title", "aria-label", "download", "type"]) };
  });
  const images = capture("img", (node) => {
    const src = reference(node, "src");
    return { src: src.value, alt: node.attr("alt") === undefined ? null : bounded(node.attr("alt")!),
      ...(src.truncated ? { referenceIntegrity: { src: "truncated" as const }, referencePrefix: src.prefix } : {}) };
  });
  // When any text was omitted, explicitly distinguish intact references and
  // scripts from legacy shapes where aggregate omissions cannot identify them.
  const scriptsOut = scripts.values.map((item) => omittedCharacters && item.textComplete === undefined
    ? { ...item, textComplete: true } : item);
  const linksOut = links.values.map((item) => omittedCharacters && item.href !== null && item.referenceIntegrity === undefined
    ? { ...item, referenceIntegrity: { href: "complete" as const } } : item);
  const documentLinksOut = documentLinks.values.map((item) => omittedCharacters && item.href !== null && item.referenceIntegrity === undefined
    ? { ...item, referenceIntegrity: { href: "complete" as const } } : item);
  const imagesOut = images.values.map((item) => omittedCharacters && item.src !== null && item.referenceIntegrity === undefined
    ? { ...item, referenceIntegrity: { src: "complete" as const } } : item);
  return {
    schema: "cre-source-page-context/v1" as const,
    sourceUrl,
    detailObservedAt: detailObservedAt ?? null,
    identity: "unverified" as const,
    visibility: "unverified" as const,
    scripts: scriptsOut,
    descriptions: descriptions.values,
    headings: headings.values,
    documentLinks: documentLinksOut,
    metadata: metadata.values,
    links: linksOut,
    images: imagesOut,
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
  // Adjacent DOM text nodes otherwise concatenate (e.g. "123Checking"),
  // hiding a word boundary that was present between separate elements.
  let bodyText = "";
  let joinedText = "";
  const stack: any[] = [...(($('body').get(0) as any)?.children ?? [])].reverse();
  while (stack.length && (bodyText.length < 3000 || joinedText.length < 3000)) {
    const node = stack.pop();
    if (node.type === "text") {
      const text = typeof node.data === "string" ? node.data : "";
      joinedText += text.slice(0, 3000 - joinedText.length);
      bodyText += text.slice(0, 3000 - bodyText.length);
      if (bodyText.length < 3000) bodyText += " ";
    } else if (Array.isArray(node.children)) {
      for (let index = node.children.length - 1; index >= 0; index--) stack.push(node.children[index]);
    }
  }
  const bodyChallenge = /\b(?:verify you are human|checking your browser|challenge-platform|cf-chl-)\b/i;
  return /\b(?:404|not found|access denied|forbidden|captcha|just a moment|service unavailable)\b/i.test(heading) ||
    bodyChallenge.test(bodyText) || bodyChallenge.test(joinedText);
}
