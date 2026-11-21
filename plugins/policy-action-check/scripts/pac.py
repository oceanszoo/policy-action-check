#!/usr/bin/env python3
"""pac: exact engine for policy-action-check (standard library only).

This file is the package module ``policy_action_check.engine``. The Claude Code plugin ships a
byte-identical copy as ``plugins/policy-action-check/scripts/pac.py`` (kept in sync by
``tools/sync.py``), so the plugin works without installing anything.

The model does the fuzzy work (scope questions, terms -> rules, words -> codes). This script does
the exact work, so the same code always gets the same verdict:

  parse / validate an action code, evaluate rule regions with unknowns, find minimal fixes,
  build the per-token suggestion panel, find similar past records, report pipeline status,
  and serve the plugin's hooks.

A code is the readable surface of a vector: one coordinate per key; a missing key is unset.
Rules are regions; checking is three-valued (true / false / unknown) over unset coordinates.

Bundle directory (default ``$CLAUDE_PROJECT_DIR/policy_space``, override with ``--space`` or
``PAC_SPACE``) is the pipeline's output, readable without Claude (spec: references/SPEC.md):

  bundle.json        manifest: format, version, sources, file digests
  generator.json     how to write action codes: dimensions, vocabulary, grammar, saved codes,
                     constants, scope decisions, next-step habits, tool map
  judge.json         how to judge a code: rules as regions, semantics, verdicts, fix cost model
  conformance.jsonl  test vectors: code -> expected judgement (any implementation must pass)
  work/              pipeline working files: state.json, scope.json, history_encoded.jsonl,
                     build_report.md
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import difflib
import hashlib
import itertools
import json
import os
import random
import re
import sys
import tempfile
from collections import Counter, deque
from pathlib import Path

T, U, F = 1.0, 0.5, 0.0
STAGES = ["filter", "scope", "build", "encode", "check", "report", "done"]
AGENT_STAGE = {"term-filter": "filter", "scope-planner": "scope", "space-builder": "build", "space-verifier": "build"}
TERM_CLASSES = {"prohibition", "obligation", "permission_exception", "condition_scope",
                "definition", "description", "example", "procedure"}
NORMATIVE = {"prohibition", "obligation", "permission_exception", "condition_scope"}
REF_SIGNALS = (
    "as defined", "defined in", "section", "above", "below", "foregoing",
    "subject to", "notwithstanding", "except as", "in accordance with",
    "referred to", "see", "these terms", "this policy",
)
REF_KINDS = {"uses_definition", "incorporates", "modifies", "overrides", "subject_to"}


# ---------------------------------------------------------------- space loading


class SpaceError(Exception):
    pass


def space_dir(arg: str | None = None) -> Path:
    if arg:
        return Path(arg)
    if os.environ.get("PAC_SPACE"):
        return Path(os.environ["PAC_SPACE"])
    name = os.environ.get("CLAUDE_PLUGIN_OPTION_SPACE_DIR") or "policy_space"
    base = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    return base / name


def _read_json(path: Path, default=None):
    if not path.exists():
        if default is not None:
            return default
        raise SpaceError(f"missing {path.name} in {path.parent}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SpaceError(f"{path.name}: invalid JSON ({exc})") from exc


GENERATOR_FORMAT, JUDGE_FORMAT, SUPPORTED_MAJOR = "pac-generator", "pac-judge", "1"
SPEC_VERSION = "1.6"
# Size budget for a built space (1.1). A bundle may raise it in generator.json "limits" with a reason.
DEFAULT_LIMITS = {"max_dimensions": 24, "max_rules": 60}


class Space:
    def __init__(self, root: Path):
        self.root = root
        self.generator = _read_json(root / "generator.json")
        self.judge = _read_json(root / "judge.json")
        for doc, fmt in ((self.generator, GENERATOR_FORMAT), (self.judge, JUDGE_FORMAT)):
            if doc.get("format") != fmt or str(doc.get("version", "")).split(".")[0] != SUPPORTED_MAJOR:
                raise SpaceError(f"expected {fmt} version {SUPPORTED_MAJOR}.x, got {doc.get('format')} {doc.get('version')}")
        self.schema = {"dimensions": self.generator["dimensions"]}
        self.rules = {"rules": self.judge["rules"]}
        self.codes = self.generator.get("saved_codes", {})
        self.scope = self.generator.get("scope", {})
        self.transitions = self.generator.get("transitions", {})
        self.tool_map = self.generator.get("tool_map", {})
        cost = self.judge.get("fix", {})
        self.fill_cost = float(cost.get("fill_cost", 0.5))
        self.change_cost = float(cost.get("change_cost", 1.0))
        self.max_changes = int(cost.get("max_changes", 3))
        self.dims = {d["key"]: d for d in self.schema["dimensions"]}
        self.history = []
        self._subjective = None
        # 1.2: past records ship in the bundle (records.jsonl); work/ is the pipeline's fallback
        hist = root / "records.jsonl"
        if not hist.exists():
            hist = root / "work" / "history_encoded.jsonl"
        if hist.exists():
            for line in hist.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.history.append(json.loads(line))
        for rec in self.history:
            if "features" not in rec and rec.get("code"):
                rec["features"] = parse_code(rec["code"], self)["features"]

    # constants from the scope plan (facts fixed for this user) apply under every code
    def constants(self) -> dict:
        return dict(self.generator.get("constants", {}))

    def defaults(self) -> dict:
        """Context dimensions may declare a value used when the key is unset (e.g. hr=none).
        Requirement dimensions (review, disclosure, ...) must not: unset means unknown."""
        return {k: d["default"] for k, d in self.dims.items() if d.get("default") is not None}

    @property
    def subjective(self) -> re.Pattern:
        """1.2: the bundle carries its own list of opinion words to refuse (subjective_terms)."""
        if self._subjective is None:
            words = self.generator.get("subjective_terms") or DEFAULT_SUBJECTIVE
            self._subjective = re.compile(r"^(" + "|".join(re.escape(w) for w in words) + r")$", re.IGNORECASE)
        return self._subjective

    def version(self) -> tuple[int, int]:
        major, _, minor = str(self.generator.get("version", "1.0")).partition(".")
        return int(major), int(minor or 0)

    def multi(self, key: str) -> bool:
        """1.1: a multi-valued dimension holds a set of values, written a+b (e.g. pur=cyber+fraud)."""
        return bool(self.dims.get(key, {}).get("multi"))

    def limits(self) -> dict:
        return {**DEFAULT_LIMITS, **{k: v for k, v in self.generator.get("limits", {}).items() if k in DEFAULT_LIMITS}}


# ---------------------------------------------------------------- codes

# Inputs must be objective, checkable conditions. Opinions and intentions are refused.
DEFAULT_SUBJECTIVE = [
    "risk", "risky", "lowrisk", "low_risk", "highrisk", "intent", "intention", "ok", "okay", "fine",
    "good", "bad", "safe_enough", "compliant", "lawful", "allowed", "harmless", "minor", "trusted",
    "trust", "believe", "think", "probably", "likely", "reasonable", "appropriate", "acceptable",
    "benign", "innocent", "harmful"]


def parse_code(text: str, space: Space, _depth: int = 0) -> dict:
    """Parse one action code. Returns {name, features, date, errors, tokens}.

    Grammar: [name ":"] token*, token = key=value | key=? | @macro | date=YYYY-MM-DD.
    Later tokens override earlier ones. Unset keys are simply absent.
    """
    text = text.strip()
    name = None
    m = re.match(r"^([A-Za-z0-9_.-]+):\s+(.*)$", text) or re.match(r"^([A-Za-z0-9_.-]+):$", text)
    if m:
        name, text = m.group(1), (m.group(2) if m.lastindex == 2 else "")
    features: dict[str, str] = {}
    sources: dict[str, str] = {}  # 1.1 provenance: "code" or "@saved_code" per key
    errors: list[dict] = []
    tokens = text.split()
    date = None
    for tok in tokens:
        if tok.startswith("@"):
            macro = tok[1:]
            body = space.codes.get(macro)
            if body is None:
                errors.append(_err(tok, "unknown saved code", difflib.get_close_matches(macro, space.codes, 1)))
                continue
            if _depth > 5:
                errors.append(_err(tok, "saved codes nest too deeply", []))
                continue
            sub = parse_code(body, space, _depth + 1)
            features.update(sub["features"])
            sources.update({k: (tok if v == "code" else v) for k, v in sub["sources"].items()})
            errors.extend(sub["errors"])
            continue
        if "=" not in tok:
            errors.append(_err(tok, "expected key=value", []))
            continue
        key, value = tok.split("=", 1)
        if key == "date":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                errors.append(_err(tok, "date must be YYYY-MM-DD", []))
            else:
                date = value
            continue
        if space.subjective.match(key) or (key not in space.dims and space.subjective.match(value)):
            errors.append({"token": tok, "subjective": True, "did_you_mean": None, "message": (
                "subjective input is not accepted: state the objective condition instead "
                "(what was done, what exists, who agreed), using the generator's keys")})
            continue
        if key not in space.dims:
            guess = difflib.get_close_matches(key, space.dims, 1)
            errors.append(_err(tok, f"unknown key '{key}'", [f"{g}={value}" for g in guess]))
            continue
        if value == "?":
            features.pop(key, None)
            sources.pop(key, None)
            continue
        values = space.dims[key]["values"]
        if "+" in value:
            parts = [x for x in value.split("+") if x]
            if not space.multi(key):
                errors.append(_err(tok, f"{key} takes one value (it is not multi-valued)", [f"{key}={parts[0]}"] if parts else []))
                continue
            bad = [x for x in parts if x not in values]
            if bad or not parts:
                guess = difflib.get_close_matches(bad[0], values, 1) if bad else []
                errors.append(_err(tok, f"invalid value for {key}: {'+'.join(bad) or value}", [f"{key}={g}" for g in guess]))
                continue
            features[key] = "+".join(v for v in values if v in parts)  # canonical: dimension order
            sources[key] = "code"
            continue
        if values and value not in values and space.subjective.match(value):
            errors.append({"token": tok, "subjective": True, "did_you_mean": None, "message": (
                f"'{value}' is a judgement, not a fact. Set {key} to one of {sorted(values)}; "
                f"evidence: {space.dims[key].get('evidence', 'see generator')}")})
            continue
        if values and value not in values:
            guess = difflib.get_close_matches(value, values, 1)
            if not guess:  # match on meanings too: hr=insurance -> hr=ins
                for code, meaning in values.items():
                    if value.lower() in str(meaning).lower():
                        guess = [code]
                        break
            errors.append(_err(tok, f"invalid value for {key}", [f"{key}={g}" for g in guess]))
            continue
        features[key] = value
        sources[key] = "code"
    return {"name": name, "features": features, "date": date, "errors": errors, "tokens": tokens,
            "sources": sources}


def _err(token, message, suggestions):
    return {"token": token, "message": message, "did_you_mean": suggestions[0] if suggestions else None}


def format_code(features: dict, space: Space, name: str | None = None) -> str:
    order = [d["key"] for d in space.schema["dimensions"]]
    body = " ".join(f"{k}={features[k]}" for k in order if k in features)
    return f"{name}: {body}" if name else body


def explain_code(features: dict, space: Space) -> str:
    parts = []
    for d in space.schema["dimensions"]:
        if d["key"] in features:
            vals = features[d["key"]].split("+")
            parts.append(f"{d.get('name', d['key'])}: " + " + ".join(str(d["values"].get(v, v)) for v in vals))
    return "; ".join(parts) if parts else "(nothing set)"


# ---------------------------------------------------------------- rule evaluation


def eval_cond(cond: dict, feats: dict) -> float:
    if "all" in cond:
        return min((eval_cond(c, feats) for c in cond["all"]), default=T)
    if "any" in cond:
        return max((eval_cond(c, feats) for c in cond["any"]), default=F)
    if "not" in cond:
        return 1.0 - eval_cond(cond["not"], feats)
    value = feats.get(cond["dim"])
    if value is None:
        return U
    members = value.split("+")  # a multi-valued key is a set; a single value is a set of one
    if "in" in cond:
        return T if any(v in cond["in"] for v in members) else F
    if "not_in" in cond:  # none of the members is listed
        return F if any(v in cond["not_in"] for v in members) else T
    raise SpaceError(f"bad condition {cond}")


def cond_dims(cond: dict) -> set[str]:
    if "all" in cond or "any" in cond:
        return set().union(*(cond_dims(c) for c in cond.get("all", cond.get("any", []))))
    if "not" in cond:
        return cond_dims(cond["not"])
    return {cond["dim"]}


def rule_dims(rule: dict) -> set[str]:
    dims = cond_dims(rule["when"])
    for e in rule.get("exceptions", []):
        dims |= cond_dims(e)
    if rule.get("require"):
        dims |= cond_dims(rule["require"])
    return dims


def rule_status(rule: dict, feats: dict) -> str:
    w = eval_cond(rule["when"], feats)
    if rule["kind"] == "prohibition":
        ex = max((eval_cond(e, feats) for e in rule.get("exceptions", [])), default=F)
        viol = min(w, 1.0 - ex)
        if viol == T:
            return "violated"
        if viol == F:
            return "satisfied" if w == T else "not_applicable"
        return "open"
    r = eval_cond(rule["require"], feats)
    if w == F:
        return "not_applicable"
    if w == T:
        return {T: "satisfied", F: "violated", U: "required_next"}[r]
    return "satisfied" if r == T else "open"


def active_rules(space: Space, date: str | None) -> list[dict]:
    day = date or os.environ.get("CLAUDE_PLUGIN_OPTION_POLICY_DATE") or dt.datetime.now(dt.UTC).date().isoformat()
    return [r for r in space.rules["rules"] if (r.get("effective_from") or "0000-00-00") <= day]


def evaluate(feats: dict, space: Space, date: str | None = None) -> dict:
    full = {**space.defaults(), **space.constants(), **feats}
    statuses = {r["id"]: rule_status(r, full) for r in active_rules(space, date)}
    violated = sorted(k for k, s in statuses.items() if s == "violated")
    required = sorted(k for k, s in statuses.items() if s == "required_next")
    open_ = sorted(k for k, s in statuses.items() if s == "open")
    if violated:
        verdict = "violates"
    elif required:
        verdict = "compliant_if_requirements_met"
    elif open_:
        verdict = "needs_info"
    else:
        verdict = "compliant"
    return {"verdict": verdict, "violated": violated, "required_next": required, "open": open_,
            "statuses": statuses, "out_of_scope": out_of_scope(full, space)}


def out_of_scope(feats: dict, space: Space) -> list[str]:
    """Scope areas the action touches that the user declared out of scope or terms-only."""
    notes = []
    for area in space.scope.get("areas", []):
        cond = area.get("match")
        if cond and eval_cond(cond, feats) == T and area.get("decision") in ("out_of_scope", "terms_only"):
            notes.append(f"{area['area']}: {area['decision']}")
    return notes


# ---------------------------------------------------------------- fixes, panel, similar, next


def minimal_fixes(feats: dict, space: Space, date: str | None = None, max_changes: int | None = None, limit: int = 5):
    """Nearest vectors with no violated and no required_next rule.

    Cost: changing a set coordinate = 1, filling an unset one = 0.5. Only fixable dimensions
    move. Returns (cost, [diffs]) or (None, reason).
    """
    max_changes = max_changes or space.max_changes
    base = evaluate(feats, space, date)
    bad = set(base["violated"]) | set(base["required_next"])
    if not bad:
        return 0, []
    rules = {r["id"]: r for r in space.rules["rules"]}
    dims = sorted({d for rid in bad for d in rule_dims(rules[rid]) if space.dims[d].get("fixable", True)})
    if not dims:
        return None, "only non-fixable dimensions (such as the purpose itself) decide this: no compliant variant"
    best, found = None, []
    for k in range(1, min(max_changes, len(dims)) + 1):
        for combo in itertools.combinations(dims, k):
            choices = [[v for v in space.dims[d]["values"] if v != feats.get(d)] for d in combo]
            for values in itertools.product(*choices):
                cost = sum(space.fill_cost if d not in feats else space.change_cost for d in combo)
                if best is not None and cost > best:
                    continue
                trial = dict(feats)
                trial.update(zip(combo, values))
                res = evaluate(trial, space, date)
                if not res["violated"] and not res["required_next"]:
                    diff = [f"{d}={feats[d]} -> {d}={v}" if d in feats else f"+{d}={v}" for d, v in zip(combo, values)]
                    if best is None or cost < best:
                        best, found = cost, [diff]
                    elif len(found) < limit:
                        found.append(diff)
        if best is not None and best <= (k + 1) * min(space.fill_cost, space.change_cost):
            break
    if best is None:
        return None, f"no fix within {max_changes} changes of fixable dimensions"
    return best, found


def panel(code: str, space: Space, date: str | None = None) -> list[dict]:
    """The live suggestion panel after each token of a code."""
    parsed = parse_code(code, space)
    prefix_tokens: list[str] = []
    prev = evaluate({}, space, parsed["date"] or date)
    out = []
    for tok in parsed["tokens"]:
        prefix_tokens.append(tok)
        p = parse_code(" ".join(prefix_tokens), space)
        bad_tok = [e for e in p["errors"] if e["token"] == tok]
        res = evaluate(p["features"], space, p["date"] or date)
        nk, sv = next_key(p["features"], res, space, p["date"] or date)
        out.append({
            "code_so_far": " ".join(prefix_tokens), "token": tok, "valid": not bad_tok,
            "did_you_mean": bad_tok[0]["did_you_mean"] if bad_tok else None,
            "now_violated": sorted(set(res["violated"]) - set(prev["violated"])),
            "now_required": sorted(set(res["required_next"]) - set(prev["required_next"])),
            "still_open": res["open"], "verdict_so_far": res["verdict"],
            "next_key": nk, "suggested_value": sv,
        })
        prev = res
    return out


def next_key(feats: dict, res: dict, space: Space, date: str | None):
    """The unset key that decides the most undecided rules, and its best compliant value."""
    rules = {r["id"]: r for r in space.rules["rules"]}
    undecided = res["open"] + res["required_next"]
    counts: dict[str, int] = {}
    for rid in undecided:
        for d in rule_dims(rules[rid]):
            if d not in feats and d not in space.constants() and d not in space.defaults():
                counts[d] = counts.get(d, 0) + 1
    if not counts:
        return None, None
    key = min(counts, key=lambda d: (-counts[d], d))
    freq = space.dims[key].get("frequency", {})

    def score(v):
        r = evaluate({**feats, key: v}, space, date)
        return (len(r["violated"]), len(r["required_next"]), len(r["open"]), -freq.get(v, 0), v)

    return key, min(space.dims[key]["values"], key=score)


def similar(feats: dict, space: Space, k: int = 3) -> list[dict]:
    scored = []
    for rec in space.history:
        other = rec.get("features", {})
        shared = set(feats) & set(other)
        if not shared:
            continue
        dist = sum(feats[d] != other[d] for d in shared) + 0.5 * len(set(feats) ^ set(other))
        scored.append((dist, rec.get("id"), rec.get("outcome"), rec.get("code")))
    scored.sort(key=lambda x: (x[0], str(x[1])))
    return [{"id": i, "distance": d, "outcome": o, "code": c} for d, i, o, c in scored[:k]]


def next_steps(step: str | None, space: Space, k: int = 3) -> list[dict]:
    if not step:
        return []
    following = space.transitions.get(step, {})
    total = sum(following.values()) or 1
    ranked = sorted(following.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
    return [{"step": s, "count": n, "share": round(n / total, 2)} for s, n in ranked]


_CITE_LINKS = {"modifies": "exception", "uses_definition": "definition", "incorporates": "incorporated",
               "overrides": "overrides", "subject_to": "subject_to"}
_CITE_BACKLINKS = {"modifies", "overrides", "subject_to"}
_CITE_ORDER = ("violated", "required_next", "open", "excepted")
_SUMMARY_START = re.compile(r"^\s*summary", re.IGNORECASE)
_SUMMARY_MARKER = re.compile(r"^\s*summary(?=\s|[:(]|$)(?:\s*\([^\r\n)]*\))?\s*:?\s*", re.IGNORECASE)


def _term_content(term: dict) -> dict:
    if _nonempty(term.get("summary")):
        return {"summary": term["summary"]}
    text = term.get("text")
    if isinstance(text, str) and _SUMMARY_START.match(text):
        # Old bundles can be judged directly, without validate. Never quote a legacy summary.
        return {"summary": _SUMMARY_MARKER.sub("", text, count=1) or text}
    return {"text": text}


def _summary_label(summary: str) -> str:
    # Keep multiline summaries in their labelled citation, not on separate Markdown quote lines.
    compact = summary.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ")
    return f"Summary (not a quote): {compact}"


def _rule_citations(rule: dict, space: Space, feats: dict) -> list[dict]:
    """Collect shipped policy terms and label how they constrain this rule."""
    terms = space.judge.get("terms")
    if terms is None:
        return [{"id": None, "role": "rule", "section": rule.get("source"), "text": None}]
    sources = set(rule.get("source_terms", []))
    roles: dict[str, set[str]] = {role: set() for role in ("rule", *_CITE_LINKS.values())}
    for ident, term in terms.items():
        for ref in term.get("refs", []):
            kind, target = ref["kind"], ref.get("to")
            if ident in sources and kind != "modifies" and kind in _CITE_LINKS and target in terms:
                roles[_CITE_LINKS[kind]].add(target)
            if target in sources and kind in _CITE_BACKLINKS and (kind == "modifies" or ident not in sources):
                roles[_CITE_LINKS[kind]].add(ident)
    conditions: dict[str, list[float]] = {}
    for e in rule.get("exceptions", []):
        for ident in set(e.get("source_terms", [])) & terms.keys():
            roles["exception"].add(ident)
            conditions.setdefault(ident, []).append(eval_cond(e, feats))
    roles["rule"] = (sources & terms.keys()) - set().union(*roles.values())
    result = []
    for role, ids in roles.items():
        for ident in sorted(ids):
            entry = {"id": ident, "role": role}
            states = conditions.get(ident, []) if role == "exception" else []
            if len(states) == 1 and states[0] in (T, F):
                entry["applies"] = states[0] == T
            result.append({**entry, "section": terms[ident].get("section"), **_term_content(terms[ident])})
    return result


def _result_terms(feats: dict, res: dict, space: Space, date: str | None) -> list[dict]:
    if res["verdict"] == "invalid_code":
        return []
    full = {**space.defaults(), **space.constants(), **feats}
    rules = {r["id"]: r for r in active_rules(space, date)}
    excepted = sorted(rid for rid, r in rules.items()
                      if r["kind"] == "prohibition" and eval_cond(r["when"], full) == T
                      and any(eval_cond(e, full) == T for e in r.get("exceptions", [])))
    return [{"rule": rid, "status": status, "terms": _rule_citations(rules[rid], space, full)}
            for status, ids in [("violated", res["violated"]), ("required_next", res["required_next"]),
                                ("open", res["open"]), ("excepted", excepted)]
            for rid in ids]


def _action_terms(actions: list[dict], terms: list[dict]) -> list[dict]:
    by_rule = {r["rule"]: {t["id"] for t in r["terms"] if t["role"] == "rule" and t["id"] is not None}
               for r in terms}
    return [{**a, "terms": sorted(set().union(*(by_rule.get(r, set()) for r in a["rules"])))} for a in actions]


def next_actions(feats: dict, res: dict, fixes, space: Space) -> list[dict]:
    """The judge's primary output: what to do next, in order.

    kinds: stop (prohibited, no compliant variant), do (a change or requirement to meet),
    establish (an objective fact is missing and decides a rule), proceed (nothing left).
    """
    rules = {r["id"]: r for r in space.rules["rules"]}
    acts: list[dict] = []
    if res["verdict"] == "invalid_code":
        return [{"kind": "fix_code", "action": "Correct the code: it has invalid or subjective tokens.", "rules": []}]
    unfixable = [rid for rid in res["violated"] if not fixes or isinstance(fixes, str)]
    for rid in unfixable:
        acts.append({"kind": "stop", "action": rules[rid].get("next_step") or "Do not proceed.", "rules": [rid]})
    if acts:
        return acts
    if isinstance(fixes, list) and fixes:
        for change in fixes[0]:
            key = change.split("=")[0].lstrip("+")
            why = [rid for rid in res["violated"] + res["required_next"] if key in rule_dims(rules[rid])]
            text = " ".join(rules[r].get("next_step", "") for r in why if rules[r].get("next_step")) or f"Change {change}."
            acts.append({"kind": "do", "action": text, "code_change": change, "rules": why})
        if len(fixes) > 1:
            acts.append({"kind": "note", "action": "Equally small alternatives: "
                         + "; ".join(", ".join(f) for f in fixes[1:]), "rules": []})
    for u in unknown_facts(feats, res, space):  # one action per unknown key, in key order
        acts.append({"kind": "establish", "action": f"Establish {u['name']} ({u['key']}=?). Evidence: "
                     f"{u['evidence'] or 'record the fact'}.", "rules": u["decides"], "key": u["key"]})
    if not acts:
        acts.append({"kind": "proceed", "action": "Proceed: no term blocks this action as coded.", "rules": []})
    return acts


def facts_in_effect(parsed: dict, space: Space) -> list[dict]:
    """1.1 provenance: every fact the judge used and where it came from.

    from: "code" (written in the code), "@name" (a saved code), "constant" (scope plan), or
    "default" (a context default; assumed, so the person should confirm it if it matters).
    """
    feats, consts, defs = parsed["features"], space.constants(), space.defaults()
    out = []
    for d in space.schema["dimensions"]:
        k = d["key"]
        if k in feats:
            origin, value = parsed.get("sources", {}).get(k, "code"), feats[k]
        elif k in consts:
            origin, value = "constant", consts[k]
        elif k in defs:
            origin, value = "default", defs[k]
        else:
            continue
        out.append({"key": k, "value": value, "from": origin, "assumed": origin == "default"})
    return out


def requirements(feats: dict, res: dict, space: Space) -> list[dict]:
    """1.1: binding requirements. The action triggers these obligations; meeting them is not optional."""
    rules = {r["id"]: r for r in space.rules["rules"]}
    out = []
    for rid in res["required_next"] + [r for r in res["violated"] if rules[r]["kind"] == "obligation"]:
        rule = rules[rid]
        keys = sorted(cond_dims(rule["require"]))
        out.append({"rule": rid, "status": res["statuses"][rid], "next_step": rule.get("next_step"),
                    "keys": keys, "source": rule.get("source")})
    return out


def unknown_facts(feats: dict, res: dict, space: Space) -> list[dict]:
    """1.1: facts nobody has stated that decide an open rule. Establish them; don't assume them."""
    rules = {r["id"]: r for r in space.rules["rules"]}
    full = {**space.defaults(), **space.constants(), **feats}
    found: dict[str, list[str]] = {}
    for rid in res["open"]:
        for k in sorted(rule_dims(rules[rid])):
            if k not in full:
                found.setdefault(k, []).append(rid)
    return [{"key": k, "name": space.dims[k].get("name", k), "evidence": space.dims[k].get("evidence"),
             "decides": rids} for k, rids in sorted(found.items())]


MAX_COMBINATIONS = 256


def possible_regions(feats: dict, unknown: list[dict], space: Space, date: str | None = None) -> list[dict]:
    """1.4: skipped facts -> every region the action could land in once they are known.

    Try each value of each unknown fact (all combinations, or one fact at a time when there are
    more than MAX_COMBINATIONS) and group the results by (verdict, violated, required_next).
    Each region lists, per fact, the values that lead there; `exact` means every combination of
    those values leads there and nothing else does."""
    keys = [u["key"] for u in unknown]
    if not keys:
        return []
    values = {k: list(space.dims[k]["values"]) for k in keys}
    total = 1
    for k in keys:
        total *= len(values[k])
    if total <= MAX_COMBINATIONS:
        tries = [dict(zip(keys, combo)) for combo in itertools.product(*(values[k] for k in keys))]
        together = True
    else:
        tries = [{k: v} for k in keys for v in values[k]]
        together = False
    regions: dict[tuple, dict] = {}
    for t in tries:
        r = evaluate({**feats, **t}, space, date)
        sig = (r["verdict"], tuple(r["violated"]), tuple(r["required_next"]))
        reg = regions.setdefault(sig, {"verdict": r["verdict"], "violated": r["violated"],
                                       "required_next": r["required_next"], "when": {}, "count": 0, "_tries": []})
        reg["count"] += 1
        reg["_tries"].append(t)
        for k, v in t.items():
            reg["when"].setdefault(k, set()).add(v)
    out = []
    for reg in sorted(regions.values(), key=lambda g: VERDICT_ORDER.index(g["verdict"])):
        when = {k: [v for v in values[k] if v in reg["when"][k]] for k in keys if k in reg["when"]}
        size = 1
        for vs in when.values():
            size *= len(vs)
        exact = together and size == reg["count"]
        out.append({"verdict": reg["verdict"], "violated": reg["violated"], "required_next": reg["required_next"],
                    "when": when, "exact": exact, "count": reg["count"], "of": len(tries),
                    "examples": [] if exact else [format_code(t, space) for t in reg["_tries"][:3]]})
    return out


def clean_notes(notes: dict | None, space: Space) -> dict:
    """Notes are free text a person adds to a choice (by key) or to the whole action ("_action").
    They are carried to the output and shown, and never read by the judge."""
    out = {}
    for k, v in (notes or {}).items():
        text = str(v).strip()
        if text and (k == "_action" or k in space.dims):
            out[k] = text
    return out


def step_of(parsed: dict) -> str | None:
    return parsed["name"] or parsed["features"].get("step")


VERDICT_ORDER = ["invalid_code", "violates", "compliant_if_requirements_met", "needs_info", "compliant"]


def split_flow(code: str) -> list[list[str]]:
    """1.4: a flow is stages joined by " > " (step by step); a stage is cases joined by " & "
    (at the same time). Returns the cases of each stage, in order."""
    return [[c.strip() for c in stage.split(" & ")] for stage in code.split(" > ")]


def check_flow(code: str, space: Space, date: str | None = None, notes: dict | None = None) -> dict:
    """Cases joined by " > " (step by step) or " & " (at the same time). Each case is judged on its
    own; the flow's verdict is the first in VERDICT_ORDER that any case has. `stages` lists the case
    indexes of each stage. A single case returns check() unchanged."""
    stages = split_flow(code)
    parts = [c for stage in stages for c in stage]
    if len(parts) == 1:
        return check(code, space, date, notes)
    steps = [check(p, space, date, notes if i == 0 else None) for i, p in enumerate(parts)]
    verdict = min((s["verdict"] for s in steps), key=VERDICT_ORDER.index)
    index, idx = [], 0
    for stage in stages:
        index.append(list(range(idx, idx + len(stage))))
        idx += len(stage)
    terms = []
    if verdict != "invalid_code":
        for step in steps:
            for entry in step["terms"]:
                if entry not in terms:
                    terms.append(entry)
        terms.sort(key=lambda e: (_CITE_ORDER.index(e["status"]), e["rule"]))
    return {"flow": steps, "stages": index, "verdict": verdict, "notes": steps[0]["notes"],
            "next_actions": [dict(a, action_index=i) for i, s in enumerate(steps) for a in s["next_actions"]
                             if a["kind"] != "proceed"] or [{"kind": "proceed", "action": "Proceed: no term blocks "
                                                             "this flow as coded.", "rules": [], "terms": []}],
            "terms": terms}


def check(code: str, space: Space, date: str | None = None, notes: dict | None = None) -> dict:
    if " > " in code or " & " in code:
        raise SpaceError("this is a flow of several actions: judge it with check_flow")
    parsed = parse_code(code, space)
    feats = parsed["features"]
    if parsed["errors"]:  # never judge a code the user didn't mean: no partial results either
        res = {"verdict": "invalid_code", "violated": [], "required_next": [], "open": [],
               "statuses": {}, "out_of_scope": []}
        cost, fixes = None, []
    else:
        res = evaluate(feats, space, parsed["date"] or date)
        cost, fixes = minimal_fixes(feats, space, parsed["date"] or date)
    terms = _result_terms(feats, res, space, parsed["date"] or date)
    return {
        "next_actions": _action_terms(next_actions(feats, res, fixes, space), terms),
        "terms": terms,
        "code": format_code(feats, space, parsed["name"]),
        "reading": explain_code(feats, space),
        "errors": parsed["errors"],
        "features": feats,
        "facts": facts_in_effect(parsed, space),
        "requirements": requirements(feats, res, space),
        "unknown_facts": unknown_facts(feats, res, space),
        "possible": possible_regions(feats, unknown_facts(feats, res, space), space, parsed["date"] or date),
        **{k: res[k] for k in ("verdict", "violated", "required_next", "open", "out_of_scope")},
        "fix": fixes if isinstance(fixes, list) else [],
        "fix_note": fixes if isinstance(fixes, str) else None,
        "fix_cost": cost,
        # the action's step is its name (name: ...), or a `step` key if the bundle has one
        "step": step_of(parsed),
        "next_steps": next_steps(step_of(parsed), space),
        "similar_records": similar(feats, space),
        "notes": clean_notes(notes, space),  # shown only; nothing above depends on them
    }


# ---------------------------------------------------------------- 1.2: no-model runtime
#
# Everything below runs from the bundle files alone: a question form, keyword lookup and a fixed
# report. No model is needed to use a bundle; the model only helps to build it.


def _single_values(space: Space, key: str) -> list[str]:
    """Probe values: every single value, plus (for a multi-valued key) all values at once, so a
    rule that needs two members together is still found."""
    vals = list(space.dims[key]["values"])
    return vals + (["+".join(vals)] if space.multi(key) and len(vals) > 1 else [])


def decides(key: str, rule: dict, feats: dict, space: Space, asked: set | None = None) -> bool:
    """A key decides a rule when giving it different values gives the rule different statuses.

    Conditions come first: a key that appears only in a rule's exceptions or requirement is asked
    once the rule applies (its `when` is true), or once every key of `when` has been asked and the
    rule still may apply: its condition stays unknown and no unasked condition key would still
    change the rule's status. Never before.
    """
    if key not in rule_dims(rule):
        return False
    if key not in cond_dims(rule["when"]):
        w = eval_cond(rule["when"], feats)
        pending = [k for k in cond_dims(rule["when"]) - set(feats) - (asked or set())
                   if decides(k, rule, feats, space)]  # condition keys the form will still ask
        if not (w == T or (w == U and asked is not None and not pending)):
            return False
    return len({rule_status(rule, {**feats, key: v}) for v in _single_values(space, key)}) > 1


def suggested_value(key: str, space: Space) -> str | None:
    """The preselected answer: the dimension's default, else its most common value in history."""
    dim = space.dims[key]
    if dim.get("default") is not None:
        return dim["default"]
    freq = dim.get("frequency") or {}
    order = list(dim["values"])
    best = [v for v in order if freq.get(v, 0) > 0]
    return max(best, key=lambda v: (freq[v], -order.index(v))) if best else None


def next_question(answers: str, space: Space, date: str | None = None, notes: dict | None = None) -> dict:
    """The form's next question, or the final judgement when no unasked key decides any rule.

    answers is a code. key=? records "don't know": the key counts as asked and stays unset.
    Order: ask_order first, then the other dimensions in display order. A key is asked when it is
    unanswered, not a constant, and decides at least one active rule given the answers so far
    (defaults are not applied while asking, so context questions are still asked).
    """
    parsed = parse_code(answers, space)
    if parsed["errors"]:
        return {"done": False, "errors": parsed["errors"]}
    asked = set(parsed["features"]) | {t.split("=", 1)[0] for t in parsed["tokens"] if t.endswith("=?")}
    feats = {**space.constants(), **parsed["features"]}
    rules = active_rules(space, parsed["date"] or date)
    order = dimension_order(space)
    todo = [k for k in order if k not in asked and k not in space.constants() and k in space.dims]
    picked, decided = None, []
    applying = [r for r in rules if eval_cond(r["when"], feats) == T]
    for pool in (applying, rules):  # pass 1: rules that already apply first, then any rule
        for key in todo:  # a key that on its own changes some rule's status
            decided = [r["id"] for r in pool if decides(key, r, feats, space, asked)]
            if decided:
                picked = key
                break
        if picked is not None:
            break
    if picked is None:  # pass 2: a rule still undecided may hinge on keys that only matter together
        undecided = [r for r in rules if rule_status(r, feats) in ("open", "required_next")]
        for key in todo:
            decided = [r["id"] for r in undecided if key in rule_dims(r)
                       and (key in cond_dims(r["when"]) or eval_cond(r["when"], feats) != F)]
            if decided:
                picked = key
                break
    if picked is not None:
        key = picked
        dim = space.dims[key]
        sv = suggested_value(key, space)
        return {"done": False, "key": key, "question": dim.get("question") or dim.get("name", key),
                "multi": bool(dim.get("multi")), "evidence": dim.get("evidence"),
                "options": [{"value": v, "label": lbl, "suggested": v == sv} for v, lbl in dim["values"].items()],
                "note_allowed": True,
                "unknown_means": (f"default {key}={dim['default']} is assumed" if dim.get("default") is not None
                                  else "left unknown"),
                "decides": sorted(decided)}
    return {"done": True, "code": format_code(parsed["features"], space, parsed["name"]),
            "check": check(answers, space, date, notes)}


def dimension_order(space: Space) -> list[str]:
    """ask_order first, then the other dimensions in display order."""
    order = [k for k in space.generator.get("ask_order", []) if k in space.dims]
    return order + [d["key"] for d in space.schema["dimensions"] if d["key"] not in order]


def categories(space: Space) -> list[dict]:
    """1.4: the case map the generator page starts from: a tree of categories, as deep as the
    material needs (a branch with no `children` stays flat). Each node is {id, path, name,
    description, preset, features, catch_all, children}. A node's `preset` and `features` include
    its ancestors' (its own preset tokens apply last). A bundle without categories gets one
    catch-all node holding every key. Picking a category is never judged; only the code is."""
    cats = space.generator.get("categories") or [{"id": "any", "name": "Any action", "features": [],
                                                   "catch_all": True}]

    def node(c, parent):
        feats = list(parent["features"]) if parent else []
        own = c.get("features") or ([] if parent else dimension_order(space))
        feats += [k for k in own if k in space.dims and k not in feats]
        preset = {**(parent["_preset"] if parent else {}), **parse_code(c.get("preset", ""), space)["features"]}
        path = f"{parent['path']}/{c['id']}" if parent else c["id"]
        n = {"id": c["id"], "path": path, "name": c.get("name") or c["id"], "description": c.get("description", ""),
             "preset": format_code(preset, space), "features": feats,
             "catch_all": bool(c.get("catch_all") or (parent and parent["catch_all"])), "_preset": preset}
        n["children"] = [node(ch, n) for ch in c.get("children", [])]
        return n

    def clean(n):
        return {k: (v if k != "children" else [clean(ch) for ch in v]) for k, v in n.items() if k != "_preset"}

    return [clean(node(c, None)) for c in cats]


def category_nodes(tree: list[dict]) -> list[dict]:
    """Every node of the case map, depth first."""
    out = []
    for n in tree:
        out.append(n)
        out += category_nodes(n["children"])
    return out


def nested_under(space: Space) -> dict[str, list[str]]:
    """1.4: a dimension with `under` (a condition on one other key) is a folder inside that key's
    folder, shown once the condition is true. Returns parent key -> child keys, display order."""
    out: dict[str, list[str]] = {}
    for d in space.schema["dimensions"]:
        if d.get("under"):
            out.setdefault(d["under"]["dim"], []).append(d["key"])
    return out


def reachable(keys: list[str], feats: dict, space: Space) -> list[str]:
    """The keys a case can set from these folders: the folders themselves, plus every nested
    folder whose `under` condition is true under feats (recursively)."""
    kids = nested_under(space)
    out, todo = [], list(keys)
    while todo:
        k = todo.pop(0)
        if k in out:
            continue
        out.append(k)
        todo += [c for c in kids.get(k, []) if eval_cond(space.dims[c]["under"], feats) == T]
    return out


def _core_features(feats: dict, space: Space) -> tuple[dict, list[str]]:
    core = feats.copy()
    while True:
        remove = [k for k in core if (under := space.dims.get(k, {}).get("under"))
                  and eval_cond(under, core) != T]
        if not remove:
            return core, sorted(feats.keys() - core.keys())
        for k in remove:
            del core[k]


def _scope_effect(feats: dict, core: dict, space: Space, date: str | None = None) -> tuple[dict, dict]:
    before, after = (evaluate(f, space, date) for f in (feats, core))
    return {k: before[k] for k in _RESULT_KEYS}, {k: after[k] for k in _RESULT_KEYS}


def fit_detail(feats: dict, space: Space, date: str | None = None) -> dict:
    core, dropped = _core_features(feats, space)
    if dropped:
        before, after = _scope_effect(feats, core, space, date)
        if before != after:
            return {"category": None, "dropped": []}
    return {"category": _fit_core(core, space), "dropped": dropped}


def map_fit(feats: dict, space: Space, date: str | None = None) -> str | None:
    return fit_detail(feats, space, date)["category"]


def _fit_core(feats: dict, space: Space) -> str | None:
    """1.4: the path of the deepest non-catch-all category whose preset agrees with feats and whose
    folders (with nesting) reach every set key; None when only a catch-all fits."""
    best = None
    for n in category_nodes(categories(space)):
        if n["catch_all"]:
            continue
        preset = parse_code(n["preset"], space)["features"]
        if any(feats.get(k) != v and not (space.multi(k) and set(v.split("+")) <= set(feats.get(k, "").split("+")))
               for k, v in preset.items()):
            continue
        deeper = best is None or n["path"].count("/") > best.count("/")
        if deeper and set(feats) <= set(reachable(n["features"], feats, space)):
            best = n["path"]
    return best


def lookup(text: str, space: Space) -> list[dict]:
    """Typed words -> candidate tokens by plain phrase matching against the bundle's lexicon.

    Lowercase, split on anything that is not a letter or digit; a phrase matches where its words
    appear in order and next to each other. Results are suggestions for the person to confirm.
    """
    words = re.findall(r"[a-z0-9]+", text.lower())
    hits: dict[str, dict] = {}
    for entry in space.generator.get("lexicon", []):
        pw = re.findall(r"[a-z0-9]+", entry["phrase"].lower())
        if not pw:
            continue
        for i in range(len(words) - len(pw) + 1):
            if words[i:i + len(pw)] == pw:
                tok = entry["token"]
                if tok not in hits or i < hits[tok]["position"]:
                    hits[tok] = {"token": tok, "phrase": entry["phrase"], "position": i}
                break
    return sorted(hits.values(), key=lambda h: (h["position"], h["token"]))


DEFAULT_TEXTS = {
    "verdict": {
        "violates": "Not allowed as coded.",
        "compliant_if_requirements_met": "Allowed only if you meet every requirement under 'Must do'.",
        "needs_info": "Can't be decided yet: some facts are unknown.",
        "compliant": "Allowed as coded.",
        "invalid_code": "The code has errors. Fix it before it can be judged.",
    },
    "headings": {
        "next": "What to do next", "verdict": "Verdict", "code": "Action", "violated": "Violates",
        "must_do": "Must do", "unknown": "Still unknown", "assumed": "Assumed (confirm if it matters)",
        "fix": "Smallest fix", "usual_next": "Usually next", "similar": "Similar past records",
        "scope": "Scope warnings", "errors": "Errors", "possible": "Possible results once the unknown facts are known", "notes": "Notes (shown only, not used in judging)",
    },
    "disclaimer": "A structured self-check, not legal advice.",
}


def _term_quote(term: dict) -> str:
    label = {"exception": "Exception", "definition": "Definition", "incorporated": "Incorporated",
             "overrides": "Overrides", "subject_to": "Subject to"}.get(term["role"], "")
    if term["role"] == "exception" and "applies" in term:
        label += " (applies)" if term["applies"] else " (does not apply)"
    if label:
        label += ": "
    citation = ", ".join(x for x in (term.get("section"), term["id"]) if x)
    content = _term_content(term)
    words = _summary_label(content["summary"]) if "summary" in content else f'"{content["text"]}"'
    return f"  > {label}{words} ({citation})"


def render_report(result: dict, space: Space, *, _defined: set[str] | None = None) -> str:
    """A fixed-layout report, filled only from the judge output and the bundle's own text."""
    if _defined is None:
        _defined = set()
    if "flow" in result:  # one report per action, in order, under a numbered heading
        texts = {**DEFAULT_TEXTS["verdict"], **space.judge.get("texts", {}).get("verdict", {})}
        parts = [f"# Flow of {len(result['flow'])} actions: {texts.get(result['verdict'], result['verdict'])}"]
        stages = result.get("stages") or [[i] for i in range(len(result["flow"]))]
        together = any(len(stage) > 1 for stage in stages)
        for n, stage in enumerate(stages, 1):
            if together:  # step headings only when some actions happen at the same time
                parts.append(f"\n# Step {n}" + (f": {len(stage)} actions at the same time" if len(stage) > 1 else ""))
            for i in stage:
                step = result["flow"][i]
                parts.append(f"\n# Action {i + 1}" + (f": {step['step']}" if step.get("step") else ""))
                parts.append(render_report(step, space, _defined=_defined))
        return "\n".join(parts)
    texts = {**DEFAULT_TEXTS, **space.judge.get("texts", {})}
    verdicts = {**DEFAULT_TEXTS["verdict"], **texts.get("verdict", {})}
    h = {**DEFAULT_TEXTS["headings"], **texts.get("headings", {})}
    rules = {r["id"]: r for r in space.rules["rules"]}

    def title(rid):
        r = rules.get(rid, {})
        return f"{r['title']} [{rid}]" if r.get("title") else rid

    citations = {e["rule"]: [t for t in e["terms"] if t.get("text") or t.get("summary")]
                 for e in result.get("terms", [])}

    def rule_item(rid, suffix=""):
        return "\n".join([title(rid) + suffix,
                          *(_term_quote(t) for t in citations.get(rid, []) if t["role"] != "definition")])

    def status_items(status):
        return [rule_item(e["rule"]) for e in result.get("terms", [])
                if e["status"] == status and citations[e["rule"]]]

    out = [f"## {h['next']}"]
    out += [f"{i}. {a['action']}" + (f" [{a['code_change']}]" if a.get("code_change") else "")
            + (" (" + ", ".join(a["terms"]) + ")" if a.get("terms") else "")
            for i, a in enumerate(result["next_actions"], 1)]
    out += ["", f"**{h['verdict']}:** {verdicts.get(result['verdict'], result['verdict'])}",
            f"**{h['code']}:** `{result['code']}`: {result['reading']}"]
    sections = [
        (h["errors"], [f"`{e['token']}`: {e['message']}" + (f" (did you mean `{e['did_you_mean']}`?)"
                       if e.get("did_you_mean") else "") for e in result["errors"]]),
        (h["violated"], [rule_item(r) for r in result["violated"]]),
        (h["must_do"], [rule_item(r["rule"], f": {r['next_step']}") if r["status"] == "required_next"
                       else f"{title(r['rule'])}: {r['next_step']}" for r in result.get("requirements", [])]),
        ("Rules still open", status_items("open")),
        ("Excepted", status_items("excepted")),
        (h["unknown"], [f"{u['name']} (`{u['key']}=?`). Evidence: {u['evidence']}"
                        for u in result.get("unknown_facts", [])]),
        (h["possible"], [_region_line(g, verdicts, title) for g in result.get("possible", [])]),
        (h["assumed"], [f"`{f['key']}={f['value']}`" for f in result.get("facts", []) if f.get("assumed")]),
        (h["fix"], [", ".join(f) for f in result["fix"]] or ([result["fix_note"]] if result.get("fix_note") else [])),
        (h["usual_next"], [f"{n['step']} ({round(n['share'] * 100)}%)" for n in result["next_steps"]]),
        (h["similar"], [f"{r['id']}: {r['outcome']} (`{r['code']}`)" for r in result["similar_records"]]),
        (h["scope"], result["out_of_scope"]),
        (h["notes"], [(f"`{k}`: " if k != "_action" else "") + v
                      for k, v in sorted(result.get("notes", {}).items(), key=lambda kv: kv[0] != "_action")]),
    ]
    for head, items in sections:
        if items:
            out += ["", f"**{head}**"] + [f"- {x}" for x in items]
    definitions = []
    for entries in citations.values():
        for t in entries:
            if t["role"] == "definition" and t["id"] not in _defined:
                definitions.append(_term_quote(t))
                _defined.add(t["id"])
    if definitions:
        out += ["", "**Terms used**", *definitions]
    out += ["", f"_{texts.get('disclaimer', DEFAULT_TEXTS['disclaimer'])}_"]
    return "\n".join(out)


def _region_line(g: dict, verdicts: dict, title) -> str:
    if g["exact"]:
        cond = " and ".join(" or ".join(f"`{k}={v}`" for v in vs) for k, vs in g["when"].items())
    else:
        more = g["count"] - len(g["examples"])
        cond = "one of " + ", ".join(f"`{c}`" for c in g["examples"]) + (f" (+{more} more)" if more > 0 else "")
    line = f"If {cond} ({g['count']} of {g['of']}): {verdicts.get(g['verdict'], g['verdict'])}"
    if g["violated"]:
        line += " Violates: " + ", ".join(title(r) for r in g["violated"]) + "."
    if g["required_next"]:
        line += " Must do: " + ", ".join(title(r) for r in g["required_next"]) + "."
    return line


def run_form(space: Space, date: str | None = None, inp=input, out=print) -> dict:
    """Interactive question form on a terminal.

    Answer with a value (a+b for several), ? for don't know, Enter for the suggestion. Add a note
    after "#" (for example: ins # only claims under 500). Notes are shown, never judged.
    """
    answers: list[str] = []
    notes: dict[str, str] = {}

    def finish():
        try:
            action_note = inp("note for the whole action (optional)> ").strip()
        except EOFError:
            action_note = ""
        if action_note:
            notes["_action"] = action_note
        res = check(" ".join(answers), space, date, notes)
        out(render_report(res, space))
        return {"done": True, "code": res["code"], "check": res}

    while True:
        q = next_question(" ".join(answers), space, date)
        if q.get("done"):
            return finish()
        opts = ", ".join(f"{o['value']}={o['label']}" + (" [suggested]" if o["suggested"] else "") for o in q["options"])
        out(f"{q['question']}\n  {opts}\n  ? = don't know ({q['unknown_means']}); add a note after #")
        while True:
            try:
                raw = inp(f"{q['key']}> ").strip()
            except EOFError:  # input ended: judge what was answered so far
                return finish()
            raw, _, note = raw.partition("#")
            raw, note = raw.strip(), note.strip()
            if raw == "" and any(o["suggested"] for o in q["options"]):
                raw = next(o["value"] for o in q["options"] if o["suggested"])
            tok = f"{q['key']}={raw}"
            if not parse_code(tok, space)["errors"]:
                answers.append(tok)
                if note:
                    notes[q["key"]] = note
                break
            out(f"  not a valid answer: {parse_code(tok, space)['errors'][0]['message']}")


# ---------------------------------------------------------------- self-exam (build time, no model)
#
# Before a bundle ships it takes an exam modelled on ordinary software testing practice:
# coverage (coverage.py), replay against history (golden / regression tests), mutation testing
# (mutmut, PIT, Stryker), property tests (Hypothesis, QuickCheck), and a pass/fail badge in
# bundle.json (a CI badge). Everything is plain computation over the bundle files.

EXAM_THRESHOLDS = {"terms_coverage": 1.0, "rule_coverage": 1.0, "replay_agreement": 0.9,
                   "replay_decisive": 0.8, "mutation_score": 0.8, "map_fit": 0.8, "references": 1.0}
EXAM_SAMPLES = 300
_BUCKET = {"violated": "violated", "required_next": "required_next", "open": "open",
           "satisfied": "-", "not_applicable": "-"}


def _exam_item(check_id: str, name: str, score: float | None, threshold: float | None, failures: list,
               detail: str | dict = "", skipped: bool = False) -> dict:
    passed = skipped or (not failures if threshold is None else (score is not None and score >= threshold))
    return {"id": check_id, "name": name, "passed": passed, "skipped": skipped,
            "score": None if score is None else round(score, 4), "threshold": threshold,
            "detail": detail, "failures": failures, "failure_count": len(failures)}


def _conformance_feats(root: Path, sp: Space) -> list[tuple[dict, str | None]]:
    out = []
    for line in _conformance(root):
        pc = parse_code(line["code"], sp)
        if not pc["errors"]:
            out.append(({**sp.defaults(), **sp.constants(), **pc["features"]}, pc["date"] or line.get("date")))
    return out


def exam_terms(root: Path, sp: Space, th: dict) -> dict:
    """Coverage of the terms: every normative clause (high or medium confidence) that isn't out of
    scope is cited by a rule's source_terms, or listed in judge.json not_encoded with a reason."""
    path = root / "work" / "terms.json"
    if not path.exists():
        return _exam_item("terms_coverage", "Every real term becomes a rule (or says why not)", None,
                          th["terms_coverage"], [], "no work/terms.json: skipped", skipped=True)
    clauses = json.loads(path.read_text(encoding="utf-8")).get("clauses", [])
    scope = _read_json(root / "work" / "scope.json", {})
    out_of_scope = {c for a in scope.get("areas", []) if a.get("decision") == "out_of_scope"
                    for c in a.get("source_terms", [])}
    cited = {c for r in sp.rules["rules"] for c in r.get("source_terms", [])}
    excused = {e.get("clause") for e in sp.judge.get("not_encoded", [])
               if e.get("reason") and "ref" not in e}
    due = [c for c in clauses if c.get("class") in NORMATIVE and c.get("confidence") in ("high", "medium")
           and c.get("id") not in out_of_scope]
    missing = [f"{c['id']}: {str(c.get('text', ''))[:90]}" for c in due if c["id"] not in cited | excused]
    score = 1.0 if not due else (len(due) - len(missing)) / len(due)
    return _exam_item("terms_coverage", "Every real term becomes a rule (or says why not)", score,
                      th["terms_coverage"], missing, f"{len(due) - len(missing)}/{len(due)} normative clauses covered")


def _reference_clauses(root: Path, sp: Space) -> dict | None:
    """Read working terms first; a shipped bundle carries its own reference graph."""
    path = root / "work" / "terms.json"
    if not path.exists():
        terms = sp.judge.get("terms")
        return terms if isinstance(terms, dict) and terms else None
    doc = _read_json(path)
    clauses = {c["id"]: c for c in doc.get("clauses", [])}
    if doc.get("glossary") or any(c.get("refs") for c in clauses.values()):
        return clauses
    return None


def _reference_failures(clauses: dict, sp: Space) -> tuple[int, list[dict]]:
    """Count the links each cited clause needs a rule to account for, including backlinks."""
    outgoing = {"incorporates", "overrides", "subject_to"}
    incoming = {"modifies", "overrides", "subject_to"}
    by_clause: dict = {ident: [] for ident in clauses}
    for ident, clause in sorted(clauses.items()):
        for ref in clause_refs(clause):
            target, kind = ref.get("to"), ref.get("kind")
            if kind == "none":
                continue
            if target is None or kind in outgoing:
                by_clause[ident].append((target, ref))
            if target in clauses and kind in incoming:
                by_clause[target].append((ident, ref))

    excused = [e for e in sp.judge.get("not_encoded", []) if str(e.get("reason") or "").strip()]
    excused_clauses = {e.get("clause") for e in excused if "ref" not in e}
    excused_refs = {(e.get("clause"), e["ref"]) for e in excused if isinstance(e.get("ref"), str)}
    total = 0
    failures = []
    for rule in sorted(sp.rules["rules"], key=lambda r: r["id"]):
        cited = set(rule.get("source_terms", []))
        for ident in sorted(cited):
            for target, ref in by_clause.get(ident, []):
                total += 1
                phrase = ref.get("text", "")
                if target is None:
                    covered = ident in excused_clauses or (ident, phrase) in excused_refs
                else:
                    covered = target in cited or target in excused_clauses
                if not covered:
                    failures.append({"rule": rule["id"], "clause": ident, "linked_clause": target,
                                     "kind": ref["kind"], "phrase": phrase})
    return total, failures


def _reference_failure_text(evidence: dict) -> str:
    rule, clause, target, kind, phrase = (
        evidence[k] for k in ("rule", "clause", "linked_clause", "kind", "phrase")
    )
    link = f"{kind}: {phrase}" if phrase else kind
    return f"{rule}: {clause} needs {target or 'an unresolved target'} ({link})"


def exam_references(root: Path, sp: Space, th: dict) -> dict:
    name = "Rules account for referenced clauses"
    clauses = _reference_clauses(root, sp)
    if clauses is None:
        return _exam_item("references", name, None, th["references"], [], "no refs or glossary", skipped=True)
    total, evidence = _reference_failures(clauses, sp)
    n = total - len(evidence)
    return _exam_item("references", name, n / total if total else 1.0, th["references"],
                      [_reference_failure_text(item) for item in evidence],
                      f"{n}/{total} links accounted for")


def exam_rules(root: Path, sp: Space, th: dict) -> dict:
    """Coverage of the rules: every rule fires in some conformance vector, and every rule that can
    pass (an obligation, or a prohibition with exceptions) is also seen passing."""
    fired: set[str] = set()
    passed: set[str] = set()
    vectors = _conformance_feats(root, sp)
    for feats, date in vectors:
        for r in active_rules(sp, date):
            st = rule_status(r, feats)
            if st in ("violated", "required_next"):
                fired.add(r["id"])
            elif st == "satisfied":
                passed.add(r["id"])
    gaps = []
    for r in sp.rules["rules"]:
        if r["id"] not in fired:
            gaps.append(f"{r['id']}: no test case triggers it")
        if (r["kind"] == "obligation" or r.get("exceptions")) and r["id"] not in passed:
            gaps.append(f"{r['id']}: no test case shows it met / excepted")
    need = sum(1 + (r["kind"] == "obligation" or bool(r.get("exceptions"))) for r in sp.rules["rules"])
    score = 1.0 if not need else (need - len(gaps)) / need
    return _exam_item("rule_coverage", "Every rule is tested on both sides", score, th["rule_coverage"], gaps,
                      f"{need - len(gaps)}/{need} rule sides covered by {len(vectors)} test cases")


def exam_replay(sp: Space, th: dict) -> list[dict]:
    """Replay the past records: judge each and compare with its recorded outcome via the
    generator's outcome_map (outcome -> acceptable verdicts). A disagreement must carry a written
    reason (the record's `disagreement`). Also: how many records the bundle can decide at all."""
    omap = sp.generator.get("outcome_map", {})
    recs = [r for r in sp.history if r.get("code")]
    if not recs:
        skip = "no records.jsonl: skipped"
        return [_exam_item("replay_agreement", "Past decisions replay the same way", None,
                           th["replay_agreement"], [], skip, skipped=True),
                _exam_item("replay_decisive", "Past records get a definite answer", None,
                           th["replay_decisive"], [], skip, skipped=True)]
    agree, mapped, decisive, unexplained, undecided = 0, 0, 0, [], []
    for rec in recs:
        res = check_flow(rec["code"], sp)
        if res["verdict"] not in ("needs_info", "invalid_code"):
            decisive += 1
        else:
            undecided.append(f"{rec.get('id')}: {res['verdict']} ({rec['code']})")
        ok = omap.get(str(rec.get("outcome")))
        if ok is None:
            continue
        mapped += 1
        if res["verdict"] in ok:
            agree += 1
        elif not rec.get("disagreement"):
            unexplained.append(f"{rec.get('id')}: recorded {rec.get('outcome')}, judged {res['verdict']} "
                               f"({rec['code']}); add a 'disagreement' reason or fix the bundle")
    items = []
    if not mapped:
        items.append(_exam_item("replay_agreement", "Past decisions replay the same way", None,
                                th["replay_agreement"], ["generator.json has no outcome_map for the records' outcomes"],
                                "cannot compare outcomes"))
    else:
        score = agree / mapped
        fails = unexplained + ([] if score >= th["replay_agreement"] else
                               [f"agreement {agree}/{mapped} is below {th['replay_agreement']}"])
        item = _exam_item("replay_agreement", "Past decisions replay the same way", score, th["replay_agreement"],
                          fails, f"{agree}/{mapped} records agree; {mapped - agree - len(unexplained)} "
                                 "disagreements explained")
        item["passed"] = item["passed"] and not unexplained
        items.append(item)
    items.append(_exam_item("replay_decisive", "Past records get a definite answer", decisive / len(recs),
                            th["replay_decisive"], undecided if decisive / len(recs) < th["replay_decisive"] else [],
                            f"{decisive}/{len(recs)} records decided (not needs_info)"))
    return items


def _mutants(rule: dict):
    """Small deliberate faults: drop one condition, flip one in/not_in, drop one exception,
    negate the requirement."""
    def leaves(cond, path=()):
        if "all" in cond or "any" in cond:
            key = "all" if "all" in cond else "any"
            for i, c in enumerate(cond[key]):
                yield from leaves(c, (*path, key, i))
        elif "not" in cond:
            yield from leaves(cond["not"], (*path, "not"))
        else:
            yield path

    def get(cond, path):
        for p in path:
            cond = cond[p]
        return cond

    def swap(cond, path, new):
        if not path:
            return new
        cond = json.loads(json.dumps(cond))
        parent = get(cond, path[:-1])
        parent[path[-1]] = new
        return cond

    when = rule["when"]
    for path in leaves(when):
        leaf = get(when, path)
        flipped = ({"dim": leaf["dim"], "not_in": leaf["in"]} if "in" in leaf
                   else {"dim": leaf["dim"], "in": leaf.get("not_in", [])})
        yield f"flip {leaf['dim']} in when", {**rule, "when": swap(when, path, flipped)}
        if len(path) >= 2 and path[-2] in ("all", "any"):
            parent = get(when, path[:-2])
            key = path[-2]
            rest = [c for i, c in enumerate(parent[key]) if i != path[-1]]
            if rest:
                yield f"drop {leaf['dim']} from when", {**rule, "when": swap(when, path[:-1], rest)}
    for i, _ in enumerate(rule.get("exceptions", [])):
        yield f"drop exception {i + 1}", {**rule, "exceptions": [e for j, e in enumerate(rule["exceptions"]) if j != i]}
    if rule.get("require"):
        yield "negate require", {**rule, "require": {"not": rule["require"]}}


def exam_mutation(root: Path, sp: Space, th: dict) -> dict:
    """Mutation testing: break each rule a little and check that some test case notices."""
    vectors = _conformance_feats(root, sp)
    total, survivors = 0, []
    for rule in sp.rules["rules"]:
        base = [(_BUCKET[rule_status(rule, f)], d) for f, d in vectors]
        for label, mutant in _mutants(rule):
            total += 1
            killed = False
            for (bucket, date), (feats, _) in zip(base, vectors, strict=True):
                if (rule.get("effective_from") or "0000-00-00") > (date or "9999-99-99"):
                    continue
                if _BUCKET[rule_status(mutant, feats)] != bucket:
                    killed = True
                    break
            if not killed:
                survivors.append(f"{rule['id']}: '{label}' is not caught by any test case")
    score = 1.0 if not total else (total - len(survivors)) / total
    return _exam_item("mutation_score", "Tests catch a broken rule", score, th["mutation_score"], survivors,
                      f"{total - len(survivors)}/{total} mutants caught")


def _sample_codes(sp: Space, n: int, seed: int = 0) -> list[str]:
    rng = random.Random(seed)
    keys = [d["key"] for d in sp.schema["dimensions"]]
    codes = []
    for _ in range(n):
        toks = []
        for k in keys:
            if rng.random() < 0.5:
                vals = list(sp.dims[k]["values"])
                toks.append(f"{k}={rng.choice(vals)}")
        codes.append(" ".join(toks))
    return codes


def _lists(res: dict) -> tuple:
    return res["verdict"], tuple(res["violated"]), tuple(res["required_next"]), tuple(res["open"])


def exam_properties(sp: Space, n: int = EXAM_SAMPLES) -> dict:
    """Property tests over generated codes: things that must always hold."""
    fails: list[str] = []
    for code in _sample_codes(sp, n):
        a, b = check(code, sp), check(code, sp)
        if a != b:
            fails.append(f"same code, different result: {code}")
        noted = check(code, sp, notes={"_action": "property-test note"})
        if {k: v for k, v in noted.items() if k != "notes"} != {k: v for k, v in a.items() if k != "notes"}:
            fails.append(f"a note changed the result: {code}")
        feats = parse_code(code, sp)["features"]
        before = evaluate(feats, sp)
        for k in sp.dims:  # knowing more never un-decides a rule (defaults aside: they are assumptions)
            if k in feats or k in sp.defaults() or k in sp.constants():
                continue
            for v in list(sp.dims[k]["values"])[:3]:
                after = evaluate({**feats, k: v}, sp)
                ok = {"violated": ("violated",), "satisfied": ("satisfied", "not_applicable"),
                      "not_applicable": ("satisfied", "not_applicable")}
                changed = [r for r, st in before["statuses"].items() if st in ok and after["statuses"][r] not in ok[st]]
                if changed:
                    fails.append(f"filling {k}={v} changed decided rules {changed}: {code}")
        if a["fix"]:  # the suggested fix really fixes
            trial = dict(feats)
            for change in a["fix"][0]:
                tok = change.split("->")[-1].strip().lstrip("+")
                key, val = tok.split("=", 1)
                trial[key] = val
            res = evaluate(trial, sp)
            if res["violated"] or res["required_next"]:
                fails.append(f"the suggested fix leaves rules unmet: {code}")
        answers: list[str] = []  # the form, answered from this vector, agrees with the typed code
        for _ in range(len(sp.dims) + 1):
            q = next_question(" ".join(answers), sp)
            if q.get("done"):
                if _lists(q["check"]) != _lists(evaluate({**feats}, sp)):
                    fails.append(f"form and typed code disagree: {code}")
                break
            answers.append(f"{q['key']}={feats.get(q['key'], '?')}")
        if len(fails) > 50:
            break
    return _exam_item("properties", "Properties hold on generated actions", None, None, fails,
                      f"{n} generated codes: determinism, notes ignored, knowing more never un-decides, "
                      "fixes fix, form = typed code")


def exam_map(root: Path, sp: Space, th: dict) -> dict:
    """1.4: the case map should match the cases it was built from. Every past record and test code
    should fit a category below the catch-all (its preset agrees, its folders reach every key)."""
    name = "Cases fit the case map below the catch-all"
    if sp.version() < (1, 4):
        return _exam_item("map_fit", name, None, th["map_fit"], [], "format before 1.4", skipped=True)
    rows = [r for r in sp.history if r.get("code")] + _conformance(root)
    fits = []
    for row in rows:
        c = row["code"]
        p = parse_code(c, sp)
        if not p["errors"] and p["features"]:
            fits.append((c, p["features"], fit_detail(p["features"], sp, p["date"] or row.get("date"))))
    if not fits:
        return _exam_item("map_fit", name, None, th["map_fit"], [], "no records or test codes", skipped=True)
    misses = [c for c, _, fit in fits if fit["category"] is None]
    score = 1 - len(misses) / len(fits)
    detail = {"fits_after_drop": [{"code": c, "dropped": fit["dropped"]} for c, f, fit in fits
                                  if fit["category"] is not None and fit["dropped"] and _fit_core(f, sp) is None]}
    return _exam_item("map_fit", name, score, th["map_fit"], misses if score < th["map_fit"] else [],
                      detail)


def exam(root: Path, write: bool = True, *, full_failures: bool = False,
         check_manifest: bool = True) -> dict:
    """The bundle's self-exam. Writes work/exam.json; write_manifest copies the result into bundle.json."""
    sp = Space(root)
    th = {**EXAM_THRESHOLDS}
    custom = sp.generator.get("exam", {})
    if custom:
        th.update({k: float(v) for k, v in custom.items() if k in EXAM_THRESHOLDS})
    checks = [exam_terms(root, sp, th), exam_references(root, sp, th), exam_rules(root, sp, th), *exam_replay(sp, th),
              exam_mutation(root, sp, th), exam_properties(sp), exam_map(root, sp, th)]
    # The finished badge generates the guide; check the shipped bytes, not a newly rendered guide.
    errs = validate(root, check_guide=False, check_manifest=check_manifest)
    checks.insert(0, _exam_item("validate", "Schema and conformance tests pass", None, None, errs,
                                "pac.py validate"))
    if custom and not custom.get("reason"):
        checks.append(_exam_item("thresholds", "Lowered thresholds give a reason", None, None,
                                 ["generator.json 'exam' changes thresholds without a 'reason'"]))
    result = {"passed": all(c["passed"] for c in checks), "engine_spec": SPEC_VERSION,
              "thresholds": th, "files": _bundle_digests(root),
              "date": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "checks": checks}
    if not full_failures:
        for item in checks:
            item["failures"] = item["failures"][:20]
    if write:
        (root / "work").mkdir(parents=True, exist_ok=True)
        (root / "work" / "exam.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


_BUNDLE_FILES = ("generator.json", "judge.json", "conformance.jsonl", "records.jsonl")


def _bundle_bytes(root: Path, names: tuple[str, ...] = _BUNDLE_FILES) -> dict[str, bytes]:
    return {name: (root / name).read_bytes() for name in names if (root / name).is_file()}


def _bundle_digests(root: Path) -> dict:
    return {name: hashlib.sha256(data).hexdigest() for name, data in _bundle_bytes(root).items()}


def _crlf_hint(name: str) -> str:
    return (f"{name}: line endings changed (CRLF), probably a Windows checkout; "
            "re-checkout with LF (see .gitattributes) or run pac.py manifest")


def verify_manifest(manifest: dict, files: dict[str, bytes]) -> dict:
    """Compare raw bytes; normalization is only a diagnostic for otherwise mismatched files."""
    mismatched, crlf_only = [], []
    for name, digest in manifest.get("files", {}).items():
        if (data := files.get(name)) is None or hashlib.sha256(data).hexdigest() == digest:
            continue
        mismatched.append(name)
        if hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest() == digest:
            crlf_only.append(name)
    badge = manifest.get("exam")
    return {"digests_ok": not mismatched, "mismatched": mismatched, "crlf_only": crlf_only,
            "exam": badge, "exam_passed": bool(badge and badge.get("passed"))}


def exam_badge(root: Path) -> dict | None:
    """The exam result for bundle.json, only if it was taken on exactly the current files."""
    res = _read_json(root / "work" / "exam.json", {})
    if not res or res.get("files") != _bundle_digests(root):
        return None
    return {"passed": res["passed"], "date": res["date"], "engine_spec": res["engine_spec"],
            "scores": {c["id"]: c["score"] for c in res["checks"] if c["score"] is not None},
            "failed": [c["id"] for c in res["checks"] if not c["passed"]],
            "checks": [{k: c[k] for k in ("id", "score", "threshold", "passed", "skipped") if k in c}
                       for c in res["checks"]]}


# ---------------------------------------------------------------- 1.5: deterministic refinement

_RESULT_KEYS = ("verdict", "violated", "required_next", "open")
_PATCH_FIELDS = {
    "add_category": ("parent", "node"), "set_under": ("dim", "under"),
    "add_value": ("dim", "value", "meaning"), "add_dimension": ("dimension",),
    "add_rule": ("rule",), "add_exception": ("rule", "exception"), "add_vector": ("line",),
    "add_lexicon": ("entry",), "add_saved_code": ("name", "code"),
    "not_encoded": ("clause",), "record_disagreement": ("record",),
    "revise_rule": ("rule", "changes", "expected_changes"),
}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[^\W_]+", text.lower()))


def _code_features(code: str, sp: Space) -> dict:
    return {k: v for stage in split_flow(code) for part in stage
            for k, v in parse_code(part, sp)["features"].items()}


def _nearest(feats: dict, sp: Space, nodes: list[dict]) -> dict | None:
    options = []
    for node in nodes:
        if node["catch_all"]:
            continue
        preset = parse_code(node["preset"], sp)["features"]
        conflicts = [k for k, v in preset.items()
                     if feats.get(k) != v and not (sp.multi(k) and
                         set(v.split("+")) <= set(feats.get(k, "").split("+")))]
        offered = set(reachable(node["features"], feats, sp))
        options.append({"path": node["path"], "preset_conflicts": conflicts,
                        "unreachable": [k for k in feats if k not in offered]})
    return min(options, key=lambda n: len(n["preset_conflicts"]) + len(n["unreachable"]), default=None)


def gaps(root: Path, focus: str | None = None, *, write: bool = False,
         allow_empty: bool = False, check_manifest: bool = True) -> dict:
    """Turn check failures and every map miss into ordered, actionable records."""
    files = _bundle_digests(root)
    result = exam(root, write=False, full_failures=True, check_manifest=check_manifest)
    sp = Space(root)
    manifest = _read_json(root / "bundle.json", {})
    checks = {c["id"]: c["failures"] for c in result["checks"]}
    clauses = {c["id"]: c for c in _read_json(root / "work" / "terms.json", {}).get("clauses", [])}
    rules = {r["id"]: r for r in sp.rules["rules"]}
    records = {str(r.get("id")): r for r in sp.history}
    nodes = category_nodes(categories(sp))
    items: list[dict] = []

    def add(kind, ident, keys, detail, evidence):
        items.append({"kind": kind, "id": f"{kind}:{ident}", "keys": [k for k in sp.dims if k in keys],
                      "detail": detail, "evidence": evidence})

    for detail in checks["terms_coverage"]:
        ident = detail.partition(": ")[0]
        clause = clauses[ident]
        touched = {k for r in rules.values() if ident in r.get("source_terms", []) for k in rule_dims(r)}
        touched.update(h["token"].split("=", 1)[0] for h in lookup(clause["text"], sp))
        add("uncovered_clause", ident, touched, detail,
            {"id": ident, "text": clause["text"], "class": clause["class"]})
    if checks["references"]:
        _, references = _reference_failures(_reference_clauses(root, sp) or {}, sp)
        for i, evidence in enumerate(references, 1):
            rule, clause = evidence["rule"], evidence["clause"]
            add("unresolved_reference", f"{rule}:{clause}:{i}", rule_dims(rules[rule]),
                _reference_failure_text(evidence), evidence)
    for detail in checks["rule_coverage"]:
        ident, _, why = detail.partition(": ")
        missing = "satisfied" if "shows it met" in why else (
            "required_next" if rules[ident]["kind"] == "obligation" else "violated")
        add("untested_rule", f"{ident}:{missing}", rule_dims(rules[ident]), detail,
            {"rule": ident, "missing_status": missing})
    for check_id, kind in (("replay_agreement", "replay_disagreement"), ("replay_decisive", "undecided_record")):
        for detail in checks[check_id]:
            ident = detail.partition(": ")[0]
            rec = records.get(ident)
            if not rec:
                # An aggregate failure is reflected by exam_passed; it is not a record.
                continue
            code = rec["code"]
            res = check_flow(code, sp)
            keys = set(_code_features(code, sp))
            if kind == "undecided_record":
                unknown = list(dict.fromkeys(f["key"] for s in res.get("flow", [res])
                                             for f in s.get("unknown_facts", [])))
                keys.update(unknown)
                evidence = {"id": ident, "code": code, "unknown": unknown}
            else:
                for step in res.get("flow", [res]):
                    for rid in (*step["violated"], *step["required_next"], *step["open"]):
                        keys.update(rule_dims(rules[rid]))
                evidence = {"id": ident, "code": code, "outcome": rec.get("outcome"), "verdict": res["verdict"]}
            add(kind, ident, keys, detail, evidence)
    for detail in checks["mutation_score"]:
        ident, _, description = detail.partition(": ")
        label = description.removesuffix(" is not caught by any test case").strip("'")
        add("surviving_mutant", f"{ident}:{label}", rule_dims(rules[ident]), detail,
            {"rule": ident, "mutant": label})
    for i, detail in enumerate(checks["properties"], 1):
        prop, _, code = detail.partition(": ")
        add("property_failure", i, _code_features(code, sp), detail, {"code": code, "property": prop})
    rows = [("record", str(r.get("id")), r) for r in sp.history if r.get("code")]
    rows += [("conformance", str(i), row) for i, row in enumerate(_conformance(root), 1)]
    by_path = {n["path"]: n for n in nodes}
    effects = []
    for source, ident, row in rows:
        code = row["code"]
        parts = [p for stage in split_flow(code) for p in stage]
        misses = []
        for i, part in enumerate(parts, 1):
            parsed = parse_code(part, sp)
            if parsed["errors"]:
                continue
            feats = parsed["features"]
            core, dropped = _core_features(feats, sp)
            if dropped:
                before, after = _scope_effect(feats, core, sp, parsed["date"] or row.get("date"))
                if before != after:
                    keys = set(dropped) | {sp.dims[k]["under"]["dim"] for k in dropped}
                    suffix = f":{i}" if len(parts) > 1 else ""
                    effects.append((f"{source}:{ident}{suffix}", keys, {
                        "code": part, "dropped": dropped, "original": before, "core": after,
                    }))
                    continue
            if _fit_core(core, sp) is None:
                misses.append(core)
        if misses:
            # For a flow, report one gap for the row, with the first unmatched case's nearest node.
            add("map_miss", f"{source}:{ident}", set().union(*misses), "No non-catch-all category fits",
                {"code": code, "nearest": _nearest(misses[0], sp, nodes)})

    for ident, keys, evidence in effects:
        add("out_of_scope_effect", ident, keys, "Dropping out-of-scope keys changes the judgment", evidence)

    words = _words(focus or "")
    focus_keys = {h["token"].split("=", 1)[0] for h in lookup(focus or "", sp)}
    for k, d in sp.dims.items():
        meanings = " ".join(str(v) for v in d["values"].values())
        if words & _words(f"{d.get('name', '')} {meanings}"):
            focus_keys.add(k)
    if focus is not None:
        def matches(item):
            if focus_keys.intersection(item["keys"]):
                return True
            nearest = item["evidence"].get("nearest")
            if not nearest:
                return False
            path = nearest["path"].split("/")
            return any(words & _words(by_path["/".join(path[:i])]["name"]) for i in range(1, len(path) + 1))

        items = [item for item in items if matches(item)]
        if not items and not focus_keys and not allow_empty:
            raise SpaceError("unmatched focus words: " + (", ".join(sorted(words)) or "(none)"))
    report = {"bundle": manifest.get("name") or root.name, "focus": focus,
              "focus_keys": [k for k in sp.dims if k in focus_keys],
              "exam_passed": result["passed"], "files": files, "gaps": items}
    if write:
        (root / "work").mkdir(exist_ok=True)
        (root / "work/gaps.json").write_bytes(_json_bytes(report))
    return report


def _json_bytes(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _json_lines(rows) -> bytes:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")


def _patch_ops(patch, report, gen, judge, vectors, records):
    """Apply the patch language to freshly loaded objects, never the live files."""
    valid_ids = {g["id"] for g in report["gaps"]}
    ops = patch.get("ops")
    if not isinstance(ops, list):
        raise SpaceError("refine_patch.json needs an ops list")
    expected = {}
    counts = Counter()

    def find(items, field, value, noun):
        for item in items:
            if item.get(field) == value:
                return item
        raise SpaceError(f"unknown {noun} '{value}'")

    def reason(text):
        return isinstance(text, str) and bool(text.strip())

    for op in ops:
        kind = op.get("op") if isinstance(op, dict) else None
        if not isinstance(kind, str) or kind not in _PATCH_FIELDS:
            raise SpaceError(f"unknown refine op {kind!r}")
        missing = [f for f in _PATCH_FIELDS[kind] if f not in op]
        if missing:
            raise SpaceError(f"{kind}: missing {', '.join(missing)}")
        if not reason(op.get("reason")):
            raise SpaceError(f"{kind}: needs a reason")
        refs = op.get("gaps")
        if not isinstance(refs, list) or not refs or any(not isinstance(g, str) or g not in valid_ids for g in refs):
            raise SpaceError(f"{kind}: gaps must reference ids in work/gaps.json")
        counts[kind] += 1
        if kind == "add_category":
            siblings = gen.setdefault("categories", [])
            if op["parent"]:
                for part in op["parent"].split("/"):
                    siblings = find(siblings, "id", part, "category").setdefault("children", [])
            siblings.append(op["node"])
        elif kind in ("set_under", "add_value"):
            dim = find(gen["dimensions"], "key", op["dim"], "dimension")
            if kind == "set_under":
                if dim.get("under") is not None:
                    raise SpaceError(f"dimension {dim['key']} already has 'under'")
                if not isinstance(op["under"], dict) or not op["under"]:
                    raise SpaceError(f"dimension {dim['key']}: invalid 'under'")
                dim["under"] = op["under"]
            else:
                if op["value"] in dim["values"]:
                    raise SpaceError(f"dimension {dim['key']}: duplicate value '{op['value']}'")
                dim["values"][op["value"]] = op["meaning"]
                dim.setdefault("frequency", {})[op["value"]] = 0
        elif kind == "add_dimension":
            dim = op["dimension"]
            if not dim.get("question") or not dim.get("evidence"):
                raise SpaceError("add_dimension needs 'question' and 'evidence'")
            gen["dimensions"].append(dim)
        elif kind == "add_rule":
            rule = op["rule"]
            if not rule.get("source"):
                raise SpaceError(f"{rule.get('id')}: every rule must cite its source")
            for field in ("source_terms", "next_step", "title"):
                if not rule.get(field):
                    raise SpaceError(f"{rule.get('id')}: every rule needs '{field}'")
            judge["rules"].append(rule)
        elif kind in ("add_exception", "revise_rule"):
            rule = find(judge["rules"], "id", op["rule"], "rule")
            if kind == "add_exception":
                rule.setdefault("exceptions", []).append(op["exception"])
            else:
                changes = op["changes"]
                fields = {"when", "require", "exceptions", "next_step", "title", "source_terms"}
                if not isinstance(changes, dict) or set(changes) - fields:
                    raise SpaceError("revise_rule: changes may only set when, require, exceptions, "
                                     "next_step, title or source_terms")
                if not isinstance(op["expected_changes"], list):
                    raise SpaceError("revise_rule: expected_changes must be a list")
                for change in op["expected_changes"]:
                    code = change["code"]
                    if not isinstance(change["expected"], dict):
                        raise SpaceError("revise_rule: expected must be an object")
                    if code in expected and expected[code] != change["expected"]:
                        raise SpaceError(f"conflicting expected_changes for {code}")
                    expected[code] = change["expected"]
                rule.update(changes)
        elif kind == "add_vector":
            line = op["line"]
            if not isinstance(line["code"], str) or not isinstance(line["expected"], dict):
                raise SpaceError("add_vector needs a string code and an expected object")
            vectors.append(line)
        elif kind == "add_lexicon":
            gen.setdefault("lexicon", []).append(op["entry"])
        elif kind == "add_saved_code":
            codes = gen.setdefault("saved_codes", {})
            if op["name"] in codes:
                raise SpaceError(f"duplicate saved code '{op['name']}'")
            codes[op["name"]] = op["code"]
        elif kind == "not_encoded":
            if "ref" in op and not reason(op["ref"]):
                raise SpaceError("not_encoded: ref must be a non-empty reference phrase")
            if any(e["clause"] == op["clause"] and e.get("ref") == op.get("ref")
                   for e in judge.get("not_encoded", [])):
                raise SpaceError(f"clause {op['clause']} already has a not_encoded entry")
            disposition = {"clause": op["clause"], "reason": op["reason"]}
            if "ref" in op:
                disposition["ref"] = op["ref"]
            judge.setdefault("not_encoded", []).append(disposition)
        elif kind == "record_disagreement":
            rec = find(records, "id", op["record"], "record")
            rec["disagreement"] = op["reason"]
    return expected, dict(counts)


def _vector_result(line, sp):
    # Match validate exactly. The guard compares actual old and new verdicts, not partial expectations.
    pc = parse_code(line["code"], sp)
    got = evaluate(pc["features"], sp, pc["date"] or line.get("date"))
    return {k: got[k] for k in _RESULT_KEYS}


def _check_patch_vectors(old_vectors, new_vectors, old_space, new_space, expected):
    refused = []
    matched = set()
    for i, line in enumerate(old_vectors):
        code = line["code"]
        was, now = _vector_result(line, old_space), _vector_result(line, new_space)
        change = expected.get(code)
        # For a partial expected object, unchanged fields still must retain their old result.
        if change is not None and was != now:
            matched.add(code)
            if (set(change) <= set(_RESULT_KEYS) and {**was, **change} == now):
                new_vectors[i] = {**line, "expected": {**line["expected"], **now}}
                continue
        if was != now:
            refused.append(f"line {i + 1} ({code}): {was} -> {now}")
    for code in expected.keys() - matched:
        refused.append(f"expected_changes has no changed existing conformance line: {code}")
    if refused:
        raise SpaceError("conformance changed without matching revise_rule.expected_changes:\n" + "\n".join(refused))


def _write_files(dest: Path, contents: dict[str, bytes]) -> None:
    for rel, data in contents.items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def apply_patch(root: Path, dry_run: bool = False) -> dict:
    """Validate a staged refine patch and commit only a fully checked candidate."""
    gap_path = root / "work/gaps.json"
    try:
        patch = _read_json(root / "work/refine_patch.json")
        if not isinstance(patch, dict):
            raise SpaceError("refine_patch.json must be an object")
        if not gap_path.is_file() or patch.get("based_on") != hashlib.sha256(gap_path.read_bytes()).hexdigest():
            raise SpaceError("gaps changed, re-run gaps")
        report = _read_json(gap_path)
        if report.get("files") != _bundle_digests(root):
            raise SpaceError("bundle changed since gaps, re-run gaps")
        if "focus" not in patch or patch["focus"] != report.get("focus"):
            raise SpaceError("refine patch focus must match work/gaps.json")
        return _apply_patch(root, patch, report, dry_run)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
        raise SpaceError(f"invalid refine patch or bundle: {exc}") from exc


def _apply_patch(root: Path, patch: dict, report: dict, dry_run: bool) -> dict:
    old_space = Space(root)
    gen, judge = copy.deepcopy((old_space.generator, old_space.judge))
    old_vectors = _conformance(root)
    vectors = copy.deepcopy(old_vectors)
    hist = "records.jsonl" if (root / "records.jsonl").is_file() else "work/history_encoded.jsonl"
    records = [json.loads(s) for s in (root / hist).read_text(encoding="utf-8").splitlines() if s.strip()] \
        if (root / hist).exists() else []
    old_records = copy.deepcopy(records)
    expected, ops = _patch_ops(patch, report, gen, judge, vectors, records)
    names = ("generator.json", "judge.json", "conformance.jsonl", "bundle.json", "BUNDLE.md", hist,
             "work/terms.json", "work/scope.json", "work/exam.json", "work/refine_log.jsonl")
    original = {n: (root / n).read_bytes() for n in names if (root / n).is_file()}
    with tempfile.TemporaryDirectory(prefix="pac-refine-") as tmp:
        staged = Path(tmp) / root.name
        _write_files(staged, original)
        changes = {}
        if gen != old_space.generator:
            changes["generator.json"] = _json_bytes(gen)
        if judge != old_space.judge:
            changes["judge.json"] = _json_bytes(judge)
        if records != old_records:
            changes[hist] = _json_lines(records)
        _write_files(staged, changes)
        if old_space.version() >= (1, 6) and (staged / "work" / "terms.json").exists():
            judge["terms"] = _embedded_terms(staged, judge)
            if judge != old_space.judge:
                changes["judge.json"] = _json_bytes(judge)
                _write_files(staged, {"judge.json": changes["judge.json"]})
        new_space = Space(staged)
        _check_patch_vectors(old_vectors, vectors, old_space, new_space, expected)
        if vectors != old_vectors:
            changes["conformance.jsonl"] = _json_lines(vectors)
            _write_files(staged, {"conformance.jsonl": changes["conformance.jsonl"]})
        errors = validate(staged, check_guide=False, check_manifest=False)
        for row in vectors[len(old_vectors):]:
            if parse_code(row["code"], new_space)["errors"]:
                errors.append(f"conformance: invalid added code {row['code']}")
        if errors:
            raise SpaceError("validate:\n" + "\n".join(errors))
        after = gaps(staged, patch["focus"], allow_empty=True, check_manifest=False)
        before_counts = dict(Counter(g["kind"] for g in report["gaps"]))
        after_counts = dict(Counter(g["kind"] for g in after["gaps"]))
        output = {"files": list(changes), "gaps_before": before_counts, "gaps_after": after_counts,
                  "gaps_shrank": len(after["gaps"]) < len(report["gaps"]), "exam_passed": after["exam_passed"]}
        if dry_run:
            output["files"] += ["bundle.json", "BUNDLE.md", "work/exam.json", "work/refine_log.jsonl"]
            return output

        old_manifest = _read_json(staged / "bundle.json", {})
        history = old_manifest.get("refined", [])
        backups = root / "work/refine_backup"
        taken = [int(p.name) for p in backups.iterdir() if p.name.isdecimal()] if backups.exists() else []
        round_no = max([0, *taken, *(e["round"] for e in history)]) + 1
        report_exam = exam(staged, write=True, check_manifest=False)
        entry = {"date": report_exam["date"], "round": round_no, "focus": patch["focus"], "ops": ops}
        _write_files(staged, {"bundle.json": _json_bytes({**old_manifest, "refined": [*history, entry]})})
        write_manifest(staged)
        journal = {**entry, "gaps_before": before_counts, "gaps_after": after_counts,
                   "exam_passed": report_exam["passed"],
                   "scores": {c["id"]: c["score"] for c in report_exam["checks"]}}
        changes.update({n: (staged / n).read_bytes() for n in ("bundle.json", "work/exam.json", "BUNDLE.md")
                        if (staged / n).exists()})
        previous = original.get("work/refine_log.jsonl", b"")
        if previous and not previous.endswith(b"\n"):
            previous += b"\n"
        changes["work/refine_log.jsonl"] = previous + _json_lines([journal])
        output.update(journal, files=list(changes))

    if any(((root / n).read_bytes() if (root / n).is_file() else None) != original.get(n) for n in names):
        raise SpaceError("bundle changed during refinement, re-run gaps")
    backup = backups / str(round_no)
    backup.mkdir(parents=True, exist_ok=False)
    _write_files(backup, {n: b for n, b in original.items() if not n.startswith("work/") or n == hist})
    applied = []
    try:
        for name, data in changes.items():
            dest = root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(dest, data)
            applied.append(name)
    except OSError as exc:
        for name in reversed(applied):
            if name in original:
                _atomic_write(root / name, original[name])
            else:
                (root / name).unlink()
        raise SpaceError(f"could not write patched bundle: {exc}") from exc
    return output


def _atomic_write(dest: Path, data: bytes):
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
        name = Path(tmp.name)
        try:
            tmp.write(data)
            tmp.close()
            name.chmod(dest.stat().st_mode & 0o777 if dest.exists() else 0o644)
            name.replace(dest)
        finally:
            name.unlink(missing_ok=True)


# ---------------------------------------------------------------- validation and status


def _scope_warnings(rule: dict, sp: Space) -> list[str]:
    """Warn only when mandatory parent predicates prove a nested key cannot apply."""
    def leaves(cond):
        if "all" in cond:
            return [leaf for child in cond["all"] for leaf in leaves(child)]
        return [cond] if "dim" in cond else []

    warnings = []
    for branch in [rule["when"], *rule.get("exceptions", []), *([rule["require"]] if rule.get("require") else [])]:
        for k in sorted(cond_dims(branch)):
            under = sp.dims.get(k, {}).get("under")
            if not under or (parent := under["dim"]) not in sp.dims:
                continue
            preds = [under] + [c for c in leaves(rule["when"]) + leaves(branch) if c["dim"] == parent]
            allowed = set(sp.dims[parent]["values"])
            for c in preds:
                allowed.difference_update(c.get("not_in", []))
            if sp.multi(parent):
                # Taking all non-forbidden members satisfies every intersecting `in` predicate.
                possible = bool(allowed) and all("in" not in c or allowed.intersection(c["in"]) for c in preds)
            else:
                possible = any(all(eval_cond(c, {parent: v}) == T for c in preds) for v in allowed)
            message = f"{rule['id']}: condition on '{k}' has no path where its 'under' on '{parent}' is true"
            if not possible and message not in warnings:
                warnings.append(message)
    return warnings


def _source_ids(rule: dict):
    for item in (rule, *rule.get("exceptions", [])):
        ids = item.get("source_terms", [])
        if isinstance(ids, list):
            yield from (ident for ident in ids if isinstance(ident, str))


def _embedding_refs(clause: dict) -> list[dict]:
    return [r for r in clause_refs(clause) if r.get("kind") in _CITE_LINKS]


def _embedded_terms(root: Path, judge: dict) -> dict:
    doc = _read_json(root / "work" / "terms.json")
    clauses = {c["id"]: c for c in doc["clauses"]}
    seeds = {ident for r in judge["rules"] for ident in _source_ids(r)}
    included = seeds.copy()
    for ident in sorted(seeds):
        if ident not in clauses:
            raise SpaceError(f"source_terms names unknown clause {ident}")
        included.update(r["to"] for r in _embedding_refs(clauses[ident]) if r.get("to") is not None)
    for ident, term in clauses.items():
        if any(r.get("kind") in _CITE_BACKLINKS and r.get("to") in seeds for r in _embedding_refs(term)):
            included.add(ident)
    policy = doc.get("source") or _read_json(root / "bundle.json", {}).get("policy")
    embedded = {}
    sources = {ident: rule.get("source") for rule in judge["rules"] for ident in _source_ids(rule)}
    fallback = policy or next((r["source"] for r in judge["rules"] if r.get("source")), None)
    for ident in sorted(included):
        if ident not in clauses:
            raise SpaceError(f"reference names unknown clause {ident}")
        clause = clauses[ident]
        embedded[ident] = {
            "section": clause.get("section", ""),
            **{k: clause[k] for k in ("text", "summary") if k in clause},
            "class": clause["class"],
            "source": clause.get("source") or policy or sources.get(ident) or fallback,
            "refs": [{k: r[k] for k in ("to", "kind", "text", "external", "reason") if k in r and r[k] != ""}
                     for r in _embedding_refs(clause) if r.get("to") is None or r["to"] in included],
        }
    return embedded


def embed_terms(root: Path) -> dict:
    """Embed one-hop policy provenance without involving a model or altering rule behavior."""
    path = root / "judge.json"
    judge = _read_json(path)
    try:
        judge["terms"] = _embedded_terms(root, judge)
    except (AttributeError, KeyError, TypeError) as exc:
        raise SpaceError(f"cannot embed terms: {exc}") from exc
    _atomic_write(path, _json_bytes(judge))
    return {"terms": list(judge["terms"])}


def _judge_terms_issues(sp: Space, warnings: list[str] | None = None) -> list[str]:
    terms = sp.judge.get("terms", {})
    if not isinstance(terms, dict):
        return ["judge terms: expected an object keyed by clause id"]
    issues = []
    for ident, term in sorted(terms.items()):
        if not isinstance(term, dict):
            issues.append(f"judge terms {ident}: expected a term object")
            continue
        fields = {"text", "summary"} & term.keys()
        if len(fields) != 1 or not _nonempty(term.get(next(iter(fields)))):
            issues.append(f"judge terms {ident}: exactly one non-empty text or summary is required")
        if isinstance(term.get("text"), str) and _SUMMARY_START.match(term["text"]):
            issues.append(f"judge terms {ident}: text starts with Summary; use summary instead")
        if warnings is not None and fields == {"summary"}:
            warnings.append(f"judge terms {ident}: summary is not a verbatim quote")
        refs = term.get("refs", [])
        if not isinstance(refs, list):
            issues.append(f"judge terms {ident}: refs must be a list")
            continue
        for ref in refs:
            if not isinstance(ref, dict) or ref.get("kind") not in tuple(_CITE_LINKS) or "to" not in ref:
                issues.append(f"judge terms {ident}: malformed reference")
                continue
            to = ref["to"]
            if to is None:
                if not isinstance(ref.get("external"), str) or not ref["external"].strip():
                    issues.append(f"judge terms {ident}: null reference needs external")
            elif not isinstance(to, str) or to not in terms:
                issues.append(f"judge terms {ident}: reference names missing judge term {to}")
    for rule in sp.rules["rules"]:
        rid = rule.get("id")
        for item, label in [(rule, rid), *((e, f"{rid} exception {i}")
                                          for i, e in enumerate(rule.get("exceptions", []), 1))]:
            if item is not rule and "source_terms" not in item:
                continue
            ids = item.get("source_terms")
            if not isinstance(ids, list) or not ids or not all(isinstance(cid, str) and cid.strip() for cid in ids):
                issues.append(f"{label}: source_terms must list at least one clause id")
                continue
            issues.extend(f"{label}: source_terms names missing judge term {cid}" for cid in ids if cid not in terms)
    if (sp.root / "work" / "terms.json").exists() and terms and not issues:
        try:
            needed = _embedded_terms(sp.root, sp.judge)
        except (SpaceError, TypeError, KeyError) as exc:
            issues.append(f"judge terms: cannot resolve work/terms.json ({exc})")
        else:
            issues.extend(f"judge terms: missing linked term {cid}" for cid in sorted(needed.keys() - terms.keys()))
    return issues


def validate(space_root: Path, stage: str | None = None, *, check_guide: bool = True,
             warnings: list[str] | None = None, check_manifest: bool = True) -> list[str]:
    errs: list[str] = []
    if stage == "filter":
        lint = terms_lint(space_root)
        if warnings is not None:
            warnings.extend(lint["warnings"])
        return lint["errors"]
    if stage == "scope":
        scope = space_root / "work" / "scope.json"
        if not scope.exists():
            errs.append("scope.json missing")
        else:
            try:
                sc = json.loads(scope.read_text(encoding="utf-8"))
                tpath = space_root / "work" / "terms.json"
                clause_ids = ({c.get("id") for c in json.loads(tpath.read_text(encoding="utf-8")).get("clauses", [])}
                              if tpath.exists() else None)
                for a in sc.get("areas", []):
                    for f in ("area", "examples", "gap", "decision"):
                        if f not in a:
                            errs.append(f"scope area missing '{f}': {a.get('area')}")
                    if a.get("decision") not in (None, "ask", "in_scope_examples", "terms_only", "out_of_scope"):
                        errs.append(f"scope area {a.get('area')}: unknown decision '{a.get('decision')}'")
                    if a.get("gap"):  # 1.1: one specific question per rule family
                        if not a.get("source_terms"):
                            errs.append(f"gap area {a.get('area')}: list the clause ids it covers in 'source_terms'")
                        if a.get("decision") == "ask" and not a.get("question"):
                            errs.append(f"gap area {a.get('area')}: decision 'ask' needs its own 'question'")
                    for cid in a.get("source_terms", []):
                        if clause_ids is not None and cid not in clause_ids:
                            errs.append(f"scope area {a.get('area')}: unknown clause {cid}")
            except json.JSONDecodeError as exc:
                errs.append(f"scope.json invalid: {exc}")
    if stage == "scope":
        return errs
    try:
        sp = Space(space_root)
    except SpaceError as exc:
        return errs + [str(exc)]
    seen = set()
    for d in sp.schema["dimensions"]:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,11}", d["key"]):
            errs.append(f"key '{d['key']}' should be short lowercase")
        if not d.get("values"):
            errs.append(f"dimension {d['key']} has no values")
        if not d.get("evidence"):
            errs.append(f"dimension {d['key']} needs 'evidence' (what objective record settles it)")
        for v in d.get("values", {}):
            if sp.subjective.match(v):
                errs.append(f"dimension {d['key']}: value '{v}' is a judgement, not an objective condition")
        for v in d.get("values", {}):
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", v):
                errs.append(f"dimension {d['key']}: value code '{v}' may use only letters, digits, _ . -")
        if d.get("multi") and d.get("fixable"):
            errs.append(f"dimension {d['key']}: a multi-valued dimension describes the action, so it can't be fixable")
        if d["key"] in seen:
            errs.append(f"duplicate key {d['key']}")
        seen.add(d["key"])
    ids = set()
    for r in sp.rules["rules"]:
        rid = r.get("id")
        if rid in ids:
            errs.append(f"duplicate rule id {rid}")
        ids.add(rid)
        if r.get("kind") not in ("prohibition", "obligation"):
            errs.append(f"{rid}: kind must be prohibition or obligation")
        if r.get("kind") == "obligation" and not r.get("require"):
            errs.append(f"{rid}: obligation needs 'require'")
        if not r.get("source"):
            errs.append(f"{rid}: every rule must cite its source")
        if not r.get("next_step"):
            errs.append(f"{rid}: every rule needs a next_step (what to do)")
        try:
            for d in rule_dims(r):
                if d not in sp.dims:
                    errs.append(f"{rid}: unknown dimension '{d}'")
            _check_values(r, sp, errs)
            if warnings is not None:
                warnings.extend(_scope_warnings(r, sp))
        except (KeyError, SpaceError) as exc:
            errs.append(f"{rid}: malformed condition ({exc})")
    manifest_doc = _read_json(space_root / "bundle.json", {})
    require_terms = _bundle_format_version(sp, manifest_doc) >= (1, 6)
    if require_terms:
        errs.extend(_judge_terms_issues(sp, warnings))
    elif warnings is not None:
        if "terms" not in sp.judge:
            warnings.append("judge terms: this older bundle does not ship clause text")
        else:
            warnings.extend(_judge_terms_issues(sp, warnings))
    terms = space_root / "work" / "terms.json"
    if terms.exists():
        clauses = {c.get("id"): c for c in json.loads(terms.read_text(encoding="utf-8")).get("clauses", [])}
        for r in sp.rules["rules"]:
            for cid in _source_ids(r):
                if cid not in clauses:
                    errs.append(f"{r.get('id')}: source_terms names unknown clause {cid}")
                elif (clauses[cid].get("class") not in NORMATIVE
                      and not (require_terms and clauses[cid].get("class") == "definition")):
                    errs.append(f"{r.get('id')}: built from descriptive clause {cid} ({clauses[cid].get('class')})")
    for name, body in sp.codes.items():
        for e in parse_code(body, sp)["errors"]:
            errs.append(f"saved code @{name}: {e['message']} ({e['token']})")
    for i, line in enumerate(_conformance(space_root)):
        pc = parse_code(line["code"], sp)
        got = evaluate(pc["features"], sp, pc["date"] or line.get("date"))
        exp = line["expected"]
        diffs = [k for k in ("verdict", "violated", "required_next", "open") if k in exp and exp[k] != got[k]]
        if diffs:
            errs.append(f"conformance line {i + 1} ({line['code']}): mismatch in {diffs}")
    if sp.version() >= (1, 2):  # 1.2: the bundle must run with no model
        for d in sp.schema["dimensions"]:
            if not d.get("question"):
                errs.append(f"dimension {d['key']} needs a 'question' (the form asks it with no model)")
        for e in sp.generator.get("lexicon", []):
            if not e.get("phrase") or not e.get("token"):
                errs.append(f"lexicon entry needs 'phrase' and 'token': {e}")
            elif parse_code(e["token"], sp)["errors"]:
                errs.append(f"lexicon '{e['phrase']}': invalid token {e['token']}")
        terms = sp.generator.get("subjective_terms", [])
        if not isinstance(terms, list) or not all(isinstance(t, str) and t for t in terms):
            errs.append("subjective_terms must be a list of words")
        for k in sp.judge.get("texts", {}):
            if k not in DEFAULT_TEXTS:
                errs.append(f"judge texts: unknown section '{k}'")
        for rec in sp.history:
            if rec.get("code") and parse_code(rec["code"], sp)["errors"]:
                errs.append(f"record {rec.get('id')}: invalid code {rec['code']}")
                break
    cats = sp.generator.get("categories")
    if cats is not None or sp.version() >= (1, 4):  # 1.4: the generator page starts from a category
        errs += _check_categories(cats, sp)
    lim = sp.limits()
    if len(sp.dims) > lim["max_dimensions"] or len(sp.rules["rules"]) > lim["max_rules"]:
        errs.append(f"space too big: {len(sp.dims)} dimensions / {len(sp.rules['rules'])} rules "
                    f"(budget {lim['max_dimensions']} / {lim['max_rules']}). Merge variables into one "
                    "multi-valued dimension, merge rules that share a region, or raise generator.json "
                    "'limits' with a 'reason'")
    elif sp.generator.get("limits") and not sp.generator["limits"].get("reason"):
        errs.append("generator.json 'limits' needs a 'reason'")
    for rec in sp.history[:2000]:
        for k, v in rec.get("features", {}).items():
            vals = v.split("+") if k in sp.dims and sp.multi(k) else [v]
            if k not in sp.dims or any(x not in sp.dims[k]["values"] for x in vals):
                errs.append(f"history {rec.get('id')}: bad feature {k}={v}")
                break
    manifest = _read_json(space_root / "bundle.json", {}) if check_manifest or check_guide else {}
    if check_guide and not errs:
        version = str(manifest.get("version", "1.0")).split(".")
        guide_required = sp.version() >= (1, 5) or (version[0] == "1" and len(version) > 1
                                                     and version[1].isdigit() and int(version[1]) >= 5)
        guide = space_root / "BUNDLE.md"
        if guide.exists() or guide_required:
            errs.extend(_guide_issues(space_root, manifest))
    if check_manifest:
        checked = verify_manifest(manifest, _bundle_bytes(space_root, (*_BUNDLE_FILES, "BUNDLE.md")))
        for name in checked["mismatched"]:
            if name in checked["crlf_only"]:
                hint = _crlf_hint(name)
                if hint not in errs:
                    errs.append(hint)
            elif name != "BUNDLE.md" or not any(e.startswith("BUNDLE.md stale;") for e in errs):
                errs.append(f"{name}: digest mismatch; run pac.py manifest")
    return errs


def clause_refs(clause: dict) -> list[dict]:
    """Normalize a legacy `modifies` alongside explicit references, without mutating the term."""
    raw = clause.get("refs", [])
    refs = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    if (target := clause.get("modifies")) and not any(
        r.get("kind") == "modifies" and r.get("to") == target for r in refs
    ):
        refs.append({"to": target, "kind": "modifies", "text": ""})
    return refs


def _term_text(text: str) -> str:
    return " ".join(text.casefold().split())


def _term_matches(phrase: str, text: str):
    return re.finditer(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text)


def _incorporates_cycles(graph: dict[str, list[str]]) -> list[str]:
    """Report back edges within the incorporates graph (other edge types cannot form a cycle)."""
    active, visited, errors = set(), set(), []
    for start, outgoing in graph.items():
        if start in visited:
            continue
        active.add(start)
        stack = [(start, iter(outgoing))]
        while stack:
            cid, edges = stack[-1]
            target = next(edges, None)
            if target is None:
                stack.pop()
                active.remove(cid)
                visited.add(cid)
            elif target in active:
                route = [node for node, _ in stack]
                cycle = " -> ".join([*route[route.index(target):], target])
                errors.append(f"incorporates cycle: {cycle}")
            elif target not in visited:
                active.add(target)
                stack.append((target, iter(graph[target])))
    return errors


def _concept_phrases(text: str):
    """Find quoted phrases and contiguous title-cased words in the original text."""
    quoted = r'''"([^"\n]+)"|“([^”\n]+)”|(?<!\w)'([^'\n]+)'(?!\w)|‘([^’\n]+)’'''
    for m in re.finditer(quoted, text):
        yield next(p for p in m.groups() if p).strip()
    title_words = []
    end = 0
    for word in re.finditer(r"[^\W\d_][\w-]*", text):
        capitalized = word.group()[0].isupper()
        if title_words and (not capitalized or not text[end:word.start()].isspace()):
            if len(title_words) >= 2:
                yield " ".join(title_words)
            title_words = []
        if capitalized:
            title_words.append(word.group())
        end = word.end()
    if len(title_words) >= 2:
        yield " ".join(title_words)


def _read_terms(root: Path) -> dict:
    path = root / "work" / "terms.json"
    if not path.exists():
        raise SpaceError("work/terms.json missing")
    doc = _read_json(path)
    if not isinstance(doc, dict):
        raise SpaceError("terms.json must be an object")
    return doc


def terms_lint(root: Path) -> dict:
    """Check the filtered text and its declared cross-references without invoking a model."""
    errors, warnings = [], []
    result = {"errors": errors, "warnings": warnings}
    try:
        doc = _read_terms(root)
    except SpaceError as exc:
        errors.append(str(exc))
        return result
    clauses, raw = {}, doc.get("clauses")
    if not isinstance(raw, list) or not raw:
        errors.append("terms.json has no clauses")
        return result
    for clause in raw:
        if not isinstance(clause, dict):
            errors.append("clause must be an object")
            continue
        cid = clause.get("id")
        if not isinstance(cid, str) or not cid.strip() or cid in clauses:
            errors.append(f"clause id missing or duplicate: {cid}")
            continue
        clauses[cid] = clause
        klass, text = clause.get("class"), clause.get("text")
        if not isinstance(klass, str) or klass not in TERM_CLASSES:
            errors.append(f"{cid}: class must be one of {sorted(TERM_CLASSES)}")
        if not isinstance(text, str) or not text.strip():
            errors.append(f"{cid}: text missing")
        if isinstance(klass, str) and klass in NORMATIVE and clause.get("confidence") not in (
            "high", "medium", "low",
        ):
            errors.append(f"{cid}: normative clause needs confidence high|medium|low")

    # Old terms files still validate as before. New filters always write the glossary.
    reference_format = "glossary" in doc or any("refs" in c for c in clauses.values())
    glossary = doc.get("glossary", [])
    if not isinstance(glossary, list):
        errors.append("glossary must be a list")
        glossary = []
    definitions = []
    for entry in glossary:
        if not isinstance(entry, dict):
            errors.append("glossary entry must be an object")
            continue
        term, defined_in = entry.get("term"), entry.get("defined_in")
        if not isinstance(term, str) or not term.strip():
            errors.append("glossary term missing")
            continue
        if not isinstance(defined_in, str) or clauses.get(defined_in, {}).get("class") != "definition":
            errors.append(f"glossary {term}: defined_in {defined_in} is not a known definition clause")
            continue
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(a, str) or not a.strip() for a in aliases):
            errors.append(f"glossary {term}: aliases must be non-empty strings")
            continue
        definitions.append((term, defined_in, {_term_text(a) for a in [term, *aliases]}))

    used = set()
    incorporates = {cid: [] for cid in clauses}
    concepts = {}
    known = set().union(*(entry[2] for entry in definitions))
    for cid, clause in clauses.items():
        text = clause.get("text", "")
        text = text if isinstance(text, str) else ""
        normalized = _term_text(text)
        raw_refs = clause.get("refs", [])
        if not isinstance(raw_refs, list) or any(not isinstance(r, dict) for r in raw_refs):
            errors.append(f"{cid}: refs must be a list of objects")
        explicit_ids = {id(r) for r in raw_refs} if isinstance(raw_refs, list) else set()
        covered, definition_refs = [], set()
        for ref in clause_refs(clause):
            target, kind = ref.get("to"), ref.get("kind")
            ref_text = ref.get("text", "")
            if id(ref) in explicit_ids:
                if "to" not in ref:
                    errors.append(f"{cid}: ref must have 'to'")
                phrase = _term_text(ref_text) if isinstance(ref_text, str) else ""
                if not phrase or phrase not in normalized:
                    errors.append(f"{cid}: ref text not found in clause: {ref_text!r}")
                elif kind != "none" or _nonempty(ref.get("reason")):
                    covered.extend(m.span() for m in re.finditer(re.escape(phrase), normalized))
            if kind == "none":
                if not _nonempty(ref.get("reason")):
                    errors.append(f"{cid}: ref kind none needs a reason")
                continue
            if not isinstance(kind, str) or kind not in REF_KINDS:
                errors.append(f"{cid}: unknown ref kind {kind}")
                continue
            if target is None:
                if not (_nonempty(ref.get("external")) or _nonempty(ref.get("reason"))):
                    errors.append(f"{cid}: unresolved ref needs external or reason: {ref_text!r}")
                else:
                    warnings.append(f"{cid}: unresolved ref {ref_text!r}: "
                                    f"{ref.get('external') or ref.get('reason')}")
            elif not isinstance(target, str) or target not in clauses:
                errors.append(f"{cid}: {kind} unknown clause {target}")
            elif kind == "incorporates":
                incorporates[cid].append(target)
            elif kind == "uses_definition":
                definition_refs.add(target)

        if not reference_format:
            continue
        for term, origin, variants in definitions:
            if cid == origin or not any(next(_term_matches(v, normalized), None) for v in sorted(variants)):
                continue
            used.add((term, origin))
            if origin not in definition_refs:
                errors.append(f"{cid}: missing uses_definition ref to {origin} for {term!r}")
        if isinstance(klass := clause.get("class"), str) and klass in NORMATIVE:
            for signal in REF_SIGNALS:
                for m in _term_matches(signal, normalized):
                    if not any(start <= m.start() and m.end() <= end for start, end in covered):
                        errors.append(f"{cid}: unlinked reference phrase {signal!r}")
                        break
        for phrase in _concept_phrases(text):
            key = _term_text(phrase)
            if key and key not in known:
                _, owners = concepts.setdefault(key, (phrase, set()))
                owners.add(cid)

    errors.extend(_incorporates_cycles(incorporates))
    for term, origin, _ in definitions:
        if (term, origin) not in used:
            warnings.append(f"{origin}: orphan definition {term!r}")
    for phrase, owners in sorted(concepts.values()):
        if len(owners) >= 2:
            warnings.append(f"concept without a page: {phrase!r} ({', '.join(sorted(owners))})")
    return result


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def terms_context(root: Path, clause_id: str) -> dict:
    """Expand the requested clause's forward refs and expose backlinks of each step."""
    doc = _read_terms(root)
    clauses = {c["id"]: c for c in doc.get("clauses", [])}
    if clause_id not in clauses:
        raise SpaceError(f"unknown clause {clause_id!r}")
    refs = {cid: [r for r in clause_refs(c) if r.get("kind") in REF_KINDS]
            for cid, c in clauses.items()}
    backlinks = {cid: [] for cid in clauses}
    for cid, links in sorted(refs.items()):
        for ref in links:
            target = ref.get("to")
            if target in backlinks:
                backlinks[target].append({"clause": cid, "kind": ref["kind"],
                                          "text": ref.get("text", "")})
    paths = {clause_id: clause_id}
    pending = deque([clause_id])
    unresolved = []
    while pending:
        cid = pending.popleft()
        for ref in refs[cid]:
            target = ref.get("to")
            if target not in clauses:
                unresolved.append({"clause": cid, "path": paths[cid], "ref": ref})
            elif target not in paths:
                paths[target] = f"{paths[cid]} -{ref['kind']}-> {target}"
                pending.append(target)
    for cid, path in list(paths.items()):
        for back in backlinks[cid]:
            source = back["clause"]
            if source not in paths:
                paths[source] = f"{path} <-{back['kind']}- {source}"
    return {"clause": clause_id, "clauses": [
        {"clause": clauses[cid], "path": path, "backlinks": backlinks[cid]} for cid, path in paths.items()
    ], "unresolved": unresolved}


def _conformance(root: Path) -> list[dict]:
    path = root / "conformance.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _guide_condition(cond: dict) -> str:
    for op, word in (("all", " and "), ("any", " or ")):
        if op in cond:
            return "(" + word.join(_guide_condition(c) for c in cond[op]) + ")"
    if "not" in cond:
        return f"not ({_guide_condition(cond['not'])})"
    op = "in" if "in" in cond else "not_in"
    return f"`{cond['dim']}` {'is one of' if op == 'in' else 'is none of'} " + ", ".join(
        f"`{value}`" for value in cond[op]
    )


def render_guide(root: Path, manifest: dict | None = None, *, digests: dict | None = None) -> bytes:
    """Render the guide from bundle files only, with stable ordering and LF endings."""
    sp = Space(root)
    man = manifest if manifest is not None else _read_json(root / "bundle.json", {})
    policy = sp.generator.get("policy") or sp.judge.get("policy") or {}
    bundle = man.get("name") or root.name
    name = policy.get("name") or bundle
    source = man.get("policy") or policy.get("source") or "Policy source not specified"
    version = man.get("version") or sp.generator["version"]
    badge = man.get("exam") or {}
    summary = "passed" if badge.get("passed") else "failed"
    date = badge.get("date") or "not run"
    cats = categories(sp)
    cases = _conformance(root)
    example_code = cases[0]["code"] if cases else next(iter(sp.codes.values()), "")
    rules = sp.rules["rules"]
    # Visible fields do not enumerate every source field. Fingerprint the authoritative
    # bundle data too, so changes to a non-rendered field also invalidate the guide.
    serialized = json.dumps(_bundle_digests(root) if digests is None else digests,
                            sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def row(*values):
        return "| " + " | ".join(str(v).replace("|", r"\|").replace("\r", "").replace("\n", "<br>")
                                for v in values) + " |"

    def table(labels):
        return [row(*labels), row(*(["---"] * len(labels)))]

    out = ["---", f"name: pac-{bundle}",
           (f"description: Check an action against {name} with no model, "
           "using the policy-action-check bundle in this folder."),
           "---", f"# {name}: policy action bundle", "",
           (f"{source} · format {version} · self-exam: {summary} ({date}) · "
           f"{len(category_nodes(cats))} categories, {len(sp.dims)} features, {len(rules)} rules "
           f"<!-- bundle data: {fingerprint} -->"),
           "", "## How to use",
           "- Web app: open pac-reader.html and load this folder.",
           (f'- Command line: `python3 pac.py --space <this folder> report "{example_code}"`, '
           'plus `categories`, `fit "<code>"`, `check`, `lookup "<words>"`.'),
           ("- Codes: `key=value` tokens; `a+b` for several values on a multi-valued key; "
           "`&` = at the same time, `>` = after."), "", "## Case map"]

    def walk(nodes, depth=0):
        for node in nodes:
            marker = " (catch-all)" if node["catch_all"] else ""
            description = node["description"].rstrip(".") or "No description"
            out.append(f"{'  ' * depth}- **{node['name']}** `{node['path']}`{marker} — "
                       f"{description}. Preset: `{node['preset'] or '(none)'}`.")
            walk(node["children"], depth + 1)

    walk(cats)
    out += ["", "## Features"]
    for dim in sp.schema["dimensions"]:
        key = dim["key"]
        q = dim.get("question") or "Not specified"
        values = "; ".join(f"`{v}` — {label}" for v, label in dim["values"].items())
        default = f"`{dim['default']}`" if dim.get("default") is not None else "none (unknown if unset)"
        inside = _guide_condition(dim["under"]) if dim.get("under") else "all cases"
        fixable = "yes" if dim.get("fixable") else "no"
        evidence = dim.get("evidence") or "Not specified"
        out += [f"- `{key}` — {dim.get('name') or key}",
                f"  - Question: {q}", f"  - Values: {values}", f"  - Default: {default}",
                f"  - Inside: {inside}", f"  - Can be fixed: {fixable}", f"  - Evidence: {evidence}"]
    terms = sp.judge.get("terms", {})
    include_terms = bool(terms) or (
        re.fullmatch(r"\d+\.\d+", version) is not None
        and tuple(map(int, version.split("."))) >= (1, 6)
    )
    columns = ("id", "title", "kind", "what to do", "source")
    out += ["", "## Rules", *table((*columns, "terms") if include_terms else columns)]
    for rule in rules:
        cells = (f"`{rule['id']}`", rule.get("title", ""), rule.get("kind", ""),
                 rule.get("next_step", ""), rule.get("source", ""))
        if include_terms:
            cells += (", ".join(sorted(set(rule.get("source_terms", [])))),)
        out.append(row(*cells))
    if include_terms:
        out += ["", "## Terms"]
        for ident, term in sorted(terms.items()):
            out += ["", f"**{ident}** · {term.get('section', '')} · {term.get('class', '')}", ""]
            content = _term_content(term)
            if "summary" in content:
                out.append(f"> {_summary_label(content['summary'])}")
            elif content["text"]:
                out.extend(f"> {line}" for line in content["text"].split("\n"))
            if refs := term.get("refs", []):
                links = ", ".join(f"{ref['kind']} → {ref.get('to') or ref.get('external')}"
                                  for ref in refs)
                out += ["", f"links: {links}"]
    out += ["", "## Examples", *table(("code", "verdict", "violated"))]
    for line in cases[:40]:
        expected = line["expected"]
        out.append(row(f"`{line['code']}`", expected.get("verdict", ""),
                       ", ".join(expected.get("violated", []))))
    out += ["", "## Saved codes"]
    out += [f"- `@{key}` — `{code}`" for key, code in sorted(sp.codes.items())] or ["None."]
    if dispositions := sp.judge.get("not_encoded"):
        out += ["", "## Not encoded"]
        for entry in dispositions:
            clause = f"`{entry['clause']}`"
            if entry.get("ref"):
                clause += f" ({entry['ref']})"
            out.append(f"- {clause} — {entry['reason']}")
    out += ["", "## Self-exam", *table(("check", "score", "threshold", "passed"))]
    thresholds = EXAM_THRESHOLDS | sp.generator.get("exam", {})
    if sp.version() < (1, 6):
        thresholds.pop("references", None)
    checks = badge.get("checks")
    if checks is None:
        scores = badge.get("scores", {})
        failed = badge.get("failed", [])
        checks = [{"id": key, "score": scores.get(key), "threshold": thresholds.get(key),
                   "passed": key not in failed}
                  for key in sorted(set(scores) | set(failed))]
    for check in checks:
        score, threshold = check.get("score"), check.get("threshold")
        outcome = "skipped" if check.get("skipped") else ("yes" if check["passed"] else "no")
        out.append(row(check["id"], "—" if score is None else score,
                       "—" if threshold is None else threshold, outcome))
    disclaimer = sp.judge.get("texts", {}).get("disclaimer", DEFAULT_TEXTS["disclaimer"])
    out += ["", f"_{disclaimer}_"]
    return ("\n".join(out).replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n").encode("utf-8")


def _guide_issues(root: Path, manifest: dict | None = None) -> list[str]:
    guide = root / "BUNDLE.md"
    if not guide.exists():
        return ["BUNDLE.md missing; run pac.py guide"]
    actual = guide.read_bytes()
    if actual == render_guide(root, manifest):
        return []
    sources = _bundle_bytes(root)
    normalized = {name: data.replace(b"\r\n", b"\n") for name, data in sources.items()}
    crlf = [name for name in sources if sources[name] != normalized[name]]
    lf = actual.replace(b"\r\n", b"\n")
    if lf != actual:
        crlf.append("BUNDLE.md")
    if crlf:
        lf_digests = {name: hashlib.sha256(data).hexdigest() for name, data in normalized.items()}
        if lf == render_guide(root, manifest, digests=lf_digests):
            return [_crlf_hint(name) for name in crlf]
    return ["BUNDLE.md stale; run pac.py guide"]


def write_guide(root: Path, manifest: dict | None = None) -> bytes:
    guide = render_guide(root, manifest)
    (root / "BUNDLE.md").write_bytes(guide)
    return guide


def _bundle_format_version(sp: Space, manifest: dict | None = None) -> tuple[int, int]:
    versions = [sp.version()]
    for doc in (sp.judge, manifest or {}):
        version = str(doc.get("version", ""))
        if re.fullmatch(r"1\.\d+", version):
            versions.append((1, int(version.split(".")[1])))
    return max(versions)


def write_manifest(root: Path, policy: str | None = None) -> dict:
    """bundle.json: format, version, file digests. Run after build so other software can trust it."""
    files = _bundle_digests(root)
    old = _read_json(root / "bundle.json", {})
    # Adding a guide makes the manifest at least 1.5. Do not advertise 1.6 just because a
    # newer engine refreshed an older generator and judge that lack 1.6 provenance.
    version = ".".join(map(str, max((1, 5), _bundle_format_version(Space(root)))))
    man = {"format": "pac-bundle", "version": version, "name": old.get("name") or root.name,
           "policy": policy or old.get("policy"), "files": files,
           "created": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
           "spec": "https://github.com/oceanszoo/policy-action-check/blob/main/plugins/policy-action-check/references/SPEC.md",
           "exam": exam_badge(root)}  # null: no exam on these exact files
    if "refined" in old:
        man["refined"] = old["refined"]
    guide = write_guide(root, man)
    files["BUNDLE.md"] = hashlib.sha256(guide).hexdigest()
    (root / "bundle.json").write_text(json.dumps(man, indent=2), encoding="utf-8")
    return man


def _check_categories(cats, sp: Space) -> list[str]:
    errs: list[str] = []
    if not isinstance(cats, list) or not cats:
        return ["generator.json needs 'categories': the case map the generator page starts from"]
    covered: set[str] = set()

    def walk(items, where, top):
        ids = set()
        for c in items:
            cid = c.get("id") if isinstance(c, dict) else None
            if not cid or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", str(cid)):
                errs.append(f"category {where}{c}: needs a short lowercase 'id'")
                continue
            path = where + cid
            if cid in ids:
                errs.append(f"duplicate category id {path}")
            ids.add(cid)
            if not c.get("name"):
                errs.append(f"category {path}: needs a 'name'")
            feats = c.get("features", [])
            if not isinstance(feats, list) or (top and not feats):
                errs.append(f"category {path}: needs 'features' (the dimension keys to choose from)")
                feats = []
            for k in feats:
                if k not in sp.dims:
                    errs.append(f"category {path}: unknown feature '{k}'")
            if len(set(feats)) != len(feats):
                errs.append(f"category {path}: a feature is listed twice")
            covered.update(feats)
            preset = c.get("preset", "")
            if parse_code(preset, sp)["errors"] or " > " in preset or " & " in preset:
                errs.append(f"category {path}: invalid preset '{preset}'")
            if not isinstance(c.get("children", []), list):
                errs.append(f"category {path}: 'children' must be a list")
            else:
                walk(c.get("children", []), path + "/", False)

    walk(cats, "", True)
    for n in category_nodes(categories(sp)):
        preset = parse_code(n["preset"], sp)["features"]
        for k in preset:
            if k not in reachable(n["features"], preset, sp):
                errs.append(f"category {n['path']}: preset key '{k}' must be one of its folders "
                            "(its own, an ancestor's, or nested under them)")
    for d in sp.schema["dimensions"]:
        u = d.get("under")
        if u is None:
            continue
        if (not isinstance(u, dict) or u.get("dim") not in sp.dims or u.get("dim") == d["key"]
                or not (("in" in u) ^ ("not_in" in u))):
            errs.append(f"dimension {d['key']}: 'under' must be {{dim, in|not_in}} on one other key")
            continue
        for v in u.get("in", u.get("not_in", [])):
            if v not in sp.dims[u["dim"]]["values"]:
                errs.append(f"dimension {d['key']}: 'under' names unknown value {u['dim']}={v}")
        seen, k = {d["key"]}, u["dim"]
        while k and sp.dims.get(k, {}).get("under"):
            if k in seen:
                errs.append(f"dimension {d['key']}: 'under' forms a loop")
                break
            seen.add(k)
            k = sp.dims[k]["under"].get("dim")
    kids = nested_under(sp)
    todo = list(covered)
    while todo:  # nested folders count as offered when their parent folder is
        k = todo.pop()
        for c in kids.get(k, []):
            if c not in covered:
                covered.add(c)
                todo.append(c)
    used = {d for r in sp.rules["rules"] for d in rule_dims(r) if d in sp.dims}
    missing = sorted(used - covered, key=list(sp.dims).index)
    if missing:
        errs.append(f"categories: no category offers {', '.join(missing)}, which rules use")
    return errs


def _check_values(rule, sp, errs):
    def walk(c):
        if "all" in c or "any" in c:
            for x in c.get("all", c.get("any", [])):
                walk(x)
        elif "not" in c:
            walk(c["not"])
        else:
            for v in c.get("in", []) + c.get("not_in", []):
                if c["dim"] in sp.dims and v not in sp.dims[c["dim"]]["values"]:
                    errs.append(f"{rule['id']}: '{v}' is not a value of {c['dim']}")

    walk(rule["when"])
    for e in rule.get("exceptions", []):
        walk(e)
    if rule.get("require"):
        walk(rule["require"])


def read_state(root: Path) -> dict:
    return _read_json(root / "work" / "state.json", {"stage": None})


def write_state(root: Path, **updates) -> dict:
    (root / "work").mkdir(parents=True, exist_ok=True)
    state = read_state(root)
    state.update(updates)
    state["updated"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    (root / "work" / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def file_digest(path: str | None) -> str | None:
    if not path or not Path(path).exists():
        return None
    p = Path(path)
    h = hashlib.sha256()
    for f in sorted([p] if p.is_file() else [x for x in p.rglob("*") if x.is_file()]):
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def status(root: Path) -> dict:
    st = read_state(root)
    built = (root / "generator.json").exists() and (root / "judge.json").exists()
    stale = []
    for key in ("policy", "history"):
        src = st.get("inputs", {}).get(key)
        if src and st.get("digests", {}).get(key) not in (None, file_digest(src)):
            stale.append(key)
    gaps = [a["area"] for a in _read_json(root / "work" / "scope.json", {}).get("areas", []) if a.get("decision") == "ask"]
    return {"space": str(root), "exists": root.exists(), "built": built, "stage": st.get("stage"),
            "stale_inputs": stale, "unanswered_scope_questions": gaps,
            "awaiting_user": bool(st.get("awaiting_user"))}


# ---------------------------------------------------------------- hooks

INTENT = re.compile(
    r"\b(polic(y|ies)|terms?( of (use|service))?|usage polic|acceptable use|complian\w*|violat\w*|allowed|"
    r"permitted|prohibit\w*|forbidden|against the rules|can (i|we) (use|let|have|make)|is it ok|"
    r"action code|policy space|regulat\w*|aup)\b",
    re.IGNORECASE,
)
CODE_LIKE = re.compile(r"(^|\s)(@[\w.-]+|[a-z][a-z0-9_]{0,11}=[\w?.-]+)(\s|$)")


def hook_prompt(payload: dict) -> dict | None:
    prompt = payload.get("prompt", "")
    root = space_dir()
    hit = bool(INTENT.search(prompt)) or bool(CODE_LIKE.search(prompt) and root.exists())
    if not hit:
        return None
    st = status(root)
    lines = [
        "[policy-action-check] This prompt looks like a policy/action question.",
        (
            "Run the policy-action-check pipeline end to end (skill policy-action-check:policy-pipeline) "
            "and give the user the final outcome. Do not ask the user to pick skills or commands."
        ),
        (
            f"Space: {st['space']} | built: {st['built']} | stage: {st['stage']} | stale: {st['stale_inputs'] or 'no'}"
            f" | unanswered scope questions: {st['unanswered_scope_questions'] or 'none'}"
        ),
    ]
    if st["built"] and CODE_LIKE.search(prompt):
        lines.append("The prompt contains an action code: check it with the engine first.")
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\n".join(lines)}}


def hook_guard(payload: dict) -> dict | None:
    """PreToolUse: map a tool call to an action code with tool_map.json and check it."""
    mode = (os.environ.get("CLAUDE_PLUGIN_OPTION_GUARD_MODE") or "warn").lower()
    root = space_dir()
    if mode == "off" or not (root / "generator.json").exists():
        return None
    tool = payload.get("tool_name", "")
    try:
        space = Space(root)
    except SpaceError:
        return None
    tmap = space.tool_map
    if not tmap.get("mappings"):
        return None
    blob = json.dumps(payload.get("tool_input", {}), ensure_ascii=False)
    if "pac.py" in blob or str(root) in blob:
        return None  # the pipeline's own engine calls and bundle writes
    tokens = []
    for entry in tmap.get("mappings", []):
        if re.search(entry["tool"], tool) and re.search(entry.get("input", ".*"), blob, re.DOTALL):
            tokens.append(entry["code"])
    if not tokens:
        return None
    code = " ".join([tmap.get("base", "")] + tokens).strip()
    res = check(code, space)
    if res["verdict"] not in ("violates", "compliant_if_requirements_met"):
        return None
    reason = (f"policy-action-check: tool call encodes as `{res['code']}` -> {res['verdict']}; "
              f"violated {res['violated'] or '-'}; required {res['required_next'] or '-'}; "
              f"fix: {res['fix'][0] if res['fix'] else res['fix_note']}")
    decision = "deny" if mode == "enforce" and res["verdict"] == "violates" else "ask"
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                   "permissionDecisionReason": reason}}


def hook_subagent_stop(payload: dict) -> dict | None:
    agent = str(payload.get("agent_type", ""))
    if agent.endswith("space-refiner"):
        try:
            apply_patch(space_dir(), dry_run=True)
        except (SpaceError, OSError) as exc:
            return {"decision": "block", "reason": str(exc)}
        return None
    stage = next((s for name, s in AGENT_STAGE.items() if agent.endswith(name)), None)
    if not stage:
        return None
    errs = validate(space_dir(), stage, check_guide=not agent.endswith("space-builder"))
    if errs:
        return {"decision": "block", "reason": "policy_space does not validate yet:\n- " + "\n- ".join(errs[:20])}
    if agent.endswith("space-verifier"):  # a bundle ships only after it passes its self-exam
        res = exam(space_dir())
        if not res["passed"]:
            fails = [f"{c['id']} ({c['detail']}): " + "; ".join(c["failures"][:3])
                     for c in res["checks"] if not c["passed"]]
            return {"decision": "block", "reason": "the bundle fails its self-exam (pac.py exam). Fix the "
                    "bundle, not the thresholds:\n- " + "\n- ".join(fails)}
    return None


def hook_stop(payload: dict) -> dict | None:
    if payload.get("stop_hook_active"):
        return None
    root = space_dir()
    st = read_state(root)
    stage = st.get("stage")
    if stage in (None, "done") or st.get("awaiting_user"):
        return None
    return {"decision": "block", "reason": (
        f"policy-action-check pipeline is at stage '{stage}'. Finish it and give the user the final "
        "outcome, or ask the user the open scope question (pac.py state --awaiting-user), "
        "or record why you stop (pac.py state --stage done).")}


def hook_session(payload: dict) -> dict | None:
    root = space_dir()
    if not root.exists():
        return None
    st = status(root)
    msg = (f"[policy-action-check] policy space at {st['space']}: built={st['built']}, stage={st['stage']}, "
           f"stale inputs={st['stale_inputs'] or 'none'}.")
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": msg}}


HOOKS = {"prompt": hook_prompt, "guard": hook_guard, "subagent-stop": hook_subagent_stop,
         "stop": hook_stop, "session": hook_session}


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pac", description=__doc__.split("\n")[0])
    ap.add_argument("--space", help="policy space directory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    note_help = "a note shown with the result, never judged: 'key=text' for a choice, or plain text"
    for name in ("check", "parse", "panel", "explain", "report"):
        p = sub.add_parser(name)
        p.add_argument("code")
        p.add_argument("--date")
        p.add_argument("--note", action="append", default=[], help=note_help)
    p = sub.add_parser("ask")
    p.add_argument("answers", nargs="?", default="")
    p.add_argument("--date")
    p.add_argument("--note", action="append", default=[], help=note_help)
    sub.add_parser("form").add_argument("--date")
    sub.add_parser("categories")
    sub.add_parser("fit").add_argument("code")
    sub.add_parser("lookup").add_argument("text")
    sub.add_parser("validate").add_argument("--stage", choices=["filter", "scope", "build"])
    sub.add_parser("terms-lint")
    sub.add_parser("terms-context").add_argument("clause_id")
    sub.add_parser("manifest").add_argument("--policy")
    sub.add_parser("embed-terms")
    sub.add_parser("guide").add_argument("--check", action="store_true")
    sub.add_parser("exam")
    p = sub.add_parser("gaps")
    p.add_argument("--focus")
    p.add_argument("--write", action="store_true")
    sub.add_parser("apply-patch").add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    st = sub.add_parser("state")
    st.add_argument("--stage", choices=STAGES)
    st.add_argument("--awaiting-user", action="store_true")
    st.add_argument("--resume", action="store_true")
    st.add_argument("--policy")
    st.add_argument("--history")
    sub.add_parser("hook").add_argument("event", choices=sorted(HOOKS))
    args = ap.parse_args(argv)

    if args.cmd == "hook":  # hooks must never crash the session
        try:
            payload = json.loads(sys.stdin.read() or "{}")
            out = HOOKS[args.event](payload)
        except Exception as exc:  # noqa: BLE001
            print(f"policy-action-check hook error: {exc}", file=sys.stderr)
            return 0
        if out:
            print(json.dumps(out))
        return 0

    root = space_dir(args.space)
    try:
        if args.cmd == "validate":
            warnings = []
            errs = validate(root, args.stage, warnings=warnings)
            print(json.dumps({"ok": not errs, "errors": errs} | ({"warnings": warnings} if warnings else {}), indent=2))
            return 0 if not errs else 1
        if args.cmd == "terms-lint":
            lint = terms_lint(root)
            print(json.dumps(lint, indent=2, ensure_ascii=False))
            return 1 if lint["errors"] else 0
        if args.cmd == "terms-context":
            print(json.dumps(terms_context(root, args.clause_id), indent=2, ensure_ascii=False))
            return 0
        if args.cmd == "exam":
            res = exam(root)
            summary = {"passed": res["passed"], "checks": [
                {k: c[k] for k in ("id", "passed", "score", "threshold", "detail", "failure_count")}
                | ({"first_failures": c["failures"][:5]} if c["failures"] else {}) for c in res["checks"]],
                "full_report": str(root / "work" / "exam.json")}
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            return 0 if res["passed"] else 1
        if args.cmd == "gaps":
            print(json.dumps(gaps(root, args.focus, write=args.write), indent=2, ensure_ascii=False))
            return 0
        if args.cmd == "apply-patch":
            print(json.dumps(apply_patch(root, args.dry_run), indent=2, ensure_ascii=False))
            return 0
        if args.cmd == "guide":
            if args.check:
                if issues := _guide_issues(root):
                    print("\n".join(issues), file=sys.stderr)
                    return 1
            else:
                write_guide(root)
            return 0
        if args.cmd == "manifest":
            print(json.dumps(write_manifest(root, args.policy), indent=2))
            return 0
        if args.cmd == "embed-terms":
            print(json.dumps(embed_terms(root), indent=2, ensure_ascii=False))
            return 0
        if args.cmd == "status":
            print(json.dumps(status(root), indent=2))
            return 0
        if args.cmd == "state":
            upd = {}
            if args.stage:
                upd["stage"] = args.stage
            if args.awaiting_user:
                upd["awaiting_user"] = True
            if args.resume:
                upd["awaiting_user"] = False
            if args.policy or args.history:
                st0 = read_state(root)
                inputs = st0.get("inputs", {})
                digests = st0.get("digests", {})
                for k in ("policy", "history"):
                    if getattr(args, k):
                        inputs[k] = getattr(args, k)
                        digests[k] = file_digest(getattr(args, k))
                upd["inputs"], upd["digests"] = inputs, digests
            print(json.dumps(write_state(root, **upd), indent=2))
            return 0
        space = Space(root)
        notes: dict[str, str] = {}
        for n in getattr(args, "note", []):
            k, sep, text = n.partition("=")
            if sep and k in space.dims:
                notes[k] = text
            else:
                notes["_action"] = (notes.get("_action", "") + " " + n).strip()
        if args.cmd == "parse":
            p = parse_code(args.code, space)
            p["canonical"] = format_code(p["features"], space, p["name"])
            print(json.dumps(p, indent=2, ensure_ascii=False))
        elif args.cmd == "explain":
            print(explain_code(parse_code(args.code, space)["features"], space))
        elif args.cmd == "panel":
            for row in panel(args.code, space, args.date):
                print(json.dumps(row, ensure_ascii=False))
        elif args.cmd == "check":
            print(json.dumps(check_flow(args.code, space, args.date, notes), indent=2, ensure_ascii=False))
        elif args.cmd == "report":
            print(render_report(check_flow(args.code, space, args.date, notes), space))
        elif args.cmd == "ask":
            print(json.dumps(next_question(args.answers, space, args.date, notes), indent=2, ensure_ascii=False))
        elif args.cmd == "lookup":
            print(json.dumps(lookup(args.text, space), indent=2, ensure_ascii=False))
        elif args.cmd == "form":
            run_form(space, args.date)
        elif args.cmd == "categories":
            print(json.dumps(categories(space), indent=2, ensure_ascii=False))
        elif args.cmd == "fit":
            parsed = parse_code(args.code, space)
            fit = fit_detail(parsed["features"], space, parsed["date"])
            if not fit["dropped"]:
                del fit["dropped"]
            print(json.dumps(fit))
        return 0
    except SpaceError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
