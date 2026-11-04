// AGENTIC-2898: experimental HTML reconstruction, not an admitted acquisition.
import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import TurndownService from "turndown";
import type { ScrapedDoc } from "../types.js";

export type JllHtmlContext = {
  url: string;
  statusCode: number;
  contentType: string;
};

type NativeHtml = {
  transformHtml(options: {
    html: string;
    url: string;
    includeTags: string[];
    excludeTags: string[];
    onlyMainContent: boolean;
  }): Promise<string>;
  extractImages(html: string, url: string): Promise<string[]>;
  extractLinks(html: string): Promise<string[]>;
  extractBaseHref(html: string, url: string): Promise<string>;
  extractAttributes(
    html: string,
    options: { selectors: Array<{ selector: string; attribute: string }> },
  ): Promise<NonNullable<ScrapedDoc["attributes"]>>;
  postProcessMarkdown(markdown: string): Promise<string>;
  extractMetadata(html: string): Promise<Record<string, unknown>>;
};

const require = createRequire(import.meta.url);
let native: NativeHtml | undefined;
function nativeHtml(): NativeHtml {
  const entry = fileURLToPath(
    new URL("../../../../apps/api/native/index.js", import.meta.url),
  );
  if (!existsSync(entry)) {
    throw new Error(
      "JLL experimental reconstruction requires a built apps/api/native package (index.js is missing)",
    );
  }
  // Keep ABI/dependency errors intact; never substitute another binary/converter.
  return (native ??= require(entry) as NativeHtml);
}

const selectors = [
  { selector: "div[component=video]", attribute: "url" },
  { selector: "iframe", attribute: "src" },
  { selector: "a", attribute: "href" },
  { selector: "video source", attribute: "src" },
  { selector: "[data-video-url]", attribute: "data-video-url" },
];

function resolveLinks(
  values: string[],
  baseHref: string,
  url: string,
): string[] {
  let base = url;
  if (baseHref) {
    try {
      base = new URL(baseHref, url).href;
    } catch {
      /* API keeps the page URL. */
    }
  }
  const links = new Set<string>();
  for (const value of values) {
    const href = value.trim();
    try {
      const resolved = /^(https?:\/\/|mailto:)/.test(href)
        ? href
        : href.startsWith("#")
          ? ""
          : new URL(href, base).href;
      if (resolved) links.add(resolved);
    } catch {
      /* Invalid links do not become document targets. */
    }
  }
  return [...links];
}

export async function jllDirectHtmlDoc(
  rawHtml: string,
  context: JllHtmlContext,
): Promise<ScrapedDoc> {
  const api = nativeHtml();
  const html = await api.transformHtml({
    html: rawHtml,
    url: context.url,
    includeTags: [],
    excludeTags: [],
    onlyMainContent: false,
  });
  const converter = new TurndownService();
  converter.addRule("inlineLink", {
    filter: (node, options) =>
      Boolean(
        options.linkStyle === "inlined" &&
          node.nodeName === "A" &&
          node.getAttribute("href"),
      ),
    replacement: (content, node) => {
      const anchor = node as HTMLAnchorElement;
      const title = anchor.title ? ` "${anchor.title}"` : "";
      return `[${content.trim()}](${anchor.getAttribute("href")!.trim()}${title})\n`;
    },
  });
  converter.use(require("joplin-turndown-plugin-gfm").gfm);
  const markdown = await api.postProcessMarkdown(converter.turndown(html));
  if (!markdown.trim())
    throw new Error("JLL direct detail has no durable page Markdown");
  const links = resolveLinks(
    await api.extractLinks(html),
    await api.extractBaseHref(html, context.url),
    context.url,
  );
  const images = await api.extractImages(html, context.url);
  const attributes = await api.extractAttributes(html, { selectors });
  const { favicon, ...htmlMetadata } = await api.extractMetadata(rawHtml);
  if (typeof favicon === "string" && favicon) {
    try {
      htmlMetadata.favicon = new URL(favicon, context.url).href;
    } catch {
      /* Same invalid-favicon omission as the API wrapper. */
    }
  }
  // HTML-derived fields first; only known response context overlays them.
  // No request/engine facts are supplied or synthesized. HTML may itself claim
  // names such as scrapeId; those remain page metadata, not request evidence.
  return {
    rawHtml,
    markdown,
    links,
    images,
    attributes,
    metadata: {
      ...htmlMetadata,
      statusCode: context.statusCode,
      contentType: context.contentType,
      sourceURL: context.url,
      url: context.url,
    },
  };
}
