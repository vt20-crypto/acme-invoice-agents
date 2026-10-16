"""LLM access behind one small interface.

Three ways to get an LLM result, always labelled with a provenance:

* ``live``      a real model call (LiveClient)
* ``authored``  a fixture file hand-written by us (FixtureClient); NOT a model output
* ``recorded``  a captured real response replayed from a fixture file (FixtureClient)

``--llm none`` means no client at all: callers take their code-only paths.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import MAX_TOOL_ITERATIONS
from ..logs import jlog

T = TypeVar("T", bound=BaseModel)

VALID_FIXTURE_PROVENANCE = {"authored", "recorded"}


class LLMUnavailable(RuntimeError):
    """No usable LLM result (no key, missing fixture, API failure); the pipeline HOLDs on it."""


class FixtureError(LLMUnavailable):
    """A fixture file exists but is malformed or lacks a valid provenance label."""


class SchemaValidationFailed(Exception):
    """The model's output did not validate against the schema. Carries the exact error text."""

    def __init__(self, error_text: str, raw: str):
        super().__init__(error_text)
        self.error_text = error_text
        self.raw = raw


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema for the arguments
    fn: Callable[..., Any]

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolRoundResult:
    obj: BaseModel
    provenance: str
    tool_trace: list[dict[str, Any]] = field(default_factory=list)


def _run_tool(tools: dict[str, Tool], name: str, arguments: dict[str, Any]) -> Any:
    tool = tools.get(name)
    if tool is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return tool.fn(**arguments)
    except Exception as exc:  # tool errors go back to the model, they do not crash the run
        return {"error": f"{type(exc).__name__}: {exc}"}


class LLMClient(ABC):
    @abstractmethod
    def structured(
        self,
        agent: str,
        messages: list[dict[str, Any]],
        schema_model: type[T],
        *,
        key: str | None = None,
        step: str = "",
    ) -> tuple[T, str]:
        """One call returning a validated ``schema_model`` and its provenance label."""

    @abstractmethod
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


class LiveClient(LLMClient):
    """OpenAI-compatible client (xAI Grok by default).

    NOT EXERCISED: no API key was available when this was written, so this class has never made
    a real call. It is written against the openai SDK's documented chat-completions interface.
    Reasoning effort is "none" for extraction and "low" for everything else. presence_penalty,
    frequency_penalty and stop are never sent (reasoning models reject them).
    """

    def __init__(self, api_key: str, base_url: str, model: str):
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    @classmethod
    def from_env(cls) -> LiveClient:
        from dotenv import load_dotenv

        load_dotenv()
        key = os.environ.get("LLM_API_KEY", "").strip()
        if not key:
            raise LLMUnavailable("LLM_API_KEY is not set (see .env.example)")
        return cls(
            key,
            os.environ.get("LLM_BASE_URL", "https://api.x.ai/v1"),
            os.environ.get("LLM_MODEL", "grok-4.3"),
        )

    @staticmethod
    def _effort(agent: str) -> str:
        return "none" if agent == "extraction" else "low"

    @staticmethod
    def _response_format(schema_model: type[BaseModel]) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_model.__name__,
                "schema": schema_model.model_json_schema(),
            },
        }

    def _create(self, agent: str, messages: list[dict[str, Any]], schema_model, tools=None):
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "response_format": self._response_format(schema_model),
            "reasoning_effort": self._effort(agent),
        }
        if tools:
            kwargs["tools"] = [t.spec() for t in tools]
        try:
            return self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise LLMUnavailable(f"API call failed: {type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _validate(schema_model: type[T], text: str) -> T:
        try:
            return schema_model.model_validate_json(text)
        except ValidationError as exc:
            raise SchemaValidationFailed(str(exc), text) from exc

    def structured(self, agent, messages, schema_model, *, key=None, step=""):
        resp = self._create(agent, messages, schema_model)
        obj = self._validate(schema_model, resp.choices[0].message.content or "")
        jlog("llm_call", agent=agent, step=step, provenance="live", model=self.model)
        return obj, "live"

    def tool_call_round(
        self, agent, messages, tools, schema_model, *, key=None, max_iterations=MAX_TOOL_ITERATIONS
    ):
        by_name = {t.name: t for t in tools}
        msgs = list(messages)
        trace: list[dict[str, Any]] = []
        for turn in range(max_iterations):
            resp = self._create(agent, msgs, schema_model, tools)
            msg = resp.choices[0].message
            if not msg.tool_calls:
                obj = self._validate(schema_model, msg.content or "")
                jlog("llm_call", agent=agent, step=f"turn{turn}", provenance="live")
                return ToolRoundResult(obj, "live", trace)
            msgs.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                }
            )
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = _run_tool(by_name, tc.function.name, args)
                trace.append({"tool": tc.function.name, "arguments": args, "result": result})
                msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, default=str),
                    }
                )
        raise LLMUnavailable(f"{agent}: no final answer within {max_iterations} tool iterations")


class FixtureClient(LLMClient):
    """Replays JSON fixtures from ``<root>/<agent>/<key>[__<step>].json``.

    Every fixture must carry ``"provenance": "authored"`` (hand-written, not a model output) or
    ``"recorded"`` (a captured real response). A fixture without a valid provenance is refused.
    The returned provenance is whatever the file declares. Multi-turn tool rounds use
    ``<key>__turn0.json``, ``<key>__turn1.json``, ...: a turn holds either ``tool_calls`` (which
    this client really executes against the supplied tools) or the final ``response``.
    """

    def __init__(self, root: str | Path = "fixtures/llm"):
        self.root = Path(root)

    def _load(self, agent: str, key: str | None, suffix: str) -> dict[str, Any]:
        if not key:
            raise LLMUnavailable(f"{agent}: no fixture key given")
        path = self.root / agent / f"{key}{suffix}.json"
        if not path.exists():
            raise LLMUnavailable(f"no fixture for {agent} at {path}")
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise FixtureError(f"{path} is not valid JSON: {exc}") from exc
        prov = data.get("provenance") if isinstance(data, dict) else None
        if prov not in VALID_FIXTURE_PROVENANCE:
            raise FixtureError(
                f"{path} refused: missing or invalid 'provenance' (need one of "
                f"{sorted(VALID_FIXTURE_PROVENANCE)}, got {prov!r})"
            )
        return data

    @staticmethod
    def _validate(schema_model: type[T], payload: Any) -> T:
        try:
            return schema_model.model_validate(payload)
        except ValidationError as exc:
            raise SchemaValidationFailed(str(exc), json.dumps(payload)) from exc

    def structured(self, agent, messages, schema_model, *, key=None, step=""):
        data = self._load(agent, key, f"__{step}" if step else "")
        prov = data["provenance"]
        jlog("llm_call", agent=agent, step=step, provenance=prov, key=key)
        return self._validate(schema_model, data.get("response")), prov

    def tool_call_round(
        self, agent, messages, tools, schema_model, *, key=None, max_iterations=MAX_TOOL_ITERATIONS
    ):
        by_name = {t.name: t for t in tools}
        trace: list[dict[str, Any]] = []
        prov = "authored"
        for turn in range(max_iterations):
            data = self._load(agent, key, f"__turn{turn}")
            prov = data["provenance"]
            jlog("llm_call", agent=agent, step=f"turn{turn}", provenance=prov, key=key)
            calls = data.get("tool_calls")
            if not calls:
                return ToolRoundResult(
                    self._validate(schema_model, data.get("response")), prov, trace
                )
            for call in calls:
                args = call.get("arguments", {})
                trace.append(
                    {
                        "tool": call["name"],
                        "arguments": args,
                        "result": _run_tool(by_name, call["name"], args),
                    }
                )
        raise LLMUnavailable(f"{agent}: no final answer within {max_iterations} tool iterations")
