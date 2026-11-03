"""Replaceable model providers.

The rest of the app only calls `complete_json(system, user, schema)`. Providers have no tools and cannot act:
they return text that the caller validates. Swapping model or vendor is configuration, not code.
"""
import json
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from leadgen.config import Settings


class ProviderError(RuntimeError):
    pass


@dataclass
class Completion:
    data: dict
    latency_ms: int
    provider: str
    model: str


class Provider(Protocol):
    name: str
    model: str

    def complete_json(self, system: str, user: str, schema: dict) -> Completion: ...

    def status(self) -> tuple[bool, str]: ...


def _parse(text: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderError("model returned text that is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ProviderError("model returned JSON that is not an object")
    return data


class OllamaProvider:
    name = "ollama"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.model = settings.ai_model
        self.url = settings.ollama_url.rstrip("/")
        self.num_gpu = settings.ai_num_gpu
        self.client = client or httpx.Client(timeout=settings.ai_timeout_seconds)

    def status(self) -> tuple[bool, str]:
        try:
            tags = self.client.get(f"{self.url}/api/tags", timeout=5).json()
        except (httpx.HTTPError, ValueError):
            return False, f"Ollama is not running at {self.url}"
        names = [m.get("name") for m in tags.get("models", [])]
        if self.model not in names:
            return False, f"Model {self.model} is not installed (installed: {', '.join(names) or 'none'})"
        return True, f"Local model {self.model} via Ollama"

    def complete_json(self, system: str, user: str, schema: dict) -> Completion:
        options = {"temperature": 0.2, "num_ctx": 4096}
        if self.num_gpu is not None:
            options["num_gpu"] = self.num_gpu
        t0 = time.perf_counter()
        try:
            resp = self.client.post(f"{self.url}/api/chat", json={
                "model": self.model, "stream": False, "format": schema, "think": False, "options": options,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            })
        except httpx.TimeoutException as exc:
            raise ProviderError("the local model took too long to answer") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"could not reach Ollama ({type(exc).__name__})") from exc
        if resp.status_code != 200:
            try:
                detail = str(resp.json().get("error", ""))[:200]
            except ValueError:
                detail = ""
            if "out of memory" in detail.lower():
                detail = "not enough memory to load the model; close other apps and retry"
            raise ProviderError(f"Ollama error {resp.status_code}: {detail}")
        content = resp.json().get("message", {}).get("content", "")
        return Completion(_parse(content), round((time.perf_counter() - t0) * 1000), self.name, self.model)


class OpenAICompatibleProvider:
    """Any server exposing /v1/chat/completions (llama.cpp server, LM Studio, vLLM, or a hosted API)."""
    name = "openai_compatible"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if not settings.ai_base_url:
            raise ProviderError("LEADGEN_AI_BASE_URL is not set")
        self.model = settings.ai_model
        self.url = settings.ai_base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {settings.ai_api_key}"} if settings.ai_api_key else {}
        self.client = client or httpx.Client(timeout=settings.ai_timeout_seconds, headers=headers)

    def status(self) -> tuple[bool, str]:
        try:
            ok = self.client.get(f"{self.url}/models", timeout=5).status_code == 200
        except httpx.HTTPError:
            ok = False
        return ok, (f"{self.model} at {self.url}" if ok else f"AI server at {self.url} is not reachable")

    def complete_json(self, system: str, user: str, schema: dict) -> Completion:
        t0 = time.perf_counter()
        try:
            resp = self.client.post(f"{self.url}/chat/completions", json={
                "model": self.model, "temperature": 0.2,
                "response_format": {"type": "json_schema", "json_schema": {"name": "result", "schema": schema}},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            })
            if resp.status_code == 429:
                raise ProviderError("the AI service's free limit was reached; try again in a minute")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"AI server request failed ({type(exc).__name__})") from exc
        content = resp.json()["choices"][0]["message"]["content"]
        return Completion(_parse(content), round((time.perf_counter() - t0) * 1000), self.name, self.model)


class GeminiProvider(OpenAICompatibleProvider):
    """Google Gemini through its OpenAI-compatible endpoint; the free tier needs only an AI Studio key."""
    name = "gemini"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
    # Checked live 2026-11-03: 2.5 models are closed to new keys; newer ones are sometimes "high demand" (503).
    DEFAULT_MODEL = "gemini-3.5-flash"
    FALLBACK_MODELS = ("gemini-3.5-flash", "gemini-3-flash-preview", "gemini-flash-latest")

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if not settings.ai_api_key:
            raise ProviderError("LEADGEN_AI_API_KEY is not set (create a free key in Google AI Studio)")
        model = settings.ai_model if settings.ai_model.startswith("gemini") else self.DEFAULT_MODEL
        super().__init__(settings.model_copy(update={"ai_base_url": settings.ai_base_url or self.BASE_URL, "ai_model": model}), client)
        self.models = [model] + [m for m in self.FALLBACK_MODELS if m != model]

    def complete_json(self, system: str, user: str, schema: dict) -> Completion:
        """Try the next model when one is busy (503), retired (404) or out of free quota (429)."""
        for i, model in enumerate(self.models):
            self.model = model
            try:
                return super().complete_json(system, user, schema)
            except ProviderError as exc:
                cause = exc.__cause__
                busy = "free limit" in str(exc) or (isinstance(cause, httpx.HTTPStatusError)
                                                    and cause.response.status_code in (404, 503))
                if not busy or i == len(self.models) - 1:
                    raise
            finally:
                self.model = self.models[0]
        raise ProviderError("no Gemini model available")


class DisabledProvider:
    name, model = "none", "none"

    def __init__(self, reason: str = "AI is switched off (LEADGEN_AI_PROVIDER=none)"):
        self.reason = reason

    def status(self) -> tuple[bool, str]:
        return False, f"{self.reason}; template drafts are still available"

    def complete_json(self, system: str, user: str, schema: dict) -> Completion:
        raise ProviderError(self.reason)


def make_provider(settings: Settings) -> Provider:
    try:
        if settings.ai_provider == "ollama":
            return OllamaProvider(settings)
        if settings.ai_provider == "openai_compatible":
            return OpenAICompatibleProvider(settings)
        if settings.ai_provider == "gemini":
            return GeminiProvider(settings)
    except ProviderError as exc:  # misconfiguration must not take the whole app down
        return DisabledProvider(f"AI is misconfigured: {exc}")
    return DisabledProvider()
