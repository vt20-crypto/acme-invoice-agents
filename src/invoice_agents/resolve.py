"""Item resolution agent: maps raw line-item names to inventory SKUs.

With an LLM it uses real tool calling (``search_inventory``). Without one, code normalization
does it. Either way the validation step re-checks every SKU exactly against the database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .db import Database
from .llm.client import LLMClient, LLMUnavailable, SchemaValidationFailed, Tool
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
    method: str  # llm | code
    provenance: str | None = None
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None  # set when the LLM path failed and code resolution was used


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
    if client is None:
        return ResolveOutcome(code_resolve(invoice, db), "code")
    messages = [
        {"role": "system", "content": RESOLVE_SYSTEM},
        {
            "role": "user",
            "content": json.dumps({"line_items": [li.raw_name for li in invoice.line_items]}),
        },
    ]
    try:
        result = client.tool_call_round(
            "resolve_items", messages, [search_inventory_tool(db)], ResolutionResult, key=key
        )
    except (LLMUnavailable, SchemaValidationFailed) as exc:
        return ResolveOutcome(code_resolve(invoice, db), "code", error=str(exc))
    assert isinstance(result.obj, ResolutionResult)
    pool = list(result.obj.items)
    proposed: list[str | None] = []
    for li in invoice.line_items:
        match = next((r for r in pool if r.raw_name == li.raw_name), None)
        if match:
            pool.remove(match)
        proposed.append(match.sku if match else None)
    return ResolveOutcome(proposed, "llm", result.provenance, result.tool_trace)
