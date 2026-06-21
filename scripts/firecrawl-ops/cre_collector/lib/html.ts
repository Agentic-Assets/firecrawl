// lib/html.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { clean } from "./util.js";


export function decodeHtmlEntities(s: string): string {
  return s
    .replace(/\\u0026/g, "&")
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .replace(/&#34;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">");
}

export function titleFromFilename(url: string): string {
  try {
    const u = new URL(url);
    const last = decodeURIComponent(u.pathname.split("/").filter(Boolean).slice(-1)[0] ?? "");
    return (
      clean(
        last
          .replace(/\.[a-z0-9]+$/i, "")
          .replace(/[-_]+/g, " ")
          .replace(/\s+/g, " ")
      ) ?? "Document"
    );
  } catch {
    return "Document";
  }
}

export function jsonLdObjects(rawHtml: string): any[] {
  const $ = cheerio.load(rawHtml);
  const out: any[] = [];
  const visit = (value: any) => {
    if (!value) return;
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (typeof value === "object") {
      out.push(value);
      if (value["@graph"]) visit(value["@graph"]);
    }
  };
  $('script[type="application/ld+json"]').each((_, el) => {
    const txt = clean($(el).text());
    if (!txt) return;
    try {
      visit(JSON.parse(txt));
    } catch {
      /* ignore malformed embedded JSON-LD */
    }
  });
  return out;
}

export function firstJsonLd(rawHtml: string, type: string): any | null {
  const wanted = type.toLowerCase();
  return (
    jsonLdObjects(rawHtml).find((obj) => {
      const t = obj["@type"];
      return Array.isArray(t)
        ? t.map((x: any) => String(x).toLowerCase()).includes(wanted)
        : String(t ?? "").toLowerCase() === wanted;
    }) ?? null
  );
}

export function stripHtmlText(html: any): string | null {
  if (typeof html !== "string") return null;
  return clean(cheerio.load(`<body>${html}</body>`).text());
}

export function extractSitemapUrlEntries(xml: string): Array<{ loc: string; lastmod: string | null }> {
  const out: Array<{ loc: string; lastmod: string | null }> = [];
  for (const m of xml.matchAll(/<url>([\s\S]*?)<\/url>/g)) {
    const block = m[1];
    const loc = block.match(/<loc>\s*([^<]+?)\s*<\/loc>/)?.[1];
    if (!loc) continue;
    const lastmod = block.match(/<lastmod>\s*([^<]+?)\s*<\/lastmod>/)?.[1]?.trim() ?? null;
    out.push({ loc: decodeHtmlEntities(loc).trim(), lastmod });
  }
  return out;
}

export function dedupeStrings(values: Array<string | null | undefined>): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const value of values) {
    const v = clean(value);
    if (!v || seen.has(v)) continue;
    seen.add(v);
    out.push(v);
  }
  return out;
}
