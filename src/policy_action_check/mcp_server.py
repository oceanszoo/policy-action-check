"""MCP server: expose a bundle's judge to any MCP client over stdio (standard library only).

    pac mcp --space path/to/bundle

Every tool runs the exact engine on the bundle files. No model is involved, so the same code
always gets the same answer, whichever client or model asks.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from policy_action_check import __version__, engine

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
CODE = {"type": "string", "description": "action code, for example 'act=biz pur=std' or '@saved-code'; "
        "join cases with ' & ' (same time) or ' > ' (step by step)"}
DATE = {"type": "string", "description": "judge as of YYYY-MM-DD (default today)"}
NOTES = {"type": "object", "additionalProperties": {"type": "string"},
         "description": "notes shown with the result and never judged: {key: text} or {'_action': text}"}

TOOLS = [
    {"name": "check", "description": "Judge an action code: verdict, violated rules, requirements, unknown facts, "
     "the smallest fix and what to do next, as JSON.",
     "inputSchema": {"type": "object", "properties": {"code": CODE, "date": DATE, "notes": NOTES}, "required": ["code"]}},
    {"name": "report", "description": "Judge an action code and return the bundle's fixed plain-text report.",
     "inputSchema": {"type": "object", "properties": {"code": CODE, "date": DATE, "notes": NOTES}, "required": ["code"]}},
    {"name": "parse", "description": "Parse an action code: features, errors with suggestions, canonical form.",
     "inputSchema": {"type": "object", "properties": {"code": CODE}, "required": ["code"]}},
    {"name": "lookup", "description": "Find code tokens for everyday words using the bundle's phrase list.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "next_question", "description": "The question form: given the answers so far (a partial code), "
     "return the next question to ask, or the result when done.",
     "inputSchema": {"type": "object", "properties": {"answers": {"type": "string", "default": ""}, "date": DATE}}},
    {"name": "categories", "description": "The bundle's case map: a tree of categories with their features.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "fit", "description": "Which case-map category an action code fits.",
     "inputSchema": {"type": "object", "properties": {"code": CODE}, "required": ["code"]}},
    {"name": "bundle_info", "description": "The bundle's manifest (with its self-exam badge), digest check, policy, and validation result.",
     "inputSchema": {"type": "object", "properties": {}}},
]


class Server:
    def __init__(self, root: Path):
        self.root = root

    def space(self) -> engine.Space:
        return engine.Space(self.root)  # re-read on every call, so edits to the bundle are seen

    def call(self, name: str, args: dict):
        notes = {str(k): str(v) for k, v in (args.get("notes") or {}).items()}
        if name == "check":
            return engine.check_flow(args["code"], self.space(), args.get("date"), notes)
        if name == "report":
            sp = self.space()
            return engine.render_report(engine.check_flow(args["code"], sp, args.get("date"), notes), sp)
        if name == "parse":
            sp = self.space()
            parsed = engine.parse_code(args["code"], sp)
            parsed["canonical"] = engine.format_code(parsed["features"], sp, parsed["name"])
            return parsed
        if name == "lookup":
            return engine.lookup(args["text"], self.space())
        if name == "next_question":
            return engine.next_question(args.get("answers", ""), self.space(), args.get("date"))
        if name == "categories":
            return engine.categories(self.space())
        if name == "fit":
            sp = self.space()
            parsed = engine.parse_code(args["code"], sp)
            return engine.fit_detail(parsed["features"], sp, parsed["date"])
        if name == "bundle_info":
            sp = self.space()
            manifest = engine._read_json(self.root / "bundle.json", {})
            return {"folder": str(self.root), "policy": sp.generator.get("policy"), "manifest": manifest,
                    "verification": engine.verify_manifest(manifest, engine._bundle_bytes(self.root)) if manifest else None,
                    "validate_errors": engine.validate(self.root)}
        raise KeyError(name)

    def handle(self, msg: dict) -> dict | None:
        method, ident = msg.get("method"), msg.get("id")
        if ident is None:  # a notification, such as notifications/initialized
            return None
        params = msg.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "policy-action-check", "version": __version__},
                      "instructions": "Judge actions against a policy bundle. Write the action as a code "
                      "(use lookup, categories or next_question to find tokens), then call check or report. "
                      "Unknown facts stay unknown; never guess a value the person did not state."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in {t["name"] for t in TOOLS}:
                return _error(ident, -32602, f"unknown tool {name!r}")
            try:
                out = self.call(name, args)
                text = out if isinstance(out, str) else json.dumps(out, indent=2, ensure_ascii=False)
                result = {"content": [{"type": "text", "text": text}], "isError": False}
                if isinstance(out, dict):
                    result["structuredContent"] = out
            except (engine.SpaceError, KeyError, TypeError, ValueError, OSError) as exc:
                result = {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True}
        else:
            return _error(ident, -32601, f"method not found: {method}")
        return {"jsonrpc": "2.0", "id": ident, "result": result}


def _error(ident, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def serve(root: Path, stdin=None, stdout=None) -> None:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    server = Server(root)
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = _error(None, -32700, "parse error")
        else:
            reply = server.handle(msg) if isinstance(msg, dict) else _error(None, -32600, "invalid request")
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pac mcp", description="Serve a bundle's judge over MCP (stdio).")
    ap.add_argument("--space", help="bundle folder (default ./policy_space or PAC_SPACE)")
    ap.add_argument("cmd", nargs="?", choices=["mcp"], help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    serve(engine.space_dir(args.space))
    return 0
