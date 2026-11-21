"""Build-time pipeline: draft a bundle from a policy (and history) with any LLM.

This is the model-agnostic version of the Claude Code plugin's subagents. It reuses their prompts
(``prompts/agents/*.md``) and replaces the plugin's hooks with the same deterministic checks:

  filter  -> work/terms.json                 checked by terms-lint and validate --stage filter
  scope   -> work/scope.json                 checked by validate --stage scope
  build   -> generator.json, judge.json, ... checked by embed-terms and validate --stage build
  verify  -> corrected bundle files          checked by the self-exam, manifest and validate

The model never runs commands. It answers with the files it wants written; the builder writes
them, runs the engine's checks, and sends any errors back for another attempt. A finished bundle
is judged by the engine alone: using a bundle never needs a model.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from policy_action_check import engine
from policy_action_check.providers import PRESETS, ProviderError, make_provider

PROMPTS = Path(__file__).resolve().parent / "prompts"
STAGE_ORDER = ["filter", "scope", "build", "verify"]
AGENTS = {"filter": "term-filter", "scope": "scope-planner", "build": "space-builder",
          "verify": "space-verifier", "refine": "space-refiner"}
OUTPUTS = {
    "filter": ["work/terms.json"],
    "scope": ["work/scope.json"],
    "build": ["generator.json", "judge.json", "conformance.jsonl", "records.jsonl",
              "work/history_encoded.jsonl", "work/build_report.md"],
    "verify": ["generator.json", "judge.json", "conformance.jsonl", "records.jsonl", "work/build_report.md"],
    "refine": ["work/refine_patch.json"],
}
REQUIRED = {"filter": ["work/terms.json"], "scope": ["work/scope.json"],
            "build": ["generator.json", "judge.json", "conformance.jsonl"], "verify": [], "refine": ["work/refine_patch.json"]}
MAX_ERRORS = 40

PREAMBLE = """You are one stage of the policy-action-check builder, running through a plain API call.
You cannot run commands, open files or fetch URLs. Everything you need is in the user message.
Wherever the instructions below say to run `pac ...` (validate, terms-lint, exam, embed-terms,
manifest, guide), the builder runs it after you reply and sends you any errors to fix. Where they
say to ask the person, write the question into the file the instructions name instead.

Reply with exactly one JSON object and nothing else (no prose before or after it):
{"files": {"<path relative to the bundle folder>": <content>, ...},
 "notes": "<the short reply the instructions ask you to give, as plain text>"}

- A .json file's content is the JSON value itself (an object), not a string.
- A .jsonl file's content is a list of objects, one per line.
- A .md file's content is a string.
- Give every file you write in full. Never abbreviate with "..." or comments.
"""


class BuildError(Exception):
    pass


@dataclass
class StageResult:
    stage: str
    attempts: int = 0
    files: list[str] = field(default_factory=list)
    notes: str = ""
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"stage": self.stage, "attempts": self.attempts, "files": self.files,
                "notes": self.notes, "errors": self.errors}


# ---------------------------------------------------------------- prompts


def agent_prompt(stage: str) -> str:
    """The plugin agent's instructions, adapted for a model that cannot run tools."""
    text = (PROMPTS / "agents" / f"{AGENTS[stage]}.md").read_text(encoding="utf-8")
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return adapt(text).strip()


def adapt(text: str) -> str:
    """Rewrite plugin paths and commands for the builder (the builder runs `pac` itself)."""
    text = re.sub(r'python3 "\$\{CLAUDE_PLUGIN_ROOT\}/scripts/pac\.py"( --space <dir>)?', "pac", text)
    text = text.replace("`${CLAUDE_PLUGIN_ROOT}/references/SPEC.md`", "SPEC.md (below)")
    text = text.replace("`${CLAUDE_PLUGIN_ROOT}/references/method.md`", "method.md (below)")
    text = re.sub(r"\$\{CLAUDE_PLUGIN_ROOT\}/(scripts/)?", "", text)
    return re.sub(r"\bpac\.py\b", "pac", text)


def system_prompt(stage: str) -> str:
    spec = adapt((PROMPTS / "SPEC.md").read_text(encoding="utf-8"))
    method = adapt((PROMPTS / "method.md").read_text(encoding="utf-8"))
    return (f"{PREAMBLE}\n# Your instructions\n\n{agent_prompt(stage)}\n\n"
            f"# method.md\n\n{method}\n\n# SPEC.md\n\n{spec}\n")


def section(title: str, body: str, lang: str = "") -> str:
    fence = "````" if "```" in body else "```"
    return f"## {title}\n\n{fence}{lang}\n{body.rstrip()}\n{fence}\n"


# ---------------------------------------------------------------- inputs


def read_source(source: str, max_chars: int) -> tuple[str, str | None]:
    """Text of a file or URL, and a note when it had to be shortened."""
    if re.match(r"https?://", source):
        req = urllib.request.Request(source, headers={"User-Agent": "policy-action-check"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode(resp.headers.get_content_charset() or "utf-8", "replace")
        text = html_to_text(raw) if "<html" in raw[:2000].lower() else raw
    else:
        text = Path(source).read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return text, None
    lines, kept, size = text.splitlines(), [], 0
    for line in lines:
        if size + len(line) + 1 > max_chars:
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(kept), f"shortened to the first {len(kept)} of {len(lines)} lines ({max_chars} characters)"


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section)>", "\n", raw)
    raw = re.sub(r"(?i)<h([1-6])[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", raw)
    raw = re.sub(r"(?i)<li[^>]*>", "\n- ", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


# ---------------------------------------------------------------- replies


def parse_reply(text: str) -> dict:
    """The JSON object in a model reply, tolerating code fences and stray prose."""
    candidates = [text.strip()]
    candidates += [m.group(1) for m in re.finditer(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)]
    if "{" in text:
        candidates.append(text[text.index("{"): text.rindex("}") + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj if "files" in obj else {"files": obj, "notes": ""}
    raise BuildError("the reply is not a JSON object with a \"files\" field")


def encode_file(path: str, content) -> bytes:
    if path.endswith(".jsonl"):
        if isinstance(content, str):
            rows = [json.loads(line) for line in content.splitlines() if line.strip()]
        elif isinstance(content, list):
            rows = content
        else:
            raise BuildError(f"{path}: expected a list of objects")
        return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
    if path.endswith(".json"):
        if isinstance(content, str):
            content = json.loads(content)
        return (json.dumps(content, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if not isinstance(content, str):
        raise BuildError(f"{path}: expected text")
    return (content.rstrip("\n") + "\n").encode("utf-8")


def write_files(root: Path, stage: str, files: dict) -> list[str]:
    if not isinstance(files, dict):
        raise BuildError("\"files\" must be an object of path -> content")
    allowed = OUTPUTS[stage]
    unknown = [p for p in files if p not in allowed]
    if unknown:
        raise BuildError(f"this stage may write only {', '.join(allowed)}; got {', '.join(unknown)}")
    missing = [p for p in REQUIRED[stage] if p not in files]
    if missing:
        raise BuildError(f"missing required file(s): {', '.join(missing)}")
    encoded = {}
    for path, content in files.items():
        try:
            encoded[path] = encode_file(path, content)
        except (json.JSONDecodeError, ValueError) as exc:
            raise BuildError(f"{path}: not valid JSON ({exc})") from exc
    for path, data in encoded.items():
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return sorted(encoded)


# ---------------------------------------------------------------- checks (the plugin's hooks)


def check_stage(root: Path, stage: str) -> list[str]:
    if stage == "filter":
        lint = engine.terms_lint(root)
        errs = [f"terms-lint: {e}" if isinstance(e, str) else f"terms-lint: {json.dumps(e)}" for e in lint["errors"]]
        return errs + engine.validate(root, "filter")
    if stage == "scope":
        return engine.validate(root, "scope")
    if stage in ("build", "verify"):
        if (root / "work" / "terms.json").exists():
            engine.embed_terms(root)
        if stage == "verify" or (root / "bundle.json").exists():
            engine.write_manifest(root)  # refresh digests so validate checks the new files
    if stage == "build":
        return engine.validate(root, "build", check_guide=False)
    if stage == "verify":
        errs = engine.validate(root, "build", check_guide=False)
        if errs:
            return errs
        res = engine.exam(root)
        if not res["passed"]:
            return ["the bundle fails its self-exam (pac exam). Fix the bundle, not the thresholds:"] + [
                f"{c['id']} ({c['detail']}): " + "; ".join(c["failures"][:5]) for c in res["checks"] if not c["passed"]]
        return []
    if stage == "refine":
        engine.apply_patch(root, dry_run=True)
        return []
    raise ValueError(stage)


def file_block(root: Path, rel: str, limit: int | None = None) -> str:
    path = root / rel
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    note = ""
    if limit and len(text) > limit:
        text, note = text[:limit], f" (first {limit} characters)"
    return section(f"Current {rel}{note}", text, "json" if rel.endswith((".json", ".jsonl")) else "")


# ---------------------------------------------------------------- the pipeline


class Builder:
    def __init__(self, root: Path, provider, *, policy: str | None = None, history: str | None = None,
                 situation: str = "", interactive: bool = False, max_attempts: int = 4,
                 max_input_chars: int = 400_000, log=None, ask=input):
        self.root, self.provider = root, provider
        self.policy, self.history, self.situation = policy, history, situation
        self.interactive, self.max_attempts, self.max_input_chars = interactive, max_attempts, max_input_chars
        self.log = log or (lambda msg: print(f"[pac build] {msg}", file=sys.stderr))
        self.ask = ask
        self.results: list[StageResult] = []
        self.applied_decisions: list[dict] = []
        self.input_notes: list[str] = []
        self._policy_text: str | None = None
        self._history_text: str | None = None

    # inputs ---------------------------------------------------------

    def policy_text(self) -> str:
        if self._policy_text is None:
            if not self.policy:
                raise BuildError("this stage needs the policy: pass --policy <file or URL>")
            self._policy_text, note = read_source(self.policy, self.max_input_chars)
            if note:
                self.input_notes.append(f"policy {note}")
        return self._policy_text

    def history_text(self) -> str | None:
        if self.history and self._history_text is None:
            self._history_text, note = read_source(self.history, self.max_input_chars // 2)
            if note:
                self.input_notes.append(f"history {note}")
        return self._history_text

    def common_inputs(self, *, policy: bool = True, history: bool = True) -> str:
        parts = [f"Bundle folder: {self.root.name}/ (write paths relative to it)\n"]
        if policy:
            parts.append(section(f"Policy ({self.policy})", self.policy_text()))
        if history and self.history_text() is not None:
            parts.append(section(f"Action history ({self.history})", self.history_text()))
        elif history:
            parts.append("## Action history\n\nNone was given. Build from the terms alone.\n")
        if self.situation:
            parts.append(section("What the person said about their situation", self.situation))
        if self.input_notes:
            parts.append("Note: " + "; ".join(self.input_notes) + ".\n")
        return "\n".join(parts)

    def terms_context(self) -> str:
        """pac terms-context for every normative clause that links to or from another clause."""
        try:
            clauses = engine._read_terms(self.root).get("clauses", [])
        except (engine.SpaceError, OSError):
            return ""
        out = {}
        for c in clauses:
            if c.get("class") not in engine.NORMATIVE:
                continue
            ctx = engine.terms_context(self.root, c["id"])
            if len(ctx["clauses"]) > 1 or ctx["unresolved"]:
                out[c["id"]] = ctx
        return section("pac terms-context for each linked normative clause", json.dumps(out, indent=1), "json") if out else ""

    def stage_input(self, stage: str) -> str:
        r = self.root
        if stage == "filter":
            return self.common_inputs(history=False)
        if stage == "scope":
            return self.common_inputs() + file_block(r, "work/terms.json")
        if stage == "build":
            return (self.common_inputs() + file_block(r, "work/terms.json") + file_block(r, "work/scope.json")
                    + self.terms_context())
        if stage == "verify":
            blocks = [file_block(r, p) for p in ("work/terms.json", "work/scope.json", "generator.json", "judge.json",
                                                 "conformance.jsonl", "work/build_report.md")]
            blocks.append(file_block(r, "records.jsonl", limit=60_000))
            exam = engine.exam(r, write=False)
            summary = [{k: c[k] for k in ("id", "passed", "score", "threshold", "detail")}
                       | ({"failures": c["failures"][:10]} if c["failures"] else {}) for c in exam["checks"]]
            blocks.append(section("pac exam (current result)", json.dumps(summary, indent=1), "json"))
            return (self.common_inputs() + "".join(blocks)
                    + "\nReturn only the files you change (full contents); return an empty \"files\" object if none.\n")
        raise ValueError(stage)

    # stages ---------------------------------------------------------

    def run_stage(self, stage: str, make_user) -> StageResult:
        """Ask, write, check; on failure send the errors (and the current files) back."""
        result = StageResult(stage)
        system = system_prompt(stage)
        message = make_user()
        for attempt in range(1, self.max_attempts + 1):
            result.attempts = attempt
            self.log(f"{stage}: asking {getattr(self.provider, 'model', 'the model')} (attempt {attempt})")
            reply = self.provider.complete(system, message)
            try:
                obj = parse_reply(reply)
                written = write_files(self.root, stage, obj.get("files") or {})
                result.files = sorted(set(result.files) | set(written))
                result.notes = str(obj.get("notes") or "")
                errors = check_stage(self.root, stage)
            except (BuildError, engine.SpaceError, OSError, KeyError, TypeError, ValueError) as exc:
                errors = [str(exc)]
            if not errors:
                result.errors = []
                self.log(f"{stage}: passed its checks")
                return result
            result.errors = errors[:MAX_ERRORS]
            self.log(f"{stage}: {len(errors)} problem(s), sending them back")
            current = "" if stage == "verify" else "".join(file_block(self.root, p) for p in OUTPUTS[stage])
            message = (make_user() + "\n" + current + "\n## Your previous reply was not accepted\n\n"
                       + "\n".join(f"- {e}" for e in result.errors)
                       + "\n\nFix every problem and reply again in the same JSON format with the complete files.\n")
        raise BuildError(f"{stage} did not pass its checks after {self.max_attempts} attempts:\n- "
                         + "\n- ".join(result.errors[:10]))

    def settle_scope(self) -> None:
        """Answer each `decision: "ask"` area: ask the person, or apply the recommendation."""
        path = self.root / "work" / "scope.json"
        scope = json.loads(path.read_text(encoding="utf-8"))
        choices = ("in_scope_examples", "terms_only", "out_of_scope")
        for area in scope.get("areas", []):
            if area.get("decision") != "ask":
                continue
            rec = area.get("recommended") if area.get("recommended") in choices else "terms_only"
            decision = rec
            if self.interactive:
                answer = self.ask(f"{area.get('question') or area.get('area')}\n"
                                  f"  [{'/'.join(choices)}] (Enter = {rec}): ").strip()
                decision = answer if answer in choices else rec
            area["decision"] = decision
            self.applied_decisions.append({"area": area.get("area"), "decision": decision,
                                           "recommended": rec, "asked": self.interactive})
        path.write_text(json.dumps(scope, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        engine.write_state(self.root, awaiting_user=False)

    def finish(self) -> dict:
        engine.write_manifest(self.root, self.policy)
        exam = engine.exam(self.root)
        engine.write_manifest(self.root, self.policy)  # stamp the exam badge on the final files
        errors = engine.validate(self.root)
        engine.write_state(self.root, stage="done")
        return {"exam": {"passed": exam["passed"], "checks": [
            {k: c[k] for k in ("id", "passed", "score", "threshold")} for c in exam["checks"]]},
            "validate_errors": errors}

    def build(self, start: str = "filter") -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        inputs = {k: v for k, v in (("policy", self.policy), ("history", self.history)) if v}
        engine.write_state(self.root, inputs=inputs, digests={k: engine.file_digest(v) for k, v in inputs.items()})
        for stage in STAGE_ORDER[STAGE_ORDER.index(start):]:
            engine.write_state(self.root, stage="build" if stage == "verify" else stage)
            self.results.append(self.run_stage(stage, lambda s=stage: self.stage_input(s)))
            if stage == "scope":
                self.settle_scope()
        final = self.finish()
        return {"ok": final["exam"]["passed"] and not final["validate_errors"], "space": str(self.root),
                "stages": [r.as_dict() for r in self.results], "scope_decisions": self.applied_decisions,
                "input_notes": self.input_notes, **final}

    def refine(self, focus: str | None = None, rounds: int = 3) -> dict:
        history = []
        before = None
        for n in range(1, rounds + 1):
            report = engine.gaps(self.root, focus, write=True)
            counts = _count_kinds(report["gaps"])
            history.append({"round": n, "exam_passed": report["exam_passed"], "gaps": counts})
            total = sum(counts.values())
            if before is not None and report["exam_passed"] and total >= before:
                break
            if not report["gaps"]:
                break
            before = total
            gap_bytes = (self.root / "work" / "gaps.json").read_bytes()
            user = (self.common_inputs(policy=bool(self.policy))
                    + section("work/gaps.json", gap_bytes.decode("utf-8"), "json")
                    + f"\nbased_on (SHA-256 of the exact work/gaps.json bytes): {hashlib.sha256(gap_bytes).hexdigest()}\n"
                    + (f"focus: {focus!r}\n" if focus else "focus: null\n")
                    + "".join(file_block(self.root, p) for p in ("work/terms.json", "generator.json", "judge.json",
                                                                 "conformance.jsonl"))
                    + file_block(self.root, "records.jsonl", limit=60_000) + self.terms_context())
            result = self.run_stage("refine", lambda u=user: u)
            self.results.append(result)
            applied = engine.apply_patch(self.root)
            history[-1]["applied"] = applied
        report = engine.gaps(self.root, focus)
        return {"ok": report["exam_passed"], "space": str(self.root), "rounds": history,
                "gaps_after": _count_kinds(report["gaps"]), "stages": [r.as_dict() for r in self.results]}


def _count_kinds(items: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for item in items:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    return counts


# ---------------------------------------------------------------- CLI


def _provider_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("model (build time only)")
    g.add_argument("--provider", help=f"{', '.join(PRESETS)} or replay (env PAC_PROVIDER, default openrouter)")
    g.add_argument("--model", help="model id, for example openai/gpt-4.1 on OpenRouter (env PAC_MODEL)")
    g.add_argument("--base-url", help="API base URL, for openai-compatible servers (env PAC_BASE_URL)")
    g.add_argument("--api-key", help="API key; prefer the provider's environment variable")
    g.add_argument("--max-tokens", type=int, help="largest reply per call (default 16000)")
    g.add_argument("--timeout", type=float, help="seconds per call (default 600)")
    g.add_argument("--replay", help="answer from a file of canned replies (testing)")
    g.add_argument("--attempts", type=int, default=4, help="tries per stage before giving up (default 4)")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pac", description="Build or refine a bundle with a language model.")
    ap.add_argument("--space", help="bundle folder (default ./policy_space)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="policy (+ history) -> bundle, end to end")
    b.add_argument("--policy", required=True, help="policy file or URL")
    b.add_argument("--history", help="action history file (CSV, JSONL or text)")
    b.add_argument("--situation", default="", help="anything about the person's situation the scope stage should know")
    b.add_argument("--from", dest="start", choices=STAGE_ORDER, default="filter", help="resume from a stage")
    b.add_argument("--interactive", action="store_true", help="ask scope questions on the terminal")
    _provider_args(b)
    r = sub.add_parser("refine", help="close exam and case-map gaps in an existing bundle")
    r.add_argument("--focus", help="limit to gaps about these words, for example privacy")
    r.add_argument("--policy", help="the policy, so new rules can cite it")
    r.add_argument("--history", help="the original history")
    r.add_argument("--rounds", type=int, default=3)
    _provider_args(r)
    sub.add_parser("providers", help="list the supported providers and their settings")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.cmd == "providers":
        for name, (wire, url, key) in PRESETS.items():
            print(f"{name:18} {wire:9} base URL {url or '(--base-url)'}  key {key or '(none)'}")
        print(f"{'replay':18} {'file':9} canned replies from --replay <file> (tests, no network)")
        return 0
    root = engine.space_dir(args.space)
    try:
        provider = make_provider(args.provider, args.model, base_url=args.base_url, api_key=args.api_key,
                                 max_tokens=args.max_tokens, timeout=args.timeout, replay=args.replay)
        builder = Builder(root, provider, policy=args.policy, history=args.history,
                          situation=getattr(args, "situation", ""), interactive=getattr(args, "interactive", False),
                          max_attempts=args.attempts)
        out = builder.build(args.start) if args.cmd == "build" else builder.refine(args.focus, args.rounds)
    except (BuildError, ProviderError, engine.SpaceError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out["ok"] else 1
