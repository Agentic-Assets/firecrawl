import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { captureDetailTables, DETAIL_TABLE_LIMITS as limits } from "../../../lib/detail-tables/index.js";
const context = { sourceUrl: "https://example.org/property?id=1", observedAt: "2026-10-28T11:00:00Z" };
test("preserves ordered duplicate labels, values, spans and nested table relationships", () => {
  const result = captureDetailTables(`<table hidden><caption>Financials</caption><tr><th rowspan="2">NOI</th><td colspan="2">$100 <b>USD</b></td></tr><tr aria-hidden="true"><td>NOI</td><td>$200<table><tr><td>Nested</td></tr></table></td></tr></table>`, context);
  assert.equal(result.status, "observed"); assert.equal(result.tables.length, 2);
  assert.equal(result.tables[0].visibility.hidden, true); assert.equal(result.tables[0].caption, "Financials");
  assert.equal(result.tables[0].rows[0].cells[0].rowspan, "2"); assert.equal(result.tables[0].rows[0].cells[1].colspan, "2");
  assert.equal(result.tables[0].rows[0].cells[1].text, "$100 USD");
  assert.equal(result.tables[0].rows[1].visibility.ariaHidden, "true"); assert.equal(result.tables[0].rows[1].cells[1].text, "$200");
  assert.equal(result.tables[1].parentTableOrdinal, 0); assert.equal(result.tables[1].rows[0].cells[0].text, "Nested");
  assert.equal(result.omissions.characters, 0);
});
test("reports bounded text, row, cell and table omissions", () => {
  const long = "x".repeat(limits.textChars + 7); const cellOverflow = "<td>x</td>".repeat(limits.cellsPerRow + 2);
  const rowOverflow = "<tr><td>x</td></tr>".repeat(limits.rowsPerTable);
  const html = `<table><tr><td>${long}</td>${cellOverflow}</tr>${rowOverflow}</table>` + "<table></table>".repeat(limits.tables);
  const result = captureDetailTables(html, context);
  assert.equal(result.tables.length, limits.tables); assert.equal(result.omissions.tables, 1);
  assert.equal(result.omissions.rows, 1); assert.equal(result.omissions.cells, 3);
  assert.equal(result.omissions.characters, 14); assert.equal(result.omissions.unknown, true);
  assert.equal(result.tables[0].rows[0].cells[0].text.length, limits.textChars);
});
test("fails inspectably on invalid or oversized input and identifies no tables", () => {
  assert.equal(captureDetailTables(null, context).error, "invalid_input");
  const oversized = captureDetailTables("x".repeat(limits.inputBytes + 1), context);
  assert.equal(oversized.error, "input_too_large"); assert.equal(oversized.omissions.unknown, true);
  assert.equal(captureDetailTables("<p>No table</p>", context).status, "empty");
  assert.equal(captureDetailTables("<table></table>", { ...context, sourceUrl: "https://example.org/?token=secret" }).error, "invalid_provenance");
});
test("enforces total character budget without silently dropping text and never emits script text", () => {
  const value = "a".repeat(limits.textChars);
  const html = "<table>" + Array.from({ length: 70 }, () => `<tr><td>${value}<script>secret script</script></td></tr>`).join("") + "</table>";
  const result = captureDetailTables(html, context);
  assert.equal(result.status, "observed");
  assert.equal(result.omissions.characters, 2 * 70 * limits.textChars - limits.totalChars);
  assert.equal(result.tables[0].rows.reduce((sum, row) => sum + row.cells[0].text.length + row.cells[0].rawText.length, 0), limits.totalChars);
  assert(!JSON.stringify(result).includes("secret script"));
});
test("restores exact captured false, null, empty and spans after pruning; rejects foreign shapes", async () => {
  const { preserveDetailTables } = await import("../../../lib/detail-tables/index.js");
  const snapshot = captureDetailTables('<table><tr><th rowspan="2"></th><td hidden colspan="2">0</td></tr></table>', context);
  assert.deepEqual(preserveDetailTables(snapshot), snapshot);
  assert.equal(preserveDetailTables({ ...snapshot, surprise: "value" }), undefined);
  assert.equal(preserveDetailTables({ ...snapshot, provenance: { ...snapshot.provenance, sourceUrl: "https://example.org/?token=secret" } }), undefined);
  assert.equal(preserveDetailTables({ ...snapshot, tables: Array(limits.tables + 1).fill(snapshot.tables[0]) }), undefined);
  assert.equal(preserveDetailTables(null), undefined);
});
test("preservation retains inspectable failed attempts", async () => {
  const { preserveDetailTables } = await import("../../../lib/detail-tables/index.js");
  const invalid = captureDetailTables(null, context);
  const oversized = captureDetailTables("x".repeat(limits.inputBytes + 1), context);
  assert.deepEqual(preserveDetailTables(invalid), invalid);
  assert.deepEqual(preserveDetailTables(oversized), oversized);
  assert.equal(preserveDetailTables({ ...invalid, omissions: { ...invalid.omissions, unknown: false } }), undefined);
});
test("preserves source whitespace and binds hash of supplied UTF-8 HTML", () => {
  const html = '<table><caption> Caption </caption><tr><td>  Café\n value  </td></tr></table>';
  const result = captureDetailTables(html, { ...context, observedAt: "2024-02-29T12:30:01.123456Z" });
  assert.equal(result.tables[0].caption, "Caption"); assert.equal(result.tables[0].rawCaption, " Caption ");
  assert.equal(result.tables[0].rows[0].cells[0].rawText, "  Café\n value  ");
  assert.equal(result.tables[0].rows[0].cells[0].text, "Café\n value");
  assert.equal(result.provenance.inputHashKind, "scraped_html_utf8");
  assert.equal(result.provenance.inputSha256, createHash("sha256").update(html, "utf8").digest("hex"));
  assert.equal(captureDetailTables(html, { ...context, observedAt: "2023-02-29T12:30:01Z" }).error, "invalid_provenance");
  assert.equal(captureDetailTables(html, { ...context, observedAt: "2024-02-30T12:30:01Z" }).error, "invalid_provenance");
  assert.equal(captureDetailTables(null, context).provenance.inputSha256, null);
});
test("text bounds do not split a surrogate pair and count omitted code units", () => {
  const html = `<table><tr><td>${"a".repeat(limits.textChars - 1)}😀</td></tr></table>`;
  const result = captureDetailTables(html, context);
  assert.equal(result.tables[0].rows[0].cells[0].text, "a".repeat(limits.textChars - 1));
  assert.equal(result.tables[0].rows[0].cells[0].rawText, "a".repeat(limits.textChars - 1));
  assert.equal(result.omissions.characters, 4);
});
test("bounds structural amplification and preserves partial snapshot", async () => {
  const { preserveDetailTables } = await import("../../../lib/detail-tables/index.js");
  const row = `<tr>${'<td></td>'.repeat(32)}</tr>`;
  const html = Array.from({ length: 32 }, () => `<table>${row.repeat(200)}</table>`).join('');
  assert(Buffer.byteLength(html) < limits.inputBytes);
  const result = captureDetailTables(html, context); const rows = result.tables.flatMap(t => t.rows);
  assert(rows.length <= limits.totalRows);
  assert(rows.reduce((n, r) => n + r.cells.length, 0) <= limits.totalCells);
  assert(Buffer.byteLength(JSON.stringify(result)) <= limits.serializedBytes);
  assert(result.omissions.rows > 0); assert(result.omissions.cells > 0); assert(result.omissions.unknown);
  assert.deepEqual(preserveDetailTables(result), result);
});
test("guard accepts long-text omissions and failures retain known provenance", async () => {
  const { preserveDetailTables } = await import("../../../lib/detail-tables/index.js");
  const result = captureDetailTables(`<table><tr><td>${'x'.repeat(1_100_000)}</td></tr></table>`, context);
  assert(result.omissions.characters > 2_000_000); assert.deepEqual(preserveDetailTables(result), result);
  const failure = captureDetailTables(null, context);
  assert.equal(failure.provenance.sourceUrl, context.sourceUrl); assert.equal(failure.provenance.observedAt, context.observedAt);
  assert.equal(failure.provenance.inputSha256, null); assert.deepEqual(preserveDetailTables(failure), failure);
});
test("serialized cap handles escape expansion with explicit cell omissions", async () => {
  const { preserveDetailTables } = await import("../../../lib/detail-tables/index.js");
  const html = '<table>' + Array.from({length: 200}, () => '<tr>' + Array.from({length: 20}, () => `<td>${'\x01'.repeat(80)}</td>`).join('') + '</tr>').join('') + '</table>';
  const result = captureDetailTables(html, context);
  assert(Buffer.byteLength(JSON.stringify(result)) <= limits.serializedBytes);
  assert(result.omissions.cells > 0);
  assert.deepEqual(preserveDetailTables(result), result);
});
