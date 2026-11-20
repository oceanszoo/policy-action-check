"""Model providers for the build-time pipeline (standard library only).

Every provider answers one question: given a system prompt and a user message, return the
model's text. Two wire formats cover the usual choices:

- OpenAI-compatible chat completions: OpenRouter, OpenAI, Ollama, LM Studio, vLLM, and any
  other server that speaks ``POST {base_url}/chat/completions``;
- the Anthropic Messages API (``POST {base_url}/v1/messages``).

Only the builder uses a model. Judging with a finished bundle never does.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


class ProviderError(Exception):
    pass


# name -> (wire format, default base URL, API key environment variable or None)
PRESETS: dict[str, tuple[str, str | None, str | None]] = {
    "openrouter": ("openai", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "openai": ("openai", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    "anthropic": ("anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY"),
    "ollama": ("openai", "http://localhost:11434/v1", None),
    "openai-compatible": ("openai", None, "PAC_API_KEY"),
}
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


@dataclass
class Provider:
    """One configured model endpoint."""

    name: str
    wire: str  # "openai" or "anthropic"
    base_url: str
    model: str
    api_key: str | None = None
    max_tokens: int = 16000
    timeout: float = 600.0
    retries: int = 3
    headers: dict[str, str] = field(default_factory=dict)

    def complete(self, system: str, user: str) -> str:
        if self.wire == "anthropic":
            return self._anthropic(system, user)
        return self._openai(system, user)

    # ------------------------------------------------------------ wire formats

    def _openai(self, system: str, user: str) -> str:
        body = {"model": self.model, "max_tokens": self.max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = self._post(self.base_url.rstrip("/") + "/chat/completions", body, headers)
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.name}: unexpected response: {json.dumps(data)[:500]}") from exc
        if choice.get("finish_reason") == "length":
            raise ProviderError(f"{self.name}: the reply hit max_tokens ({self.max_tokens}); "
                                "raise --max-tokens or use a model with a larger output limit")
        return text

    def _anthropic(self, system: str, user: str) -> str:
        body = {"model": self.model, "max_tokens": self.max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        headers = {"anthropic-version": "2023-06-01"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        data = self._post(self.base_url.rstrip("/") + "/v1/messages", body, headers)
        stop = data.get("stop_reason")
        if stop == "refusal":
            raise ProviderError(f"{self.name}: the model declined the request ({data.get('stop_details')})")
        if stop == "max_tokens":
            raise ProviderError(f"{self.name}: the reply hit max_tokens ({self.max_tokens}); raise --max-tokens")
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ProviderError(f"{self.name}: unexpected response: {json.dumps(data)[:500]}")
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

    def _post(self, url: str, body: dict, headers: dict[str, str]) -> dict:
        payload = json.dumps(body).encode("utf-8")
        all_headers = {"Content-Type": "application/json", "User-Agent": "policy-action-check"}
        all_headers |= headers | self.headers
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=payload, headers=all_headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:800]
                if exc.code in RETRY_STATUS and attempt < self.retries:
                    time.sleep(_delay(attempt, exc.headers.get("retry-after")))
                    continue
                raise ProviderError(f"{self.name}: HTTP {exc.code} from {url}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt < self.retries:
                    time.sleep(_delay(attempt, None))
                    continue
                raise ProviderError(f"{self.name}: cannot reach {url}: {exc}") from exc
            except json.JSONDecodeError as exc:
                raise ProviderError(f"{self.name}: reply from {url} is not JSON") from exc
        raise ProviderError(f"{self.name}: no reply from {url}")  # pragma: no cover


def _delay(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 60.0)
        except ValueError:
            pass
    return min(2 ** attempt + random.random(), 30.0)


class ReplayProvider:
    """Answers from a file of canned replies, in order: for tests and offline dry runs.

    The file is JSON (a list of strings) or JSON Lines (one string per line).
    """

    name = "replay"
    model = "replay"

    def __init__(self, path: str | Path):
        text = Path(path).read_text(encoding="utf-8")
        try:
            replies = json.loads(text)
        except json.JSONDecodeError:
            replies = [json.loads(line) for line in text.splitlines() if line.strip()]
        if not isinstance(replies, list):
            raise ProviderError("replay file must hold a list of replies")
        self.replies = [r if isinstance(r, str) else json.dumps(r) for r in replies]
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if len(self.calls) > len(self.replies):
            raise ProviderError(f"replay: no reply left for call {len(self.calls)}")
        return self.replies[len(self.calls) - 1]


def make_provider(name: str | None = None, model: str | None = None, *, base_url: str | None = None,
                  api_key: str | None = None, max_tokens: int | None = None, timeout: float | None = None,
                  replay: str | None = None):
    """Build a provider from arguments, falling back to PAC_* environment variables."""
    name = (name or os.environ.get("PAC_PROVIDER") or ("replay" if replay else "openrouter")).lower()
    if name == "replay":
        path = replay or os.environ.get("PAC_REPLAY")
        if not path:
            raise ProviderError("replay provider needs --replay <file>")
        return ReplayProvider(path)
    if name not in PRESETS:
        raise ProviderError(f"unknown provider {name!r}; choose one of: {', '.join([*PRESETS, 'replay'])}")
    wire, default_url, key_env = PRESETS[name]
    model = model or os.environ.get("PAC_MODEL")
    if not model:
        raise ProviderError("choose a model with --model (or PAC_MODEL)")
    url = base_url or os.environ.get("PAC_BASE_URL") or default_url
    if not url:
        raise ProviderError(f"provider {name!r} needs --base-url (or PAC_BASE_URL)")
    key = api_key or (os.environ.get(key_env) if key_env else None) or os.environ.get("PAC_API_KEY")
    if key_env and not key and name != "openai-compatible":
        raise ProviderError(f"provider {name!r} needs an API key: set {key_env} (or PAC_API_KEY)")
    return Provider(name=name, wire=wire, base_url=url, model=model, api_key=key,
                    max_tokens=max_tokens or int(os.environ.get("PAC_MAX_TOKENS") or 16000),
                    timeout=timeout or float(os.environ.get("PAC_TIMEOUT") or 600))
