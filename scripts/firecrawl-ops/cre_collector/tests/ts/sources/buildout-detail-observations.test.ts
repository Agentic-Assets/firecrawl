import assert from "node:assert/strict";
import test from "node:test";
import { offlineBuildoutDetailObservations as parse } from "../../../sources/pure/buildout-detail-observations.js";

const table = (rows: string) => `<table class="table COMPONENT__table">${rows}</table>`;
const row = (label: string, value: string) => `<tr><td>${label}</td><td>${value}</td></tr>`;

test("retains ordered source literals and conflicts without identity or promotion claims", () => {
  const result = parse(table(row("NOI", "$10,000") + row("NOI", "$12,000") + row("Year Built", "1995") + row("Cap Rate", "6.5%")));
  assert.deepEqual(result.observations.map(x => [x.field, x.literal, x.rowOrdinal]), [["noi", "$10,000", 0], ["noi", "$12,000", 1], ["yearBuilt", "1995", 2], ["capRate", "6.5%", 3]]);
  assert.equal(result.identity, "unverified");
  assert.equal(result.visibility, "unverified");
  assert.equal(result.promotion, "not_authorized");
});

test("omits hidden, withheld, and unsafe value text and does not pair across rows", () => {
  const html = table('<tr hidden><td>NOI</td><td>$999</td></tr>' + row("Cap Rate", '<span aria-hidden="true">7%</span>') + row("NOI", "Withheld $123") + row("Year Built", "Bearer secret") + '<tr><td>NOI</td></tr><tr><td>$456</td></tr>') + '<table>' + row("NOI", "$500") + '</table>';
  const result = parse(html);
  assert.deepEqual(result.observations, []);
  assert.equal(result.omittedCount, 5);
  assert.doesNotMatch(JSON.stringify(result), /999|123|secret|456|500|7%/);
});

test("rejects whole output when resource limits are exceeded", () => {
  assert.equal(parse("x".repeat(2_000_001)).state, "not_evaluated_limit");
  const tooMany = parse(table(row("NOI", "$1").repeat(101)));
  assert.equal(tooMany.state, "not_evaluated_limit");
  assert.deepEqual(tooMany.observations, []);
});


test("rejects inline visibility controls regardless of CSS spacing or ARIA case", () => {
  const html = table(
    '<tr style="display : none"><td>NOI</td><td>$111</td></tr>' +
    '<tr aria-hidden="TRUE"><td>Cap Rate</td><td>7%</td></tr>' +
    '<tr><td>Year Built</td><td><span style="VISIBILITY : HIDDEN !important">2001</span></td></tr>' +
    '<tr style="color: red; DISPLAY : NONE !important"><td>NOI</td><td>$222</td></tr>'
  );
  const result = parse(html);
  assert.equal(result.omittedCount, 4);
  assert.deepEqual(result.observations, []);
  assert.doesNotMatch(JSON.stringify(result), /111|222|2001|7%/);
});


test("counts recognized labels with malformed cell counts as omissions", () => {
  const result = parse(table('<tr><td>NOI</td><td>$100</td><td>$200</td></tr>' + '<tr><td>Cap Rate</td></tr>'));
  assert.equal(result.omittedCount, 2);
  assert.deepEqual(result.observations, []);
  assert.doesNotMatch(JSON.stringify(result), /100|200/);
});


test("fails closed on deeply nested pages without emitting earlier rows", () => {
  const html = table(row("NOI", "$10")) + "<div>".repeat(5000) + table(row("NOI", "$20").repeat(100)) + "</div>".repeat(5000);
  const result = parse(html);
  assert.equal(result.state, "not_evaluated_limit");
  assert.deepEqual(result.observations, []);
  assert.equal(result.omittedCount, null);
});
