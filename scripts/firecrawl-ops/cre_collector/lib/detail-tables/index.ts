import { createHash } from "node:crypto";
import * as cheerio from "cheerio";

export const DETAIL_TABLE_LIMITS = {
  inputBytes: 2_000_000, tables: 32, rowsPerTable: 200, cellsPerRow: 32,
  textChars: 4096, contextChars: 512, totalChars: 250_000,
  totalRows: 1000, totalCells: 4000, serializedBytes: 2_000_000,
} as const;

type Visibility = { hidden: boolean; ariaHidden: string | null; style: string | null };
type Cell = { tag: "th" | "td"; text: string; rawText: string; rowspan: string | null; colspan: string | null; visibility: Visibility };
type Row = { cells: Cell[]; visibility: Visibility };
type Table = { ordinal: number; parentTableOrdinal: number | null; caption: string | null; rawCaption: string | null; visibility: Visibility; rows: Row[] };
export type DetailTableObservation = {
  schema: "cre-detail-tables/v1";
  status: "observed" | "empty" | "failed";
  error: "invalid_input" | "invalid_provenance" | "input_too_large" | "parse_failed" | null;
  provenance: { sourceUrl: string | null; observedAt: string | null; inputSha256: string | null; inputHashKind: "scraped_html_utf8"; identity: "unverified"; visibility: "unverified" };
  tables: Table[];
  omissions: { tables: number; rows: number; cells: number; characters: number; unknown: boolean };
};

/** Capture literal table evidence only. Caller-supplied provenance remains unverified. */
export function captureDetailTables(rawHtml: unknown, options: { sourceUrl: string; observedAt: string }): DetailTableObservation {
  const output: DetailTableObservation = {
    schema: "cre-detail-tables/v1", status: "failed", error: null,
    provenance: { sourceUrl: null, observedAt: null, inputSha256: null, inputHashKind: "scraped_html_utf8", identity: "unverified", visibility: "unverified" },
    tables: [], omissions: { tables: 0, rows: 0, cells: 0, characters: 0, unknown: false },
  };
  try {
    const u = new URL(options?.sourceUrl);
    const t = options?.observedAt;
    const secret = /^(token|access[_-]?token|api[_-]?key|signature|sig|password|secret|session|x-(?:amz|goog)-(?:signature|credential|security[_-]?token))$/i;
    if (!["http:", "https:"].includes(u.protocol) || u.username || u.password || u.hash ||
        [...u.searchParams.keys()].some(k => secret.test(k)) || options.sourceUrl.length > 4096 ||
        !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z$/.test(t) || !Number.isFinite(Date.parse(t)) || new Date(Date.parse(t)).toISOString().slice(0, 19) !== t.slice(0, 19)) throw new Error();
    output.provenance.sourceUrl = options.sourceUrl; output.provenance.observedAt = t;
  } catch { output.error = "invalid_provenance"; output.omissions.unknown = true; return output; }
  if (typeof rawHtml !== "string") { output.error = "invalid_input"; output.omissions.unknown = true; return output; }
  if (Buffer.byteLength(rawHtml, "utf8") > DETAIL_TABLE_LIMITS.inputBytes) {
    output.error = "input_too_large"; output.omissions.unknown = true; return output;
  }
  try {
    const $ = cheerio.load(rawHtml);
    const all = $("table").toArray();
    output.omissions.tables = Math.max(0, all.length - DETAIL_TABLE_LIMITS.tables);
    let budget = DETAIL_TABLE_LIMITS.totalChars;
    let totalRows = 0;
    let totalCells = 0;
    const bounded = (value: string, limit: number): string => {
      let kept = Math.min(value.length, limit, budget);
      if (kept > 0 && kept < value.length && /[\uD800-\uDBFF]/.test(value[kept - 1]) && /[\uDC00-\uDFFF]/.test(value[kept])) kept--;
      output.omissions.characters += value.length - kept;
      budget -= kept;
      return value.slice(0, kept);
    };
    const visibility = (node: cheerio.Cheerio<any>): Visibility => ({
      hidden: node.attr("hidden") !== undefined,
      ariaHidden: node.attr("aria-hidden") === undefined ? null : bounded(node.attr("aria-hidden")!, DETAIL_TABLE_LIMITS.contextChars),
      style: node.attr("style") === undefined ? null : bounded(node.attr("style")!, DETAIL_TABLE_LIMITS.contextChars),
    });
    for (let i = 0; i < Math.min(all.length, DETAIL_TABLE_LIMITS.tables); i++) {
      const table = $(all[i]);
      const parent = table.parent().closest("table").get(0);
      const captionNode = table.children("caption").first().clone();
      captionNode.find("script,style,table").remove();
      const captionText = captionNode.length ? captionNode.text() : null;
      const caption = captionText === null ? null : bounded(captionText.trim(), DETAIL_TABLE_LIMITS.contextChars);
      const rawCaption = captionText === null ? null : bounded(captionText, DETAIL_TABLE_LIMITS.contextChars);
      const item: Table = { ordinal: i, parentTableOrdinal: parent ? all.findIndex(node => node === parent) : null, caption, rawCaption, visibility: visibility(table), rows: [] };
      const rows = table.find("tr").toArray().filter(row => $(row).closest("table").get(0) === all[i]);
      const rowLimit = Math.min(DETAIL_TABLE_LIMITS.rowsPerTable, DETAIL_TABLE_LIMITS.totalRows - totalRows);
      output.omissions.rows += Math.max(0, rows.length - rowLimit);
      for (const row of rows.slice(0, rowLimit)) {
        totalRows++;
        const rowNode = $(row); const cells = rowNode.children("th,td").toArray();
        const cellLimit = Math.min(DETAIL_TABLE_LIMITS.cellsPerRow, DETAIL_TABLE_LIMITS.totalCells - totalCells);
        output.omissions.cells += Math.max(0, cells.length - cellLimit);
        totalCells += Math.min(cells.length, cellLimit);
        const saved: Row = { visibility: visibility(rowNode), cells: [] };
        for (const cell of cells.slice(0, cellLimit)) {
          const node = $(cell); const textNode = node.clone(); textNode.find("script,style,table").remove();
          const attr = (name: string): string | null => node.attr(name) === undefined ? null : bounded(node.attr(name)!, DETAIL_TABLE_LIMITS.contextChars);
          const originalText = textNode.text();
          saved.cells.push({ tag: cell.tagName === "th" ? "th" : "td", text: bounded(originalText.trim(), DETAIL_TABLE_LIMITS.textChars),
            rawText: bounded(originalText, DETAIL_TABLE_LIMITS.textChars), rowspan: attr("rowspan"), colspan: attr("colspan"), visibility: visibility(node) });
        }
        item.rows.push(saved);
      }
      output.tables.push(item);
    }
    output.omissions.unknown = output.omissions.tables > 0 || output.omissions.rows > 0;
    output.status = all.length ? "observed" : "empty";
    output.provenance.inputSha256 = createHash("sha256").update(rawHtml, "utf8").digest("hex");
    // JSON escaping can expand retained text substantially. Drop a suffix of
    // whole cells/rows if necessary and account for it, keeping earlier data.
    let size = Buffer.byteLength(JSON.stringify(output), "utf8");
    if (size > DETAIL_TABLE_LIMITS.serializedBytes) {
      for (let ti = output.tables.length - 1; ti >= 0 && size > DETAIL_TABLE_LIMITS.serializedBytes - 1000; ti--) {
        const table = output.tables[ti];
        for (let ri = table.rows.length - 1; ri >= 0 && size > DETAIL_TABLE_LIMITS.serializedBytes - 1000; ri--) {
          const row = table.rows[ri];
          while (row.cells.length && size > DETAIL_TABLE_LIMITS.serializedBytes - 1000) {
            const cell = row.cells.pop()!;
            size -= Buffer.byteLength(JSON.stringify(cell), "utf8") + 1;
            output.omissions.cells++;
          }
          if (size > DETAIL_TABLE_LIMITS.serializedBytes - 1000) {
            table.rows.pop();
            size -= Buffer.byteLength(JSON.stringify(row), "utf8") + 1;
            output.omissions.rows++;
            output.omissions.unknown = true;
          }
        }
      }
    }
    return output;
  } catch { output.tables = []; output.status = "failed"; output.provenance.inputSha256 = null; output.omissions.unknown = true; output.error = "parse_failed"; return output; }
}

/** Restore a bounded observation after generic listing pruning. This validates shape,
 * not origin: the caller must pass its own captured snapshot, never external input. */
export function preserveDetailTables(value: unknown): DetailTableObservation | undefined {
  const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
  const keys = (v: Record<string, unknown>, names: string[]) => Object.keys(v).sort().join("\0") === names.sort().join("\0");
  const integer = (v: unknown, maximum: number) => typeof v === "number" && Number.isSafeInteger(v) && v >= 0 && v <= maximum;
  if (!object(value) || !keys(value, ["schema", "status", "error", "provenance", "tables", "omissions"]) ||
      value.schema !== "cre-detail-tables/v1" || !["observed", "empty", "failed"].includes(value.status as string) ||
      !(value.status === "failed" ? ["invalid_input", "invalid_provenance", "input_too_large", "parse_failed"].includes(value.error as string) : value.error === null) ||
      !object(value.provenance) || !keys(value.provenance, ["sourceUrl", "observedAt", "inputSha256", "inputHashKind", "identity", "visibility"]) ||
      value.provenance.identity !== "unverified" || value.provenance.visibility !== "unverified" ||
      value.provenance.inputHashKind !== "scraped_html_utf8" ||
      !(value.status === "failed" ? value.provenance.inputSha256 === null : typeof value.provenance.inputSha256 === "string" && /^[a-f0-9]{64}$/.test(value.provenance.inputSha256))) return undefined;
  // Reuse the input guard rather than maintaining a second URL/clock policy.
  if (typeof value.provenance.sourceUrl === "string" && typeof value.provenance.observedAt === "string") {
    const check = captureDetailTables("", { sourceUrl: value.provenance.sourceUrl, observedAt: value.provenance.observedAt });
    if (check.status !== "empty" || value.error === "invalid_provenance") return undefined;
  } else if (value.provenance.sourceUrl !== null || value.provenance.observedAt !== null ||
             value.error !== "invalid_provenance") return undefined;
  const omissions = value.omissions;
  if (!object(omissions) || !keys(omissions, ["tables", "rows", "cells", "characters", "unknown"]) ||
      !["tables", "rows", "cells"].every(k => integer(omissions[k], DETAIL_TABLE_LIMITS.inputBytes)) ||
      !integer(omissions.characters, 4 * DETAIL_TABLE_LIMITS.inputBytes) ||
      typeof omissions.unknown !== "boolean" || !Array.isArray(value.tables) || value.tables.length > DETAIL_TABLE_LIMITS.tables ||
      (value.status === "observed" ? value.tables.length === 0 : value.tables.length !== 0) ||
      (value.status === "failed" && !omissions.unknown)) return undefined;
  let chars = 0;
  let totalRows = 0;
  let totalCells = 0;
  const str = (v: unknown, limit: number, nullable = false) => {
    if (nullable && v === null) return true;
    if (typeof v !== "string" || v.length > limit) return false;
    chars += v.length; return chars <= DETAIL_TABLE_LIMITS.totalChars;
  };
  const visibility = (v: unknown) => object(v) && keys(v, ["hidden", "ariaHidden", "style"]) &&
    typeof v.hidden === "boolean" && str(v.ariaHidden, DETAIL_TABLE_LIMITS.contextChars, true) && str(v.style, DETAIL_TABLE_LIMITS.contextChars, true);
  for (let i = 0; i < value.tables.length; i++) {
    const t = value.tables[i];
    if (!object(t) || !keys(t, ["ordinal", "parentTableOrdinal", "caption", "rawCaption", "visibility", "rows"]) || t.ordinal !== i ||
        !(t.parentTableOrdinal === null || (integer(t.parentTableOrdinal, i - 1))) ||
        !str(t.caption, DETAIL_TABLE_LIMITS.contextChars, true) || !str(t.rawCaption, DETAIL_TABLE_LIMITS.contextChars, true) || !visibility(t.visibility) ||
        !Array.isArray(t.rows) || t.rows.length > DETAIL_TABLE_LIMITS.rowsPerTable) return undefined;
    totalRows += t.rows.length;
    if (totalRows > DETAIL_TABLE_LIMITS.totalRows) return undefined;
    for (const row of t.rows) {
      if (!object(row) || !keys(row, ["cells", "visibility"]) || !visibility(row.visibility) ||
          !Array.isArray(row.cells) || row.cells.length > DETAIL_TABLE_LIMITS.cellsPerRow) return undefined;
      totalCells += row.cells.length;
      if (totalCells > DETAIL_TABLE_LIMITS.totalCells) return undefined;
      for (const cell of row.cells) {
        if (!object(cell) || !keys(cell, ["tag", "text", "rawText", "rowspan", "colspan", "visibility"]) ||
            !["th", "td"].includes(cell.tag as string) || !str(cell.text, DETAIL_TABLE_LIMITS.textChars) || !str(cell.rawText, DETAIL_TABLE_LIMITS.textChars) ||
            !str(cell.rowspan, DETAIL_TABLE_LIMITS.contextChars, true) || !str(cell.colspan, DETAIL_TABLE_LIMITS.contextChars, true) ||
            !visibility(cell.visibility)) return undefined;
      }
    }
  }
  if (Buffer.byteLength(JSON.stringify(value), "utf8") > DETAIL_TABLE_LIMITS.serializedBytes) return undefined;
  return value as DetailTableObservation;
}
