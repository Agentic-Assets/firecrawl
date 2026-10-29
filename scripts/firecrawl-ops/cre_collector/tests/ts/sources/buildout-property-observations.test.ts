process.argv = [process.argv[0]!, process.argv[1]!];
import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { buildoutPropertyObservationsIncomplete, canCaptureBuildoutPropertyObservations, captureBuildoutObservationForCaller, captureBuildoutPropertyObservations, validateBuildoutPropertyObservations } from "../../../sources/pure/buildout-property-observations/index.js";
import { buildoutEnricherFor, groupEnrichItems, runEnrichGroups } from "../../../lib/enrich.js";
import { firecrawl } from "../../../lib/scrape.js";
import { observePages, validateObservationInput } from "../../../lib/page-observations/index.js";
import { captureEnrichmentEvidence } from "../../../lib/enrichment-evidence/index.js";
import { registeredBuildoutFirmFor } from "../../../sources/buildout-registry.js";

const source = "https://buildout.com/example";
const section = (label: string, body: string) => `<div><div><h2 class="pdt-subheader">${label}</h2></div><div><p>${body}</p></div></div>`;
const card = (name: string, email: string) => `<div class="pdt-broker"><div class="pdt-broker-name">${name}</div><div class="pdt-broker-title">Director</div><a href="mailto:${email}">Email</a><a href="tel:+12345">Phone</a></div>`;
const page = (body: string) => `<div class="pdt-site-content"><div id="template-tab-container"><div id="overview">${body}</div></div></div><footer><h2 class="pdt-subheader">Property Description</h2><p>Unrelated footer</p></footer>`;

test("ordered sections and cards, including hidden and duplicate groups, stay unqualified", () => {
  const html = page(section("Property Description", "First description") + section("Location Description", "Other description") + `<div class="pdt-brokers">${card("Agent", "agent@example.test")}</div><div class="pdt-brokers" hidden>${card("Agent", "agent@example.test")}${card("", "")}</div>`);
  const result = captureBuildoutPropertyObservations(html, source);
  assert.equal(result.status, "observed");
  assert.equal(result.contactsStatus, "ambiguous");
  assert.deepEqual(result.sections.map(row => row.label), ["Property Description", "Location Description"]);
  assert.equal(result.contacts.length, 3);
  assert.equal(result.contacts[0].ordinal, 0);
  assert.equal(result.contacts[1].groupOrdinal, 1);
  assert.equal(result.contacts[1].disposition, "static_hidden");
  assert.equal(result.contacts[2].fields.name.values[0], null);
  assert.equal(result.omittedContacts, 0);
});

test("missing/duplicate panels and footer never borrow unrelated text", () => {
  assert.equal(captureBuildoutPropertyObservations("<footer>Unrelated</footer>", source).status, "unavailable");
  assert.equal(captureBuildoutPropertyObservations(page("") + page(""), source).status, "ambiguous");
  const duplicate = captureBuildoutPropertyObservations(page(section("Property Description", "One") + section("Property Description", "Two")), source);
  assert.equal(duplicate.sections.length, 2);
  const shared = captureBuildoutPropertyObservations(page(`<div><div><h2 class="pdt-subheader">One</h2></div><div>First value</div><div><h2 class="pdt-subheader">Two</h2></div><div><a href="https://example.test/">Second value</a></div></div>`), source);
  assert(shared.sections.every(row => row.status === "ambiguous"));
  assert(shared.sections.every(row => row.text?.includes("value")));
  assert(shared.sections.some(row => row.links.length));
  const empty = captureBuildoutPropertyObservations(page(section("Property Description", "")), source);
  assert.equal(empty.sections[0].text, null);
  assert.equal(empty.contactsStatus, "unavailable");
  const footer = captureBuildoutPropertyObservations(page(`<footer>${section("Property Description", "Footer")}<div class="pdt-brokers">${card("Fake", "fake@example.test")}</div></footer>`), source);
  assert.equal(footer.sections.length, 0);
  assert.equal(footer.contactsStatus, "unavailable");
  assert.equal(footer.excludedSections, 1);
  assert.equal(footer.excludedContacts, 1);
});

test("ambiguous panels retain each panel's sections and cards with placement", () => {
  const first = page(section("Property Description", "One") + `<div class="pdt-brokers">${card("First", "first@example.test")}</div>`);
  const second = page(section("Property Description", "Two") + `<div class="pdt-brokers">${card("Second", "second@example.test")}</div>`);
  const result = captureBuildoutPropertyObservations(first + second, source);
  assert.equal(result.status, "ambiguous");
  assert.equal(result.rootCount, 2);
  assert.equal(result.panelCount, 2);
  assert.deepEqual(result.sections.map(x => [x.panelOrdinal, x.rootOrdinal, x.text, x.status]), [[0, 0, "One", "ambiguous"], [1, 1, "Two", "ambiguous"]]);
  assert.deepEqual(result.contacts.map(x => [x.panelOrdinal, x.rootOrdinal, x.fields.name.values[0]]), [[0, 0, "First"], [1, 1, "Second"]]);
  assert.equal(result.contactsStatus, "ambiguous");
  assert(validateBuildoutPropertyObservations(result, source));
  const twoPanels = `<div class="pdt-site-content"><div id="template-tab-container"><div id="overview">${section("Property Description", "One")}</div><div id="overview">${section("Property Description", "Two")}</div></div></div>`;
  const sameRoot = captureBuildoutPropertyObservations(twoPanels, source);
  assert.equal(sameRoot.rootCount, 1);
  assert.equal(sameRoot.panelCount, 2);
  assert.equal(sameRoot.status, "ambiguous");
  assert.deepEqual(sameRoot.sections.map(x => x.text), ["One", "Two"]);
});

test("carrier validation enforces aggregate byte budget", () => {
  const result = captureBuildoutPropertyObservations(page(section("Property Description", "Original")), source);
  const prototype = result.sections[0];
  const forged = {...result, sections: Array.from({length: 11}, (_, ordinal) => ({...prototype, ordinal, text: "x".repeat(16_000)}))};
  assert.equal(validateBuildoutPropertyObservations(forged, source), false);
});

test("field limits explicitly omit overlong text without truncating a URL", () => {
  const result = captureBuildoutPropertyObservations(page(section("Property Description", "x".repeat(20_000))), source);
  assert.equal(result.sections[0].text, null);
  assert(result.omittedFields > 0);
  const config = registeredBuildoutFirmFor("leibsohn")!.detailConfig!;
  const url = `https://buildout.com/plugins/${config.pluginKey}/${config.host}/inventory/fixture-sale`;
  const captured = captureBuildoutPropertyObservations(page(section("Property Description", "x".repeat(20_000))), url);
  const evidence = captureEnrichmentEvidence({sourceKey: "leibsohn", externalId: null, url}, {rawHtml: "", markdown: "", links: []}, null,
    {kind: "buildout", sourceObservation: captured, sourceObservationIncomplete: buildoutPropertyObservationsIncomplete(captured)});
  assert.equal(evidence.omissions.sourceObservation, "source_observation_partial");
  assert(evidence.sourceObservation);
});

test("normal enrichment retains scoped evidence but never promotes description or contacts", async () => {
  const old = firecrawl.scrape;
  const html = page(section("Property Description", "Source text") + `<div class="pdt-brokers">${card("Agent", "agent@example.test")}</div>`);
  const url = "http://www.leibsohn.com/properties/?propertyId=fixture-sale";
  try {
    (firecrawl as any).scrape = async () => ({rawHtml: html, markdown: "Original full markdown", links: []});
    const config = registeredBuildoutFirmFor("leibsohn")!.detailConfig!;
    const result = await runEnrichGroups(groupEnrichItems([{sourceKey: "leibsohn", externalId: "fixture", url}]),
      () => ({enricher: buildoutEnricherFor(config), label: "bespoke"}), () => "Leibsohn");
    assert.equal(result.listings[0].url, url);
    assert.equal(result.listings[0].id, "fixture");
    assert.equal(result.listings[0].markdown, "Original full markdown");
    assert.equal(result.listings[0].description, undefined);
    assert.equal(result.listings[0].brokerIds, undefined);
    const evidence = result.sources[0].retainedObservations[0] as any;
    assert.equal(evidence.sourceObservation.contacts[0].fields.name.values[0], "Agent");
    assert.equal(evidence.sourceObservation.routeBinding, "configured_claim_route");
    assert.equal(evidence.sourceObservation.contacts[0].groupOrdinal, 0);
    assert.equal(evidence.sourceObservation.omittedFields, 0);
  } finally { (firecrawl as any).scrape = old; }
});

test("unqualified error shell still retains scoped observation", async () => {
  const old = firecrawl.scrape;
  try {
    (firecrawl as any).scrape = async () => ({rawHtml: page(section("Property Description", "Partial listing not found")), markdown: "listing not found", links: []});
    const config = registeredBuildoutFirmFor("leibsohn")!.detailConfig!;
    const result = await buildoutEnricherFor(config).enrichWithEvidence!([{sourceKey: "leibsohn", externalId: "fixture", url: "http://www.leibsohn.com/properties/?propertyId=fixture-sale"}]);
    assert.deepEqual(result.listings, []);
    assert.equal((result.retainedObservations[0] as any).qualification, "rejected_missing_detail_shell");
    assert.equal((result.retainedObservations[0] as any).sourceObservation.sections[0].text, "Partial listing not found");
  } finally { (firecrawl as any).scrape = old; }
});


test("evidence-only observer captures source-scoped fields without canonical output", async () => {
  const config = registeredBuildoutFirmFor("leibsohn")!.detailConfig!;
  const url = `https://buildout.com/plugins/${config.pluginKey}/${config.host}/inventory/fixture-sale?pluginId=0&iframe=true&embedded=true`;
  const html = page(section("Property Description", "Source text") + `<div class="pdt-brokers">${card("Agent", "agent@example.test")}</div>`);
  const items = validateObservationInput({items: [{sourceKey: "leibsohn", sourceId: "native", url}]});
  const result = await observePages(items, async () => ({rawHtml: html, markdown: "Original", links: []}), captureBuildoutObservationForCaller);
  assert.deepEqual(result.parts[0].listings, []);
  const evidence = (result.parts[0].sources[0].retainedObservations[0] as any).evidence;
  assert.equal(evidence.sourceObservation.sections[0].text, "Source text");
  assert.equal(evidence.sourceObservation.routeBinding, "unverified_caller_url");
  assert.equal(evidence.sourceObservation.contacts[0].fields.name.values[0], "Agent");
  assert.equal(evidence.sourceFreshness, "unverified");
});

test("source and carrier guards reject unrelated source, host, credential query and malformed shape", () => {
  const config = registeredBuildoutFirmFor("leibsohn")!.detailConfig!;
  const url = `https://buildout.com/plugins/${config.pluginKey}/${config.host}/inventory/fixture-sale`;
  assert(canCaptureBuildoutPropertyObservations("leibsohn", url));
  assert(!canCaptureBuildoutPropertyObservations("cbre", url));
  assert(!canCaptureBuildoutPropertyObservations("leibsohn", url.replace("buildout.com", "other.example")));
  assert(!canCaptureBuildoutPropertyObservations("leibsohn", url + "?token=fixture"));
  const observation = captureBuildoutPropertyObservations(page(section("Property Description", "Text")), url);
  assert(validateBuildoutPropertyObservations(observation, url));
  assert(!validateBuildoutPropertyObservations({...observation, status: "invented"}, url));
  assert.equal(captureBuildoutObservationForCaller("cbre", url, {rawHtml: ""}), undefined);
  assert(!validateBuildoutPropertyObservations({...observation, contacts: [{extra: "forged"}]}, url));
});


test("credential-bearing source links remain subject to the existing retention guard", () => {
  const config = registeredBuildoutFirmFor("leibsohn")!.detailConfig!;
  const url = `https://buildout.com/plugins/${config.pluginKey}/${config.host}/inventory/fixture-sale`;
  const html = page(section("Property Description", '<a href="https://example.test/file?token=SYNTHETIC">Reference</a>'));
  const obs = captureBuildoutPropertyObservations(html, url);
  const evidence = captureEnrichmentEvidence({sourceKey: "leibsohn", externalId: null, url}, {rawHtml: html, markdown: "", links: []}, null,
    {kind: "buildout", sourceObservation: obs});
  const code = `import json,sys\nfrom source_evidence.automatic import _refuse_credential_fields\ntry: _refuse_credential_fields(json.load(sys.stdin))\nexcept ValueError: sys.exit(0)\nsys.exit(1)`;
  const result = spawnSync("python3", ["-c", code], {input: JSON.stringify(evidence), encoding: "utf8"});
  assert.equal(result.status, 0, result.stderr);
});

test("nested groups own each card once and exclude footer cards and fields", () => {
  const html = page(`<div class="pdt-brokers">${card("Outer", "outer@example.test")}<footer>${card("Footer", "footer@example.test")}</footer><div class="pdt-brokers">${card("Inner", "inner@example.test")}</div></div>`);
  const result = captureBuildoutPropertyObservations(html, source);
  assert.deepEqual(result.contacts.map(x => [x.groupOrdinal, x.fields.name.values]), [[0, ["Outer"]], [1, ["Inner"]]]);
  assert.equal(result.excludedContacts, 1);
  assert(validateBuildoutPropertyObservations(result, source));
});

test("conflicting card fields retain ordered occurrences and mark ambiguity", () => {
  const html = page(`<div class="pdt-brokers"><div class="pdt-broker"><div class="pdt-broker-name">One<footer>Bad</footer></div><div class="pdt-broker-name">Two</div><div class="pdt-broker-title">First</div><div class="pdt-broker-title">Second</div><a href="mailto:a@example.test">A</a><a href="mailto:b@example.test">B</a><footer><a href="mailto:bad@example.test">Bad</a></footer><img class="pdt-broker-photo" src="/a"><img class="pdt-broker-photo" src="/b"></div></div>`);
  const result = captureBuildoutPropertyObservations(html, source);
  const contact = result.contacts[0];
  assert.deepEqual(contact.fields.name.values, ["One", "Two"]);
  assert.deepEqual(contact.fields.title.values, ["First", "Second"]);
  assert.deepEqual(contact.fields.emailHref.values, ["mailto:a@example.test", "mailto:b@example.test"]);
  assert.deepEqual(contact.fields.photoUrl.values, ["/a", "/b"]);
  assert.deepEqual(contact.fields.phoneHref.values, []);
  assert(!contact.text?.includes("Bad"));
  assert.equal(result.excludedContactFields, 1);
  assert.equal(contact.status, "ambiguous");
  assert(buildoutPropertyObservationsIncomplete(result));
  assert(validateBuildoutPropertyObservations(result, source));
  const many = captureBuildoutPropertyObservations(page(`<div class="pdt-brokers"><div class="pdt-broker">${'<div class="pdt-broker-name">A</div>'.repeat(101)}</div></div>`), source);
  assert.equal(many.contacts[0].fields.name.values.length, 100);
  assert.equal(many.contacts[0].fields.name.omitted, 1);
  assert(buildoutPropertyObservationsIncomplete(many));
});

test("nested panels do not borrow sections or cards from one another", () => {
  const html = page(section("Outer", "Outer body") + `<div class="pdt-brokers">${card("Outer", "outer@example.test")}</div><div id="overview">${section("Inner", "Inner body")}<div class="pdt-brokers">${card("Inner", "inner@example.test")}</div></div>`);
  const result = captureBuildoutPropertyObservations(html, source);
  assert.equal(result.panelCount, 2);
  assert.deepEqual(result.sections.map(x => [x.panelOrdinal, x.label, x.text]), [[0, "Outer", "Outer body"], [1, "Inner", "Inner body"]]);
  assert.deepEqual(result.contacts.map(x => [x.panelOrdinal, x.fields.name.values]), [[0, ["Outer"]], [1, ["Inner"]]]);
  assert(validateBuildoutPropertyObservations(result, source));
});
