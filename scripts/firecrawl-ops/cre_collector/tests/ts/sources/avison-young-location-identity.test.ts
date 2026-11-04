import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { assertAvisonYoungDetailDoc, enrichAvisonYoungListing } from "../../../sources/avison-young.js";
import type { ScrapedDoc } from "../../../types.js";
const urls = ["https://ayus4316jmturkroad.sharplaunch.com", "https://ayusdiscoverypoint.sharplaunch.com", "https://www.avisonyoung.us/properties/4316-j-m-turk-road-flowery-branch-sale", "https://www.avisonyoung.us/properties/2300-discovery-drive-orlando-lease"];
const bases = [{ id: "17341", name: "4316 J M Turk Road", street: "4316 J M Turk Road", city: "Flowery Branch", state: "GA", sharpLaunchUrl: urls[0], externalUrl: urls[2] }, { id: "17315", name: "Discovery Point", street: "2300 Discovery Drive", city: "Orlando", state: "FL", sharpLaunchUrl: urls[1], externalUrl: urls[3] }];
const htmls = [1, 2, 3, 4].map(i => readFileSync(new URL(`../../fixtures/raw_data/avison-young-identity-${i}.html`, import.meta.url), "utf8"));
const doc = (i: number, html = htmls[i]): ScrapedDoc => ({ rawHtml: html, markdown: "", links: [], metadata: { statusCode: 200, sourceURL: urls[i] } });
for (const i of [0, 2, 3])
    test(`Avison primary layout ${i + 1} binds semantic feed identity`, () => assert.doesNotThrow(() => assertAvisonYoungDetailDoc(doc(i), urls[i], bases[i % 2])));
test("Avison native lease without visible street remains unsupported", () => assert.throws(() => assertAvisonYoungDetailDoc(doc(1), urls[1], bases[1]), /identity/));
for (const i of [0, 2, 3])
    for (const field of ["name", "street", "city", "state"] as const)
        test(`Avison layout ${i + 1} refuses wrong feed ${field}`, () => assert.throws(() => assertAvisonYoungDetailDoc(doc(i), urls[i], { ...bases[i % 2], [field]: field === "state" ? "TX" : "Unrelated property" }), /identity/));
for (const i of [0, 2, 3])
    for (const mutation of ["title", "og-title", "og-url", "canonical", "ambiguous-heading", "challenge"])
        test(`Avison layout ${i + 1} refuses ${mutation}`, () => { let h = htmls[i]; if (mutation === "title")
            h = h.replace(/<title>[^<]+/, "<title>Unrelated building"); if (mutation === "og-title")
            h = h.replace(/property="og:title" content="[^"]+/, 'property="og:title" content="Unrelated building'); if (mutation === "og-url")
            h = h.replace(/property="og:url" content="[^"]+/, 'property="og:url" content="https://other.sharplaunch.com/'); if (mutation === "canonical")
            h = h.includes('rel="canonical"') ? h.replace(/rel="canonical" href="[^"]+/, 'rel="canonical" href="https://other.sharplaunch.com/') : h.replace("</head>", '<link rel="canonical" href="https://other.sharplaunch.com/"></head>'); if (mutation === "ambiguous-heading")
            h = h.replace("</body>", "<h1>Another property</h1></body>"); if (mutation === "challenge")
            h = h.replace("</body>", "Checking your browser</body>"); assert.throws(() => assertAvisonYoungDetailDoc(doc(i, h), urls[i], bases[i % 2]), /identity|challenge/); });
for (const i of [2, 3])
    test(`Avison wrapper ${i + 1} refuses conflicting or duplicate structured identity`, () => { for (const h of [htmls[i].replace('"addressRegion":"' + bases[i % 2].state + '"', '"addressRegion":"TX"'), htmls[i].replace("</head>", '<script type="application/ld+json">{"@type":"RealEstateListing","name":"Another property"}</script></head>')])
        assert.throws(() => assertAvisonYoungDetailDoc(doc(i, h), urls[i], bases[i % 2]), /identity/); });
for (const i of [0, 2, 3])
    test(`Avison layout ${i + 1} refuses nonregistered URL binding`, () => assert.throws(() => assertAvisonYoungDetailDoc(doc(i), urls[i], { ...bases[i % 2], sharpLaunchUrl: "https://unrelated.sharplaunch.com/", externalUrl: "https://www.avisonyoung.us/properties/unrelated" }), /identity/));
test("Avison official wrapper retains property-mode child preservation with unsupported native detail", async () => { const old = { ...process.env }; try {
    process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS = "1";
    delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    process.env.AVISON_YOUNG_DETAIL_TRANSPORT = "direct";
    process.env.AVISON_YOUNG_DIRECT_DETAIL_ATTEMPTS = "1";
    const out = await enrichAvisonYoungListing({ ...bases[1], preserveChildCollections: true, detailUnavailable: true, contactsDetailed: [], photos: [] }, false, async (url) => doc(url === urls[1] ? 1 : 3));
    assert.equal(out.preserveChildCollections, true);
    assert.equal(out.detailUnavailable, undefined);
    assert.equal(out.detailObservedWithChildPreservation, true);
    assert.match(out.detailScrape.warnings.join(";"), /identity/);
}
finally {
    for (const k of Object.keys(process.env))
        if (!(k in old))
            delete process.env[k];
    Object.assign(process.env, old);
} });
test("Avison explicit strict mode still refuses the unsupported native lease page", async () => {
    const previous = process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
    process.env.AVISON_YOUNG_DETAIL_TRANSPORT = "direct";
    try {
        await assert.rejects(() => enrichAvisonYoungListing(bases[1], true, async (url) => doc(url === urls[1] ? 1 : 3)), /identity/);
    }
    finally {
        if (previous === undefined)
            delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
        else
            process.env.AVISON_YOUNG_DETAIL_TRANSPORT = previous;
    }
});
for (const i of [0, 2, 3])
    test(`Avison layout ${i + 1} refuses location prefixes and incomplete states`, () => {
        for (const html of [htmls[i].replace(i % 2 ? "Orlando, Florida" : "Flowery Branch, Georgia", i % 2 ? "Orlando Heights, Florida" : "Flowery Branch Heights, Georgia"), htmls[i].replace(i % 2 ? "Orlando, Florida" : "Flowery Branch, Georgia", i % 2 ? "Orlando, " : "Flowery Branch, ")]) {
            assert.throws(() => assertAvisonYoungDetailDoc(doc(i, html), urls[i], bases[i % 2]), /identity/);
        }
    });
test("Avison official wrapper refuses structured street-number substring", () => {
    const html = htmls[3].replaceAll('"streetAddress":"2300 Discovery Drive"', '"streetAddress":"12300 Discovery Drive"');
    assert.throws(() => assertAvisonYoungDetailDoc(doc(3, html), urls[3], bases[1]), /identity/);
});
