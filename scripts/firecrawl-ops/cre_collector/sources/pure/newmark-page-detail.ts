import * as cheerio from "cheerio";
import { newmarkNimCanonicalIdentity } from "./newmark-nim-identity.js";

/** Unverified inventory assertion copied from one uniquely URL-bound Newmark row. */
export type NewmarkPageWitness = {
  recordId: string; slug: string; sourceUrl: string;
  address: string; city: string; state: string; zip: string;
};
const clean = (value: string) => value.replace(/\s+/g, " ").trim();
const normalize = (value: string) => clean(value).toLowerCase();
const hasUnit = (value: string) => /(?:\b(?:suite|ste|unit|apt|apartment|floor|fl)\b|#)/i.test(value);
const safeText = (value: unknown): value is string =>
  typeof value === "string" && value.trim().length > 0 && value.length <= 256 && !/[\u0000-\u001f\u007f]/.test(value);

export function extractNewmarkPage(claim: { externalId: string; url: string }, witness: NewmarkPageWitness,
  html: string, status: number | undefined): { prose: string; contacts: {name: string; email: string}[] } | null {
  if (status !== 200 || !witness || !Object.values(witness).every(safeText)) return null;
  if (hasUnit(witness.address) || hasUnit(witness.city)) return null;
  let identity;
  try { identity = newmarkNimCanonicalIdentity({slug: witness.slug, externalWebsiteUrl: witness.sourceUrl}); }
  catch { return null; }
  if (identity.id !== claim.externalId || identity.url !== claim.url || witness.sourceUrl !== claim.url) return null;
  const $ = cheerio.load(html);
  const canon = $('link[rel="canonical"]'), og = $('meta[property="og:url"]');
  if (canon.length !== 1 || og.length !== 1 || canon.attr("href") !== claim.url || og.attr("content") !== claim.url) return null;
  const details = $("main .property-detail"), title = details.find(".property-detail__title");
  if (details.length !== 1 || title.length !== 1 || title.find("h1").length !== 1 ||
      title.find(".street-address").length !== 1 || hasUnit(title.text())) return null;
  const heading = clean(title.find("h1").text());
  const address = normalize(title.find(".street-address").text());
  const locality = normalize([witness.city, [witness.state, witness.zip].join(" ")].join(", "));
  const splitAddress = normalize(heading) === normalize(witness.address) && address === locality;
  // A marketing title is allowed, but a heading that is itself a complete
  // street address must not name a different property than the witness.
  const headingStreet = /^(\d+[a-z]?\s+[a-z0-9 .'-]+\s(?:street|st|avenue|ave|road|rd|drive|dr|boulevard|blvd|lane|ln|court|ct|parkway|pkwy|place|pl|way)\.?)(?=$|[\s,;|:/–—-])/i.exec(heading)?.[1];
  // Numeric headings outside this narrow recognizable form may themselves
  // assert an address. Hold the fallback instead of assuming a marketing name.
  if (!splitAddress && /\d/.test(heading) && !headingStreet) return null;
  const headingSuffix = headingStreet ? clean(heading.slice(headingStreet.length)) : "";
  const headingLocality = headingSuffix ? normalize(headingSuffix.replace(/^[,;|:/–—-]\s*/, "")) : null;
  const compatibleLocality = !headingLocality || [normalize(witness.city),
    normalize([witness.city, witness.state].join(", ")), locality].includes(headingLocality);
  const fullAddress = safeText(heading) &&
    (!headingStreet || normalize(headingStreet) === normalize(witness.address)) &&
    compatibleLocality &&
    address === normalize([witness.address, witness.city, [witness.state, witness.zip].join(" ")].join(", "));
  if (!splitAddress && !fullAddress) return null;
  const block = details.find(".property-detail__prose");
  if (block.length !== 1 || block.find("script,style").length) return null;
  const prose = clean(block.text());
  if (!prose || prose.length > 12000) return null;
  const contacts: {name: string; email: string}[] = [];
  for (const node of details.find(".property-detail__contact").toArray()) {
    const el = $(node), names = el.find(".property-detail__contact-name"), emails = el.find('a[href^="mailto:"]');
    if (names.length !== 1 || emails.length !== 1) continue;
    const name = clean(names.text()), email = (emails.attr("href") ?? "").slice(7).split("?")[0]!.trim().toLowerCase();
    if (safeText(name) && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) && email.length <= 254)
      contacts.push({name, email});
  }
  return {prose, contacts};
}

/** Compare only recognized Newmark property identities; unknown URLs stay distinct. */
export function sameNewmarkPropertyUrl(left: string, right: string): boolean {
  if (left === right) return true;
  try {
    const a = newmarkNimCanonicalIdentity({externalWebsiteUrl: left});
    const b = newmarkNimCanonicalIdentity({externalWebsiteUrl: right});
    return a.id !== null && b.id !== null && a.id === b.id;
  } catch { return false; }
}

/** An explicit page URL assertion that conflicts with the claimed source URL. */
export function contradictsNewmarkPageUrl(html: string, url: string): boolean {
  const $ = cheerio.load(html);
  const assertions = [
    ...$('link[rel="canonical"]').toArray().map(node => $(node).attr("href")),
    ...$('meta[property="og:url"]').toArray().map(node => $(node).attr("content")),
  ].filter((value): value is string => typeof value === "string" && value.trim().length > 0);
  return assertions.some(value => !sameNewmarkPropertyUrl(value, url));
}
