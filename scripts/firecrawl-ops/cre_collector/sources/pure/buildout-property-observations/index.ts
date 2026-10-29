/** Bounded, unqualified observations from source Buildout panels. */
import * as cheerio from "cheerio";
import { clean } from "../../../lib/util.js";
import { allBuildoutFeeds } from "../../buildout-feeds/index.js";

const MAX_HTML_BYTES = 2_000_000;
const MAX_ENTRIES = 100;
const MAX_PANELS = 100;
const MAX_FIELD_CHARS = 16_000;
const MAX_TOTAL_BYTES = 160_000;
const MAX_LINKS = 100;

type Disposition = "static_hidden" | "not_assessed";
type FieldOccurrences = { values: (string | null)[]; omitted: number };
type Contact = { ordinal: number; rootOrdinal: number; panelOrdinal: number; groupOrdinal: number; disposition: Disposition;
  status: "observed" | "ambiguous"; fields: { name: FieldOccurrences; title: FieldOccurrences; emailHref: FieldOccurrences;
    phoneHref: FieldOccurrences; photoUrl: FieldOccurrences }; text: string | null; links: (string | null)[]; omittedLinks: number };
type Section = { ordinal: number; rootOrdinal: number; panelOrdinal: number; label: string | null; text: string | null; links: (string | null)[];
  omittedLinks: number; disposition: Disposition; status: "observed" | "ambiguous" };
export type BuildoutPropertyObservations = { schema: "buildout-property-observations/v1";
  status: "observed" | "unavailable" | "ambiguous"; contactsStatus: "observed" | "unavailable" | "ambiguous";
  sourceUrl: string; routeBinding: "unverified_caller_url" | "configured_claim_route"; sections: Section[]; contacts: Contact[]; omittedSections: number; omittedContacts: number;
  rootCount: number; panelCount: number; omittedPanels: number;
  excludedSections: number; excludedContacts: number; excludedContactFields: number; omittedFields: number };

/** Accept a known source and Buildout-shaped URL without asserting ownership. */
export function canCaptureBuildoutPropertyObservations(sourceKey: string, sourceUrl: string): boolean {
  if (!allBuildoutFeeds.some(feed => feed.sourceKey === sourceKey) || typeof sourceUrl !== "string" || sourceUrl.length > 4096) return false;
  try {
    const url = new URL(sourceUrl);
    const sensitive = /^(?:token|key|access_token|api_key|apikey|authorization|auth|signature|sig|x-amz-signature|x-amz-credential|x-goog-signature|x-goog-credential|client_secret)$/i;
    return url.protocol === "https:" && url.host === "buildout.com" && !url.username && !url.password && !url.hash &&
      ![...url.searchParams.keys()].some(key => sensitive.test(key)) &&
      /^\/plugins\/[a-f0-9]{40}\/(?:[a-zA-Z0-9.-]+\/)?inventory\/[a-zA-Z0-9_-]+$/.test(url.pathname);
  } catch { return false; }
}

export function captureBuildoutPropertyObservations(html: unknown, sourceUrl: string,
  routeBinding: "unverified_caller_url" | "configured_claim_route" = "unverified_caller_url"): BuildoutPropertyObservations {
  const result: BuildoutPropertyObservations = { schema: "buildout-property-observations/v1", status: "unavailable",
    contactsStatus: "unavailable", sourceUrl, routeBinding, sections: [], contacts: [], omittedSections: 0, omittedContacts: 0,
    rootCount: 0, panelCount: 0, omittedPanels: 0, excludedSections: 0, excludedContacts: 0, excludedContactFields: 0, omittedFields: 0 };
  if (typeof html !== "string" || !html || Buffer.byteLength(html, "utf8") > MAX_HTML_BYTES) return result;
  const $ = cheerio.load(html);
  const outside = (element: any) => $(element).closest("footer, header, nav").length > 0;
  const roots = $(".pdt-site-content").toArray().filter(element => !outside(element));
  const panels = roots.flatMap((root, rootOrdinal) => $(root).find("#template-tab-container #overview").toArray()
    .filter(panel => !outside(panel) && $(panel).closest(".pdt-site-content").get(0) === root)
    .map(element => ({element, rootOrdinal})));
  result.rootCount = roots.length;
  result.panelCount = panels.length;
  result.omittedPanels = Math.max(0, panels.length - MAX_PANELS);
  if (!panels.length) { result.status = roots.length > 1 ? "ambiguous" : "unavailable"; return result; }
  result.status = roots.length !== 1 || panels.length !== 1 ? "ambiguous" : "observed";
  let remaining = MAX_TOTAL_BYTES;
  const bounded = (value: unknown): string | null => {
    const text = clean(value);
    if (!text) return null;
    const bytes = Buffer.byteLength(text, "utf8");
    if (text.length > MAX_FIELD_CHARS || bytes > remaining) { result.omittedFields++; return null; }
    remaining -= bytes;
    return text;
  };
  const disposition = (element: any): Disposition => {
    const nodes = $(element).parents().addBack();
    return nodes.filter('[hidden], [aria-hidden="true"]').length || nodes.toArray().some(node =>
      /(?:display\s*:\s*none|visibility\s*:\s*hidden)/i.test($(node).attr("style") ?? ""))
      ? "static_hidden" : "not_assessed";
  };
  const links = (elements: any[]) => ({ values: elements.slice(0, MAX_LINKS).map(link => bounded($(link).attr("href"))),
    omitted: Math.max(0, elements.length - MAX_LINKS) });
  const headers: { element: any; panel: any; rootOrdinal: number; panelOrdinal: number }[] = [];
  const groups: { element: any; panel: any; rootOrdinal: number; panelOrdinal: number }[] = [];
  panels.slice(0, MAX_PANELS).forEach(({element: panel, rootOrdinal}, panelOrdinal) => {
    const belongs = (element: any) => $(element).closest("#overview").get(0) === panel &&
      $(element).closest(".pdt-site-content").get(0) === roots[rootOrdinal];
    const allHeaders = $(panel).find("h2.pdt-subheader").toArray().filter(belongs);
    result.excludedSections += allHeaders.filter(outside).length;
    headers.push(...allHeaders.filter(element => !outside(element)).map(element => ({element, panel, rootOrdinal, panelOrdinal})));
    const allGroups = $(panel).find(".pdt-brokers").toArray().filter(belongs);
    groups.push(...allGroups.filter(element => !outside(element)).map(element => ({element, panel, rootOrdinal, panelOrdinal})));
  });
  result.omittedSections = Math.max(0, headers.length - MAX_ENTRIES);
  headers.slice(0, MAX_ENTRIES).forEach(({element, panel, rootOrdinal, panelOrdinal}, ordinal) => {
    const heading = $(element);
    const bodies = heading.parent().siblings().filter((_, node) => !outside(node) && $(node).closest("#overview").get(0) === panel && $(node).closest(".pdt-site-content").get(0) === roots[rootOrdinal]).clone();
    bodies.find("footer, header, nav, #overview, .pdt-site-content").remove();
    const ambiguous = result.status === "ambiguous" || heading.parent().parent().find("h2.pdt-subheader").length !== 1 || bodies.find("h2.pdt-subheader").length > 0;
    const found = links(bodies.find("a").toArray());
    const label = heading.clone();
    label.find("footer, header, nav, #overview, .pdt-site-content").remove();
    result.sections.push({ ordinal, rootOrdinal, panelOrdinal, label: bounded(label.text()), text: bounded(bodies.text()),
      links: found.values, omittedLinks: found.omitted, disposition: disposition(element),
      status: ambiguous ? "ambiguous" : "observed" });
  });
  if (groups.length) result.contactsStatus = groups.length > 1 || result.status === "ambiguous" ? "ambiguous" : "observed";
  const groupIndex = new Map(groups.map((group, index) => [group.element, index]));
  let seenCards = 0;
  panels.slice(0, MAX_PANELS).forEach(({element: panel, rootOrdinal}, panelOrdinal) => {
    for (const element of $(panel).find(".pdt-brokers .pdt-broker").toArray()) {
      if ($(element).closest("#overview").get(0) !== panel || $(element).closest(".pdt-site-content").get(0) !== roots[rootOrdinal]) continue;
      const group = $(element).closest(".pdt-brokers").get(0);
      const groupOrdinal = groupIndex.get(group);
      if (outside(element)) { result.excludedContacts++; continue; }
      if (groupOrdinal === undefined) continue;
      const ordinal = seenCards++;
      if (ordinal >= MAX_ENTRIES) { result.omittedContacts++; continue; }
      const card = $(element);
      result.excludedContactFields += card.find(".pdt-broker-name, .pdt-broker-title, a, img.pdt-broker-photo").toArray().filter(node => outside(node) &&
        $(node).closest(".pdt-broker").get(0) === element).length;
      const owned = (selector: string) => card.find(selector).toArray().filter(node => !outside(node) &&
        $(node).closest(".pdt-broker").get(0) === element && $(node).closest(".pdt-brokers").get(0) === group &&
        $(node).closest("#overview").get(0) === panel && $(node).closest(".pdt-site-content").get(0) === roots[rootOrdinal]);
      const field = (nodes: any[], read: (node: any) => unknown): FieldOccurrences => ({
        values: nodes.slice(0, MAX_LINKS).map(node => bounded(read(node))), omitted: Math.max(0, nodes.length - MAX_LINKS) });
      const ownedText = (node: any) => {
        const copy = $(node).clone();
        copy.find("footer, header, nav, .pdt-broker, .pdt-brokers, #overview, .pdt-site-content").remove();
        return copy.text();
      };
      const fields = {
        name: field(owned(".pdt-broker-name"), ownedText),
        title: field(owned(".pdt-broker-title"), ownedText),
        emailHref: field(owned('a[href^="mailto:"]'), node => $(node).attr("href")),
        phoneHref: field(owned('a[href^="tel:"]'), node => $(node).attr("href")),
        photoUrl: field(owned("img.pdt-broker-photo"), node => $(node).attr("src")),
      };
      const found = links(owned("a"));
      const text = card.clone();
      text.find("footer, header, nav, .pdt-broker, .pdt-brokers, #overview, .pdt-site-content").remove();
      result.contacts.push({ ordinal, rootOrdinal, panelOrdinal, groupOrdinal, disposition: disposition(element),
        status: Object.values(fields).some(value => value.values.length + value.omitted > 1) ? "ambiguous" : "observed",
        fields, text: bounded(text.text()), links: found.values, omittedLinks: found.omitted });
    }
  });
  return result;
}

/** Validate the bounded carrier without asserting contact identity or visibility. */
export function validateBuildoutPropertyObservations(value: unknown, sourceUrl: string): value is BuildoutPropertyObservations {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const object = value as Record<string, any>;
  const expected = ["schema", "status", "contactsStatus", "sourceUrl", "routeBinding", "sections", "contacts", "omittedSections", "omittedContacts", "rootCount", "panelCount", "omittedPanels", "excludedSections", "excludedContacts", "excludedContactFields", "omittedFields"];
  if (Object.keys(object).length !== expected.length || expected.some(key => !Object.hasOwn(object, key)) ||
      object.schema !== "buildout-property-observations/v1" || object.sourceUrl !== sourceUrl ||
      !["unverified_caller_url", "configured_claim_route"].includes(object.routeBinding) ||
      !["observed", "unavailable", "ambiguous"].includes(object.status) ||
      !["observed", "unavailable", "ambiguous"].includes(object.contactsStatus) ||
      !Array.isArray(object.sections) || object.sections.length > MAX_ENTRIES ||
      !Array.isArray(object.contacts) || object.contacts.length > MAX_ENTRIES ||
      ["omittedSections", "omittedContacts", "rootCount", "panelCount", "omittedPanels", "excludedSections", "excludedContacts", "excludedContactFields", "omittedFields"].some(k => !Number.isSafeInteger(object[k]) || object[k] < 0)) return false;
  const keysMatch = (x: any, keys: string[]) => x && typeof x === "object" && !Array.isArray(x) &&
    Object.keys(x).length === keys.length && keys.every(key => Object.hasOwn(x, key));
  let remaining = MAX_TOTAL_BYTES;
  const nullable = (x: unknown) => {
    if (x === null) return true;
    if (typeof x !== "string" || x.length > MAX_FIELD_CHARS) return false;
    remaining -= Buffer.byteLength(x, "utf8");
    return remaining >= 0;
  };
  const common = (x: any, index: number) => x && typeof x === "object" && !Array.isArray(x) && x.ordinal === index && Number.isSafeInteger(x.rootOrdinal) && x.rootOrdinal >= 0 && x.rootOrdinal < object.rootCount &&
    Number.isSafeInteger(x.panelOrdinal) && x.panelOrdinal >= 0 && x.panelOrdinal < Math.min(object.panelCount, MAX_PANELS) &&
    ["static_hidden", "not_assessed"].includes(x.disposition) && Array.isArray(x.links) && x.links.length <= MAX_LINKS &&
    x.links.every(nullable) && Number.isSafeInteger(x.omittedLinks) && x.omittedLinks >= 0;
  return object.sections.every((x: any, i: number) => keysMatch(x, ["ordinal", "rootOrdinal", "panelOrdinal", "label", "text", "links", "omittedLinks", "disposition", "status"]) && common(x, i) &&
    ["observed", "ambiguous"].includes(x.status) && nullable(x.label) && nullable(x.text)) &&
    object.contacts.every((x: any, i: number) => keysMatch(x, ["ordinal", "rootOrdinal", "panelOrdinal", "groupOrdinal", "disposition", "status", "fields", "text", "links", "omittedLinks"]) && common(x, i) && Number.isSafeInteger(x.groupOrdinal) && x.groupOrdinal >= 0 &&
      ["observed", "ambiguous"].includes(x.status) && nullable(x.text) &&
      keysMatch(x.fields, ["name", "title", "emailHref", "phoneHref", "photoUrl"]) &&
      Object.values(x.fields).every((field: any) => keysMatch(field, ["values", "omitted"]) &&
        Array.isArray(field.values) && field.values.length <= MAX_LINKS && field.values.every(nullable) &&
        Number.isSafeInteger(field.omitted) && field.omitted >= 0));
}

/** Completeness is local to this carrier, never a source-coverage claim. */
export function buildoutPropertyObservationsIncomplete(value: BuildoutPropertyObservations): boolean {
  return value.status !== "observed" || value.contactsStatus === "ambiguous" ||
    value.omittedPanels > 0 || value.omittedSections > 0 || value.omittedContacts > 0 || value.omittedFields > 0 ||
    value.sections.some(section => section.status === "ambiguous" || section.omittedLinks > 0) ||
    value.contacts.some(contact => contact.status === "ambiguous" || contact.omittedLinks > 0 ||
      Object.values(contact.fields).some(field => field.omitted > 0));
}

/** Caller URL is only a route-shape witness, not proof of source ownership. */
export function captureBuildoutObservationForCaller(sourceKey: string, sourceUrl: string, doc: {rawHtml?: unknown}) {
  if (!canCaptureBuildoutPropertyObservations(sourceKey, sourceUrl)) return undefined;
  const value = captureBuildoutPropertyObservations(doc.rawHtml, sourceUrl);
  if (!validateBuildoutPropertyObservations(value, sourceUrl)) throw new Error("invalid source observation");
  return { value, incomplete: buildoutPropertyObservationsIncomplete(value) };
}
