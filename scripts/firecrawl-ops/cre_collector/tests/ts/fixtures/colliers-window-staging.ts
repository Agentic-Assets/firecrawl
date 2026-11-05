/** Synthetic cross-language staging fixture, never contacts the source. */
import { createHash } from "node:crypto";
import { chmodSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { buildColliersWindowArtifact } from "../../../sources/colliers-partial-window-worker.js";
const dir = process.env.COLLIERS_FIXTURE_DIRECTORY;
if (!dir) throw new Error("fixture directory missing");
chmodSync(dir, 0o700);
const binding = { generation: "gen", attempt: 1, startedAt: "2026-11-04T15:00:00Z", finishedAt: "2026-11-04T16:00:00Z" };
const common = { transaction: "sale", pass: 1, page: 1, start: 1 };
const list = { success: true, total: 2, totalAvail: 2, numProjects: 1,
  html: '<li class="item"><span class="headline">Fixture</span><a href="/handler/modern.aspx?pv=native-e2e">View</a></li>' };
const map = { success: true, total: 2, totalAvail: 2, projectLocations: [{ ProjectId: "native-e2e" }] };
const rows = [
  { schema: "colliers-partial-pages/v1", sourceKey: "colliers", generation: "gen", attempt: 1 },
  { ...common, pageSize: 1, observedAt: "2026-11-04T15:30:00.000Z", kind: "unqualified_response_pair",
    documents: [{ list: JSON.stringify(list), map: JSON.stringify(map) }] },
  { ...common, observedAt: "2026-11-04T15:30:01.000Z", kind: "parsed_page",
    documents: [{ mapProjectId: "native-e2e", detailPv: "native-e2e", detailUrl: "https://my.rcm1.com/handler/modern.aspx?pv=native-e2e" }] },
  { ...common, start: 2, pageSize: 1, observedAt: "2026-11-04T15:30:02.000Z", kind: "recovery_hole",
    reason: "provider_failure", documents: [] },
];
const bytes = Buffer.from(rows.map(row => JSON.stringify(row)).join("\n") + "\n");
const source = join(dir, "source"), output = join(dir, "evidence");
writeFileSync(source, bytes, { mode: 0o600 });
const digest = createHash("sha256").update(bytes).digest("hex");
const native = Buffer.concat([Buffer.from([0xef,0xbb,0xbf]), Buffer.from(JSON.stringify({ ProjectSummary: { ProjectId: "native-e2e", Address: { City: "Native City", State: "TX" } },
  ProjectDetails: { ProjectFields: [{ Name: "Units", Value: "10" }] } }))]);
const decodedText = new TextDecoder("utf-8", { fatal: true }).decode(native);
const artifactPath = join(dir, "artifact");
const result = await buildColliersWindowArtifact({ ...binding, sourcePath: source, sourceBytes: bytes.length,
  sourceSha256: digest, evidencePath: output, artifactPath, cursor: 1,
  producerSha: "a".repeat(40), targetSha256: "b".repeat(64), originalSourceMainSha256: "c".repeat(64) },
  async () => ({ detail: JSON.parse(decodedText), decodedText, nativeBytes: native }));
const artifactBytes = readFileSync(artifactPath);
if (artifactBytes.length !== result.artifact.bytes ||
    createHash("sha256").update(artifactBytes).digest("hex") !== result.artifact.sha256) throw new Error("fixture artifact differs");
const artifact = JSON.parse(artifactBytes.toString("utf8"));
process.stdout.write(JSON.stringify({ rows: artifact.listings, artifactFinishedAt: result.detailFinishedAt }) + "\n");
