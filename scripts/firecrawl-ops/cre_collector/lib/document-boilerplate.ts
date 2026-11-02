// lib/document-boilerplate.ts - site-wide boilerplate that is never a listing document.
//
// AGENTIC-5143 (W8), plan AGENTIC-5134 sections 0 (fact 4) and 4. A broker's detail page
// carries site chrome (regulatory notices, service pages) that harvestDetail() would
// otherwise classify as a document and attach to every listing on the site.
//
// Pure: no network, no argv/config import (harvest.ts must stay import-safe). To teach the
// collector a new boilerplate shape, add one DocumentBoilerplateRule row below.
//
// Consistency contract with the producer-side rule, GetCREdata documents/host_rules.py
// (rows `transwestern_boilerplate` and `jll_services_boilerplate`): the same host
// matching (exact host or any subdomain) and the same path patterns, evaluated against
// the path as a server reads it (see normalizedPath). Rows marked "collector-only" are
// shapes this collector already excluded before W8; GetCREdata does not list them.
//
// Not covered here: the plan's generic guard (a canonical URL attached to 25 or more
// listings whose doc_type is not `om`). It needs corpus-wide counts, so it stays a
// GetCREdata sync-time rule; a per-listing collector cannot evaluate it.

export interface DocumentBoilerplateRule {
  /** Stable identifier for logs and tests. */
  name: string;
  /** Lowercase hosts; a rule applies to the exact host or any subdomain of it. Omitted = any host. */
  hosts?: readonly string[];
  /** Case-insensitive patterns tested against the normalized path plus the query string
   *  (`/path?x=1`, no fragment). Empty = every path. */
  pathPatterns: readonly RegExp[];
  reason: string;
}

export const DOCUMENT_BOILERPLATE_RULES: readonly DocumentBoilerplateRule[] = [
  {
    name: "transwestern_boilerplate",
    hosts: ["transwestern.com"],
    pathPatterns: [/^\/Upload\/TREC\//i, /^\/service\//i, /^\/services\//i],
    reason: "Transwestern site boilerplate (Texas TREC notices, service pages)",
  },
  {
    name: "jll_services_boilerplate",
    hosts: ["jll.com"],
    pathPatterns: [/^\/(?:[a-z]{2}(?:-[a-z]{2})?\/)?services?\//i],
    reason: "JLL services page (debt advisory), not a listing document",
  },
  {
    // collector-only: the footer privacy policy, excluded by transwestern.ts before W8.
    name: "transwestern_privacy_policy",
    hosts: ["transwestern.com"],
    pathPatterns: [/(?:^|\/)privacy-policy\/?(?:\?|$)/i],
    reason: "Transwestern privacy policy page",
  },
  {
    // collector-only: a benefits transparency link in the Transwestern footer, excluded
    // by transwestern.ts before W8.
    name: "transwestern_aetna_transparency",
    hosts: ["health1.aetna.com"],
    pathPatterns: [],
    reason: "Transwestern footer link to an Aetna benefits site",
  },
  {
    // collector-only, any host: a bare site root is a web page, not a file. Seen live on
    // Transwestern, where the "Download flyer" button's same-page `#property-downloads`
    // anchor was resolved against the site host and attached as `https://transwestern.com/#...`.
    // A root URL that carries a query string (`/?file=...`) is left alone.
    name: "site_root_page",
    pathPatterns: [/^\/$/i],
    reason: "site root or same-page anchor, not a file",
  },
];

/** Resolve a path the way a server reads it: percent-decoded (re-decoded for double
 *  encoding), `;params` dropped, backslashes as slashes, repeated slashes collapsed and
 *  dot segments resolved, so an encoded or dotted spelling cannot slip past a pattern. */
function normalizedPath(pathname: string): string {
  let raw = pathname || "/";
  for (let i = 0; i < 3; i += 1) {
    let decoded: string;
    try {
      decoded = decodeURIComponent(raw);
    } catch {
      break; // malformed escape: match what we have
    }
    if (decoded === raw) break;
    raw = decoded;
  }
  const kept: string[] = [];
  for (const segment of raw.replace(/\\/g, "/").split("/")) {
    const name = segment.split(";", 1)[0]!;
    if (name === "" || name === ".") continue;
    if (name === "..") kept.pop();
    else kept.push(name);
  }
  return `/${kept.join("/")}`;
}

function hostMatches(host: string, ruleHost: string): boolean {
  return host === ruleHost || host.endsWith(`.${ruleHost}`);
}

/** The first rule that marks `url` as boilerplate, or null. Never throws. */
export function matchDocumentBoilerplate(url: string): DocumentBoilerplateRule | null {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
  const target = normalizedPath(parsed.pathname) + parsed.search;
  for (const rule of DOCUMENT_BOILERPLATE_RULES) {
    if (rule.hosts && !rule.hosts.some((ruleHost) => hostMatches(host, ruleHost))) continue;
    if (rule.pathPatterns.length === 0 || rule.pathPatterns.some((pattern) => pattern.test(target))) {
      return rule;
    }
  }
  return null;
}

export function isBoilerplateDocumentUrl(url: string): boolean {
  return matchDocumentBoilerplate(url) !== null;
}
