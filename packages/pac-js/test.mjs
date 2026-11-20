// Smoke test of the npm package surface: exports, the Node loader, and the packed file list.
// Output parity with the Python engine is tested in apps/pac-reader/tests/parity.mjs.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import * as pac from "policy-action-check";
import { loadBundle } from "policy-action-check/node";

const here = dirname(fileURLToPath(import.meta.url));
const sample = join(here, "../../tests/fixtures/sample-bundle");

const { space, manifest, verification } = await loadBundle(sample);
assert.equal(manifest.format, "pac-bundle");
assert.equal(verification.digests_ok, true, JSON.stringify(verification));
assert.equal(verification.exam_passed, true);
const result = pac.checkFlow("@support-bot disc=n", space, "2026-11-19");
assert.equal(result.verdict, "violates");
assert.match(pac.renderReport(result, space), /Not allowed as coded\./);
assert.equal(pac.parseCode("act=biz pur=zzz", space).errors.length, 1);

const pkg = JSON.parse(readFileSync(join(here, "package.json"), "utf8"));
for (const f of pkg.files) assert.ok(readFileSync(join(here, f)), `missing packed file ${f}`);
const exported = Object.keys(pac);
for (const name of ["Space", "check", "checkFlow", "renderReport", "nextQuestion", "lookup", "verifyManifest"]) {
  assert.ok(exported.includes(name), `missing export ${name}`);
  assert.match(readFileSync(join(here, "index.d.ts"), "utf8"), new RegExp(`\\b${name}\\b`), `no type for ${name}`);
}
console.log("pac-js package ok");
