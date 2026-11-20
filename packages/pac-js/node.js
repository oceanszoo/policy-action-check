// Node helpers for pac.js: open a bundle folder from disk. The core (pac.js) stays browser-safe.
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { Space, verifyManifest } from "./pac.js";

export const BUNDLE_FILES = ["generator.json", "judge.json", "conformance.jsonl", "records.jsonl", "BUNDLE.md"];

// Read a bundle folder. Returns { space, manifest, verification } where verification compares
// the files with the digests in bundle.json (null when there is no bundle.json).
export async function loadBundle(dir) {
  const read = (name) => (existsSync(join(dir, name)) ? readFileSync(join(dir, name), "utf8") : null);
  const files = Object.fromEntries(BUNDLE_FILES.map((n) => [n, read(n)]).filter(([, text]) => text !== null));
  const space = Space.fromTexts(files["generator.json"] ?? "null", files["judge.json"] ?? "null", files["records.jsonl"] ?? "");
  const manifestText = read("bundle.json");
  const manifest = manifestText ? JSON.parse(manifestText) : null;
  const verification = manifest ? await verifyManifest(manifest, files) : null;
  return { space, manifest, verification };
}
