import json

from invoice_agents.llm.client import FixtureClient
from invoice_agents.models import Invoice, LineItem
from invoice_agents.resolve import resolve_items

LAB = {"provenance": "authored", "note": "Hand-written fixture, not a model output"}


def inv(*names):
    return Invoice(vendor="V", line_items=[LineItem(raw_name=n, quantity=1) for n in names])


def write_fixtures(root, key, final_items):
    d = root / "resolve_items"
    d.mkdir(parents=True)
    (d / f"{key}__turn0.json").write_text(
        json.dumps(
            {
                **LAB,
                "tool_calls": [{"name": "search_inventory", "arguments": {"query": "Wigdet A"}}],
            }
        )
    )
    (d / f"{key}__turn1.json").write_text(json.dumps({**LAB, "response": {"items": final_items}}))


def test_code_tier_resolves_without_calling_the_llm(db):
    class Boom(FixtureClient):
        def tool_call_round(self, *a, **k):
            raise AssertionError("LLM must not be called")

    out = resolve_items(inv("Widget A", "WidgetA (rush order)", "GadgetX"), db, Boom("x"), "k")
    assert out.proposed == ["WidgetA", "WidgetA", "GadgetX"]
    assert out.tiers == ["code"] * 3 and out.method == "code" and out.provenance is None


def test_llm_tier_only_sees_names_code_could_not_resolve(db, tmp_path):
    write_fixtures(tmp_path, "k", [{"raw_name": "Wigdet A", "sku": "WidgetA"}])
    out = resolve_items(inv("WidgetB", "Wigdet A"), db, FixtureClient(tmp_path, verify=False), "k")
    assert out.proposed == ["WidgetB", "WidgetA"]
    assert out.tiers == ["code", "llm"] and out.method == "tiered"
    call = out.tool_trace[0]
    assert call["tool"] == "search_inventory" and call["result"][0]["item"] == "WidgetA"


def test_llm_may_return_null_and_stays_unresolved(db, tmp_path):
    write_fixtures(tmp_path, "k", [{"raw_name": "Wigdet A", "sku": None}])
    out = resolve_items(inv("Wigdet A"), db, FixtureClient(tmp_path, verify=False), "k")
    assert out.proposed == [None] and out.tiers == ["unresolved"]


def test_llm_unavailable_leaves_lines_unresolved_with_error(db, tmp_path):
    out = resolve_items(inv("WidgetB", "Wigdet A"), db, FixtureClient(tmp_path, verify=False), "k")
    assert out.proposed == ["WidgetB", None] and out.error
