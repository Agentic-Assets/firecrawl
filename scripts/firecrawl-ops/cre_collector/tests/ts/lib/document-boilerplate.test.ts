// Pure unit tests for lib/document-boilerplate.ts (AGENTIC-5143). No network, no argv effects.
// URL shapes are the ones GetCREdata documents/host_rules.py is tested against, so the two
// rule sets stay consistent.

import test from "node:test";
import assert from "node:assert/strict";
import {
  DOCUMENT_BOILERPLATE_RULES,
  isBoilerplateDocumentUrl,
  matchDocumentBoilerplate,
} from "../../../lib/document-boilerplate.js";

const TRANSWESTERN_DENIED = [
  "https://transwestern.com/Upload/TREC/TREC-H-A-SA.pdf",
  "https://transwestern.com/Upload/TREC/CN%201-2.pdf",
  "https://transwestern.com/Upload/TREC/TREC-FW-D.pdf",
  "https://transwestern.com/service/structured-finance",
  "https://www.transwestern.com/services/capital-markets",
];

const JLL_DENIED = [
  "https://www.jll.com/en-us/services/financing/debt-advisory",
  "https://www.jll.com/services/financing/debt-advisory",
  "https://www.jll.com/en/service/debt-advisory/",
];

test("Transwestern TREC notices and service pages are boilerplate, not listing documents", () => {
  for (const url of TRANSWESTERN_DENIED) {
    assert.equal(matchDocumentBoilerplate(url)?.name, "transwestern_boilerplate", url);
  }
});

test("JLL services pages are boilerplate, with or without a locale prefix", () => {
  for (const url of JLL_DENIED) {
    assert.equal(matchDocumentBoilerplate(url)?.name, "jll_services_boilerplate", url);
  }
});

test("collector-only footer shapes stay excluded (privacy policy, Aetna benefits site)", () => {
  assert.equal(matchDocumentBoilerplate("https://transwestern.com/privacy-policy")?.name, "transwestern_privacy_policy");
  assert.equal(matchDocumentBoilerplate("https://transwestern.com/en/privacy-policy/")?.name, "transwestern_privacy_policy");
  assert.equal(matchDocumentBoilerplate("https://transwestern.com/privacy-policy?lang=en")?.name, "transwestern_privacy_policy");
  assert.equal(isBoilerplateDocumentUrl("https://transwestern.com/privacy-policy-archive.pdf"), false);
  assert.equal(
    matchDocumentBoilerplate("https://health1.aetna.com/app/public/#/one/insurerCode=AETNACVS_I&brandCode=ALICFI/machine-readable-transparency-in-coverage?reportingEntityType=TPA_21&lock=true")?.name,
    "transwestern_aetna_transparency",
  );
});

test("a bare site root or same-page anchor is a page, not a document, on any host", () => {
  for (const url of [
    "https://transwestern.com/#property-downloads",
    "https://www.jll.com/",
    "https://example-broker.test",
    "https://example-broker.test/#downloads",
  ]) {
    assert.equal(matchDocumentBoilerplate(url)?.name, "site_root_page", url);
  }
  // A root URL with a query string, or any real path, is not caught by the root rule.
  for (const url of [
    "https://example-broker.test/?file=offering-memorandum.pdf",
    "https://example-broker.test/om.pdf",
    "https://transwestern.com/property/sample-property#property-downloads",
  ]) {
    assert.equal(isBoilerplateDocumentUrl(url), false, url);
  }
});

test("real listing documents and brochures are kept", () => {
  const kept = [
    // Transwestern: per-listing GUID store, property-path flyer, other Upload folder
    "https://i.transwestern.com/00000000-0000-4000-8000-000000000000/a.pdf",
    "https://transwestern.com/property/sample-property/flyer.pdf",
    "https://www.transwestern.com/Upload/Property/offering-memorandum.pdf",
    "https://transwestern.com/serviceability/om.pdf",
    // JLL: brochure pages and CDN assets, including the property subdomain
    "https://property.jll.com/listings/example/brochure",
    "https://cdn.jll.com/assets/offering-memorandum.pdf",
    "https://www.jll.com/en-us/property/example/brochure.pdf",
    // Other hosts never match, even with a boilerplate-shaped path
    "https://example-broker.test/Upload/TREC/TREC-H-A-SA.pdf",
    "https://example-broker.test/services/financing/debt-advisory",
    "https://res.cloudinary.com/example/image/upload/v1/om.pdf",
    // Host matching is label-anchored
    "https://notjll.com/services/financing/debt-advisory",
    "https://jll.com.example.test/services/financing/debt-advisory",
    "https://nottranswestern.com/service/structured-finance",
  ];
  for (const url of kept) assert.equal(isBoilerplateDocumentUrl(url), false, url);
});

test("alternate spellings of a denied path are still denied", () => {
  const spellings = [
    "https://TRANSWESTERN.COM/upload/trec/x.pdf", // case
    "https://transwestern.com./Upload/TREC/x.pdf", // trailing dot host
    "https://transwestern.com//Upload//TREC/x.pdf", // repeated slashes
    "https://transwestern.com/foo/../Upload/TREC/x.pdf", // dot segments
    "https://transwestern.com/%55pload/%54REC/x.pdf", // percent-encoded letters
    "https://transwestern.com/%2555pload/TREC/x.pdf", // double encoded
    "https://transwestern.com/Upload;v=1/TREC/x.pdf", // path parameters
    "https://transwestern.com/Upload\\TREC\\x.pdf", // backslashes
    "https://transwestern.com/%2e%2e/service/structured-finance", // encoded dot segments
    "https://transwestern.com/Upload/TREC/x.pdf?download=1#page=2", // query and fragment
    "https://WWW.JLL.COM/EN-US/Services/Financing/Debt-Advisory",
  ];
  for (const url of spellings) assert.equal(isBoilerplateDocumentUrl(url), true, url);
});

test("a malformed escape or URL never throws and is not treated as boilerplate", () => {
  for (const url of ["", "not a url", "http://", "https://transwestern.com/%E0%A4%A", "//transwestern.com/service/x"]) {
    assert.doesNotThrow(() => isBoilerplateDocumentUrl(url), url);
  }
  assert.equal(isBoilerplateDocumentUrl(""), false);
  assert.equal(isBoilerplateDocumentUrl("not a url"), false);
  // A bad escape stops decoding but the raw path is still matched.
  assert.equal(isBoilerplateDocumentUrl("https://transwestern.com/Upload/TREC/%E0%A4%A.pdf"), true);
});

test("the rule list is well formed: unique names, lowercase hosts, case-insensitive patterns, a reason on every row", () => {
  const names = DOCUMENT_BOILERPLATE_RULES.map((rule) => rule.name);
  assert.equal(new Set(names).size, names.length);
  for (const rule of DOCUMENT_BOILERPLATE_RULES) {
    assert.ok(rule.reason.length > 0, rule.name);
    for (const host of rule.hosts ?? []) assert.equal(host, host.toLowerCase(), rule.name);
    for (const pattern of rule.pathPatterns) assert.ok(pattern.flags.includes("i"), `${rule.name} ${pattern}`);
  }
});
