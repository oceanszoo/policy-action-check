"""The MCP server speaks JSON-RPC over stdio and answers with the exact engine."""

import json
import subprocess
import sys
from pathlib import Path

from policy_action_check import engine
from policy_action_check.mcp_server import TOOLS, Server

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests" / "fixtures" / "sample-bundle"


def rpc(server, method, params=None, ident=1):
    return server.handle({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})


def test_initialize_and_list_tools():
    server = Server(SAMPLE)
    init = rpc(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "0"}})["result"]
    assert init["protocolVersion"] == "2025-06-18" and "tools" in init["capabilities"]
    assert rpc(server, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == "2025-06-18"
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in rpc(server, "tools/list")["result"]["tools"]}
    assert {"check", "report", "lookup", "next_question", "categories", "fit", "parse", "bundle_info"} == names
    assert all(t["inputSchema"]["type"] == "object" for t in TOOLS)


def test_check_matches_the_engine():
    server = Server(SAMPLE)
    res = rpc(server, "tools/call", {"name": "check", "arguments": {"code": "@support-bot disc=n", "date": "2026-11-19"}})
    out = res["result"]
    assert not out["isError"]
    want = engine.check_flow("@support-bot disc=n", engine.Space(SAMPLE), "2026-11-19", {})
    assert out["structuredContent"] == json.loads(json.dumps(want))
    assert json.loads(out["content"][0]["text"])["verdict"] == "violates"


def test_report_is_plain_text_and_errors_are_tool_errors():
    server = Server(SAMPLE)
    text = rpc(server, "tools/call", {"name": "report", "arguments": {"code": "act=biz pur=std"}})["result"]
    assert "Allowed as coded." in text["content"][0]["text"]
    bad = rpc(server, "tools/call", {"name": "check", "arguments": {}})["result"]
    assert bad["isError"]
    assert rpc(server, "tools/call", {"name": "nope"})["error"]["code"] == -32602
    assert rpc(server, "no/such")["error"]["code"] == -32601


def test_stdio_round_trip():
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "lookup", "arguments": {"text": "chatbot"}}}]
    proc = subprocess.run([sys.executable, "-m", "policy_action_check", "--space", str(SAMPLE), "mcp"],
                          input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, text=True,
                          timeout=60, check=True, cwd=ROOT)
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[1]["result"]["isError"] is False
