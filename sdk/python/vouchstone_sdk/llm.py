"""Unified LLM core — one provider-agnostic chat interface for every
LLM-touching feature in the SDK (the harness loop, Forge engines, eval
graders, enrichment).

Three built-in providers, selected by model string:

- ``openrouter/<vendor>/<model>`` → **OpenRouter** — the any-LLM gateway
  (openrouter.ai exposes an OpenAI-compatible API), so an enterprise can run
  the harness on any model on the market by setting ``OPENROUTER_API_KEY``.
  Needs only the ``llm-openai`` extra (it reuses the OpenAI client).
- ``claude-*`` / ``anthropic/<model>`` → **Anthropic** (``llm-anthropic``).
- anything else → **OpenAI** (``llm-openai``).

Tool calling is normalized: pass OpenAI-style JSON-schema tool specs, get
back :class:`ToolCallRequest` objects, feed results back with
``tool_result_message`` — the provider differences (Anthropic's
``input_schema``/``tool_use`` blocks vs OpenAI's ``function``/``tool_calls``)
are translated here, once.

Additional gateways register through the ``LLM_PROVIDERS`` plugin registry
(group ``vouchstone.llm_providers``) — a private enterprise gateway plugs in
exactly like the built-ins.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .plugins import PluginRegistry

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


@dataclass
class ToolCallRequest:
    """A tool the model asked to run, provider-normalized."""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResponse:
    content: str
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    # The provider-native assistant message, replayable into the next
    # request's message list so multi-step tool loops keep exact provider
    # semantics (Anthropic requires the original content blocks back).
    raw_assistant_message: Any = None


class LLMProvider(ABC):
    """Implement ``chat()`` to plug any model gateway into the harness."""

    provider_name: str = "unknown"

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> ChatResponse:
        ...

    @abstractmethod
    def tool_result_message(self, call: ToolCallRequest, result: str,
                            *, is_error: bool = False) -> dict[str, Any]:
        """Provider-native message carrying one tool call's result."""

    @abstractmethod
    def assistant_message(self, response: ChatResponse) -> dict[str, Any]:
        """Provider-native assistant message to append before tool results."""


class OpenAIProvider(LLMProvider):
    """OpenAI-compatible chat-completions — also the base for OpenRouter and
    any other OpenAI-compatible enterprise gateway (base_url + api_key)."""

    provider_name = "openai"

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self._api_key = api_key
        self._base_url = base_url
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import openai
            except ImportError as exc:
                raise ImportError(
                    f"the '{self.provider_name}' LLM provider requires the OpenAI "
                    "client. Install it with: pip install 'vouchstone-sdk[llm-openai]'"
                ) from exc
            kwargs: dict[str, Any] = {}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = openai.AsyncOpenAI(**kwargs)
        return self._client

    async def chat(
        self, messages: list[dict[str, Any]], *, model: str,
        tools: list[dict[str, Any]] | None = None, system: str | None = None,
        temperature: float = 0.2, max_tokens: int = 4096,
    ) -> ChatResponse:
        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = tools
        resp = await self._get_client().chat.completions.create(
            model=model, messages=full_messages,
            temperature=temperature, max_tokens=max_tokens, **kwargs,
        )
        choice = resp.choices[0].message
        tool_calls = [
            ToolCallRequest(
                id=tc.id, name=tc.function.name,
                arguments=json.loads(tc.function.arguments or "{}"),
            )
            for tc in (choice.tool_calls or [])
        ]
        usage = {}
        if getattr(resp, "usage", None):
            usage = {"tokens_in": resp.usage.prompt_tokens or 0,
                     "tokens_out": resp.usage.completion_tokens or 0}
        return ChatResponse(
            content=choice.content or "", tool_calls=tool_calls, usage=usage,
            raw_assistant_message=choice,
        )

    def assistant_message(self, response: ChatResponse) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": response.content or None}
        if response.tool_calls:
            msg["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                for c in response.tool_calls
            ]
        return msg

    def tool_result_message(self, call: ToolCallRequest, result: str,
                            *, is_error: bool = False) -> dict[str, Any]:
        content = result if not is_error else f"ERROR: {result}"
        return {"role": "tool", "tool_call_id": call.id, "content": content}


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter (openrouter.ai) — any model on the market through one
    OpenAI-compatible endpoint. Model ids are ``<vendor>/<model>`` (e.g.
    ``anthropic/claude-sonnet-4-6``, ``meta-llama/llama-3.3-70b-instruct``).
    Reads ``OPENROUTER_API_KEY`` unless a key is passed explicitly; a
    missing key raises at first call with the variable named, never a
    silent anonymous request."""

    provider_name = "openrouter"

    def __init__(self, api_key: str | None = None):
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        super().__init__(api_key=key, base_url=OPENROUTER_BASE_URL)

    def _get_client(self) -> Any:
        if not self._api_key:
            raise RuntimeError(
                "OpenRouter provider selected but no API key configured -- "
                "set OPENROUTER_API_KEY or pass api_key=."
            )
        return super()._get_client()


class AnthropicProvider(LLMProvider):
    provider_name = "anthropic"

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise ImportError(
                    "the 'anthropic' LLM provider requires the Anthropic client. "
                    "Install it with: pip install 'vouchstone-sdk[llm-anthropic]'"
                ) from exc
            self._client = (
                anthropic.AsyncAnthropic(api_key=self._api_key)
                if self._api_key else anthropic.AsyncAnthropic()
            )
        return self._client

    @staticmethod
    def _to_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """OpenAI function-tool spec -> Anthropic tool spec."""
        out = []
        for t in tools:
            fn = t.get("function", t)
            out.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return out

    async def chat(
        self, messages: list[dict[str, Any]], *, model: str,
        tools: list[dict[str, Any]] | None = None, system: str | None = None,
        temperature: float = 0.2, max_tokens: int = 4096,
    ) -> ChatResponse:
        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = self._to_anthropic_tools(tools)
        if system:
            kwargs["system"] = system
        resp = await self._get_client().messages.create(
            model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens, **kwargs,
        )
        text = "".join(
            getattr(block, "text", "") for block in resp.content
            if getattr(block, "type", None) == "text"
        )
        tool_calls = [
            ToolCallRequest(id=block.id, name=block.name, arguments=dict(block.input))
            for block in resp.content if getattr(block, "type", None) == "tool_use"
        ]
        usage = {}
        if getattr(resp, "usage", None):
            usage = {"tokens_in": getattr(resp.usage, "input_tokens", 0),
                     "tokens_out": getattr(resp.usage, "output_tokens", 0)}
        return ChatResponse(
            content=text, tool_calls=tool_calls, usage=usage,
            raw_assistant_message=resp.content,
        )

    def assistant_message(self, response: ChatResponse) -> dict[str, Any]:
        # Anthropic requires the original content blocks (incl. tool_use)
        # replayed verbatim.
        return {"role": "assistant", "content": response.raw_assistant_message}

    def tool_result_message(self, call: ToolCallRequest, result: str,
                            *, is_error: bool = False) -> dict[str, Any]:
        return {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": call.id,
            "content": result, "is_error": is_error,
        }]}


# ── Provider registry + model-string resolution ──────────────────────

LLM_PROVIDERS = PluginRegistry("vouchstone.llm_providers")
LLM_PROVIDERS.register("openai", OpenAIProvider)
LLM_PROVIDERS.register("anthropic", AnthropicProvider)
LLM_PROVIDERS.register("openrouter", OpenRouterProvider)


def resolve_provider(model: str, *, api_key: str | None = None) -> tuple[LLMProvider, str]:
    """Model string -> (provider instance, provider-native model id).

    - ``openrouter/<anything>``       → OpenRouter, model id after the prefix
    - ``anthropic/<model>``           → Anthropic, model id after the prefix
    - ``claude-*``                    → Anthropic
    - ``openai/<model>``              → OpenAI, model id after the prefix
    - anything else                   → OpenAI, as-is
    """
    if model.startswith("openrouter/"):
        return LLM_PROVIDERS.get("openrouter")(api_key=api_key), model.split("/", 1)[1]
    if model.startswith("anthropic/"):
        return LLM_PROVIDERS.get("anthropic")(api_key=api_key), model.split("/", 1)[1]
    if model.startswith("claude-"):
        return LLM_PROVIDERS.get("anthropic")(api_key=api_key), model
    if model.startswith("openai/"):
        return LLM_PROVIDERS.get("openai")(api_key=api_key), model.split("/", 1)[1]
    return LLM_PROVIDERS.get("openai")(api_key=api_key), model
