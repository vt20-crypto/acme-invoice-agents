"""LLM access: one client, two transports.

* ``live``  calls the model (xAI Grok through the OpenAI SDK, Responses API) and writes every
            response to the cache.
* ``cache`` replays recorded responses only; a missing entry raises CacheMiss (the pipeline
            turns that into a HOLD with a system issue).
* ``auto``  per call: the recorded response if there is one, else a live call if a key is
            configured, else NoRecordedAI, which callers turn into their rules-only path.
* ``none``  means no client at all: callers take their code-only paths.

Cache entries live at ``cache/llm/<step>/<doc-fingerprint16>-<request-hash12>.json`` and hold the
response plus provenance ``recorded``. The request hash covers the exact messages, schema, tools,
model and settings, so a changed prompt, schema or input is simply a different (missing) entry.
No secrets are ever written to the cache.
"""

from __future__ import annotations

import hashlib
import json
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import MAX_TOOL_ITERATIONS
from ..logs import jlog

T = TypeVar("T", bound=BaseModel)

PRICE_IN_PER_M = 1.25  # grok-4.3, USD per million input tokens
PRICE_OUT_PER_M = 2.50  # output tokens, including reasoning tokens
BUDGET_STOP_USD = 2.50


class LLMUnavailable(RuntimeError):
    """No usable LLM result (no key, cache miss, API failure); the pipeline HOLDs on it."""


class CacheMiss(LLMUnavailable):
    """No recorded response for this exact request."""


class NoRecordedAI(CacheMiss):
    """Auto mode: no recorded response and no API key. Callers fall back to rules only."""


class BudgetExceeded(LLMUnavailable):
    """The estimated live spend reached the stop limit."""


class SchemaValidationFailed(Exception):
    """The model's output did not validate against the schema. Carries the exact error text."""

    def __init__(self, error_text: str, raw: str):
        super().__init__(error_text)
        self.error_text = error_text
        self.raw = raw


def failure_code(exc: BaseException) -> str:
    return "cache_missing" if isinstance(exc, CacheMiss) else "llm_unavailable"


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., Any]

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass
class ToolRoundResult:
    obj: BaseModel
    provenance: str
    tool_trace: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ModelTurn:
    """One model response: final text and/or tool calls (each {id, name, arguments-json-str})."""

    content: str | None
    tool_calls: list[dict[str, str]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


def settings_for(agent: str) -> dict[str, Any]:
    """Extraction needs no reasoning; the judgment steps use low effort."""
    return {"reasoning_effort": "none" if agent == "extraction" else "low"}


def request_hash(
    agent: str,
    messages: list[dict[str, Any]],
    schema_model: type[BaseModel],
    tools: list[Tool] | None,
    model: str,
) -> str:
    payload = {
        "agent": agent,
        "messages": messages,
        "schema": schema_model.model_json_schema(),
        "tools": [t.spec() for t in tools or []],
        "model": model,
        "settings": settings_for(agent),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[
        :12
    ]


def cost_usd(usage: dict[str, Any]) -> float:
    return (
        usage.get("input_tokens", 0) * PRICE_IN_PER_M
        + usage.get("output_tokens", 0) * PRICE_OUT_PER_M
    ) / 1_000_000


def cache_spend(root: Path) -> float:
    """Estimated total spend recorded in the cache (every entry stores its own estimate)."""
    total = 0.0
    for p in root.rglob("*.json"):
        try:
            total += float(json.loads(p.read_text()).get("usage", {}).get("cost_usd_estimate", 0.0))
        except (ValueError, OSError):
            continue
    return total


class Transport(ABC):
    provenance: str
    model: str

    @abstractmethod
    def call(
        self,
        agent: str,
        key: str | None,
        messages: list[dict[str, Any]],
        schema_model: type[BaseModel],
        tools: list[Tool] | None,
    ) -> ModelTurn: ...


def _path(root: Path, agent: str, key: str | None, rhash: str) -> Path:
    return root / agent / f"{key or 'nokey'}-{rhash}.json"


class CacheTransport(Transport):
    provenance = "recorded"

    def __init__(self, root: str | Path, model: str):
        self.root, self.model = Path(root), model

    def call(self, agent, key, messages, schema_model, tools):
        rhash = request_hash(agent, messages, schema_model, tools, self.model)
        path = _path(self.root, agent, key, rhash)
        if not path.exists():
            raise CacheMiss(f"no cached response for {agent} ({path.name})")
        data = json.loads(path.read_text())
        if data.get("provenance") != "recorded":
            raise CacheMiss(f"{path.name} is not a recorded response")
        r = data["response"]
        return ModelTurn(r.get("content"), r.get("tool_calls", []), data.get("usage", {}))


class AutoTransport(Transport):
    """Recorded response first, then live (which records it), else NoRecordedAI. ``provenance``
    reports what served the most recent call."""

    def __init__(self, cache: Transport, live: Transport | None):
        self.cache, self.live = cache, live
        self.model = cache.model
        self.provenance = cache.provenance

    def call(self, agent, key, messages, schema_model, tools):
        try:
            turn = self.cache.call(agent, key, messages, schema_model, tools)
            self.provenance = self.cache.provenance
            return turn
        except CacheMiss:
            if self.live is None:
                raise NoRecordedAI(
                    f"no recorded response for {agent} and no API key is configured"
                ) from None
        turn = self.live.call(agent, key, messages, schema_model, tools)
        self.provenance = self.live.provenance
        return turn


def _to_responses_input(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    """Chat-style messages -> (instructions, Responses API input items)."""
    instructions = None
    items: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "system" and instructions is None:
            instructions = m["content"]
        elif role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": m["tool_call_id"],
                    "output": m["content"],
                }
            )
        elif role == "assistant" and m.get("tool_calls"):
            if m.get("content"):
                items.append({"role": "assistant", "content": m["content"]})
            for tc in m["tool_calls"]:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tc["id"],
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    }
                )
        else:
            items.append({"role": role, "content": m["content"]})
    return instructions, items


class LiveTransport(Transport):
    """xAI Grok through the OpenAI SDK, Responses API with structured outputs.

    Never sends presence_penalty, frequency_penalty or stop. Writes each response to the cache.
    The API key is read from the environment only and is never logged or stored.
    """

    provenance = "live"

    def __init__(self, api_key: str, base_url: str, model: str, cache_root: str | Path):
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.root = Path(cache_root)
        self.session_cost = 0.0
        self._base_spend = cache_spend(self.root)

    def call(self, agent, key, messages, schema_model, tools):
        if self._base_spend + self.session_cost >= BUDGET_STOP_USD:
            raise BudgetExceeded(
                f"estimated spend ${self._base_spend + self.session_cost:.2f} reached the "
                f"${BUDGET_STOP_USD:.2f} stop limit"
            )
        rhash = request_hash(agent, messages, schema_model, tools, self.model)
        instructions, items = _to_responses_input(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": items,
            "reasoning": {"effort": settings_for(agent)["reasoning_effort"]},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_model.__name__,
                    "schema": schema_model.model_json_schema(),
                }
            },
        }
        if instructions:
            kwargs["instructions"] = instructions
        if tools:
            kwargs["tools"] = [t.spec() for t in tools]
        try:
            resp = self._client.responses.create(**kwargs)
        except Exception as exc:
            raise LLMUnavailable(f"API call failed: {type(exc).__name__}") from exc
        calls = [
            {"id": o.call_id, "name": o.name, "arguments": o.arguments}
            for o in resp.output
            if o.type == "function_call"
        ]
        content = None if calls else resp.output_text
        u = resp.usage
        usage = {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "reasoning_tokens": u.output_tokens_details.reasoning_tokens,
        }
        usage["cost_usd_estimate"] = round(cost_usd(usage), 6)
        self.session_cost += usage["cost_usd_estimate"]
        jlog(
            "llm_usage",
            agent=agent,
            model=self.model,
            **usage,
            running_estimate_usd=round(self._base_spend + self.session_cost, 4),
        )
        path = _path(self.root, agent, key, rhash)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "provenance": "recorded",
                    "model": self.model,
                    "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "settings": settings_for(agent),
                    "request": {
                        "agent": agent,
                        "messages": len(messages),
                        "schema": schema_model.__name__,
                        "tools": [t.name for t in tools or []],
                    },
                    "usage": usage,
                    "response": {"content": content, "tool_calls": calls},
                },
                indent=2,
            )
            + "\n"
        )
        return ModelTurn(content, calls, usage)


class LLMClient:
    """Structured calls and a tool-calling loop on top of a transport."""

    def __init__(self, transport: Transport):
        self.transport = transport

    @property
    def provenance(self) -> str:
        return self.transport.provenance

    @staticmethod
    def _validate(schema_model: type[T], text: str) -> T:
        try:
            return schema_model.model_validate_json(text)
        except ValidationError as exc:
            raise SchemaValidationFailed(str(exc), text) from exc

    def structured(
        self,
        agent: str,
        messages: list[dict[str, Any]],
        schema_model: type[T],
        *,
        key: str | None = None,
    ) -> tuple[T, str]:
        turn = self.transport.call(agent, key, messages, schema_model, None)
        jlog("llm_call", agent=agent, provenance=self.provenance)
        return self._validate(schema_model, turn.content or ""), self.provenance

    def tool_call_round(
        self,
        agent: str,
        messages: list[dict[str, Any]],
        tools: list[Tool],
        schema_model: type[T],
        *,
        key: str | None = None,
        max_iterations: int = MAX_TOOL_ITERATIONS,
    ) -> ToolRoundResult:
        """Model emits tool calls, we execute them and feed results back, until it returns a
        final answer matching ``schema_model``. Capped at ``max_iterations`` model turns."""
        by_name = {t.name: t for t in tools}
        msgs = list(messages)
        trace: list[dict[str, Any]] = []
        for _ in range(max_iterations):
            turn = self.transport.call(agent, key, msgs, schema_model, tools)
            jlog("llm_call", agent=agent, provenance=self.provenance)
            if not turn.tool_calls:
                obj = self._validate(schema_model, turn.content or "")
                return ToolRoundResult(obj, self.provenance, trace)
            msgs.append(
                {
                    "role": "assistant",
                    "content": turn.content,
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": c["arguments"]},
                        }
                        for c in turn.tool_calls
                    ],
                }
            )
            for c in turn.tool_calls:
                try:
                    args = json.loads(c["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool = by_name.get(c["name"])
                try:
                    result = tool.fn(**args) if tool else {"error": f"unknown tool {c['name']!r}"}
                except Exception as exc:
                    result = {"error": f"{type(exc).__name__}: {exc}"}
                trace.append({"tool": c["name"], "arguments": args, "result": result})
                msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": c["id"],
                        "content": json.dumps(result, default=str),
                    }
                )
        raise LLMUnavailable(f"{agent}: no final answer within {max_iterations} tool iterations")


def make_client(
    mode: str, cache_dir: str | Path = "cache/llm", api_key: str | None = None
) -> LLMClient | None:
    """``none`` -> None. ``cache`` -> replay only. ``live`` -> real calls that fill the cache.
    ``auto`` -> recorded response, else live when a key is set, else rules only. ``api_key``
    overrides LLM_API_KEY for this client only (the UI passes a per-session key this way)."""
    from dotenv import load_dotenv

    if mode == "none":
        return None
    load_dotenv()
    model = os.environ.get("LLM_MODEL", "grok-4.3")
    if mode == "cache":
        return LLMClient(CacheTransport(cache_dir, model))
    key = (api_key or os.environ.get("LLM_API_KEY", "")).strip()
    if mode == "auto":
        live = None
        if key:
            base = os.environ.get("LLM_BASE_URL", "https://api.x.ai/v1")
            live = LiveTransport(key, base, model, cache_dir)
        return LLMClient(AutoTransport(CacheTransport(cache_dir, model), live))
    if not key:
        raise LLMUnavailable("LLM_API_KEY is not set (see .env.example)")
    base = os.environ.get("LLM_BASE_URL", "https://api.x.ai/v1")
    return LLMClient(LiveTransport(key, base, model, cache_dir))
