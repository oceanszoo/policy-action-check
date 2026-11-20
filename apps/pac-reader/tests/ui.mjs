// Browser smoke test of the single-file build (dist/pac-reader.html), opened from disk.
// Usage: node tests/ui.mjs [--shots <dir>]
import * as pac from "../pac.js";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { citationJudge, summaryJudge, policyText, exceptionText, policySummary, exceptionSummary, definitionSummary } from "./citation-fixture.mjs";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const here = dirname(fileURLToPath(import.meta.url));
const shots = process.argv.includes("--shots") ? process.argv[process.argv.indexOf("--shots") + 1] : null;
const fail = (m) => { console.error("FAIL:", m); process.exit(1); };

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 900, height: 1000 } });
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
await page.goto(pathToFileURL(join(here, "../dist/pac-reader.html")).href);
await page.click("#load-example");
await page.waitForSelector("#pick-category .cat");
if (!(await page.textContent("#bundle-info")).includes("self-exam passed")) fail("exam badge not shown");
const guide = page.locator("#bundle-info .bundle-guide");
await guide.locator("summary").click();
const exampleDir = join(here, "../../../plugins/policy-action-check/examples/anthropic-aup");
const exampleGuide = await readFile(join(exampleDir, "BUNDLE.md"), "utf8");
if (await guide.getAttribute("open") === null || (await guide.locator("pre").textContent()) !== exampleGuide) fail("example Guide does not show the original plain text");
await guide.locator("summary").click();
if (shots) await page.screenshot({ path: join(shots, "0-categories.png"), fullPage: true });

// build cases: category -> features -> relationship
const pickCat = (name) => page.locator("#pick-category .cat", { hasText: name }).click();
const val = async (ci, key, v) => {  // open the folder in the feature directory, then choose the feature
  const leaf = `#cases .val[data-case="${ci}"][data-key="${key}"][data-value="${v}"]`;
  if (!(await page.locator(leaf).count())) {
    const folder = page.locator(`#cases .folder[data-case="${ci}"][data-open="${key}"]`);
    if (!(await folder.count())) await page.click(`#cases .folder[data-more="${ci}"]`);
    await page.click(`#cases .folder[data-case="${ci}"][data-open="${key}"]`);
  }
  await page.click(leaf);
};
const use = () => page.click("#use-here");
await pickCat("Content creation"); await pickCat("Ordinary content");
await val(0, "act", "biz");
const ordinaryCode = await page.textContent("#build-code");
const space = pac.Space.fromTexts(await readFile(join(exampleDir, "generator.json"), "utf8"),
  await readFile(join(exampleDir, "judge.json"), "utf8"));
const ordinary = pac.check(ordinaryCode, space), boundary = pac.check("act=biz pur=std fic=real", space);
if (ordinaryCode !== "act=biz pur=std" || ordinary.verdict !== "compliant" || ordinary.violated.length
    || ordinary.verdict !== boundary.verdict || ordinary.violated.join() !== boundary.violated.join()) fail("ordinary content judgment differs from boundary fixture");
await page.click("#judge-cases");
await page.waitForSelector("#result:not([hidden])");
if (!(await page.textContent("#report")).includes("Allowed as coded.")) fail("ordinary content not allowed in browser");
await page.click("#restart");
await page.waitForSelector("#pick-category .cat");
await pickCat("Security or systems work");  // has narrower categories: drill in, then use it as is
if (!(await page.textContent("#pick-category .crumbs")).includes("Security or systems work")) fail("no breadcrumb");
await use();
await val(0, "act", "biz");
await page.click('#cases .folder[data-case="0"][data-open="pur"]');
if (!(await page.locator('#cases .folder[data-case="0"][data-open="auth"]').count())) fail("auth folder not nested under purpose");
// auth stays skipped: it can't be confirmed
await page.click("#add-case");
await pickCat("Chatbot or assistant"); await pickCat("Everyday help");
await val(1, "act", "biz"); await val(1, "disc", "y");
await page.click('#cases .seg button[data-rel="same"][data-case="1"]');
await page.click("#add-case");
await pickCat("Recommendation in a high-risk area"); await pickCat("Insurance"); await pickCat("About a specific person"); await pickCat("Other or adverse decision");
await val(2, "act", "biz"); await val(2, "pur", "std");
await page.fill('#cases input[data-note="2"]', "claims team pilot");
const built = await page.textContent("#build-code");
if (built !== "act=biz pur=cyber & act=biz pur=std disc=y chat=y > act=biz pur=std hr=ins scope=ind fav=part") fail("built code: " + built);
if (shots) await page.screenshot({ path: join(shots, "1-cases.png"), fullPage: true });
await page.click("#judge-cases");
await page.waitForSelector("#result:not([hidden])");
const cases = await page.textContent("#report");
for (const want of ["Step 1: 2 actions at the same time", "Possible results once the unknown facts are known", "If auth=own (1 of 3): Allowed as coded.", "claims team pilot", "Qualified human-in-the-loop"]) {
  if (!cases.includes(want)) fail(`cases report lacks "${want}"`);
}
if (shots) await page.screenshot({ path: join(shots, "2-cases-report.png"), fullPage: true });

// a single case with a violation: the fix and the case note are shown
await page.click("#restart");
await page.waitForSelector("#pick-category .cat");
if (await page.locator('.tab[data-tab="form"]').count()) fail("old question tab still shown");
await pickCat("Security or systems work"); await pickCat("Systems without permission");
await page.fill('#cases input[data-note="0"]', "red-team request from ticket 42");
await page.click("#judge-cases");
await page.waitForSelector("#result:not([hidden])");
const report = await page.textContent("#report");
if (!report.includes("Not allowed as coded")) fail("expected a violation verdict, got: " + report.slice(0, 200));
if (!report.includes("red-team request from ticket 42")) fail("note not shown");
if (!report.includes("auth=unauth -> auth=own")) fail("fix not shown");
if (shots) await page.screenshot({ path: join(shots, "3-report.png"), fullPage: true });

if (await page.locator("#code, .tab").count()) fail("old tabs still shown");

// arrange: move a likely category and a feature to the top; the order is remembered, judging unchanged
await page.click("#restart");
await page.click("#arrange");
const firstCat = async () => (await page.locator("#pick-category .cat strong").first().textContent());
const before = await firstCat();
await page.locator('#pick-category .mv[data-id="security"][data-dir="-1"]').click();
await page.locator('#pick-category .mv[data-id="security"][data-dir="-1"]').click();
if ((await firstCat()) !== "Security or systems work") fail(`category not moved up (was ${before}, now ${await firstCat()})`);
await pickCat("Security or systems work"); await use();
await page.locator('#cases .folder[data-case="0"][data-open="pur"]').click();
await page.locator('#cases .mv[data-mv="folder"][data-id="pur"][data-dir="-1"]').click();
if ((await page.locator("#cases .tree .node:not(.root) > .folder .fname").first().textContent()) !== (await page.evaluate(() => "what the action does (observable functions; one or more, written a+b)"))) fail("folder not moved up");
await page.locator('#cases .mv[data-mv="value"][data-id="cyber"][data-dir="-1"]').click();
const vals = await page.locator('#cases .val[data-key="pur"]').evaluateAll((els) => els.map((e) => e.dataset.value));
if (vals.indexOf("cyber") !== 6) fail("value not moved up: " + vals.join(","));
await page.reload();
await page.click("#load-example");
await page.waitForSelector("#pick-category .cat");
if ((await firstCat()) !== "Security or systems work") fail("order not remembered");
await page.click("#arrange"); await page.click("#reset-order");
if ((await firstCat()) !== "Chatbot or assistant") fail("reset did not restore the order");

const accepts = await page.locator("#pick-files").getAttribute("accept");
if (!accepts?.split(",").map((item) => item.trim()).includes(".md")) fail("file picker does not accept Markdown");

// A separately loaded guide participates in the digest check but is never interpreted as HTML.
const bundleFiles = ["generator.json", "judge.json", "bundle.json"].map((name) => join(exampleDir, name));
const plainText = '<img src="missing" onerror="this.remove()">\n# Untrusted Markdown\n';
const payloads = await Promise.all(bundleFiles.map(async (path) => ({
  name: path.split("/").pop(),
  mimeType: "application/json",
  buffer: await readFile(path),
})));
await page.locator("#pick-files").setInputFiles([
  ...payloads,
  { name: "BUNDLE.md", mimeType: "text/markdown", buffer: Buffer.from(plainText) },
]);
await page.waitForSelector("#bundle-info .badge.bad");
if (!(await page.textContent("#bundle-info .badge.bad")).includes("BUNDLE.md")) fail("tampered guide digest not reported");
await guide.locator("summary").click();
if ((await guide.locator("pre").textContent()) !== plainText || await guide.locator("img").count()) fail("guide was not rendered as plain text");
await page.locator("#pick-files").setInputFiles(payloads);
await page.waitForSelector("#bundle-info .bundle-guide", { state: "detached" });

// A synthetic policy excerpt tests exact text, attribution, and literal HTML rendering.
await page.locator("#pick-files").setInputFiles([
  payloads[0],
  { name: "judge.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify(citationJudge(space.judge))) },
]);
await page.waitForSelector("#bundle-info .badge.warn", { state: "visible" });
await pickCat("Security or systems work"); await pickCat("Systems without permission");
await page.click("#judge-cases");
const quoted = page.locator("#report blockquote").filter({ hasText: policyText });
if (await quoted.locator("span").textContent() !== policyText
    || await quoted.locator("cite").textContent() !== "Example rules, test-rule"
    || await quoted.locator("b").count()) fail("violated rule did not render its exact policy text and attribution as plain text");
const exception = page.locator("#report blockquote").filter({ hasText: exceptionText });
if (!(await exception.textContent()).includes("Exception (does not apply)")) fail("exception citation not labelled");

await page.locator("#pick-files").setInputFiles([
  payloads[0],
  { name: "judge.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify(summaryJudge(space.judge))) },
]);
await pickCat("Security or systems work"); await pickCat("Systems without permission");
await page.click("#judge-cases");
for (const [label, summary] of [["", policySummary], ["Exception (does not apply): ", exceptionSummary],
  ["Definition: ", definitionSummary]]) {
  const cited = page.locator("#report .term-summary").filter({ hasText: summary });
  if (await cited.locator("span").textContent() !== summary
      || await cited.locator("strong").textContent() !== `${label}Summary (not a quote): `
      || await cited.locator("b").count()) fail("summary was not rendered as labelled, unquoted plain text");
}
if (await page.locator("#report blockquote").count()) fail("a paraphrase was rendered as a policy blockquote");

if (errors.length) fail("page errors: " + errors.join("; "));
await browser.close();
console.log("ui ok");
