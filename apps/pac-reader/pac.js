// pac.js: a no-model reader for policy-action-check bundles (SPEC.md, format 1.x).
//
// Loads generator.json + judge.json (+ optional records.jsonl) and does everything a person needs
// at use time: parse a code, judge it, find the smallest fix, run the question form, look up
// typed words, and render the fixed report. Plain JavaScript, no dependencies, no network.
// Works in the browser and in Node (ES module). The reference implementation is
// plugins/policy-action-check/scripts/pac.py; tests/parity.mjs checks that both agree.

export const SUPPORTED_MAJOR = "1";
const T = 1, U = 0.5, F = 0;

export const DEFAULT_SUBJECTIVE = [
  "risk", "risky", "lowrisk", "low_risk", "highrisk", "intent", "intention", "ok", "okay", "fine",
  "good", "bad", "safe_enough", "compliant", "lawful", "allowed", "harmless", "minor", "trusted",
  "trust", "believe", "think", "probably", "likely", "reasonable", "appropriate", "acceptable",
  "benign", "innocent", "harmful"];

export const DEFAULT_TEXTS = {
  verdict: {
    violates: "Not allowed as coded.",
    compliant_if_requirements_met: "Allowed only if you meet every requirement under 'Must do'.",
    needs_info: "Can't be decided yet: some facts are unknown.",
    compliant: "Allowed as coded.",
    invalid_code: "The code has errors. Fix it before it can be judged.",
  },
  headings: {
    next: "What to do next", verdict: "Verdict", code: "Action", violated: "Violates",
    must_do: "Must do", unknown: "Still unknown", assumed: "Assumed (confirm if it matters)",
    fix: "Smallest fix", usual_next: "Usually next", similar: "Similar past records",
    scope: "Scope warnings", errors: "Errors", possible: "Possible results once the unknown facts are known", notes: "Notes (shown only, not used in judging)",
  },
  disclaimer: "A structured self-check, not legal advice.",
};

export const VERDICT_ORDER = ["invalid_code", "violates", "compliant_if_requirements_met", "needs_info", "compliant"];

// ------------------------------------------------------------------ small helpers

const has = (o, k) => o != null && Object.prototype.hasOwnProperty.call(o, k);
const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
const sortStr = (xs) => [...xs].sort(cmp);
const keysOf = (o) => Object.keys(o || {});

function roundHalfEven(num, den, digits) {
  // round(num/den, digits) with ties to even, on the exact ratio
  const scale = 10 ** digits;
  const q = Math.floor((num * scale) / den);
  const rem = num * scale - q * den;
  let r = q;
  if (2 * rem > den) r = q + 1;
  else if (2 * rem === den) r = q % 2 === 0 ? q : q + 1;
  return r / scale;
}

// difflib.SequenceMatcher.ratio and get_close_matches (n=1, cutoff=0.6), as in Python
function longestMatch(a, b, alo, ahi, blo, bhi) {
  let besti = alo, bestj = blo, bestsize = 0;
  let j2len = new Map();
  for (let i = alo; i < ahi; i++) {
    const next = new Map();
    for (let j = blo; j < bhi; j++) {
      if (a[i] !== b[j]) continue;
      const k = (j2len.get(j - 1) || 0) + 1;
      next.set(j, k);
      if (k > bestsize) { besti = i - k + 1; bestj = j - k + 1; bestsize = k; }
    }
    j2len = next;
  }
  return [besti, bestj, bestsize];
}

function matchingSize(a, b) {
  const queue = [[0, a.length, 0, b.length]];
  let total = 0;
  while (queue.length) {
    const [alo, ahi, blo, bhi] = queue.pop();
    const [i, j, k] = longestMatch(a, b, alo, ahi, blo, bhi);
    if (k) {
      total += k;
      if (alo < i && blo < j) queue.push([alo, i, blo, j]);
      if (i + k < ahi && j + k < bhi) queue.push([i + k, ahi, j + k, bhi]);
    }
  }
  return total;
}

export function ratio(a, b) {
  const t = a.length + b.length;
  return t ? (2 * matchingSize(a, b)) / t : 1;
}

export function closeMatch(word, possibilities, cutoff = 0.6) {
  let best = null;
  for (const x of possibilities) {
    const s = ratio(x, word);   // difflib: seq1 = candidate, seq2 = word
    if (s >= cutoff && (best === null || s > best[0] || (s === best[0] && x > best[1]))) best = [s, x];
  }
  return best ? [best[1]] : [];
}

// ------------------------------------------------------------------ bundle

export class BundleError extends Error {}

export class Space {
  constructor(generator, judge, records = []) {
    for (const [doc, fmt] of [[generator, "pac-generator"], [judge, "pac-judge"]]) {
      if (!doc || doc.format !== fmt || String(doc.version || "").split(".")[0] !== SUPPORTED_MAJOR) {
        throw new BundleError(`expected ${fmt} version ${SUPPORTED_MAJOR}.x, got ${doc && doc.format} ${doc && doc.version}`);
      }
    }
    this.generator = generator;
    this.judge = judge;
    this.dimensions = generator.dimensions;
    this.dims = Object.fromEntries(generator.dimensions.map((d) => [d.key, d]));
    this.rules = judge.rules;
    this.ruleById = Object.fromEntries(judge.rules.map((r) => [r.id, r]));
    this.codes = generator.saved_codes || {};
    this.scope = generator.scope || {};
    this.transitions = generator.transitions || {};
    const fix = judge.fix || {};
    this.fillCost = Number(fix.fill_cost ?? 0.5);
    this.changeCost = Number(fix.change_cost ?? 1.0);
    this.maxChanges = Number.parseInt(fix.max_changes ?? 3, 10);
    const words = generator.subjective_terms && generator.subjective_terms.length ? generator.subjective_terms : DEFAULT_SUBJECTIVE;
    this.subjective = new RegExp("^(" + words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")$", "i");
    this.history = records.map((r) => ({ ...r }));
    for (const rec of this.history) {
      if (!rec.features && rec.code) rec.features = parseCode(rec.code, this).features;
    }
  }

  static fromTexts(generatorText, judgeText, recordsText = "") {
    const records = recordsText.split(/\r?\n/).filter((l) => l.trim()).map((l) => JSON.parse(l));
    return new Space(JSON.parse(generatorText), JSON.parse(judgeText), records);
  }

  constants() { return { ...(this.generator.constants || {}) }; }

  defaults() {
    const out = {};
    for (const d of this.dimensions) if (d.default !== undefined && d.default !== null) out[d.key] = d.default;
    return out;
  }

  multi(key) { return Boolean(this.dims[key] && this.dims[key].multi); }
}

// ------------------------------------------------------------------ codes

const err = (token, message, suggestions) => ({ token, message, did_you_mean: suggestions.length ? suggestions[0] : null });

export function parseCode(text, space, depth = 0) {
  text = text.trim();
  let name = null;
  let m = text.match(/^([A-Za-z0-9_.-]+):\s+([\s\S]*)$/) || text.match(/^([A-Za-z0-9_.-]+):$/);
  if (m) { name = m[1]; text = m[2] !== undefined ? m[2] : ""; }
  const features = {}, sources = {}, errors = [];
  const tokens = text.split(/\s+/).filter(Boolean);
  let date = null;
  for (const tok of tokens) {
    if (tok.startsWith("@")) {
      const macro = tok.slice(1);
      const body = has(space.codes, macro) ? space.codes[macro] : undefined;
      if (body === undefined) { errors.push(err(tok, "unknown saved code", closeMatch(macro, keysOf(space.codes)))); continue; }
      if (depth > 5) { errors.push(err(tok, "saved codes nest too deeply", [])); continue; }
      const sub = parseCode(body, space, depth + 1);
      Object.assign(features, sub.features);
      for (const [k, v] of Object.entries(sub.sources)) sources[k] = v === "code" ? tok : v;
      errors.push(...sub.errors);
      continue;
    }
    if (!tok.includes("=")) { errors.push(err(tok, "expected key=value", [])); continue; }
    const eq = tok.indexOf("=");
    const key = tok.slice(0, eq), value = tok.slice(eq + 1);
    if (key === "date") {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) errors.push(err(tok, "date must be YYYY-MM-DD", []));
      else date = value;
      continue;
    }
    if (space.subjective.test(key) || (!(has(space.dims, key)) && space.subjective.test(value))) {
      errors.push({ token: tok, subjective: true, did_you_mean: null, message:
        "subjective input is not accepted: state the objective condition instead " +
        "(what was done, what exists, who agreed), using the generator's keys" });
      continue;
    }
    if (!(has(space.dims, key))) {
      const guess = closeMatch(key, keysOf(space.dims));
      errors.push(err(tok, `unknown key '${key}'`, guess.map((g) => `${g}=${value}`)));
      continue;
    }
    if (value === "?") { delete features[key]; delete sources[key]; continue; }
    const values = space.dims[key].values;
    const vkeys = keysOf(values);
    if (value.includes("+")) {
      const parts = value.split("+").filter(Boolean);
      if (!space.multi(key)) {
        errors.push(err(tok, `${key} takes one value (it is not multi-valued)`, parts.length ? [`${key}=${parts[0]}`] : []));
        continue;
      }
      const bad = parts.filter((x) => !(has(values, x)));
      if (bad.length || !parts.length) {
        const guess = bad.length ? closeMatch(bad[0], vkeys) : [];
        errors.push(err(tok, `invalid value for ${key}: ${bad.join("+") || value}`, guess.map((g) => `${key}=${g}`)));
        continue;
      }
      features[key] = vkeys.filter((v) => parts.includes(v)).join("+");
      sources[key] = "code";
      continue;
    }
    if (vkeys.length && !(has(values, value)) && space.subjective.test(value)) {
      errors.push({ token: tok, subjective: true, did_you_mean: null, message:
        `'${value}' is a judgement, not a fact. Set ${key} to one of ${pyList(sortStr(vkeys))}; ` +
        `evidence: ${space.dims[key].evidence || "see generator"}` });
      continue;
    }
    if (vkeys.length && !(has(values, value))) {
      let guess = closeMatch(value, vkeys);
      if (!guess.length) {
        for (const [code, meaning] of Object.entries(values)) {
          if (String(meaning).toLowerCase().includes(value.toLowerCase())) { guess = [code]; break; }
        }
      }
      errors.push(err(tok, `invalid value for ${key}`, guess.map((g) => `${key}=${g}`)));
      continue;
    }
    features[key] = value;
    sources[key] = "code";
  }
  return { name, features, date, errors, tokens, sources };
}

const pyList = (xs) => "[" + xs.map((x) => `'${x}'`).join(", ") + "]";

export function formatCode(features, space, name = null) {
  const body = space.dimensions.filter((d) => has(features, d.key)).map((d) => `${d.key}=${features[d.key]}`).join(" ");
  return name ? `${name}: ${body}` : body;
}

export function explainCode(features, space) {
  const parts = [];
  for (const d of space.dimensions) {
    if (!has(features, d.key)) continue;
    const vals = features[d.key].split("+");
    parts.push(`${d.name || d.key}: ` + vals.map((v) => String(d.values[v] ?? v)).join(" + "));
  }
  return parts.length ? parts.join("; ") : "(nothing set)";
}

// ------------------------------------------------------------------ rule evaluation

export function evalCond(cond, feats) {
  if ("all" in cond) return cond.all.reduce((m, c) => Math.min(m, evalCond(c, feats)), T);
  if ("any" in cond) return cond.any.reduce((m, c) => Math.max(m, evalCond(c, feats)), F);
  if ("not" in cond) return 1 - evalCond(cond.not, feats);
  const value = feats[cond.dim];
  if (value === undefined || value === null) return U;
  const members = value.split("+");
  if ("in" in cond) return members.some((v) => cond.in.includes(v)) ? T : F;
  if ("not_in" in cond) return members.some((v) => cond.not_in.includes(v)) ? F : T;
  throw new BundleError(`bad condition ${JSON.stringify(cond)}`);
}

export function condDims(cond) {
  if ("all" in cond || "any" in cond) {
    const out = new Set();
    for (const c of cond.all || cond.any) for (const d of condDims(c)) out.add(d);
    return out;
  }
  if ("not" in cond) return condDims(cond.not);
  return new Set([cond.dim]);
}

export function ruleDims(rule) {
  const dims = condDims(rule.when);
  for (const e of rule.exceptions || []) for (const d of condDims(e)) dims.add(d);
  if (rule.require) for (const d of condDims(rule.require)) dims.add(d);
  return dims;
}

export function ruleStatus(rule, feats) {
  const w = evalCond(rule.when, feats);
  if (rule.kind === "prohibition") {
    const ex = (rule.exceptions || []).reduce((m, e) => Math.max(m, evalCond(e, feats)), F);
    const viol = Math.min(w, 1 - ex);
    if (viol === T) return "violated";
    if (viol === F) return w === T ? "satisfied" : "not_applicable";
    return "open";
  }
  const r = evalCond(rule.require, feats);
  if (w === F) return "not_applicable";
  if (w === T) return r === T ? "satisfied" : r === F ? "violated" : "required_next";
  return r === T ? "satisfied" : "open";
}

export function today() { return new Date().toISOString().slice(0, 10); }

export function activeRules(space, date) {
  const day = date || today();
  return space.rules.filter((r) => (r.effective_from || "0000-00-00") <= day);
}

export function evaluate(feats, space, date = null) {
  const full = { ...space.defaults(), ...space.constants(), ...feats };
  const statuses = {};
  for (const r of activeRules(space, date)) statuses[r.id] = ruleStatus(r, full);
  const pick = (s) => sortStr(Object.keys(statuses).filter((k) => statuses[k] === s));
  const violated = pick("violated"), required = pick("required_next"), open = pick("open");
  const verdict = violated.length ? "violates" : required.length ? "compliant_if_requirements_met"
    : open.length ? "needs_info" : "compliant";
  return { verdict, violated, required_next: required, open, statuses, out_of_scope: outOfScope(full, space) };
}

export function outOfScope(feats, space) {
  const notes = [];
  for (const area of space.scope.areas || []) {
    if (area.match && evalCond(area.match, feats) === T && ["out_of_scope", "terms_only"].includes(area.decision)) {
      notes.push(`${area.area}: ${area.decision}`);
    }
  }
  return notes;
}

// ------------------------------------------------------------------ fixes, similar, next

function* combinations(xs, k, start = 0, acc = []) {
  if (acc.length === k) { yield acc; return; }
  for (let i = start; i < xs.length; i++) yield* combinations(xs, k, i + 1, [...acc, xs[i]]);
}

function* product(lists, i = 0, acc = []) {
  if (i === lists.length) { yield acc; return; }
  for (const x of lists[i]) yield* product(lists, i + 1, [...acc, x]);
}

export function minimalFixes(feats, space, date = null, limit = 5) {
  const maxChanges = space.maxChanges;
  const base = evaluate(feats, space, date);
  const bad = new Set([...base.violated, ...base.required_next]);
  if (!bad.size) return [0, []];
  const dimSet = new Set();
  for (const rid of bad) for (const d of ruleDims(space.ruleById[rid])) if (space.dims[d].fixable ?? true) dimSet.add(d);
  const dims = sortStr(dimSet);
  if (!dims.length) return [null, "only non-fixable dimensions (such as the purpose itself) decide this: no compliant variant"];
  let best = null, found = [];
  for (let k = 1; k <= Math.min(maxChanges, dims.length); k++) {
    for (const combo of combinations(dims, k)) {
      const choices = combo.map((d) => keysOf(space.dims[d].values).filter((v) => v !== feats[d]));
      const cost = combo.reduce((s, d) => s + (has(feats, d) ? space.changeCost : space.fillCost), 0);
      for (const values of product(choices)) {
        if (best !== null && cost > best) continue;
        const trial = { ...feats };
        combo.forEach((d, i) => { trial[d] = values[i]; });
        const res = evaluate(trial, space, date);
        if (!res.violated.length && !res.required_next.length) {
          const diff = combo.map((d, i) => (has(feats, d) ? `${d}=${feats[d]} -> ${d}=${values[i]}` : `+${d}=${values[i]}`));
          if (best === null || cost < best) { best = cost; found = [diff]; }
          else if (found.length < limit) found.push(diff);
        }
      }
    }
    if (best !== null && best <= (k + 1) * Math.min(space.fillCost, space.changeCost)) break;
  }
  if (best === null) return [null, `no fix within ${maxChanges} changes of fixable dimensions`];
  return [best, found];
}

export function similar(feats, space, k = 3) {
  const scored = [];
  for (const rec of space.history) {
    const other = rec.features || {};
    const shared = keysOf(feats).filter((d) => has(other, d));
    if (!shared.length) continue;
    const onlyOne = new Set([...keysOf(feats).filter((d) => !(has(other, d))), ...keysOf(other).filter((d) => !(has(feats, d)))]).size;
    const dist = shared.filter((d) => feats[d] !== other[d]).length + 0.5 * onlyOne;
    scored.push({ id: rec.id, distance: dist, outcome: rec.outcome, code: rec.code });
  }
  scored.sort((a, b) => a.distance - b.distance || cmp(String(a.id), String(b.id)));
  return scored.slice(0, k).map((s) => ({ id: s.id, distance: s.distance, outcome: s.outcome ?? null, code: s.code ?? null }));
}

export function nextSteps(step, space, k = 3) {
  if (!step) return [];
  const following = space.transitions[step] || {};
  const total = Object.values(following).reduce((a, b) => a + b, 0) || 1;
  return Object.entries(following).sort((a, b) => b[1] - a[1] || cmp(a[0], b[0])).slice(0, k)
    .map(([s, n]) => ({ step: s, count: n, share: roundHalfEven(n, total, 2) }));
}

export function unknownFacts(feats, res, space) {
  const full = { ...space.defaults(), ...space.constants(), ...feats };
  const found = {};
  for (const rid of res.open) {
    for (const k of sortStr(ruleDims(space.ruleById[rid]))) {
      if (!(has(full, k))) (found[k] = found[k] || []).push(rid);
    }
  }
  return sortStr(keysOf(found)).map((k) => ({ key: k, name: space.dims[k].name ?? k,
    evidence: space.dims[k].evidence ?? null, decides: found[k] }));
}

const LINK_ROLES = {
  modifies: "exception", uses_definition: "definition", incorporates: "incorporated",
  overrides: "overrides", subject_to: "subject_to",
};
const TERM_ROLES = ["rule", ...Object.values(LINK_ROLES)];
const CITATION_STATUSES = ["violated", "required_next", "open", "excepted"];

export function termContent(term) {
  if (typeof term.summary === "string" && term.summary.trim()) return { summary: term.summary };
  if (typeof term.text === "string" && /^\s*summary/i.test(term.text)) {
    // Older bundles could put a labelled paraphrase in text. Don't present it
    // as verbatim even when a bundle was opened without running validate.
    const marker = /^\s*summary(?=\s|[:(]|$)(?:\s*\([^\r\n)]*\))?\s*:?\s*/i;
    return { summary: term.text.replace(marker, "") || term.text };
  }
  return { text: term.text ?? null };
}

function ruleTerms(rule, full, space) {
  const terms = space.judge.terms;
  if (!terms) return [{ id: null, role: "rule", section: rule.source ?? null, text: null }];
  const idsByRole = Object.fromEntries(TERM_ROLES.map((role) => [role, new Set()]));
  const sourceIds = new Set(rule.source_terms || []);
  const exceptions = rule.exceptions || [];
  for (const e of exceptions) for (const id of e.source_terms || []) idsByRole.exception.add(id);
  for (const id of sourceIds) {
    for (const ref of terms[id]?.refs || []) {
      if (ref.kind !== "modifies" && has(LINK_ROLES, ref.kind) && ref.to != null) {
        idsByRole[LINK_ROLES[ref.kind]].add(ref.to);
      }
    }
  }
  for (const [id, term] of Object.entries(terms)) {
    for (const ref of term.refs || []) {
      if ((ref.kind === "modifies" || (!sourceIds.has(id) && ["overrides", "subject_to"].includes(ref.kind)))
          && sourceIds.has(ref.to)) {
        idsByRole[LINK_ROLES[ref.kind]].add(id);
      }
    }
  }
  const linkedIds = new Set(TERM_ROLES.slice(1).flatMap((role) => [...idsByRole[role]]));
  idsByRole.rule = new Set([...sourceIds].filter((id) => !linkedIds.has(id)));
  const out = [];
  for (const role of TERM_ROLES) {
    for (const id of sortStr(idsByRole[role])) {
      if (!has(terms, id)) continue;
      const term = terms[id];
      const entry = { id, role };
      if (role === "exception") {
        const bindings = exceptions.filter((e) => (e.source_terms || []).includes(id));
        if (bindings.length === 1) {
          const value = evalCond(bindings[0], full);
          if (value !== U) entry.applies = value === T;
        }
      }
      entry.section = term.section ?? null;
      out.push({ ...entry, ...termContent(term) });
    }
  }
  return out;
}

export function constraintTerms(feats, res, space, date = null) {
  if (res.verdict === "invalid_code") return [];
  const full = { ...space.defaults(), ...space.constants(), ...feats };
  const excepted = activeRules(space, date)
    .filter((r) => r.kind === "prohibition" && evalCond(r.when, full) === T
      && (r.exceptions || []).some((e) => evalCond(e, full) === T))
    .map((r) => r.id);
  const groups = { violated: res.violated, required_next: res.required_next, open: res.open, excepted };
  return CITATION_STATUSES.flatMap((status) => sortStr(groups[status]).map((id) => ({
    rule: id, status, terms: ruleTerms(space.ruleById[id], full, space),
  })));
}

export function nextActions(feats, res, fixes, space) {
  if (res.verdict === "invalid_code") {
    return [{ kind: "fix_code", action: "Correct the code: it has invalid or subjective tokens.", rules: [] }];
  }
  const rules = space.ruleById;
  const acts = [];
  const noFix = !fixes || typeof fixes === "string" || (Array.isArray(fixes) && !fixes.length);
  if (noFix) for (const rid of res.violated) acts.push({ kind: "stop", action: rules[rid].next_step || "Do not proceed.", rules: [rid] });
  if (acts.length) return acts;
  if (Array.isArray(fixes) && fixes.length) {
    for (const change of fixes[0]) {
      const key = change.split("=")[0].replace(/^\++/, "");
      const why = [...res.violated, ...res.required_next].filter((rid) => ruleDims(rules[rid]).has(key));
      const text = why.filter((r) => rules[r].next_step).map((r) => rules[r].next_step).join(" ") || `Change ${change}.`;
      acts.push({ kind: "do", action: text, code_change: change, rules: why });
    }
    if (fixes.length > 1) {
      acts.push({ kind: "note", action: "Equally small alternatives: " + fixes.slice(1).map((f) => f.join(", ")).join("; "), rules: [] });
    }
  }
  for (const u of unknownFacts(feats, res, space)) {
    acts.push({ kind: "establish", action: `Establish ${u.name} (${u.key}=?). Evidence: ${u.evidence || "record the fact"}.`,
      rules: u.decides, key: u.key });
  }
  if (!acts.length) acts.push({ kind: "proceed", action: "Proceed: no term blocks this action as coded.", rules: [] });
  return acts;
}

export function factsInEffect(parsed, space) {
  const feats = parsed.features, consts = space.constants(), defs = space.defaults();
  const out = [];
  for (const d of space.dimensions) {
    const k = d.key;
    let origin, value;
    if (has(feats, k)) { origin = (parsed.sources || {})[k] || "code"; value = feats[k]; }
    else if (has(consts, k)) { origin = "constant"; value = consts[k]; }
    else if (has(defs, k)) { origin = "default"; value = defs[k]; }
    else continue;
    out.push({ key: k, value, from: origin, assumed: origin === "default" });
  }
  return out;
}

export function requirements(feats, res, space) {
  const rules = space.ruleById;
  const ids = [...res.required_next, ...res.violated.filter((r) => rules[r].kind === "obligation")];
  return ids.map((rid) => ({ rule: rid, status: res.statuses[rid], next_step: rules[rid].next_step ?? null,
    keys: sortStr(condDims(rules[rid].require)), source: rules[rid].source ?? null }));
}

export const MAX_COMBINATIONS = 256;

// 1.4: skipped facts -> every region the action could land in once they are known (SPEC "Possible results").
export function possibleRegions(feats, unknown, space, date = null) {
  const keys = unknown.map((u) => u.key);
  if (!keys.length) return [];
  const values = Object.fromEntries(keys.map((k) => [k, keysOf(space.dims[k].values)]));
  const total = keys.reduce((n, k) => n * values[k].length, 1);
  let tries = [], together;
  if (total <= MAX_COMBINATIONS) {
    tries = [{}];
    for (const k of keys) tries = tries.flatMap((t) => values[k].map((v) => ({ ...t, [k]: v })));
    together = true;
  } else {
    for (const k of keys) for (const v of values[k]) tries.push({ [k]: v });
    together = false;
  }
  const regions = new Map();
  for (const t of tries) {
    const r = evaluate({ ...feats, ...t }, space, date);
    const sig = JSON.stringify([r.verdict, r.violated, r.required_next]);
    if (!regions.has(sig)) regions.set(sig, { verdict: r.verdict, violated: r.violated, required_next: r.required_next, when: {}, count: 0, tries: [] });
    const reg = regions.get(sig);
    reg.count++;
    reg.tries.push(t);
    for (const [k, v] of Object.entries(t)) (reg.when[k] ||= new Set()).add(v);
  }
  return [...regions.values()].sort((a, b) => VERDICT_ORDER.indexOf(a.verdict) - VERDICT_ORDER.indexOf(b.verdict)).map((reg) => {
    const when = {};
    for (const k of keys) if (reg.when[k]) when[k] = values[k].filter((v) => reg.when[k].has(v));
    const size = Object.values(when).reduce((n, vs) => n * vs.length, 1);
    const exact = together && size === reg.count;
    return { verdict: reg.verdict, violated: reg.violated, required_next: reg.required_next, when, exact, count: reg.count,
      of: tries.length, examples: exact ? [] : reg.tries.slice(0, 3).map((t) => formatCode(t, space)) };
  });
}

export function regionLine(g, verdicts, title) {
  let cond;
  if (g.exact) {
    cond = Object.entries(g.when).map(([k, vs]) => vs.map((v) => `\`${k}=${v}\``).join(" or ")).join(" and ");
  } else {
    const more = g.count - g.examples.length;
    cond = "one of " + g.examples.map((c) => `\`${c}\``).join(", ") + (more > 0 ? ` (+${more} more)` : "");
  }
  let line = `If ${cond} (${g.count} of ${g.of}): ${verdicts[g.verdict] ?? g.verdict}`;
  if (g.violated.length) line += " Violates: " + g.violated.map(title).join(", ") + ".";
  if (g.required_next.length) line += " Must do: " + g.required_next.map(title).join(", ") + ".";
  return line;
}

export function cleanNotes(notes, space) {
  const out = {};
  for (const [k, v] of Object.entries(notes || {})) {
    const text = String(v).trim();
    if (text && (k === "_action" || has(space.dims, k))) out[k] = text;
  }
  return out;
}

const stepOf = (parsed) => parsed.name || parsed.features.step || null;

export function check(code, space, date = null, notes = null) {
  if (code.includes(" > ") || code.includes(" & ")) throw new BundleError("this is a flow of several actions: judge it with checkFlow");
  const parsed = parseCode(code, space);
  const feats = parsed.features;
  let res, cost, fixes;
  if (parsed.errors.length) {
    res = { verdict: "invalid_code", violated: [], required_next: [], open: [], statuses: {}, out_of_scope: [] };
    cost = null; fixes = [];
  } else {
    res = evaluate(feats, space, parsed.date || date);
    [cost, fixes] = minimalFixes(feats, space, parsed.date || date);
  }
  const terms = constraintTerms(feats, res, space, parsed.date || date);
  const byRule = new Map(terms.map(({ rule, terms }) => [rule, terms
    .filter((term) => term.role === "rule" && term.id !== null).map((term) => term.id)]));
  return {
    next_actions: nextActions(feats, res, fixes, space)
      .map((a) => ({ ...a, terms: sortStr(new Set(a.rules.flatMap((id) => byRule.get(id) || []))) })),
    terms,
    code: formatCode(feats, space, parsed.name),
    reading: explainCode(feats, space),
    errors: parsed.errors,
    features: feats,
    facts: factsInEffect(parsed, space),
    requirements: requirements(feats, res, space),
    unknown_facts: unknownFacts(feats, res, space),
    possible: possibleRegions(feats, unknownFacts(feats, res, space), space, parsed.date || date),
    verdict: res.verdict, violated: res.violated, required_next: res.required_next, open: res.open,
    out_of_scope: res.out_of_scope,
    fix: Array.isArray(fixes) ? fixes : [],
    fix_note: typeof fixes === "string" ? fixes : null,
    fix_cost: cost,
    step: stepOf(parsed),
    next_steps: nextSteps(stepOf(parsed), space),
    similar_records: similar(feats, space),
    notes: cleanNotes(notes, space),
  };
}

// 1.4: stages joined by " > " (step by step); cases in a stage joined by " & " (at the same time).
export function splitFlow(code) {
  return code.split(" > ").map((stage) => stage.split(" & ").map((c) => c.trim()));
}

export function checkFlow(code, space, date = null, notes = null) {
  const split = splitFlow(code);
  const parts = split.flat();
  if (parts.length === 1) return check(code, space, date, notes);
  const steps = parts.map((p, i) => check(p, space, date, i === 0 ? notes : null));
  const verdict = steps.map((s) => s.verdict).sort((a, b) => VERDICT_ORDER.indexOf(a) - VERDICT_ORDER.indexOf(b))[0];
  let acts = [];
  steps.forEach((s, i) => { for (const a of s.next_actions) if (a.kind !== "proceed") acts.push({ ...a, action_index: i }); });
  if (!acts.length) acts = [{ kind: "proceed", action: "Proceed: no term blocks this flow as coded.", rules: [], terms: [] }];
  let idx = 0;
  const stages = split.map((stage) => stage.map(() => idx++));
  const terms = [];
  if (verdict !== "invalid_code") {
    const seen = new Set();
    for (const step of steps) {
      for (const citation of step.terms) {
        const key = JSON.stringify(citation);
        if (!seen.has(key)) terms.push(citation);
        seen.add(key);
      }
    }
    terms.sort((a, b) => CITATION_STATUSES.indexOf(a.status) - CITATION_STATUSES.indexOf(b.status)
      || cmp(a.rule, b.rule));
  }
  return { flow: steps, stages, verdict, notes: steps[0].notes, next_actions: acts, terms };
}

// ------------------------------------------------------------------ question form

function probeValues(space, key) {
  const vals = keysOf(space.dims[key].values);
  return space.multi(key) && vals.length > 1 ? [...vals, vals.join("+")] : vals;
}

export function decides(key, rule, feats, space, asked = null) {
  if (!ruleDims(rule).has(key)) return false;
  const whenDims = condDims(rule.when);
  if (!whenDims.has(key)) {
    const w = evalCond(rule.when, feats);
    const pending = [...whenDims].filter((k) => !(has(feats, k)) && !(asked && asked.has(k)) && decides(k, rule, feats, space));
    if (!(w === T || (w === U && asked !== null && !pending.length))) return false;
  }
  return new Set(probeValues(space, key).map((v) => ruleStatus(rule, { ...feats, [key]: v }))).size > 1;
}

export function suggestedValue(key, space) {
  const dim = space.dims[key];
  if (dim.default !== undefined && dim.default !== null) return dim.default;
  const freq = dim.frequency || {};
  const order = keysOf(dim.values);
  let best = null;
  for (const v of order) if ((freq[v] || 0) > 0 && (best === null || freq[v] > freq[best])) best = v;
  return best;
}

export function nextQuestion(answers, space, date = null, notes = null) {
  const parsed = parseCode(answers, space);
  if (parsed.errors.length) return { done: false, errors: parsed.errors };
  const asked = new Set([...keysOf(parsed.features), ...parsed.tokens.filter((t) => t.endsWith("=?")).map((t) => t.split("=")[0])]);
  const consts = space.constants();
  const feats = { ...consts, ...parsed.features };
  const rules = activeRules(space, parsed.date || date);
  const order = dimensionOrder(space);
  const todo = order.filter((k) => !asked.has(k) && !(has(consts, k)) && has(space.dims, k));
  let picked = null, decided = [];
  const applying = rules.filter((r) => evalCond(r.when, feats) === T);
  for (const pool of [applying, rules]) {  // rules that already apply first, then any rule
    for (const key of todo) {
      decided = pool.filter((r) => decides(key, r, feats, space, asked)).map((r) => r.id);
      if (decided.length) { picked = key; break; }
    }
    if (picked !== null) break;
  }
  if (picked === null) {
    const undecided = rules.filter((r) => ["open", "required_next"].includes(ruleStatus(r, feats)));
    for (const key of todo) {
      decided = undecided.filter((r) => ruleDims(r).has(key) && (condDims(r.when).has(key) || evalCond(r.when, feats) !== F)).map((r) => r.id);
      if (decided.length) { picked = key; break; }
    }
  }
  if (picked !== null) {
    const key = picked, dim = space.dims[key], sv = suggestedValue(key, space);
    return { done: false, key, question: dim.question || dim.name || key, multi: Boolean(dim.multi),
      evidence: dim.evidence ?? null,
      options: Object.entries(dim.values).map(([v, label]) => ({ value: v, label, suggested: v === sv })),
      note_allowed: true,
      unknown_means: dim.default !== undefined && dim.default !== null ? `default ${key}=${dim.default} is assumed` : "left unknown",
      decides: sortStr(decided) };
  }
  return { done: true, code: formatCode(parsed.features, space, parsed.name), check: check(answers, space, date, notes) };
}

// ------------------------------------------------------------------ keyword lookup

export function dimensionOrder(space) {
  const order = (space.generator.ask_order || []).filter((k) => has(space.dims, k));
  for (const d of space.dimensions) if (!order.includes(d.key)) order.push(d.key);
  return order;
}

// 1.4: the case map: a tree of categories as deep as the material needs (SPEC "Generator page").
export function categories(space) {
  const cats = (space.generator.categories && space.generator.categories.length)
    ? space.generator.categories : [{ id: "any", name: "Any action", features: [], catch_all: true }];
  const node = (c, parent) => {
    const feats = parent ? [...parent.features] : [];
    const own = (c.features && c.features.length) ? c.features : (parent ? [] : dimensionOrder(space));
    for (const k of own) if (has(space.dims, k) && !feats.includes(k)) feats.push(k);
    const preset = { ...(parent ? parent._preset : {}), ...parseCode(c.preset || "", space).features };
    const n = {
      id: c.id, path: parent ? `${parent.path}/${c.id}` : c.id, name: c.name || c.id, description: c.description || "",
      preset: formatCode(preset, space), features: feats, catch_all: !!(c.catch_all || (parent && parent.catch_all)), _preset: preset,
    };
    n.children = (c.children || []).map((ch) => node(ch, n));
    return n;
  };
  const clean = (n) => { const { _preset, ...rest } = n; return { ...rest, children: n.children.map(clean) }; };
  return cats.map((c) => clean(node(c, null)));
}

export function categoryNodes(tree) {
  const out = [];
  for (const n of tree) { out.push(n); out.push(...categoryNodes(n.children)); }
  return out;
}

// 1.4: parent key -> keys whose folder sits inside it (dimension `under`).
export function nestedUnder(space) {
  const out = {};
  for (const d of space.dimensions) if (d.under) (out[d.under.dim] ||= []).push(d.key);
  return out;
}

export function reachable(keys, feats, space) {
  const kids = nestedUnder(space);
  const out = [], todo = [...keys];
  while (todo.length) {
    const k = todo.shift();
    if (out.includes(k)) continue;
    out.push(k);
    for (const c of kids[k] || []) if (evalCond(space.dims[c].under, feats) === T) todo.push(c);
  }
  return out;
}

function coreFeatures(feats, space) {
  const core = { ...feats };
  while (true) {
    const remove = Object.keys(core).filter((k) => space.dims[k]?.under && evalCond(space.dims[k].under, core) !== T);
    if (!remove.length) return { core, dropped: Object.keys(feats).filter((k) => !has(core, k)).sort(cmp) };
    for (const k of remove) delete core[k];
  }
}

export function fitDetail(feats, space, date = null) {
  const { core, dropped } = coreFeatures(feats, space);
  if (dropped.length) {
    const before = evaluate(feats, space, date), after = evaluate(core, space, date);
    if (["verdict", "violated", "required_next", "open"].some((k) => JSON.stringify(before[k]) !== JSON.stringify(after[k]))) {
      return { category: null, dropped: [] };
    }
  }
  return { category: fitCore(core, space), dropped };
}

export function mapFit(feats, space, date = null) {
  return fitDetail(feats, space, date).category;
}

function fitCore(feats, space) {
  let best = null;
  for (const n of categoryNodes(categories(space))) {
    if (n.catch_all) continue;
    const preset = parseCode(n.preset, space).features;
    const agrees = Object.entries(preset).every(([k, v]) => feats[k] === v
      || (space.multi(k) && has(feats, k) && v.split("+").every((x) => feats[k].split("+").includes(x))));
    if (!agrees) continue;
    const reach = reachable(n.features, feats, space);
    if (Object.keys(feats).every((k) => reach.includes(k)) && (best === null || n.path.split("/").length > best.split("/").length)) best = n.path;
  }
  return best;
}

export function lookup(text, space) {
  const words = text.toLowerCase().match(/[a-z0-9]+/g) || [];
  const hits = {};
  for (const entry of space.generator.lexicon || []) {
    const pw = entry.phrase.toLowerCase().match(/[a-z0-9]+/g) || [];
    if (!pw.length) continue;
    for (let i = 0; i + pw.length <= words.length; i++) {
      if (pw.every((w, j) => words[i + j] === w)) {
        const tok = entry.token;
        if (!(has(hits, tok)) || i < hits[tok].position) hits[tok] = { token: tok, phrase: entry.phrase, position: i };
        break;
      }
    }
  }
  return Object.values(hits).sort((a, b) => a.position - b.position || cmp(a.token, b.token));
}

// ------------------------------------------------------------------ report

export function reportSections(result, space, seenDefinitions = new Set()) {
  // The fixed report as data in SPEC order, empty sections dropped. Rule citations are
  // aligned with items, keeping plain display text separate from quoted policy text.
  const texts = { ...DEFAULT_TEXTS, ...(space.judge.texts || {}) };
  const h = { ...DEFAULT_TEXTS.headings, ...(texts.headings || {}) };
  const verdicts = { ...DEFAULT_TEXTS.verdict, ...(texts.verdict || {}) };
  const title = (rid) => { const r = space.ruleById[rid] || {}; return r.title ? `${r.title} [${rid}]` : rid; };
  const notes = Object.entries(result.notes || {}).sort((a, b) => (a[0] !== "_action") - (b[0] !== "_action"));
  const hasContent = (term) => {
    const { text, summary } = termContent(term);
    return Boolean(text || summary);
  };
  const byRule = new Map((result.terms || []).map((entry) => [entry.rule, entry.terms.filter(hasContent)]));
  const quotes = (id) => (byRule.get(id) || []).filter((q) => q.role !== "definition");
  const group = (status) => (result.terms || []).filter((entry) => entry.status === status && byRule.get(entry.rule).length);
  const open = group("open").map((entry) => entry.rule);
  const excepted = group("excepted").map((entry) => entry.rule);
  const definitions = [];
  for (const entry of result.terms || []) {
    for (const q of entry.terms) {
      if (q.role !== "definition" || !hasContent(q) || seenDefinitions.has(q.id)) continue;
      seenDefinitions.add(q.id);
      definitions.push(q);
    }
  }
  const sections = [
    [h.errors, result.errors.map((e) => `\`${e.token}\`: ${e.message}` + (e.did_you_mean ? ` (did you mean \`${e.did_you_mean}\`?)` : ""))],
    [h.violated, result.violated.map(title), result.violated.map(quotes)],
    [h.must_do, (result.requirements || []).map((r) => `${title(r.rule)}: ${r.next_step}`),
      (result.requirements || []).map((r) => r.status === "required_next" ? quotes(r.rule) : [])],
    ["Rules still open", open.map(title), open.map(quotes)],
    ["Excepted", excepted.map(title), excepted.map(quotes)],
    [h.unknown, (result.unknown_facts || []).map((u) => `${u.name} (\`${u.key}=?\`). Evidence: ${u.evidence}`)],
    [h.possible, (result.possible || []).map((g) => regionLine(g, verdicts, title))],
    [h.assumed, (result.facts || []).filter((f) => f.assumed).map((f) => `\`${f.key}=${f.value}\``)],
    [h.fix, result.fix.length ? result.fix.map((f) => f.join(", ")) : (result.fix_note ? [result.fix_note] : [])],
    [h.usual_next, result.next_steps.map((n) => `${n.step} (${Math.round(n.share * 100)}%)`)],
    [h.similar, result.similar_records.map((r) => `${r.id}: ${r.outcome} (\`${r.code}\`)`)],
    [h.scope, result.out_of_scope],
    [h.notes, notes.map(([k, v]) => (k !== "_action" ? `\`${k}\`: ` : "") + v)],
    ["Terms used", definitions.map(() => ""), definitions.map((q) => [q])],
  ];
  return sections.filter(([, items]) => items.length).map(([heading, items, citations]) => ({ heading, items, citations }));
}

export function termRoleLabel(term) {
  if (term.role === "exception") {
    return term.applies === true ? "Exception (applies)" : term.applies === false ? "Exception (does not apply)" : "Exception";
  }
  return { definition: "Definition", incorporated: "Incorporated", overrides: "Overrides", subject_to: "Subject to" }[term.role] || "";
}

function termLine(term) {
  const label = termRoleLabel(term);
  const citation = [term.section, term.id].filter(Boolean).join(", ");
  const { summary, text } = termContent(term);
  const body = summary !== undefined
    ? `Summary (not a quote): ${summary.replace(/\r\n?|\n/g, " ")}` : `"${text}"`;
  return `  > ${label ? `${label}: ` : ""}${body} (${citation})`;
}

export function renderReport(result, space, seenDefinitions = new Set()) {
  if (result.flow) {
    const vt = { ...DEFAULT_TEXTS.verdict, ...((space.judge.texts || {}).verdict || {}) };
    const parts = [`# Flow of ${result.flow.length} actions: ${vt[result.verdict] ?? result.verdict}`];
    const stages = result.stages || result.flow.map((_, i) => [i]);
    const together = stages.some((st) => st.length > 1);
    stages.forEach((stage, n) => {
      if (together) parts.push(`\n# Step ${n + 1}` + (stage.length > 1 ? `: ${stage.length} actions at the same time` : ""));
      for (const i of stage) {
        const step = result.flow[i];
        parts.push(`\n# Action ${i + 1}` + (step.step ? `: ${step.step}` : ""));
        parts.push(renderReport(step, space, seenDefinitions));
      }
    });
    return parts.join("\n");
  }
  const texts = { ...DEFAULT_TEXTS, ...(space.judge.texts || {}) };
  const verdicts = { ...DEFAULT_TEXTS.verdict, ...(texts.verdict || {}) };
  const h = { ...DEFAULT_TEXTS.headings, ...(texts.headings || {}) };
  const out = [`## ${h.next}`];
  result.next_actions.forEach((a, i) => out.push(`${i + 1}. ${a.action}`
    + (a.code_change ? ` [${a.code_change}]` : "")
    + (a.terms?.length ? ` (${a.terms.join(", ")})` : "")));
  out.push("", `**${h.verdict}:** ${verdicts[result.verdict] ?? result.verdict}`, `**${h.code}:** \`${result.code}\`: ${result.reading}`);
  for (const { heading, items, citations } of reportSections(result, space, seenDefinitions)) {
    out.push("", `**${heading}**`);
    items.forEach((item, i) => {
      if (item) out.push(`- ${item}`);
      out.push(...(citations?.[i] || []).map(termLine));
    });
  }
  out.push("", `_${texts.disclaimer ?? DEFAULT_TEXTS.disclaimer}_`);
  return out.join("\n");
}

// ------------------------------------------------------------------ bundle manifest

export async function sha256Hex(text) {
  const data = new TextEncoder().encode(text);
  const buf = await globalThis.crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function verifyManifest(manifest, files) {
  // CRLF normalization is diagnostic only; always compare the original UTF-8 bytes first.
  const mismatched = [], crlf_only = [];
  for (const [name, digest] of Object.entries((manifest && manifest.files) || {})) {
    if (files[name] === undefined) continue;
    if ((await sha256Hex(files[name])) !== digest) {
      mismatched.push(name);
      if ((await sha256Hex(files[name].replace(/\r\n/g, "\n"))) === digest) crlf_only.push(name);
    }
  }
  const exam = (manifest && manifest.exam) || null;
  return { digests_ok: !mismatched.length, mismatched, crlf_only, exam, exam_passed: Boolean(exam && exam.passed) };
}
