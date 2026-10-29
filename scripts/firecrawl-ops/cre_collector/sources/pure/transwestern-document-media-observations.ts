import type * as cheerio from "cheerio";
import type { DocItem, LinkItem, MediaItem } from "../../types.js";
import { transwesternConcealed } from "./transwestern-property-panel.js";

type Channel = "document" | "media" | "link";
type Role = "property" | "sitewide" | "unknown";
type Basis = "property_attachment_table" | "property_banner_download" | "footer_navigation" | "other_dom_occurrence" | "no_matching_dom_occurrence";
type Occurrence = { url: string; channel: Channel; role: Role; basis: Basis; element?: string; attribute?: string };
const MAX_OCCURRENCES = 1000;

// Location observations only: neither type, access, visibility, content,
// property identity nor publication authority follows from a link.
export function transwesternDocumentMediaObservations(
  $: cheerio.CheerioAPI, documents: DocItem[], media: MediaItem[], links: LinkItem[], sourceUrl: string,
  bodySha256: string, observedAt: string,
) {
  const candidates = new Map<string, Set<Channel>>();
  let skippedCandidateCount = 0;
  let skippedLinkEntryCount = 0;
  const validCandidate = (value: unknown): value is string => {
    if (typeof value !== "string") return false;
    try {
      const url = new URL(value);
      return Buffer.byteLength(value, "utf8") <= 4096 && ["http:", "https:"].includes(url.protocol) &&
        !url.username && !url.password && url.href === value;
    } catch { return false; }
  };
  const addCandidate = (url: string, channel: Channel) => {
    const channels = candidates.get(url) ?? new Set<Channel>();
    channels.add(channel);
    candidates.set(url, channels);
  };
  for (const [channel, items] of [["document", documents], ["media", media]] as const) {
    for (const item of items) {
      if (!validCandidate(item?.url)) { skippedCandidateCount++; continue; }
      addCandidate(item.url, channel);
    }
  }
  const linkUrls = new Set<string>();
  for (const link of links) {
    if (!validCandidate(link?.url)) { skippedLinkEntryCount++; continue; }
    linkUrls.add(link.url);
  }
  const eligibleLinks = new Set<string>();
  $("a.download-flyer-btn[href], a.download-att-btn[href]").each((_, node) => {
    try {
      const url = new URL($(node).attr("href")!, sourceUrl).href;
      if (linkUrls.has(url)) eligibleLinks.add(url);
    } catch { /* No resolved link candidate. */ }
  });
  for (const url of eligibleLinks) addCandidate(url, "link");
  const property = $("section.property-dtl");
  const inner = property.children(".fix-wrap").children(".property-dtl-inner");
  const table = inner.find(".available-space.availablespace-table > .availablespace-main > #tblAttachments, " +
    ".available-space.availablespace-table > .availablespace-main > .dataTables_wrapper > .dataTables_scroll > .dataTables_scrollBody > #tblAttachments");
  const banner = $("section.cmn-banner.bnr-inner.property-dtl-bnr");
  const footer = $("footer");
  const occurrences: Occurrence[] = [];
  const matched = new Set<string>();
  let omittedCount = 0;
  const add = (value: Occurrence) => {
    if (occurrences.length < MAX_OCCURRENCES) occurrences.push(value);
    else omittedCount++;
  };
  $("a[href], iframe[src], embed[src], object[data], video[src], source[src]").each((_, node) => {
    const el = $(node);
    const attribute = node.tagName === "a" ? "href" : node.tagName === "object" ? "data" : "src";
    const raw = el.attr(attribute);
    let url: URL;
    try {
      url = new URL(raw ?? "", sourceUrl);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return;
    } catch { return; }
    const key = url.href;
    const channels = candidates.get(key);
    if (!channels) return;
    const visiblePath = ![node, ...el.parents().toArray()].some(parent => transwesternConcealed($, parent));
    const propertyTable = visiblePath && property.length === 1 && inner.length === 1 && table.length === 1 && $("#tblAttachments").length === 1 &&
      el.is("a[href]") && el.closest("#tblAttachments").is(table) && el.closest("section.property-dtl").is(property);
    const propertyBanner = visiblePath && banner.length === 1 && el.is("a.download-flyer-btn[href], a.download-att-btn[href]") &&
      banner.children(".fix-wrap").children(".banner-caption").length === 1 &&
      el.closest(".banner-caption").is(banner.children(".fix-wrap").children(".banner-caption"));
    const siteFooter = visiblePath && footer.length === 1 && el.closest("footer").is(footer) &&
      footer.children("section.footer-links").length === 1 &&
      el.closest("section.footer-links").is(footer.children("section.footer-links"));
    const scopeCount = Number(propertyTable) + Number(propertyBanner) + Number(siteFooter);
    const role: Role = scopeCount !== 1 ? "unknown" : siteFooter ? "sitewide" : "property";
    const basis: Basis = scopeCount !== 1 ? "other_dom_occurrence" : propertyTable ? "property_attachment_table" :
      propertyBanner ? "property_banner_download" : "footer_navigation";
    for (const channel of channels) add({ url: key, channel, role, basis, element: node.tagName, attribute });
    matched.add(key);
  });
  for (const [url, channels] of candidates) {
    if (!matched.has(url)) for (const channel of channels) add({ url, channel, role: "unknown", basis: "no_matching_dom_occurrence" });
  }
  return { schema: "transwestern-document-media-roles/v1", sourceUrl, bodySha256, observedAt,
    state: omittedCount || skippedCandidateCount || skippedLinkEntryCount ? "incomplete" : "inspected", candidateCount: candidates.size,
    eligibleLinkCandidateCount: eligibleLinks.size, occurrenceCount: occurrences.length, omittedCount,
    skippedCandidateCount, skippedLinkEntryCount, occurrences };
}
