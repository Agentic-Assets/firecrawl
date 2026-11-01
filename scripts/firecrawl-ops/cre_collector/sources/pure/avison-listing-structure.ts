import * as cheerio from "cheerio";
import { stripHtmlText } from "../../lib/html.js";
import { clean, num } from "../../lib/util.js";
import type { ScrapedDoc } from "../../types.js";

// Mirrored by cre_backfill_raw_data.py avison_price_per_acre; keep the rules equal.
export const AVISON_ACREAGE_TOLERANCE = 0.02;
const ACRES_RE = /([0-9]+(?:\.[0-9]+)?)\s*(?:\+\s*\/\s*-|±)?\s*(?:ac|acres?)\b/gi;

// The detail page fact list's "Sale type" value ("Investment", "Owner User"),
// verbatim. The first page that states one wins; nothing is interpreted here.
export function avisonYoungSaleType(docs: Array<{ doc: ScrapedDoc }>): string | null {
  for (const { doc } of docs) {
    if (typeof doc?.rawHtml !== "string" || !doc.rawHtml) continue;
    const $ = cheerio.load(doc.rawHtml);
    for (const li of $("li").toArray()) {
      const spans = $(li).children("span");
      if (spans.length === 2 && clean(spans.first().text())?.toLowerCase() === "sale type") {
        const value = clean(spans.last().text());
        if (value) return value;
      }
    }
  }
  return null;
}

// SharpLaunch gives sale_unit_price with no unit. On a land row it is a price
// per acre only when sale_price / sale_unit_price reproduces the stated acreage
// (total_surface in acres, or "N acres" in the name, meta title or description)
// within the tolerance. A withheld price (hide_price 1) yields nothing.
export function avisonYoungPricePerAcre(row: any): number | null {
  if (row?.hide_price === 1) return null;
  const unitPrice = num(row?.sale_unit_price);
  const price = num(row?.sale_price);
  const types = Array.isArray(row?.type) ? row.type : [];
  if (clean(row?.sale_currency)?.toUpperCase() !== "USD") return null;
  if (!unitPrice || !price || unitPrice <= 0 || unitPrice >= price) return null;
  if (!types.some((t: unknown) => typeof t === "string" && /^land\b/i.test(t))) return null;
  const acreages: number[] = [];
  if (/^(?:ac|acres?)$/i.test(clean(row?.total_surface_unit) ?? "") && num(row?.total_surface)) {
    acreages.push(Number(row.total_surface));
  }
  const prose = [row?.name, row?.meta_title, row?.description].map(stripHtmlText).filter(Boolean).join(" ");
  for (const m of prose.replace(/,/g, "").matchAll(ACRES_RE)) acreages.push(Number(m[1]));
  const implied = price / unitPrice;
  return acreages.some((acres) => acres > 0 && Math.abs(implied - acres) <= AVISON_ACREAGE_TOLERANCE * acres)
    ? unitPrice
    : null;
}
