import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { writeCollectorArtifact } from "../../../lib/source-evidence/artifact-output.js";

test("private observation output is exclusive and owner-readable only", (t) => {
  const directory = mkdtempSync(join(tmpdir(), "cre-evidence-output-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const path = join(directory, "artifact.json");
  const json = JSON.stringify({ sources: [{ retainedObservations: [{ raw: [false, null, []] }] }], listings: [] });
  writeCollectorArtifact(path, json, true);
  assert.equal(readFileSync(path, "utf8"), json);
  assert.equal(statSync(path).mode & 0o777, 0o600);
  assert.throws(() => writeCollectorArtifact(path, "replacement", true));
  assert.equal(readFileSync(path, "utf8"), json);
});

test("retained evidence cannot fall through to stdout", () => {
  assert.throws(() => writeCollectorArtifact(undefined, "private evidence", true), /private --out/);
});

test("ordinary listing output retains its existing overwrite behavior", (t) => {
  const directory = mkdtempSync(join(tmpdir(), "cre-listing-output-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const path = join(directory, "artifact.json");
  writeFileSync(path, "prior");
  writeCollectorArtifact(path, "current", false);
  assert.equal(readFileSync(path, "utf8"), "current");
});
