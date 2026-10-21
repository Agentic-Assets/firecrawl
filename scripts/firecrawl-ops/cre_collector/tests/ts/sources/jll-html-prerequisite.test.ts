import assert from "node:assert/strict";
import test from "node:test";
import {
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";

test("experimental module imports without native output and conversion explicitly refuses it", () => {
  const root = mkdtempSync(join(tmpdir(), "jll-unbuilt-"));
  const collector = join(root, "scripts/firecrawl-ops/cre_collector");
  const source = fileURLToPath(
    new URL("../../../sources/jll-html-doc.ts", import.meta.url),
  );
  const target = join(collector, "sources/jll-html-doc.ts");
  try {
    mkdirSync(join(collector, "sources"), { recursive: true });
    copyFileSync(source, target);
    assert.deepEqual(readFileSync(source), readFileSync(target));
    symlinkSync(
      fileURLToPath(new URL("../../../node_modules", import.meta.url)),
      join(collector, "node_modules"),
      "junction",
    );
    const child = spawnSync(
      process.execPath,
      [
        "--import",
        import.meta.resolve("tsx"),
        "--input-type=module",
        "-e",
        `
      import assert from 'node:assert/strict';
      const {jllDirectHtmlDoc} = await import(${JSON.stringify(pathToFileURL(target).href)});
      await assert.rejects(jllDirectHtmlDoc('<h1>Example</h1>', {url:'https://property.jll.com/listings/example',statusCode:200,contentType:'text/html'}), /requires a built apps\\/api\\/native package/);
    `,
      ],
      { cwd: collector, encoding: "utf8", timeout: 15000 },
    );
    assert.equal(child.status, 0, child.stderr);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
