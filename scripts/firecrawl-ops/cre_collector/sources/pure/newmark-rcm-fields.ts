/** Pure field mapping for an already-qualified Newmark RCM property object.
 * Caller owns identity, source scope, acquisition, visibility review and merge.
 */
import { load } from "cheerio";

const object = (v: unknown): Record<string, any> | null => v !== null && typeof v === "object" && !Array.isArray(v) ? v as Record<string, any> : null;
const string = (v: unknown): string | undefined => typeof v === "string" && v.trim() ? v.trim() : undefined;
const secretKey = /^(?:token|access[_-]?token|api[_-]?key|key|signature|sig|password|secret|session|access[_-]?key(?:[_-]?id)?|x-(?:amz|goog)-(?:signature|credential|security[_-]?token))$/i;
function reference(value: unknown, baseUrl: string): string | undefined {
  const text = string(value); if (!text) return;
  try {
    const url = new URL(text, baseUrl);
    if (url.protocol !== "https:" || url.username || url.password || url.hash ||
        url.hostname === "localhost" || url.hostname.endsWith(".localhost") || url.hostname.endsWith(".local") ||
        /^[0-9.]+$/.test(url.hostname) || url.hostname.includes(":") || /%(?![0-9a-f]{2})/i.test(url.search) ||
        [...url.searchParams.keys()].some(key => { for (let i=0; i<5; i++) { if (secretKey.test(key)) return true; try { const decoded=decodeURIComponent(key); if (decoded===key) return false; key=decoded; } catch { return true; } } return true; })) return;
    return url.href;
  } catch { return; }
}
function plainText(html: string): string | undefined {
  const $ = load(html); $("script,style,noscript,[hidden],[aria-hidden=\"true\"]").remove();
  return string($.text());
}
export interface NewmarkRcmFields {
  description?: string;
  photos?: string[];
  contactsDetailed?: Array<{name: string; title?: string; email?: string; phone?: string; company?: string; license?: string; avatarUrl?: string; profileUrl?: string}>;
  documents?: Array<{url: string}>;
  salePriceUsd?: number;
}

/** Returns additive candidates only. Never changes ID, URL, transaction or calls a provider.
 * `priceCurrency` is a caller assertion that must be bound to source currency or US property context.
 */
export function mapNewmarkRcmFields(input: unknown, baseUrl: string, transaction: "sale" | "lease",
  context?: { priceCurrency: "USD" }): NewmarkRcmFields {
  const data = object(input); if (!data) return {};
  const fields = object(data.projectfields) ?? {};
  const sections = Array.isArray(data.sections) ? data.sections.map(object).filter((s): s is Record<string, any> => !!s) : [];
  const out: NewmarkRcmFields = {};
  const summary = string(fields.summary);
  if (summary) { const description = plainText(summary); if (description) out.description = description; }
  if (!out.description) {
    const overview = sections.filter(s => /^(?:property\s+)?overview$/i.test(string(s.title) ?? ""));
    if (overview.length === 1) {
      const contents = Array.isArray(overview[0].contents) ? overview[0].contents : [];
      const texts = contents.map((x: unknown) => string(object(x)?.content)).filter((x: string | undefined): x is string => !!x)
        .map((x: string) => plainText(x)).filter((x: string | undefined): x is string => !!x);
      if (texts.length === 1) out.description = texts[0];
    }
  }
  if (transaction === "sale" && context?.priceCurrency === "USD" && typeof fields.value === "number" &&
      Number.isFinite(fields.value) && fields.value > 0 && fields.valuesymbol === "$") out.salePriceUsd = fields.value;
  const photos: string[] = [], contacts: NonNullable<NewmarkRcmFields["contactsDetailed"]> = [], documents: NonNullable<NewmarkRcmFields["documents"]> = [];
  const addPhoto = (raw: unknown) => { const u = reference(raw, baseUrl); if (!u) return;
    const host = new URL(u).hostname.toLowerCase(); if (host === "maps.googleapis.com" || host === "maps.google.com") return;
    if (/\.(?:jpe?g|png|webp|gif)(?:[?#]|$)/i.test(u) && !photos.includes(u)) photos.push(u); };
  for (const photo of Array.isArray(data.photos) ? data.photos : []) { const p = object(photo); if (p && p.type === "image") addPhoto(p.imageUrl ?? p.url); }
  for (const section of sections) {
    for (const value of Array.isArray(section.contacts) ? section.contacts : []) {
      const c = object(value); if (!c) continue;
      const name = string(c.Fullname) ?? string([string(c.Firstname), string(c.Lastname)].filter(Boolean).join(" ")); if (!name || /^(?:n\/?a|unknown|none|contact us)$/i.test(name)) continue;
      const contact: NonNullable<NewmarkRcmFields["contactsDetailed"]>[number] = { name };
      if (string(c.Title)) contact.title = string(c.Title);
      if (string(c.Email)) contact.email = string(c.Email);
      if (string(c.Phone)) contact.phone = string(c.Phone);
      if (string(c.CompanyName)) contact.company = string(c.CompanyName);
      if (string(c.License)) contact.license = string(c.License);
      const avatar = reference(c.ProfileImageUrl, baseUrl), profile = reference(c.ExpertBioUrl, baseUrl);
      if (avatar) contact.avatarUrl = avatar;
      if (profile) contact.profileUrl = profile;
      contacts.push(contact);
    }
    const addDoc = (raw: unknown) => { const u = reference(raw, baseUrl); if (!u || !/\.pdf(?:[?#]|$)/i.test(u) || /agreement|login|register|consent/i.test(new URL(u).pathname)) return;
      if (!documents.some(x => x.url === u)) documents.push({url:u}); };
    for (const image of Array.isArray(section.images) ? section.images : []) { const p = object(image); if (p) addDoc(p.link); }
    for (const content of Array.isArray(section.contents) ? section.contents : []) { const c = object(content); if (!c || typeof c.content !== "string") continue; const $ = load(c.content); $("a[href]").each((_, a) => addDoc($(a).attr("href"))); }
  }
  if (photos.length) out.photos = photos;
  if (contacts.length) out.contactsDetailed = contacts;
  if (documents.length) out.documents = documents;
  return out;
}
