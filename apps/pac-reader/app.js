// Policy Action Reader: the page. All judging is done by pac.js; this file only draws it.
import * as pac from "./pac.js";
import { EXAMPLE } from "./example-bundle.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const inline = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>");

const state = { space: null, name: "", result: null, cases: [], picking: true, catPath: [], arrange: false, order: { cats: {}, folders: [], values: {} } };

// ------------------------------------------------------------------ display order
// People may move likely categories, folders and features to the top. This changes only what is
// shown first, never the judgement. It is kept per bundle in this browser.

const orderKey = () => `pac-reader-order:${state.name}`;
function loadOrder() {
  try { state.order = { cats: {}, folders: [], values: {}, ...JSON.parse(localStorage.getItem(orderKey()) || "{}") }; }
  catch { state.order = { cats: {}, folders: [], values: {} }; }
}
function saveOrder() { try { localStorage.setItem(orderKey(), JSON.stringify(state.order)); } catch { /* order lasts this visit only */ } }
function ordered(items, id, saved) {
  const rank = new Map((saved || []).map((x, i) => [x, i]));
  return items.map((it, i) => [it, i]).sort((a, b) => (rank.get(id(a[0])) ?? 1e6 + a[1]) - (rank.get(id(b[0])) ?? 1e6 + b[1])).map(([it]) => it);
}
function moveIn(list, item, dir) {
  const i = list.indexOf(item), j = i + dir;
  if (i < 0 || j < 0 || j >= list.length) return list;
  const out = [...list];
  [out[i], out[j]] = [out[j], out[i]];
  return out;
}
const mover = (kind, scope, id) => state.arrange
  ? `<span class="mover"><span role="button" tabindex="0" class="mv" data-mv="${kind}" data-scope="${esc(scope)}" data-id="${esc(id)}" data-dir="-1" title="Move up">↑</span><span role="button" tabindex="0" class="mv" data-mv="${kind}" data-scope="${esc(scope)}" data-id="${esc(id)}" data-dir="1" title="Move down">↓</span></span>` : "";

// ------------------------------------------------------------------ loading

async function loadFiles(fileList) {
  const files = {};
  for (const f of fileList) {
    const base = f.name.split("/").pop();
    if (/^(generator|judge|bundle)\.json$|^(records|conformance)\.jsonl$|^BUNDLE\.md$/.test(base)) files[base] = await f.text();
  }
  const folder = fileList[0] && fileList[0].webkitRelativePath ? fileList[0].webkitRelativePath.split("/")[0] : "";
  await openBundle(files, folder);
}

async function openBundle(files, label) {
  $("load-error").hidden = true;
  try {
    if (!files["generator.json"] || !files["judge.json"]) throw new Error("The bundle needs generator.json and judge.json.");
    state.space = pac.Space.fromTexts(files["generator.json"], files["judge.json"], files["records.jsonl"] || "");
  } catch (e) {
    $("load-error").textContent = `Can't open this bundle: ${e.message}`;
    $("load-error").hidden = false;
    return;
  }
  const manifest = files["bundle.json"] ? JSON.parse(files["bundle.json"]) : null;
  const check = manifest ? await pac.verifyManifest(manifest, files) : null;
  const g = state.space.generator;
  const policy = (g.policy && (g.policy.name || g.policy.source)) || (manifest && manifest.name) || label || "policy bundle";
  state.name = policy;
  loadOrder();
  let exam = '<span class="badge warn">no self-exam</span>';
  if (check && check.exam) {
    exam = check.exam.passed ? '<span class="badge ok">self-exam passed</span>'
      : `<span class="badge bad">self-exam failed: ${esc((check.exam.failed || []).join(", "))}</span>`;
  }
  const changed = check ? check.mismatched.filter((name) => !check.crlf_only.includes(name)) : [];
  const digests = !manifest ? '<span class="badge warn">no bundle.json</span>'
    : check.digests_ok ? '<span class="badge ok">files match bundle.json</span>' : [
      changed.length && `<span class="badge bad">changed since bundle.json: ${esc(changed.join(", "))}</span>`,
      check.crlf_only.length && `<span class="badge bad">line endings changed (Windows checkout?): ${esc(check.crlf_only.join(", "))}</span>`,
    ].filter(Boolean).join(" ");
  $("bundle-info").innerHTML = `<div><strong>${esc(policy)}</strong> <span class="muted">format ${esc(g.version)}</span></div>
    <div class="muted">${state.space.dimensions.length} questions · ${state.space.rules.length} rules · ${state.space.history.length} past records</div>
    <div>${exam} ${digests}</div>
    ${files["BUNDLE.md"] !== undefined ? '<details class="bundle-guide"><summary>Guide</summary><pre></pre></details>' : ""}
    ${check && check.exam && !check.exam.passed ? '<div class="error">This bundle failed its self-exam. Results may be incomplete.</div>' : ""}`;
  const guide = $("bundle-info").querySelector(".bundle-guide pre");
  if (guide) guide.textContent = files["BUNDLE.md"];
  $("bundle-info").hidden = false;
  $("work").hidden = false;
  restart();
}

// ------------------------------------------------------------------ start over

function judgeDate() { return $("judge-date").value || null; }

function restart() {
  state.result = null;
  state.cases = []; state.picking = true; state.catPath = [];
  $("result").hidden = true;
  renderCases();
}

// ------------------------------------------------------------------ case builder
// Step 1: open the case map (a tree of categories, as deep as the bundle makes it) and pick a node.
// Step 2: open feature folders and choose features; folders nest (dimension `under`); an empty
// folder is skipped. Step 3: say how each case relates to the one before: at the same time (&) or
// after it (>). Only the code is judged.

function newCase(cat, names) {
  const sp = state.space;
  const values = {};
  const preset = pac.parseCode(cat.preset, sp).features;
  for (const [k, v] of Object.entries(preset)) values[k] = v.split("+");
  return { cat, title: names.join(" › "), name: "", values, more: false, open: new Set(cat.features.slice(0, 1)), note: "", rel: "then" };
}

function caseFeats(c) {
  const out = {};
  for (const d of state.space.dimensions) {
    const on = c.values[d.key] || [];
    if (on.length) out[d.key] = Object.keys(d.values).filter((v) => on.includes(v)).join("+");
  }
  return out;
}

// A nested folder whose condition no longer holds is hidden, so its choice is dropped too.
function prune(c) {
  for (let changed = true; changed;) {
    changed = false;
    const feats = caseFeats(c);
    for (const d of state.space.dimensions) {
      if (d.under && (c.values[d.key] || []).length && pac.evalCond(d.under, feats) !== 1) { c.values[d.key] = []; changed = true; }
    }
  }
}

function caseCode(c) {
  const feats = caseFeats(c);
  const body = Object.entries(feats).map(([k, v]) => `${k}=${v}`).join(" ");
  const name = c.name.trim().replace(/\s+/g, "-").replace(/[^A-Za-z0-9_.-]/g, "");
  return name ? `${name}: ${body}` : body;
}

function flowCode() {
  return state.cases.map((c, i) => (i === 0 ? "" : c.rel === "same" ? " & " : " > ") + caseCode(c)).join("");
}

function categoryPicker() {
  const tree = pac.categories(state.space);
  const trail = [];
  let level = tree;
  for (const id of state.catPath) { const n = level.find((x) => x.id === id); if (!n) break; trail.push(n); level = n.children; }
  const scope = trail.length ? trail[trail.length - 1].path : "";
  level = ordered(level, (x) => x.id, state.order.cats[scope]);
  const here = trail[trail.length - 1];
  const n = state.cases.length;
  const crumbs = [`<button class="crumb" data-depth="0" type="button">All categories</button>`]
    .concat(trail.map((t, d) => `<button class="crumb" data-depth="${d + 1}" type="button">${esc(t.name)}</button>`)).join(" › ");
  $("pick-category").innerHTML = `<p class="step-label">Step 1 · ${n ? `Case ${n + 1}: ` : ""}choose a category</p>
    <div class="crumbs">${crumbs}</div>
    ${here ? `<div class="row" style="margin:0 0 10px"><button id="use-here" class="btn primary" type="button">Use “${esc(here.name)}”</button>
      <span class="muted" style="align-self:center">or open a narrower category below</span></div>` : ""}
    <div class="cats">${level.map((c, i) => `<div class="cat-wrap"><button class="cat" data-cat="${i}" type="button"><strong>${esc(c.name)}</strong>${c.description ? `<span>${esc(c.description)}</span>` : ""}${c.children.length ? `<em class="sub">▸ ${c.children.length} narrower</em>` : ""}</button>${mover("cat", scope, c.id)}</div>`).join("")}</div>
    ${n ? '<div class="row"><button id="cancel-pick" class="btn ghost small" type="button">Cancel</button></div>' : ""}`;
  const choose = (node, path) => {
    state.cases.push(newCase(node, path.map((t) => t.name)));
    state.picking = false; state.catPath = [];
    renderCases();
  };
  for (const b of $("pick-category").querySelectorAll(".cat")) {
    b.addEventListener("click", () => {
      const i = Number(b.dataset.cat);
      if (level[i].children.length) { state.catPath.push(level[i].id); categoryPicker(); } else choose(level[i], [...trail, level[i]]);
    });
  }
  for (const b of $("pick-category").querySelectorAll(".crumb")) {
    b.addEventListener("click", () => { state.catPath = state.catPath.slice(0, Number(b.dataset.depth)); categoryPicker(); });
  }
  if ($("use-here")) $("use-here").addEventListener("click", () => choose(here, trail));
  if ($("cancel-pick")) $("cancel-pick").addEventListener("click", () => { state.picking = false; state.catPath = []; renderCases(); });
  for (const m of $("pick-category").querySelectorAll(".mv")) {
    m.addEventListener("click", () => {
      state.order.cats[scope] = moveIn(level.map((x) => x.id), m.dataset.id, Number(m.dataset.dir));
      saveOrder(); categoryPicker();
    });
  }
}

// Step 2 is a directory: the category is the root folder, each feature group (dimension) is a
// folder, and its features (values) are the entries you choose. Folders for facts that only make
// sense after a choice sit inside that folder once the choice is made.
function folderHtml(c, ci, key, feats, kids, sibs) {
  const d = state.space.dims[key];
  const on = c.values[key] || [];
  const open = c.open.has(key);
  const chosen = on.length ? on.map((v) => d.values[v] ?? v).join(", ")
    : (d.default != null ? `not chosen: ${d.values[d.default] ?? d.default} is assumed` : "not chosen");
  const inner = byFolderOrder((kids[key] || []).filter((k) => pac.evalCond(state.space.dims[k].under, feats) === 1));
  const vals = ordered(Object.entries(d.values), ([v]) => v, state.order.values[key]);
  const leaves = open ? `<div class="leaves">${vals.map(([v, m]) => `<span class="leaf"><button class="val${on.includes(v) ? " on" : ""}" role="${d.multi ? "checkbox" : "radio"}" aria-checked="${on.includes(v)}" data-case="${ci}" data-key="${esc(key)}" data-value="${esc(v)}" type="button" title="${esc(`${key}=${v}`)}">${esc(m)}</button>${mover("value", `${key}|${vals.map(([x]) => x).join(",")}`, v)}</span>`).join("")}</div>`
    + (inner.length ? `<ul>${inner.map((k) => folderHtml(c, ci, k, feats, kids, inner)).join("")}</ul>` : "") : "";
  const more = !open && inner.length ? ` · ${inner.length} inside` : "";
  return `<li class="node${open ? " open" : ""}"><button class="folder" data-case="${ci}" data-open="${esc(key)}" type="button" aria-expanded="${open}" title="${esc(d.question || "")}">
      <span class="twisty">${open ? "▾" : "▸"}</span><span class="fname">${esc(d.name || key)}</span><span class="fval${on.length ? " set" : ""}">${esc(chosen + more)}</span></button>${mover("folder", sibs.join(","), key)}${leaves}</li>`;
}

const byFolderOrder = (keys) => ordered(keys, (k) => k, state.order.folders);

function treeHtml(c, ci) {
  const sp = state.space;
  const feats = caseFeats(c);
  const kids = pac.nestedUnder(sp);
  const rootOf = (k) => { const seen = new Set(); while (sp.dims[k].under && !seen.has(k)) { seen.add(k); k = sp.dims[k].under.dim; } return k; };
  const nestedIn = (k, set) => { let x = k; const seen = new Set(); while (sp.dims[x].under && !seen.has(x)) { seen.add(x); x = sp.dims[x].under.dim; if (set.includes(x)) return true; } return false; };
  const shown = byFolderOrder(c.cat.features.filter((k) => !nestedIn(k, c.cat.features)));
  const offered = pac.dimensionOrder(sp).filter((k) => c.cat.features.includes(k) || nestedIn(k, c.cat.features));
  const others = pac.dimensionOrder(sp).filter((k) => !offered.includes(k));
  const otherTop = byFolderOrder(others.filter((k) => !nestedIn(k, others) && !offered.includes(rootOf(k))));
  const otherSet = others.filter((k) => (c.values[k] || []).length).length;
  return `<ul class="tree"><li class="node open root"><div class="folder rootname"><span class="twisty">▾</span><span class="fname">${esc(c.title)}</span></div>
    <ul>${shown.map((k) => folderHtml(c, ci, k, feats, kids, shown)).join("")}
    ${otherTop.length ? `<li class="node${c.more ? " open" : ""}"><button class="folder" data-case="${ci}" data-more="${ci}" type="button" aria-expanded="${!!c.more}">
        <span class="twisty">${c.more ? "▾" : "▸"}</span><span class="fname">Other features</span><span class="fval${otherSet ? " set" : ""}">${otherTop.length} groups${otherSet ? `, ${otherSet} chosen` : ""}</span></button>
        ${c.more ? `<ul>${otherTop.map((k) => folderHtml(c, ci, k, feats, kids, otherTop)).join("")}</ul>` : ""}</li>` : ""}
    </ul></li></ul>`;
}

function renderCases() {
  if (!state.space) return;
  const sp = state.space;
  $("cases").innerHTML = state.cases.map((c, ci) => {
    const rel = ci === 0 ? "" : `<div class="relation"><span class="step-label" style="margin:0">Step 3 · relationship</span> Case ${ci + 1} happens
      <span class="seg"><button data-rel="then" data-case="${ci}" class="${c.rel === "then" ? "on" : ""}" type="button">after case ${ci}</button><button data-rel="same" data-case="${ci}" class="${c.rel === "same" ? "on" : ""}" type="button">at the same time as case ${ci}</button></span></div>`;
    return `${rel}<div class="case">
      <div class="case-head"><h3>Case ${ci + 1}: ${esc(c.cat.name)}</h3>
        <input data-name="${ci}" type="text" placeholder="name (optional)" value="${esc(c.name)}" aria-label="Case name">
        <span class="tools">
          ${ci > 0 ? `<button class="btn small ghost" data-move="${ci}" data-dir="-1" type="button" title="Move up">↑</button>` : ""}
          ${ci < state.cases.length - 1 ? `<button class="btn small ghost" data-move="${ci}" data-dir="1" type="button" title="Move down">↓</button>` : ""}
          <button class="btn small ghost" data-remove="${ci}" type="button">Remove</button>
        </span></div>
      <p class="step-label">Step 2 · open a folder and choose the features this case has (skip any you can't confirm)</p>
      ${treeHtml(c, ci)}
      <label class="field">Note for this case (shown in the report, never judged)<input data-note="${ci}" type="text" value="${esc(c.note)}"></label>
    </div>`;
  }).join("");
  $("pick-category").hidden = !state.picking;
  if (state.picking) categoryPicker();
  $("build-bar").hidden = !state.cases.length || state.picking;
  $("build-code").textContent = flowCode() || "(nothing set yet)";
  $("build-error").hidden = true;
}

function onMove(m) {
  const dir = Number(m.dataset.dir), id = m.dataset.id;
  if (m.dataset.mv === "value") {
    const [key, list] = m.dataset.scope.split("|");
    state.order.values[key] = moveIn(list.split(","), id, dir);
  } else if (m.dataset.mv === "folder") {
    const sibs = m.dataset.scope.split(",");
    const moved = moveIn(sibs, id, dir);
    const all = byFolderOrder(pac.dimensionOrder(state.space));
    const slots = all.map((k, i) => (sibs.includes(k) ? i : -1)).filter((i) => i >= 0);
    slots.forEach((slot, n) => { all[slot] = moved[n]; });
    state.order.folders = all;
  }
  saveOrder();
  renderCases();
}

function onCasesClick(e) {
  const mv = e.target.closest(".mv");
  if (mv) return onMove(mv);
  const t = e.target.closest("button");
  if (!t) return;
  const ci = Number(t.dataset.case ?? t.dataset.move ?? t.dataset.remove ?? t.dataset.more);
  if (t.dataset.value !== undefined) {
    const c = state.cases[ci], key = t.dataset.key, v = t.dataset.value;
    const on = c.values[key] || [];
    if (state.space.multi(key)) c.values[key] = on.includes(v) ? on.filter((x) => x !== v) : [...on, v];
    else c.values[key] = on.includes(v) ? [] : [v];
    prune(c);
    if (c.values[key].length) for (const k of pac.nestedUnder(state.space)[key] || []) c.open.add(k);
  } else if (t.dataset.open !== undefined) {
    const o = state.cases[ci].open;
    if (o.has(t.dataset.open)) o.delete(t.dataset.open); else o.add(t.dataset.open);
  } else if (t.dataset.more !== undefined) {
    state.cases[ci].more = !state.cases[ci].more;
  } else if (t.dataset.rel) {
    state.cases[ci].rel = t.dataset.rel;
  } else if (t.dataset.move !== undefined) {
    const j = ci + Number(t.dataset.dir);
    [state.cases[ci], state.cases[j]] = [state.cases[j], state.cases[ci]];
  } else if (t.dataset.remove !== undefined) {
    state.cases.splice(ci, 1);
    if (!state.cases.length) state.picking = true;
  } else return;
  renderCases();
}

function judgeCases() {
  const empty = state.cases.findIndex((c) => !Object.values(c.values).some((v) => v.length));
  if (empty >= 0) {
    $("build-error").textContent = `Case ${empty + 1} has no facts yet. Pick at least one, or remove the case.`;
    $("build-error").hidden = false;
    return;
  }
  const notes = state.cases.map((c, i) => (c.note.trim() ? `Case ${i + 1}: ${c.note.trim()}` : "")).filter(Boolean).join("; ");
  showResult(pac.checkFlow(flowCode(), state.space, judgeDate(), notes ? { _action: notes } : null));
}

// ------------------------------------------------------------------ result

function actionHtml(a) {
  return `<li><span class="kind">${esc(a.kind)}</span> ${esc(a.action)}${a.code_change ? ` <code>${esc(a.code_change)}</code>` : ""}${a.terms?.length ? ` (${esc(a.terms.join(", "))})` : ""}</li>`;
}

function citationHtml(q) {
  const label = pac.termRoleLabel(q);
  const attribution = [q.section, q.id].filter(Boolean).join(", ");
  const { text, summary } = pac.termContent(q);
  if (summary !== undefined) {
    const summaryLabel = label ? `${label}: Summary (not a quote)` : "Summary (not a quote)";
    return `<div class="term-summary"><strong>${esc(summaryLabel)}: </strong><span>${esc(summary)}</span><cite>${esc(attribution)}</cite></div>`;
  }
  return `<blockquote>${label ? `<strong>${esc(label)}: </strong>` : ""}<span>${esc(text)}</span><cite>${esc(attribution)}</cite></blockquote>`;
}

function oneResultHtml(r, seenDefinitions = new Set()) {
  const sp = state.space;
  const vt = { ...pac.DEFAULT_TEXTS.verdict, ...((sp.judge.texts || {}).verdict || {}) };
  const h = { ...pac.DEFAULT_TEXTS.headings, ...((sp.judge.texts || {}).headings || {}) };
  return `<div class="verdict ${esc(r.verdict)}">${esc(vt[r.verdict] ?? r.verdict)}</div>
    <h3>${esc(h.next)}</h3><ol>${r.next_actions.map(actionHtml).join("")}</ol>
    <h3>${esc(h.code)}</h3><div class="action-code">${esc(r.code || "(nothing set)")}</div>
    <p class="muted">${esc(r.reading)}</p>
    ${pac.reportSections(r, sp, seenDefinitions).map((s) => `<h3>${esc(s.heading)}</h3><ul>${s.items.map((x, i) => `<li>${inline(x)}${(s.citations?.[i] || []).map(citationHtml).join("")}</li>`).join("")}</ul>`).join("")}`;
}

function showResult(result) {
  state.result = result;
  const sp = state.space;
  const disclaimer = (sp.judge.texts || {}).disclaimer ?? pac.DEFAULT_TEXTS.disclaimer;
  let html;
  if (result.flow) {
    const vt = { ...pac.DEFAULT_TEXTS.verdict, ...((sp.judge.texts || {}).verdict || {}) };
    const stages = result.stages || result.flow.map((_, i) => [i]);
    const together = stages.some((st) => st.length > 1);
    const seenDefinitions = new Set();
    html = `<div class="verdict ${esc(result.verdict)}">Flow of ${result.flow.length} actions: ${esc(vt[result.verdict] ?? result.verdict)}</div>`
      + stages.map((stage, n) => (together ? `<p class="stage-head">Step ${n + 1}${stage.length > 1 ? `: ${stage.length} actions at the same time` : ""}</p>` : "")
        + stage.map((i) => { const s = result.flow[i]; return `<div class="flow-step"><h3>Action ${i + 1}${s.step ? `: ${esc(s.step)}` : ""}</h3>${oneResultHtml(s, seenDefinitions)}</div>`; }).join("")).join("");
  } else {
    html = oneResultHtml(result);
  }
  $("report").innerHTML = `<div class="report">${html}<p class="muted"><em>${esc(disclaimer)}</em></p></div>`;
  $("result").hidden = false;
  $("result").scrollIntoView({ behavior: "smooth", block: "start" });
}

function download(name, text, type) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// ------------------------------------------------------------------ wiring

$("pick-folder").addEventListener("change", (e) => loadFiles([...e.target.files]));
$("pick-files").addEventListener("change", (e) => loadFiles([...e.target.files]));
$("load-example").addEventListener("click", () => openBundle(EXAMPLE, "example"));
const drop = $("drop");
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); loadFiles([...e.dataTransfer.files]); });
$("judge-date").value = pac.today();
$("cases").addEventListener("click", onCasesClick);
$("arrange").addEventListener("click", () => {
  state.arrange = !state.arrange;
  $("arrange").setAttribute("aria-pressed", String(state.arrange));
  $("arrange").textContent = state.arrange ? "Done arranging" : "Arrange order";
  $("reset-order").hidden = !state.arrange;
  $("work").classList.toggle("arranging", state.arrange);
  renderCases();
});
document.addEventListener("keydown", (e) => { if (e.target.classList && e.target.classList.contains("mv") && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); e.target.click(); } });
$("reset-order").addEventListener("click", () => { state.order = { cats: {}, folders: [], values: {} }; saveOrder(); renderCases(); });
$("cases").addEventListener("input", (e) => {
  const t = e.target;
  if (t.dataset.name !== undefined) { state.cases[Number(t.dataset.name)].name = t.value; $("build-code").textContent = flowCode() || "(nothing set yet)"; }
  if (t.dataset.note !== undefined) state.cases[Number(t.dataset.note)].note = t.value;
});
$("add-case").addEventListener("click", () => { state.picking = true; state.catPath = []; renderCases(); $("pick-category").scrollIntoView({ behavior: "smooth", block: "start" }); });
$("judge-cases").addEventListener("click", judgeCases);
$("restart").addEventListener("click", () => { restart(); window.scrollTo({ top: 0, behavior: "smooth" }); });
$("copy-code").addEventListener("click", () => {
  const r = state.result;
  const code = r.flow ? (r.stages || r.flow.map((_, i) => [i])).map((st) => st.map((i) => r.flow[i].code).join(" & ")).join(" > ") : r.code;
  navigator.clipboard && navigator.clipboard.writeText(code);
});
$("dl-md").addEventListener("click", () => download("policy-check.md", pac.renderReport(state.result, state.space), "text/markdown"));
$("dl-json").addEventListener("click", () => download("policy-check.json", JSON.stringify(state.result, null, 2), "application/json"));
