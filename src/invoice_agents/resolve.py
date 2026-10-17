"""Item resolution agent: maps raw line-item names to inventory SKUs.

With an LLM it uses real tool calling (``search_inventory``). Without one, code normalization
does it. Either way the validation step re-checks every SKU exactly against the database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .db import Database
from .llm.client import LLMClient, LLMUnavailable, SchemaValidationFailed, Tool, failure_code
from .models import Invoice, ResolutionResult
from .validation import code_resolve

RESOLVE_SYSTEM = (
    "You map invoice line-item names to inventory SKUs. For every line item, call "
    "search_inventory with the item name, then choose the SKU only if an inventory item is "
    "clearly the same product (ignore notes such as '(rush order)'). Use null when nothing "
    "matches; never invent a SKU. Reply with JSON matching the schema, one entry per line item "
    "in the same order."
)


@dataclass
class ResolveOutcome:
    proposed: list[str | None]
    tiers: list[str]  # per line: "code" | "llm" | "unresolved"
    method: str  # code | tiered (an LLM tool round was needed)
    provenance: str | None = None
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None  # set when the LLM path failed (lines stay unresolved)
    error_code: str | None = None
    searched: list[bool] = field(default_factory=list)  # per line: the LLM search actually ran


def search_inventory_tool(db: Database) -> Tool:
    return Tool(
        name="search_inventory",
        description="Search inventory by item name; returns items with stock and unit price.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Item name to look up"}},
            "required": ["query"],
        },
        fn=lambda query: db.search_inventory(query),
    )


def resolve_items(
    invoice: Invoice, db: Database, client: LLMClient | None, key: str
) -> ResolveOutcome:
    """Tier 1: exact match after code normalization. Tier 2: the LLM, with the search_inventory
    tool, only for names tier 1 could not resolve. Validation re-checks every SKU afterwards."""
    proposed = code_resolve(invoice, db)
    tiers = ["code" if sku else "unresolved" for sku in proposed]
    todo = [i for i, sku in enumerate(proposed) if sku is None]
    nobody = [False] * len(proposed)
    if not todo or client is None:
        return ResolveOutcome(proposed, tiers, "code", searched=nobody)
    names = [invoice.line_items[i].raw_name for i in todo]
    messages = [
        {"role": "system", "content": RESOLVE_SYSTEM},
        {"role": "user", "content": json.dumps({"line_items": names})},
    ]
    try:
        result = client.tool_call_round(
            "resolve_items", messages, [search_inventory_tool(db)], ResolutionResult, key=key
        )
    except (LLMUnavailable, SchemaValidationFailed) as exc:
        return ResolveOutcome(
            proposed, tiers, "tiered", error=str(exc), error_code=failure_code(exc), searched=nobody
        )
    assert isinstance(result.obj, ResolutionResult)
    pool = list(result.obj.items)
    for i in todo:
        match = next((r for r in pool if r.raw_name == invoice.line_items[i].raw_name), None)
        if match:
            pool.remove(match)
            if match.sku:
                proposed[i], tiers[i] = match.sku, "llm"
    searched = [i in todo for i in range(len(proposed))]
    return ResolveOutcome(
        proposed, tiers, "tiered", result.provenance, result.tool_trace, searched=searched
    )
