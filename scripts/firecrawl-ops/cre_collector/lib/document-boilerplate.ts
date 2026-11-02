// lib/document-boilerplate.ts - site-wide boilerplate that is never a listing document.
//
// AGENTIC-5143 (W8), plan AGENTIC-5134 sections 0 (fact 4) and 4. A broker's detail page
// carries site chrome (regulatory notices, service pages) that harvestDetail() would
// otherwise classify as a document and attach to every listing on the site.
//
// Pure: no network, no argv/config import (harvest.ts must stay import-safe). To teach the
// collector a new boilerplate shape, add one DocumentBoilerplateRule row below, mirror it in
// cre_document_boilerplate.py and tests/fixtures/document_boilerplate_rules.json (the shared
// parity fixture both implementations are tested against), and add a denied URL there.
//
// Consistency contract with the producer-side rule, GetCREdata documents/host_rules.py
// (rows `transwestern_boilerplate` and `jll_services_boilerplate`): the same host matching
// (exact host or any subdomain, trailing dots stripped) and the same path patterns,
// evaluated against the path as a server reads it (see normalizedPath). The fixture records
// the GetCREdata commit and blob the two mirrored rows were checked against. Rows marked
// "collector-only" are shapes this collector already excluded before W8; GetCREdata does
// not list them.
//
// Scope: this filter applies to the output of harvestDetail(). Adapters that build
// `documents` or `brochures` without harvestDetail are not covered (see lib/AGENTS.md).
//
// Not covered here: the plan's generic guard (a canonical URL attached to 25 or more
// listings whose doc_type is not `om`). It needs corpus-wide counts, so it stays a
// GetCREdata sync-time rule; a per-listing collector cannot evaluate it.

export interface DocumentBoilerplateRule {
  /** Stable identifier for logs and tests. */
  name: string;
  /** Lowercase hosts; a rule applies to the exact host or any subdomain of it. */
  hosts: readonly string[];
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
];

function decodeEscape(escapes: string): string {
  try {
    return decodeURIComponent(escapes);
  } catch {
    return escapes;
  }
}

/** Decode %XX escapes like Python's `unquote`, leniently: a run of escapes that is not valid
 *  UTF-8 is retried escape by escape, and an escape that still fails stays as written, so one
 *  bad escape never stops the rest of the path from being decoded. */
function decodeEscapeRuns(value: string): string {
  return value.replace(/(?:%[0-9a-f]{2})+/gi, (run) => {
    const whole = decodeEscape(run);
    return whole !== run ? whole : run.replace(/%[0-9a-f]{2}/gi, decodeEscape);
  });
}

/** Resolve a path the way a server reads it: percent-decoded (re-decoded for double
 *  encoding), `;params` dropped, backslashes as slashes, repeated slashes collapsed and
 *  dot segments resolved, so an encoded or dotted spelling cannot slip past a pattern. */
function normalizedPath(pathname: string): string {
  let raw = pathname || "/";
  for (let i = 0; i < 3; i += 1) {
    const decoded = decodeEscapeRuns(raw);
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
  const host = parsed.hostname.toLowerCase().replace(/\.+$/, "");
  const target = normalizedPath(parsed.pathname) + parsed.search;
  for (const rule of DOCUMENT_BOILERPLATE_RULES) {
    if (!rule.hosts.some((ruleHost) => hostMatches(host, ruleHost))) continue;
    if (rule.pathPatterns.length === 0 || rule.pathPatterns.some((pattern) => pattern.test(target))) {
      return rule;
    }
  }
  return null;
}

export function isBoilerplateDocumentUrl(url: string): boolean {
  return matchDocumentBoilerplate(url) !== null;
}
