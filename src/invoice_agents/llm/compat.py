"""Fixture compatibility: what a stored LLM response was made for.

A fixture is only valid for the exact prompt, schema, input and settings it was written against.
Hashes of those are stored with the fixture and checked on load; any mismatch is a stale fixture
(an execution failure), never silently used.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def prompt_hash(system: str, user_template: str) -> str:
    """Hash of the system prompt plus the user-message template (not the per-document data)."""
    return _sha({"system": system, "user_template": user_template})


def schema_hash(schema_model: type[BaseModel]) -> str:
    return _sha(schema_model.model_json_schema())


def input_hash(messages: list[dict[str, Any]]) -> str:
    return _sha(messages)


def settings_for(agent: str) -> dict[str, Any]:
    """Model settings that change behavior. Mirrors LiveClient (none for extraction, low else)."""
    return {"reasoning_effort": "none" if agent == "extraction" else "low"}


def compat_meta(
    agent: str,
    messages: list[dict[str, Any]],
    schema_model: type[BaseModel],
    prompt: str | None,
    model: str,
) -> dict[str, Any]:
    return {
        "prompt_hash": prompt,
        "schema_hash": schema_hash(schema_model),
        "input_hash": input_hash(messages),
        "model": model,
        "settings": settings_for(agent),
    }


def mismatches(
    stored: dict[str, Any] | None, current: dict[str, Any], expected_model: str | None
) -> list[str]:
    """Names of the compatibility fields that differ ([] means compatible)."""
    if not stored:
        return ["meta (fixture has no compatibility metadata)"]
    bad = [
        k
        for k in ("prompt_hash", "schema_hash", "input_hash", "settings")
        if stored.get(k) != current[k]
    ]
    if expected_model and stored.get("model") not in (expected_model, "authored"):
        bad.append("model")
    return bad
