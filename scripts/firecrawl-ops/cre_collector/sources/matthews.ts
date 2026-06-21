import * as cheerio from "cheerio";
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { clean, moneyToNumber, pmap, prune } from "../lib/util.js";
import { SourceResult, Tx } from "../types.js";

const MATTHEWS_HOST = "https://www.matthews.com";
const MATTHEWS_SOURCE_URL = `${MATTHEWS_HOST}/listings`;
const MATTHEWS_SITEMAP_URL = `${MATTHEWS_HOST}/sitemap.xml`;
const MATTHEWS_NON_PHOTO = /headshot|web-use|brand-logo|logo|og-default|placeholder|favicon|sprite/i;

let matthewsNextSlot = 0;
let matthewsInterval = 1800;

const matthewsSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

async function matthewsGate(): Promise<void> {
  const now = Date.now();
  const slot = Math.max(now, matthewsNextSlot);
  matthewsNextSlot = slot + matthewsInterval;
  const wait = slot - now;
  if (wait > 0) await matthewsSleep(wait);
}

async function matthewsFetch(url: string): Promise<string> {
  for (let attempt = 0; attempt < 6; attempt++) {
    await matthewsGate();
    let status = 0;
    try {
      const res = await fetch(url, {
        headers: {
          "User-Agent":
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
          Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
      });
      status = res.status;
      if (res.ok) return res.text();
    } catch {
      /* retry transient network failures below */
    }
    if (status === 0 || status === 429 || status === 403 || status === 503) {
      matthewsInterval = Math.min(matthewsInterval + 700, 7000);
      await matthewsSleep(20000 + attempt * 15000 + Math.random() * 5000);
      continue;
    }
    throw new Error(`Matthews HTTP ${status}`);
  }
  throw new Error("Matthews: throttled after retries");
}

function matthewsImages(html: string): string[] {
  const urls: string[] = [];
  const seen = new Set<string>();
  const add = (raw: string | null) => {
    if (!raw) return;
    let url = raw.trim();
    if (url.startsWith("//")) url = "https:" + url;
    if (!/^https:\/\/cms\.matthews\.com\/wp-content\/uploads\//i.test(url)) return;
    if (MATTHEWS_NON_PHOTO.test(url) || seen.has(url)) return;
    seen.add(url);
    urls.push(url);
  };

  const nextRe = /\/_next\/image\?url=([^&"'\\ ]+)/gi;
  let match: RegExpExecArray | null;
  while ((match = nextRe.exec(html))) {
    try {
      add(decodeURIComponent(match[1]));
    } catch {
      /* ignore malformed image proxy URLs */
    }
  }

  const directRe =
    /https?:\/\/cms\.matthews\.com\/wp-content\/uploads\/[^"'\\ )]+?\.(?:jpe?g|png|webp)/gi;
  while ((match = directRe.exec(html))) add(match[0]);
  return urls;
}

function matthewsBrokers($: cheerio.CheerioAPI): {
  name: string | null;
  email: string | null;
  phone: string | null;
  avatarUrl: string | null;
}[] {
  const out: {
    name: string | null;
    email: string | null;
    phone: string | null;
    avatarUrl: string | null;
  }[] = [];

  $('a[id="agentName"]').each((_, el) => {
    const name = clean($(el).text());
    if (!name || out.some((broker) => broker.name === name)) return;

    let card = $(el);
    for (let i = 0; i < 6; i++) {
      const parent = card.parent();
      if (parent.length === 0) break;
      card = parent;
      if (card.find('a[href^="tel:"], a[href^="mailto:"]').length > 0) break;
    }

    const mailHref = card.find('a[href^="mailto:"]').first().attr("href") ?? "";
    const telText = clean(card.find('a[href^="tel:"]').first().text());
    const telHref = card.find('a[href^="tel:"]').first().attr("href") ?? "";
    let avatar = card.find('img[src*="cms.matthews.com"]').first().attr("src") ?? null;
    if (avatar?.startsWith("//")) avatar = "https:" + avatar;

    out.push({
      name,
      email: clean(mailHref.replace(/^mailto:/i, "").split("?")[0]),
      phone: telText || clean(telHref.replace(/^tel:/i, "")),
      avatarUrl: avatar?.startsWith("http") ? avatar : null,
    });
  });

  return out;
}

function parseMatthewsAddress(line: string | null): {
  street: string | null;
  city: string | null;
  state: string | null;
  postalCode: string | null;
} {
  const out = {
    street: null as string | null,
    city: null as string | null,
    state: null as string | null,
    postalCode: null as string | null,
  };
  if (!line) return out;

  const parts = line
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
  const stateZip = parts[parts.length - 1]?.match(/^([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$/);
  if (stateZip) {
    out.state = stateZip[1];
    out.postalCode = stateZip[2];
    parts.pop();
  } else {
    if (parts.length && /^\d{5}(-\d{4})?$/.test(parts[parts.length - 1])) {
      out.postalCode = parts.pop()!;
    }
    if (parts.length && /^[A-Z]{2}$/.test(parts[parts.length - 1])) {
      out.state = parts.pop()!;
    }
  }
  if (parts.length) out.city = parts.pop()!;
  if (parts.length) out.street = parts.join(", ");
  return out;
}

function matthewsDetailUrlsFromSitemap(xml: string): string[] {
  return Array.from(new Set(xml.match(/https:\/\/www\.matthews\.com\/properties\/[^<\s"')]+/gi) ?? []));
}

export function matthewsTenureFromUrl(url: string): Tx {
  return /\/properties\/leasing-/i.test(url) ? "lease" : "sale";
}

export function parseMatthewsDetail(html: string, url: string, tx: Tx): any | null {
  const $ = cheerio.load(html);
  const title = clean($("#propertyTitle").first().text()) || clean($("h1").first().text());
  const photos = matthewsImages(html);
  if (!title && photos.length === 0) return null;

  const addr = parseMatthewsAddress(clean($("#propertyAddress").first().text()));
  const priceText = clean($("#propertyPrice").first().text());
  const realPrice =
    priceText && !/call|inquire|contact|request|tbd|offer/i.test(priceText) ? priceText : null;

  const labels: string[] = [];
  const values: string[] = [];
  $(".key-info-title").each((_, el) => {
    const text = clean($(el).text());
    if (text) labels.push(text.replace(/:$/, ""));
  });
  $(".key-info-value").each((_, el) => {
    values.push(clean($(el).text()) ?? "");
  });

  const facts: Record<string, string> = {};
  for (let i = 0; i < Math.min(labels.length, values.length); i++) {
    if (labels[i] && values[i] && !(labels[i] in facts)) facts[labels[i]] = values[i];
  }
  const factGet = (re: RegExp): string | null => {
    const key = Object.keys(facts).find((factKey) => re.test(factKey));
    return key ? facts[key] : null;
  };

  const capText = factGet(/cap\s*rate|^cap\b/i);
  const capRatePct = capText ? Number((capText.match(/([0-9]+(?:\.[0-9]+)?)/) ?? [])[1]) || null : null;
  const assetType = factGet(/^type$|property type/i);
  const leasableText = factGet(/leasable area|building (?:size|sf)|gla|rentable/i);
  const buildingSizeSqft = leasableText ? Number(leasableText.replace(/[^0-9.]/g, "")) || null : null;
  const lotText = factGet(/lot size/i);
  const lotSizeAcres = lotText ? Number((lotText.match(/([0-9.]+)\s*acre/i) ?? [])[1]) || null : null;
  const yearText = factGet(/year built/i);
  const yearBuilt = yearText ? Number((yearText.match(/(\d{4})/) ?? [])[1]) || null : null;

  const highlights: string[] = [];
  $("h3").each((_, el) => {
    if (!/^highlights$/i.test(clean($(el).text()) ?? "")) return;
    const prose = $(el).nextAll(".prose").first();
    const text = prose.length ? prose.text() : "";
    for (const part of text.split(/\u2022|\*/)) {
      const item = clean(part);
      if (item && !highlights.includes(item)) highlights.push(item);
    }
  });

  const docHref = $("#propertyDocumentLink").first().attr("href") ?? null;
  const brochures = docHref
    ? [
        {
          name: "Offering Memorandum",
          url: docHref.startsWith("http") ? docHref : `${MATTHEWS_HOST}${docHref}`,
        },
      ]
    : [];

  const brokerIds = matthewsBrokers($)
    .map((broker) =>
      brokerRef({
        name: broker.name,
        email: broker.email,
        phone: broker.phone,
        avatarUrl: broker.avatarUrl,
        office: null,
        company: "Matthews",
      })
    )
    .filter((id): id is number => id !== null);

  const slug = (url.split("/properties/")[1] ?? url).replace(/[/?#].*$/, "");

  return prune({
    id: slug,
    name: title,
    transactionType: tx === "sale" ? "Sale" : "Lease",
    assetType,
    description: highlights.length ? highlights.join("; ") : null,
    street: addr.street,
    city: addr.city,
    state: addr.state,
    postalCode: addr.postalCode,
    country: "US",
    salePriceUsd: tx === "sale" && realPrice ? moneyToNumber(realPrice) : null,
    salePriceText: tx === "sale" ? realPrice : null,
    capRatePct,
    leaseRateText: tx === "lease" ? realPrice ?? priceText ?? null : null,
    sizeText: leasableText ? `${leasableText} SF` : null,
    buildingSizeSqft,
    lotSizeAcres,
    yearBuilt,
    brokerIds,
    brochures,
    photos,
    url,
    canonicalUrl: url,
    highlights,
  });
}

export async function srcMatthews(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  const xml = await matthewsFetch(MATTHEWS_SITEMAP_URL);
  const detailUrls = matthewsDetailUrlsFromSitemap(xml);
  if (!detailUrls.length) {
    throw new Error(
      "Matthews: no /properties/ URLs found in sitemap.xml (fetch may have been blocked or transformed)"
    );
  }

  const urls = detailUrls.filter((url) => matthewsTenureFromUrl(url) === tx);
  const take = Number.isFinite(max) ? urls.slice(0, max) : urls;

  if (monitor) {
    return {
      company: "Matthews",
      sourceUrl: MATTHEWS_SOURCE_URL,
      method: "Public sitemap.xml enumeration filtered by /properties/leasing-* tenure slug",
      totalAvailable: urls.length,
      listings: take.map((url) =>
        prune({
          id: (url.split("/properties/")[1] ?? url).replace(/[/?#].*$/, ""),
          url,
          canonicalUrl: url,
          transactionType: tx === "sale" ? "Sale" : "Lease",
        })
      ),
    };
  }

  let failures = 0;
  const parsed = await pmap(take, Math.min(CONCURRENCY, 2), async (url) => {
    try {
      const html = await matthewsFetch(url);
      return parseMatthewsDetail(html, url, tx);
    } catch (err) {
      failures++;
      console.error(`  matthews/${tx}: ${url} failed: ${err}`);
      return null;
    }
  });
  const listings = parsed.filter((listing): listing is any => listing !== null);
  if (!listings.length) {
    throw new Error("Matthews: sitemap enumerated detail pages but none parsed");
  }

  return {
    company: "Matthews",
    sourceUrl: MATTHEWS_SOURCE_URL,
    method: "Public sitemap.xml enumeration to server-rendered detail pages, DOM parsed via throttled plain fetch",
    totalAvailable: urls.length,
    listings,
    truncated: failures > 0,
    note: failures > 0 ? `${failures} detail page(s) failed to fetch` : undefined,
  };
}
