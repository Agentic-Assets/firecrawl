import test from "node:test";
import assert from "node:assert/strict";
import * as cheerio from "cheerio";
import { transwesternDescription } from "../../../sources/transwestern.js";

const doc = { rawHtml: "", markdown: "Overview\nUnrelated navigation blurb\nDetails\n", links: [] } as any;
const page = (tabs: string, panels: string) => `<section class="property-dtl"><div class="fix-wrap"><div class="property-dtl-inner"><div class="property-dscptn"><div class="prpty-dscptn-left"><div id="prpty-dscptn-tab"><ul class="resp-tabs-list">${tabs}</ul><div class="resp-tabs-container">${panels}</div></div></div></div></div></div></section>`;
const tabs = "<li>Description</li><li>Details</li>";
const panels = '<div>First line<br>Second line <a href="https://example.com/plan">plan</a><span hidden>secret</span></div><div>Other details</div>';
const description = (html: string) => transwesternDescription(cheerio.load(html), doc, "https://transwestern.com/property/example");

test("reads the property Description panel and preserves line breaks and links", () => {
  const result = description(page(tabs, panels));
  assert.match(result!, /First line\s+\nSecond line/);
  assert.match(result!, /\[plan\]\(https:\/\/example.com\/plan\)/);
  assert.doesNotMatch(result!, /secret|Other details|navigation/);
});

test("holds ambiguous or concealed panels without global fallback", () => {
  const native = page(tabs, panels);
  for (const html of [native + native, page("<li>Description</li><li>Description</li>", panels),
    page("<li>Details</li>", "<div>Unrelated</div>"), native.replace('class="property-dtl"', 'class="property-dtl" hidden'),
    page(tabs, "<div>Only one panel</div>"), page(tabs, "<div></div><div>Other details</div>"),
    native.replace('id="prpty-dscptn-tab"', 'class="renamed-tabs"')]) {
    assert.equal(description(`<div id="overview">Navigation blurb</div>${html}`), null);
  }
});

test("resolves source-relative links and preserves legitimate scoped text", () => {
  const html = page(tabs, '<div>The tenant maintains a privacy policy. <a href="/property/specs">Specifications</a> <a href="javascript:alert(1)">Unsafe</a></div><div>Details</div>');
  const result = description(html)!;
  assert.match(result, /tenant maintains a privacy policy/i);
  assert.match(result, /\[Specifications\]\(https:\/\/transwestern.com\/property\/specs\)/);
  assert.doesNotMatch(result, /javascript:/);
});

test("retains the existing explicit and Markdown fallbacks for legacy layouts", () => {
  assert.equal(description('<div class="property-description">Known legacy description</div>'), "Known legacy description");
  assert.equal(description('<div>Legacy page</div>'), "Unrelated navigation blurb");
  assert.equal(description('<div id="overview">Privacy Policy</div>'), null);
});
