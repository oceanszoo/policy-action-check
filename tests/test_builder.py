"""The model-agnostic builder: prompts, reply parsing, repair loop, and a full offline build."""

import json
import shutil
from pathlib import Path

import pytest

from policy_action_check import builder, engine
from policy_action_check.providers import ReplayProvider

SAMPLE = Path(__file__).resolve().parent / "fixtures" / "sample-bundle"


def sample_terms() -> dict:
    """work/terms.json that matches the sample bundle's embedded terms."""
    judge = json.loads((SAMPLE / "judge.json").read_text(encoding="utf-8"))
    clauses = []
    for cid, term in judge["terms"].items():
        clause = {"id": cid, "section": term["section"], "class": term["class"],
                  "text": term.get("text") or term["summary"], "refs": term["refs"]}
        if term["class"] in engine.NORMATIVE:
            clause |= {"signals": ["no"], "binds": "user", "confidence": "high"}
        clauses.append(clause)
    return {"source": "https://example.com/sample-service-terms", "glossary": [], "clauses": clauses}


def policy_text(terms: dict) -> str:
    return "# Sample Service Terms\n\n" + "\n".join(f"- {c['text']}" for c in terms["clauses"]) + "\n"


def bundle_files(drop_terms: bool = True) -> dict:
    judge = json.loads((SAMPLE / "judge.json").read_text(encoding="utf-8"))
    if drop_terms:
        judge.pop("terms", None)  # embed-terms fills it from work/terms.json
    return {"generator.json": json.loads((SAMPLE / "generator.json").read_text(encoding="utf-8")),
            "judge.json": judge,
            "conformance.jsonl": [json.loads(x) for x in (SAMPLE / "conformance.jsonl").read_text().splitlines()],
            "work/build_report.md": "No history; no disagreements."}


class Scripted:
    """A provider that replays replies and records each request."""

    model = "scripted"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def complete(self, system, user):
        self.calls.append((system, user))
        reply = self.replies.pop(0)
        if callable(reply):
            reply = reply(user)
        return reply if isinstance(reply, str) else json.dumps(reply)


def test_agent_prompts_are_adapted_for_a_model_without_tools():
    for stage in ("filter", "scope", "build", "verify", "refine"):
        text = builder.system_prompt(stage)
        assert "CLAUDE_PLUGIN_ROOT" not in text and "pac.py" not in text
        assert '"files"' in text and "# SPEC.md" in text


@pytest.mark.parametrize("reply", [
    '{"files": {"a.json": {}}, "notes": "n"}',
    'Here you go:\n```json\n{"files": {"a.json": {}}, "notes": "n"}\n```\nDone.',
    'prose {"files": {"a.json": {}}, "notes": "n"} trailing',
])
def test_parse_reply_tolerates_fences_and_prose(reply):
    assert builder.parse_reply(reply)["files"] == {"a.json": {}}


def test_parse_reply_rejects_non_json():
    with pytest.raises(builder.BuildError):
        builder.parse_reply("I could not do it.")


def test_write_files_refuses_paths_outside_the_stage(tmp_path):
    with pytest.raises(builder.BuildError, match="may write only"):
        builder.write_files(tmp_path, "filter", {"work/terms.json": {}, "../evil.json": {}})
    with pytest.raises(builder.BuildError, match="missing required"):
        builder.write_files(tmp_path, "build", {"generator.json": {}})


def test_full_build_offline_with_a_repair_round(tmp_path):
    terms = sample_terms()
    (tmp_path / "policy.md").write_text(policy_text(terms), encoding="utf-8")
    scope = {"areas": [{"area": "Physical equipment", "examples": 0, "gap": True, "source_terms": ["c022", "c023", "c024"],
                        "recommended": "terms_only", "decision": "ask",
                        "question": "Do you control physical equipment? (recommended: terms only)"}],
             "unmatched_history_share": None, "constants": {}}
    broken = bundle_files()
    del broken["conformance.jsonl"]
    provider = Scripted([
        {"files": {"work/terms.json": terms}, "notes": "28 clauses"},
        {"files": {"work/scope.json": scope}, "notes": "one gap"},
        {"files": broken, "notes": "first try"},  # missing a required file: sent back
        {"files": bundle_files(), "notes": "fixed"},
        {"files": {}, "notes": "verified, nothing to change"},
    ])
    space = tmp_path / "space"
    out = builder.Builder(space, provider, policy=str(tmp_path / "policy.md"), log=lambda m: None).build()
    assert out["ok"], out
    assert [s["attempts"] for s in out["stages"]] == [1, 1, 2, 1]
    assert out["scope_decisions"] == [{"area": "Physical equipment", "decision": "terms_only",
                                       "recommended": "terms_only", "asked": False}]
    assert "missing required file(s): conformance.jsonl" in provider.calls[3][1]
    assert out["exam"]["passed"] and not out["validate_errors"]
    manifest = json.loads((space / "bundle.json").read_text())
    assert manifest["exam"]["passed"] and (space / "BUNDLE.md").exists()
    # the finished bundle judges with no model at all
    res = engine.check("@support-bot disc=n", engine.Space(space))
    assert res["verdict"] == "violates"


def test_interactive_scope_question(tmp_path):
    root = tmp_path / "space"
    (root / "work").mkdir(parents=True)
    (root / "work" / "scope.json").write_text(json.dumps({"areas": [
        {"area": "A", "decision": "ask", "recommended": "out_of_scope", "question": "Q?"}]}))
    b = builder.Builder(root, None, interactive=True, ask=lambda prompt: "in_scope_examples", log=lambda m: None)
    b.settle_scope()
    assert json.loads((root / "work" / "scope.json").read_text())["areas"][0]["decision"] == "in_scope_examples"


def test_build_gives_up_after_the_attempt_limit(tmp_path):
    (tmp_path / "p.md").write_text("Do not do bad things.")
    provider = Scripted(["not json"] * 2)
    with pytest.raises(builder.BuildError, match="after 2 attempts"):
        builder.Builder(tmp_path / "s", provider, policy=str(tmp_path / "p.md"), max_attempts=2,
                        log=lambda m: None).build()


def test_refine_stops_without_a_model_when_there_are_no_gaps(tmp_path):
    space = Path(shutil.copytree(SAMPLE, tmp_path / "space"))
    provider = Scripted([])
    out = builder.Builder(space, provider, log=lambda m: None).refine()
    assert out["ok"] and provider.calls == []


def test_refine_round_applies_a_checked_patch(tmp_path):
    space = Path(shutil.copytree(SAMPLE, tmp_path / "space"))
    lines = (space / "conformance.jsonl").read_text().splitlines()
    removed = [x for x in lines if json.loads(x)["expected"]["violated"] == ["P-weapons"]]
    assert removed
    (space / "conformance.jsonl").write_text("".join(x + "\n" for x in lines if x not in removed))
    engine.write_manifest(space)
    assert not engine.gaps(space)["exam_passed"]

    def patch(user, based_on=None):
        sha = based_on or user.split("based_on (SHA-256 of the exact work/gaps.json bytes): ")[1].split()[0]
        gap_ids = [g["id"] for g in json.loads((space / "work" / "gaps.json").read_text())["gaps"]]
        ops = [{"op": "add_vector", "line": json.loads(x), "reason": "the terms forbid weapons work",
                "gaps": gap_ids} for x in removed]
        return {"files": {"work/refine_patch.json": {"focus": None, "based_on": sha, "ops": ops}}}

    provider = Scripted([lambda user: patch(user, based_on="0" * 64), patch])  # first one is stale: sent back
    out = builder.Builder(space, provider, log=lambda m: None).refine(rounds=2)
    assert out["ok"], out
    assert "gaps changed, re-run gaps" in provider.calls[1][1]
    assert out["rounds"][0]["gaps"] and out["gaps_after"] == {}
    assert len((space / "conformance.jsonl").read_text().splitlines()) == len(lines)


def test_replay_provider_reads_json_lines(tmp_path):
    path = tmp_path / "r.jsonl"
    path.write_text('"first"\n{"files": {}}\n')
    p = ReplayProvider(path)
    assert p.complete("s", "u") == "first" and json.loads(p.complete("s", "u")) == {"files": {}}


def test_cli_build_with_replay(tmp_path, capsys):
    terms = sample_terms()
    (tmp_path / "policy.md").write_text(policy_text(terms), encoding="utf-8")
    replies = [{"files": {"work/terms.json": terms}}, {"files": {"work/scope.json": {"areas": [], "constants": {}}}},
               {"files": bundle_files()}, {"files": {}}]
    (tmp_path / "replies.json").write_text(json.dumps([json.dumps(r) for r in replies]))
    from policy_action_check.cli import main
    code = main(["--space", str(tmp_path / "b"), "build", "--policy", str(tmp_path / "policy.md"),
                 "--provider", "replay", "--replay", str(tmp_path / "replies.json")])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"]


def test_build_from_verify_reuses_existing_files(tmp_path):
    space = Path(shutil.copytree(SAMPLE, tmp_path / "space"))
    terms = sample_terms()
    (space / "work").mkdir()
    (space / "work" / "terms.json").write_text(json.dumps(terms))
    (tmp_path / "policy.md").write_text(policy_text(terms))
    provider = Scripted([{"files": {}, "notes": "nothing to change"}])
    out = builder.Builder(space, provider, policy=str(tmp_path / "policy.md"), log=lambda m: None).build("verify")
    assert out["ok"], out
    assert [s["stage"] for s in out["stages"]] == ["verify"] and len(provider.calls) == 1
    assert "pac exam (current result)" in provider.calls[0][1]


def test_policy_from_a_url(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    page = b"<html><head><style>x{}</style></head><body><h1>Terms</h1><p>Do not resell access.</p></body></html>"

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        text, note = builder.read_source(f"http://127.0.0.1:{httpd.server_port}/terms", 10_000)
    finally:
        httpd.shutdown()
    assert note is None and "# Terms" in text and "Do not resell access." in text and "x{}" not in text
    short, note = builder.read_source(str(tmp_path / "p.md") if (tmp_path / "p.md").write_text("a\n" * 50) else "", 20)
    assert note and short.count("a") < 50
