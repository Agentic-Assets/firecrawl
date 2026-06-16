// lib/harvest.ts - pure detail-page harvester.
//
// harvestDetail() turns a scraped detail page (ScrapedDoc) into the four
// "capture everything" output arrays:
//   - media[]     : video / virtual-tour / matterport / 360 URLs   -> cre_listing_media
//   - links[]     : external-listing / social / map / other links  -> cre_listing_links
//   - documents[] : classified docs (om/brochure/flyer/floor_plan/financials/rent_roll/other)
//                                                                   -> cre_listing_documents
//   - images[]    : full gallery, NO truncation, noise dropped      -> cre_listing_images
//
// Design contract (locked):
//   * Pure: no network, no import-time side effects. Do NOT import ./config.js
//     (it parses argv at import time; the harvest unit test must stay no-argv).
//   * NEVER throws: every field access is guarded; malformed/garbage input
//     yields empty arrays, not an exception.
//   * Dedups EVERY output array by url (case-insensitive normalized key).
//   * Images keep ALL gallery urls (never truncate); only obvious noise is
//     dropped (google map tiles, data:/svg URIs, recaptcha/analytics assets).
//   * ctx.extra* (promoted stranded raw_data fields) merge LAST so a source's
//     high-confidence native field can be supplied; bare strings normalize to the
//     default-typed object (mediaType/linkType/docType = 'other').
//
// Input precedence (Section 3.2 of the impl spec):
//   1. doc.attributes  (preferred structured path)
//   2. doc.links       (always present per scrape.ts)
//   3. doc.images      (else regex-extract <img>/gallery urls from rawHtml)
//   4. ctx.extra*      (promoted fields, merged last)
//   5. rawHtml regex   (iframe/video-source/data-video-url) ONLY when
//                       doc.attributes is undefined (fork omitted the format)

import { clean } from "./util.js";
import type { AttrBlock, DocItem, LinkItem, MediaItem, ScrapedDoc } from "../types.js";

export interface HarvestCtx {
  extraMedia?: (MediaItem | string)[];
  extraLinks?: (LinkItem | string)[];
  extraDocs?: (DocItem | string)[];
  extraImages?: string[];
  baseUrl?: string;
}

export interface HarvestResult {
  media: MediaItem[];
  links: LinkItem[];
  documents: DocItem[];
  images: string[];
}

// ---------------------------------------------------------------------------
// Low-level guards (never throw)
// ---------------------------------------------------------------------------

// Resolve a raw href against an optional base url and keep only http(s) urls.
// Returns null for fragments, data:/javascript:/mailto:/tel:, relative paths we
// cannot resolve, and anything non-string. Never throws.
function httpUrl(raw: any, baseUrl?: string): string | null {
  if (typeof raw !== "string") return null;
  const s = raw.trim();
  if (!s) return null;
  // Cheap reject of obvious non-http schemes and bare fragments before URL().
  if (/^(data:|javascript:|mailto:|tel:|#)/i.test(s)) return null;
  let resolved = s;
  if (!/^https?:\/\//i.test(s)) {
    if (baseUrl) {
      try {
        resolved = new URL(s, baseUrl).href;
      } catch {
        return null;
      }
    } else {
      return null;
    }
  }
  try {
    const u = new URL(resolved);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    return u.href;
  } catch {
    return null;
  }
}

// Lower-cased haystack for case-insensitive pattern matching. Never throws.
function lc(s: any): string {
  return typeof s === "string" ? s.toLowerCase() : "";
}

// Best-effort hostname (without leading www.) for provider labeling. Never throws.
function hostOf(url: string): string | null {
  try {
    return new URL(url).hostname.replace(/^www\./i, "") || null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// MEDIA detection
// ---------------------------------------------------------------------------

// Extract a YouTube video id from any youtube url form. Video ids are
// case-sensitive, so this reads the ORIGINAL url, never a lowercased copy.
function youtubeId(url: string): string | null {
  const short = url.match(/youtu\.be\/([a-zA-Z0-9_-]{6,})/);
  const watch = url.match(/[?&]v=([a-zA-Z0-9_-]{6,})/);
  const embed = url.match(/\/embed\/([a-zA-Z0-9_-]{6,})/);
  return short?.[1] ?? watch?.[1] ?? embed?.[1] ?? null;
}

// Extract a Vimeo video id (and optional privacy hash) from either the share
// form (vimeo.com/<id>/<hash>) or the player form
// (player.vimeo.com/video/<id>?h=<hash>). The hash gates UNLISTED videos, so it
// must survive into the canonical embed url or the embed will not play.
function vimeoIdHash(url: string): { id: string; hash: string | null } | null {
  const m = lc(url).match(/vimeo\.com\/(?:video\/)?(\d+)(?:\/([0-9a-z]+))?/);
  if (!m) return null;
  let hash: string | null = m[2] ?? null;
  if (!hash) {
    const hq = url.match(/[?&]h=([0-9a-zA-Z]+)/);
    if (hq) hash = hq[1];
  }
  return { id: m[1]!, hash };
}

// Normalize a recognized video/tour url to its canonical player/embed url where
// a stable form exists (vimeo -> player.vimeo.com/video/<id>[?h=<hash>], youtube
// -> youtube.com/embed/<id>). Returns null when no canonical embed is derivable.
function deriveEmbedUrl(url: string, provider: string | null): string | null {
  try {
    if (provider === "vimeo") {
      const v = vimeoIdHash(url);
      if (v) {
        return v.hash
          ? `https://player.vimeo.com/video/${v.id}?h=${v.hash}`
          : `https://player.vimeo.com/video/${v.id}`;
      }
    }
    if (provider === "youtube") {
      const id = youtubeId(url);
      if (id) return `https://www.youtube.com/embed/${id}`;
    }
  } catch {
    /* fall through to null */
  }
  return null;
}

// Classify a url as media. Returns null when the url is not a media url. The
// provider/mediaType pairing follows the locked detection table.
function classifyMedia(
  url: string,
  opts: { embedUrl?: string | null; title?: string | null } = {}
): MediaItem | null {
  const l = lc(url);
  // Host-pattern matches run against the parsed hostname (anchored at a label
  // boundary), so `vimeo.com` matches `https://vimeo.com/...` and `*.vimeo.com`
  // but NOT a `notvimeo.com` or a `vimeo.com.evil.test` host. Path-pattern
  // matches (e.g. `/360`, `virtual-tour`) run against the full lowercased url.
  const host = lc(hostOf(url));
  const hostHas = (re: RegExp) => re.test(host);
  let mediaType: MediaItem["mediaType"] | null = null;
  let provider: string | null = null;

  if (hostHas(/(?:^|\.)(?:vimeo\.com)$/) || hostHas(/(?:^|\.)player\.vimeo\.com$/)) {
    mediaType = "video";
    provider = "vimeo";
  } else if (hostHas(/(?:^|\.)(?:youtube\.com|youtu\.be|youtube-nocookie\.com)$/)) {
    mediaType = "video";
    provider = "youtube";
  } else if (hostHas(/(?:^|\.)(?:wistia\.(?:com|net|io)|wi\.st)$/)) {
    mediaType = "video";
    provider = "wistia";
  } else if (hostHas(/(?:^|\.)(?:brightcove\.(?:com|net)|bcove\.video)$/)) {
    mediaType = "video";
    provider = "brightcove";
  } else if (hostHas(/(?:^|\.)matterport\.com$/)) {
    mediaType = "matterport";
    provider = "matterport";
  } else if (hostHas(/(?:^|\.)kuula\.co$/) || /\/360(?:[/?#.]|$)|virtual-?tour/.test(l)) {
    mediaType = "virtual_tour";
    provider = hostOf(url);
  }

  if (!mediaType) return null;

  // Prefer a canonical embed derived from a KNOWN provider (vimeo/youtube) over
  // a raw embed-style src, so a youtube watch/youtu.be url normalizes to its
  // /embed/ form even when the src itself was the watch url. For unknown
  // providers, the supplied embed-style src is the best embed we have.
  const embedUrl = deriveEmbedUrl(url, provider) ?? httpUrl(opts.embedUrl);
  return {
    mediaType,
    provider,
    url,
    embedUrl: embedUrl ?? null,
    title: clean(opts.title),
  };
}

// ---------------------------------------------------------------------------
// DOCUMENT detection (classified)
// ---------------------------------------------------------------------------

const DOC_EXT = /\.(?:pdf|docx?|xlsx?|pptx?)(?:[?#]|$)/i;

// Classify a url as a document, returning its docType, or null when it is not a
// document. A document requires either a recognized document extension OR a
// documentary keyword in the url path / anchor text (mirrors colliers-main
// looksDoc heuristic). Keyword classification is ordered most-specific first.
function classifyDoc(url: string, title?: string | null): DocItem | null {
  const hay = `${lc(url)} ${lc(title)}`;
  const hasExt = DOC_EXT.test(url);

  // Keyword -> docType, most-specific first so e.g. "rent-roll" is not eaten by
  // a broader bucket. A keyword hit qualifies the url as a document even without
  // a file extension (gated dataroom / deal-room links often lack one).
  let docType: DocItem["docType"] | null = null;
  if (/rent[-_ ]?roll/.test(hay)) docType = "rent_roll";
  else if (/financ|pro[-_ ]?forma|proforma|\bt-?12\b/.test(hay)) docType = "financials";
  else if (/floor[-_ ]?plan|site[-_ ]?plan|floorplan|siteplan/.test(hay)) docType = "floor_plan";
  else if (/offering|memorandum|(?:^|[/_-])om(?:[/_.-]|$)|teaser|dataroom|data[-_ ]room|deal[-_ ]room/.test(hay))
    docType = "om";
  else if (/flyer/.test(hay)) docType = "flyer";
  else if (/brochure|marketing|\bpackage\b|\bdeck\b|\bpib\b/.test(hay)) docType = "brochure";

  // Buildout-hosted file links carry no extension and no keyword, but a
  // /sharing/ or /docs/ path (or an explicit ?file=<id> download) IS a
  // downloadable document (the offering package / flyer behind a Buildout
  // listing). Host is checked on the parsed hostname (anchored) and the
  // path/query on the url, so it is scoped to buildout.com without
  // false-positiving a generic ?file= on another host.
  const docHost = hostOf(url) ?? "";
  const isHostedDownload =
    /(?:^|\.)buildout\.com$/i.test(docHost) &&
    (/\/(?:sharing|docs)\//i.test(url) || /[?&]file=\d+/i.test(url));

  if (docType) return { url, title: clean(title), docType };
  // No documentary keyword: a bare document file extension OR a recognized
  // hosted-download link qualifies it, classified as 'other'.
  if (hasExt || isHostedDownload) return { url, title: clean(title), docType: "other" };
  return null;
}

// ---------------------------------------------------------------------------
// LINK detection
// ---------------------------------------------------------------------------

// Off-brokerage canonical listing hosts (matched against the parsed hostname at
// a label boundary): a link to one of these is an external_listing (the same
// property syndicated elsewhere).
const EXTERNAL_LISTING_HOSTS =
  /(?:^|\.)(?:loopnet\.com|crexi\.com|costar\.com|brevitas\.com|catylist\.com|commercialcafe\.com|cityfeet\.com|propertyshark\.com|biproxi\.com|realnex\.com|thebrokerlist\.com|commercialsearch\.com|ten-x\.com|tenx\.com|rcm1\.com|buildout\.com)$/i;

// Social hosts (hostname-anchored). youtube channel/user links are SOCIAL, but a
// youtube watch/embed url is already classified as MEDIA before this runs.
const SOCIAL_HOSTS =
  /(?:^|\.)(?:facebook\.com|fb\.com|twitter\.com|x\.com|linkedin\.com|instagram\.com|youtube\.com)$/i;

// Map links are path-dependent (the `/maps` path on an otherwise-general host),
// so this matches against the full url, not just the hostname.
const MAP_URL =
  /google\.[a-z.]+\/maps|goo\.gl\/maps|maps\.app\.goo\.gl|maps\.google|bing\.com\/maps|openstreetmap\.org/i;

// Detect a broker-bio / agent-profile url. These are DETECTED ONLY TO EXCLUDE
// them: bios live in cre_listing_contacts.profile_url, so a matched bio link is
// dropped (not emitted as 'other'). Conservative pattern: agent/broker/team/
// people profile path segments.
function isBrokerBio(url: string): boolean {
  return /\/(?:agent|broker|team|people|person|professional|our-team|advisors?|expert)s?\//i.test(url);
}

// Classify a url as a link. Returns null when the url should be dropped
// (broker-bio links are dropped here so they never fall into 'other'). Media and
// documents are classified BEFORE this is called, so a media/doc url never
// reaches link classification.
function classifyLink(url: string, rel?: string | null): LinkItem | null {
  if (isBrokerBio(url)) return null; // bio lives in contacts.profile_url; drop
  const host = hostOf(url) ?? "";
  let linkType: LinkItem["linkType"];
  if (MAP_URL.test(url)) linkType = "map";
  else if (SOCIAL_HOSTS.test(host)) linkType = "social";
  else if (EXTERNAL_LISTING_HOSTS.test(host)) linkType = "external_listing";
  else linkType = "other";
  return { url, rel: clean(rel), linkType };
}

// ---------------------------------------------------------------------------
// IMAGE noise filter
// ---------------------------------------------------------------------------

// Reject obvious non-gallery noise. NEVER truncates the gallery (the contract
// hard rule); only drops map tiles, data:/svg URIs, and recaptcha/analytics
// assets.
function isImageNoise(url: string): boolean {
  const l = lc(url);
  if (l.startsWith("data:")) return true;
  if (/\.svg(?:[?#]|$)/.test(l)) return true;
  if (/staticmap|maps\.googleapis\.com\/maps\/api\/staticmap|maps\.google.*\/maps\/api/.test(l)) return true;
  if (/recaptcha|gstatic\.com|googletagmanager|google-analytics|doubleclick|\/pixel\b/.test(l)) return true;
  return false;
}

// ---------------------------------------------------------------------------
// rawHtml regex fallbacks (only used when the structured format is absent)
// ---------------------------------------------------------------------------

// Extract candidate media/embed urls from rawHtml when doc.attributes is absent.
// Scans iframe src, video source src, data-video-url, and div[component=video]
// url attributes. Best-effort, never throws.
function regexMediaCandidates(rawHtml: string): string[] {
  if (typeof rawHtml !== "string" || !rawHtml) return [];
  const out: string[] = [];
  const patterns = [
    /<iframe[^>]+src\s*=\s*["']([^"']+)["']/gi,
    /<video[^>]*>[\s\S]*?<source[^>]+src\s*=\s*["']([^"']+)["']/gi,
    /<source[^>]+src\s*=\s*["']([^"']+)["']/gi,
    /\bdata-video-url\s*=\s*["']([^"']+)["']/gi,
    /<div[^>]+component\s*=\s*["']?video["']?[^>]*\burl\s*=\s*["']([^"']+)["']/gi,
  ];
  for (const re of patterns) {
    let m: RegExpExecArray | null;
    while ((m = re.exec(rawHtml)) !== null) {
      if (m[1]) out.push(m[1]);
    }
  }
  return out;
}

// Extract <img src> / data-src gallery urls from rawHtml when doc.images is
// absent. Best-effort, never throws.
function regexImageCandidates(rawHtml: string): string[] {
  if (typeof rawHtml !== "string" || !rawHtml) return [];
  const out: string[] = [];
  const patterns = [
    /<img[^>]+(?:data-src|data-lazy-src|src)\s*=\s*["']([^"']+)["']/gi,
    /<source[^>]+srcset\s*=\s*["']([^"'\s]+)/gi,
  ];
  for (const re of patterns) {
    let m: RegExpExecArray | null;
    while ((m = re.exec(rawHtml)) !== null) {
      if (m[1]) out.push(m[1]);
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Dedup helpers (case-insensitive normalized url key)
// ---------------------------------------------------------------------------

function urlKey(url: string): string {
  // Normalize trailing slash + scheme/host casing so http://X/ and HTTP://x are
  // one key; keep the path/query case-sensitive (some CDNs are case-sensitive).
  try {
    const u = new URL(url);
    const host = u.host.toLowerCase();
    const proto = u.protocol.toLowerCase();
    let path = u.pathname.replace(/\/+$/, "");
    if (path === "") path = "/";
    return `${proto}//${host}${path}${u.search}`;
  } catch {
    return url.replace(/\/+$/, "").toLowerCase();
  }
}

// Canonical IDENTITY for a media item, so two different urls of the SAME video
// (e.g. the vimeo.com/<id>/<hash> share url and the player.vimeo.com/video/<id>
// iframe src, or a youtube watch url and its /embed/ form) collapse to ONE
// MediaItem instead of two near-duplicate rows. Provider+id for known providers;
// normalized embed/url otherwise.
function mediaIdentity(m: MediaItem): string {
  if (m.provider === "vimeo") {
    const v = vimeoIdHash(m.url);
    if (v) return `vimeo:${v.id}`;
  }
  if (m.provider === "youtube") {
    const id = youtubeId(m.url);
    if (id) return `youtube:${id}`;
  }
  if (m.provider === "matterport") {
    const mm = m.url.match(/[?&]m=([a-zA-Z0-9]+)/);
    if (mm) return `matterport:${mm[1]}`;
  }
  return `u:${urlKey(m.embedUrl ?? m.url)}`;
}

// An embed-form url is the player/iframe variant; the landing form (vimeo.com,
// youtube watch / youtu.be) is preferred for the human-facing `url`.
function isEmbedForm(url: string): boolean {
  return /player\.vimeo\.com|\/embed\/|youtube-nocookie/i.test(url);
}

// Merge two MediaItems sharing one identity: keep the landing url (not the embed
// form) for `url`, the richer embed url (one carrying a privacy hash wins) for
// `embedUrl`, and the first available title.
function mergeMedia(a: MediaItem, b: MediaItem): MediaItem {
  const url = isEmbedForm(a.url) && !isEmbedForm(b.url) ? b.url : a.url;
  let embedUrl: string | null = a.embedUrl ?? b.embedUrl ?? null;
  if (a.embedUrl && b.embedUrl) {
    const aHash = /[?&]h=/.test(a.embedUrl);
    const bHash = /[?&]h=/.test(b.embedUrl);
    embedUrl = bHash && !aHash ? b.embedUrl : a.embedUrl;
  }
  return {
    mediaType: a.mediaType,
    provider: a.provider ?? b.provider,
    url,
    embedUrl,
    title: a.title ?? b.title,
  };
}

// ---------------------------------------------------------------------------
// ctx.extra* normalization
// ---------------------------------------------------------------------------

function normMedia(m: MediaItem | string, baseUrl?: string): MediaItem | null {
  if (typeof m === "string") {
    const u = httpUrl(m, baseUrl);
    return u ? { mediaType: "other", provider: null, url: u, embedUrl: null, title: null } : null;
  }
  if (m && typeof m === "object") {
    const u = httpUrl((m as any).url, baseUrl);
    if (!u) return null;
    const mt = (m as any).mediaType;
    return {
      mediaType:
        mt === "video" || mt === "virtual_tour" || mt === "matterport" || mt === "other" ? mt : "other",
      provider: clean((m as any).provider),
      url: u,
      embedUrl: httpUrl((m as any).embedUrl, baseUrl),
      title: clean((m as any).title),
    };
  }
  return null;
}

function normLink(ln: LinkItem | string, baseUrl?: string): LinkItem | null {
  if (typeof ln === "string") {
    const u = httpUrl(ln, baseUrl);
    return u ? { url: u, rel: null, linkType: "other" } : null;
  }
  if (ln && typeof ln === "object") {
    const u = httpUrl((ln as any).url, baseUrl);
    if (!u) return null;
    const lt = (ln as any).linkType;
    const allowed = ["external_listing", "social", "map", "broker_bio", "document", "video", "other"];
    return {
      url: u,
      rel: clean((ln as any).rel),
      linkType: allowed.includes(lt) ? lt : "other",
    };
  }
  return null;
}

function normDoc(d: DocItem | string, baseUrl?: string): DocItem | null {
  if (typeof d === "string") {
    const u = httpUrl(d, baseUrl);
    return u ? { url: u, title: null, docType: "other" } : null;
  }
  if (d && typeof d === "object") {
    const u = httpUrl((d as any).url, baseUrl);
    if (!u) return null;
    const dt = (d as any).docType;
    const allowed = ["om", "brochure", "flyer", "floor_plan", "financials", "rent_roll", "other"];
    return {
      url: u,
      title: clean((d as any).title),
      docType: allowed.includes(dt) ? dt : "other",
    };
  }
  return null;
}

// ---------------------------------------------------------------------------
// harvestDetail (the one exported entry point)
// ---------------------------------------------------------------------------

export function harvestDetail(doc: ScrapedDoc, ctx: HarvestCtx = {}): HarvestResult {
  const baseUrl = clean(ctx?.baseUrl) ?? undefined;

  // Per-type dedup accumulators. links/documents/images key by normalized url
  // (first write wins, so a structured high-confidence item is not overwritten by
  // a later bare-string ctx.extra* item for the same url). media keys by canonical
  // IDENTITY (mediaIdentity) and MERGES collisions, so two urls of the same video
  // fold into one item with the best landing url + richest embed url.
  const media = new Map<string, MediaItem>();
  const links = new Map<string, LinkItem>();
  const documents = new Map<string, DocItem>();
  const images = new Map<string, string>();

  // Guard: a null/garbage doc must not throw. Treat missing fields as empty.
  const d: ScrapedDoc = doc && typeof doc === "object" ? doc : ({} as ScrapedDoc);
  const rawHtml = typeof d.rawHtml === "string" ? d.rawHtml : "";

  // Insert a media item under its canonical identity, merging on collision so two
  // urls of the same video collapse to one (keeping the best landing url + embed).
  const addMedia = (m: MediaItem) => {
    const key = mediaIdentity(m);
    const prev = media.get(key);
    media.set(key, prev ? mergeMedia(prev, m) : m);
  };

  // Route a single (url, title?) candidate through media -> document -> link, in
  // that precedence (a video url is media, not a link; a pdf is a document).
  // embedUrl is set only when the value came from an iframe/embed-style selector.
  const route = (rawUrl: any, opts: { title?: string | null; embedUrl?: string | null } = {}) => {
    const u = httpUrl(rawUrl, baseUrl);
    if (!u) return;
    const m = classifyMedia(u, opts);
    if (m) {
      addMedia(m);
      return;
    }
    const doc2 = classifyDoc(u, opts.title);
    if (doc2) {
      if (!documents.has(urlKey(doc2.url))) documents.set(urlKey(doc2.url), doc2);
      return;
    }
    const ln = classifyLink(u, null);
    if (ln) {
      if (!links.has(urlKey(ln.url))) links.set(urlKey(ln.url), ln);
    }
  };

  const addImage = (rawUrl: any) => {
    const u = httpUrl(rawUrl, baseUrl);
    if (!u || isImageNoise(u)) return;
    const k = urlKey(u);
    if (!images.has(k)) images.set(k, u);
  };

  // 1) Structured attributes path (preferred). Each AttrBlock's selector hints
  //    whether its values are embed-style (iframe/video-source/data-video-url/
  //    component=video) so a matched media url gets its src as embedUrl.
  const attrBlocks: AttrBlock[] = Array.isArray(d.attributes) ? d.attributes : [];
  const hasAttributes = Array.isArray(d.attributes);
  for (const block of attrBlocks) {
    if (!block || typeof block !== "object") continue;
    const sel = lc(block.selector);
    const values = Array.isArray(block.values) ? block.values : [];
    const isEmbedSelector =
      /iframe|video|component=video|data-video-url/.test(sel) || sel.includes("[data-video-url]");
    for (const v of values) {
      const u = httpUrl(v, baseUrl);
      if (!u) continue;
      // For an embed-style selector, the matched src IS the embed url for media.
      route(u, { embedUrl: isEmbedSelector ? u : null });
    }
  }

  // 2) doc.links path.
  const docLinks = Array.isArray(d.links) ? d.links : [];
  for (const v of docLinks) route(v);

  // 3) Images: structured doc.images, else regex over rawHtml.
  if (Array.isArray(d.images)) {
    for (const v of d.images) addImage(v);
  } else {
    for (const v of regexImageCandidates(rawHtml)) addImage(v);
  }

  // 4) rawHtml media regex fallback — ONLY when the structured attributes format
  //    is absent (fork omitted it). When attributes ARE present we trust them and
  //    skip the regex so we never double-count.
  if (!hasAttributes) {
    for (const v of regexMediaCandidates(rawHtml)) {
      const u = httpUrl(v, baseUrl);
      if (u) route(u, { embedUrl: u });
    }
  }

  // 5) ctx.extra* merged LAST (a source's promoted native field). A bare STRING
  //    carries no type intent, so it is re-routed through the same media->doc->
  //    link classifier (a promoted vimeo string lands in media, a promoted .pdf
  //    in documents). A TYPED object carries explicit intent and is trusted as
  //    given. All added with first-write-wins dedup, so a structured item
  //    already captured from the page wins ties.
  for (const raw of ctx?.extraMedia ?? []) {
    if (typeof raw === "string") {
      // A bare extraMedia string asserts the MEDIA channel: classify it for a
      // provider/embed, but if unrecognized still keep it as media 'other'
      // (the caller promoted it as media), never demote it to a link.
      const u = httpUrl(raw, baseUrl);
      if (!u) continue;
      const m =
        classifyMedia(u) ?? { mediaType: "other" as const, provider: null, url: u, embedUrl: null, title: null };
      addMedia(m);
    } else {
      const m = normMedia(raw, baseUrl);
      if (m) addMedia(m);
    }
  }
  for (const raw of ctx?.extraDocs ?? []) {
    if (typeof raw === "string") {
      const u = httpUrl(raw, baseUrl);
      // A bare doc string is forced to the documents channel even when it lacks a
      // documentary keyword/extension (the source asserted it is a doc), as the
      // default-typed 'other'. classifyDoc would otherwise reject a keyword-less,
      // extension-less url.
      if (u) {
        const dd = classifyDoc(u) ?? { url: u, title: null, docType: "other" as const };
        if (!documents.has(urlKey(dd.url))) documents.set(urlKey(dd.url), dd);
      }
    } else {
      const dd = normDoc(raw, baseUrl);
      if (dd && !documents.has(urlKey(dd.url))) documents.set(urlKey(dd.url), dd);
    }
  }
  for (const raw of ctx?.extraLinks ?? []) {
    if (typeof raw === "string") {
      route(raw); // a promoted string may be media/doc; re-route, not force-link
    } else {
      const ln = normLink(raw, baseUrl);
      if (ln && !links.has(urlKey(ln.url))) links.set(urlKey(ln.url), ln);
    }
  }
  for (const raw of ctx?.extraImages ?? []) addImage(raw);

  return {
    media: [...media.values()],
    links: [...links.values()],
    documents: [...documents.values()],
    images: [...images.values()],
  };
}
