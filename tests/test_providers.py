"""Provider wire formats, checked against a local HTTP server (no network, no keys)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from policy_action_check.providers import Provider, ProviderError, make_provider


class Recorder(BaseHTTPRequestHandler):
    replies: ClassVar[list] = []
    seen: ClassVar[list] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        status, payload = type(self).replies.pop(0)
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if status == 429:
            self.send_header("retry-after", "0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Recorder.replies, Recorder.seen = [], []
    httpd = HTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", Recorder
    httpd.shutdown()


def test_openai_compatible_request_and_reply(server):
    url, rec = server
    rec.replies = [(200, {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]})]
    p = Provider(name="openrouter", wire="openai", base_url=url + "/api/v1", model="m", api_key="k", max_tokens=99)
    assert p.complete("sys", "usr") == "hello"
    req = rec.seen[0]
    assert req["path"] == "/api/v1/chat/completions"
    assert req["headers"]["authorization"] == "Bearer k"
    assert req["body"] == {"model": "m", "max_tokens": 99, "messages": [
        {"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}]}


def test_anthropic_request_and_reply(server):
    url, rec = server
    rec.replies = [(200, {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "a"},
                                      {"type": "text", "text": "b"}], "stop_reason": "end_turn"})]
    p = Provider(name="anthropic", wire="anthropic", base_url=url, model="claude-x", api_key="k")
    assert p.complete("sys", "usr") == "ab"
    req = rec.seen[0]
    assert req["path"] == "/v1/messages"
    assert req["headers"]["x-api-key"] == "k" and req["headers"]["anthropic-version"] == "2023-06-01"
    assert req["body"]["system"] == "sys" and req["body"]["messages"] == [{"role": "user", "content": "usr"}]


def test_retries_rate_limits_then_succeeds(server):
    url, rec = server
    rec.replies = [(429, {"error": "slow down"}), (200, {"choices": [{"message": {"content": "ok"}}]})]
    p = Provider(name="ollama", wire="openai", base_url=url + "/v1", model="m", retries=2)
    assert p.complete("s", "u") == "ok" and len(rec.seen) == 2
    assert "authorization" not in rec.seen[0]["headers"]


def test_client_errors_are_not_retried(server):
    url, rec = server
    rec.replies = [(401, {"error": "bad key"})]
    p = Provider(name="openai", wire="openai", base_url=url, model="m", api_key="x", retries=3)
    with pytest.raises(ProviderError, match="HTTP 401"):
        p.complete("s", "u")
    assert len(rec.seen) == 1


@pytest.mark.parametrize("wire,payload", [
    ("openai", {"choices": [{"message": {"content": "half"}, "finish_reason": "length"}]}),
    ("anthropic", {"content": [{"type": "text", "text": "half"}], "stop_reason": "max_tokens"}),
])
def test_truncated_replies_are_errors(server, wire, payload):
    url, rec = server
    rec.replies = [(200, payload)]
    with pytest.raises(ProviderError, match="max_tokens"):
        Provider(name=wire, wire=wire, base_url=url, model="m", api_key="k").complete("s", "u")


def test_make_provider_presets_and_environment(monkeypatch):
    for var in ("PAC_PROVIDER", "PAC_MODEL", "PAC_BASE_URL", "PAC_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ProviderError, match="--model"):
        make_provider("openrouter")
    with pytest.raises(ProviderError, match="OPENROUTER_API_KEY"):
        make_provider("openrouter", "some/model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    p = make_provider(None, "some/model")
    assert (p.name, p.wire, p.base_url, p.api_key) == ("openrouter", "openai", "https://openrouter.ai/api/v1", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert make_provider("anthropic", "claude-opus-5-5").wire == "anthropic"
    assert make_provider("ollama", "llama3.1").api_key is None
    with pytest.raises(ProviderError, match="--base-url"):
        make_provider("openai-compatible", "m")
    assert make_provider("openai-compatible", "m", base_url="http://localhost:8000/v1").base_url.endswith("/v1")
    with pytest.raises(ProviderError, match="unknown provider"):
        make_provider("nope", "m")
